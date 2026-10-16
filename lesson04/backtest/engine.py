"""收盘信号、下一日开盘成交的多资产现金账户。"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import MarketData, StrategyContext
from .result import BacktestResult, performance
from .strategy import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    commission: float = 0.0003
    sell_tax: float = 0.0
    slippage: float = 0.0005
    lot_size: int = 100
    annual_days: int = 252
    start: str | None = None
    end: str | None = None

    def __post_init__(self):
        if not np.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash 必须为有限正数")
        for name in ("commission", "sell_tax", "slippage"):
            value = getattr(self, name)
            if not np.isfinite(value) or not 0 <= value < 1:
                raise ValueError(f"{name} 必须在 [0, 1) 内")
        if self.commission + self.sell_tax >= 1:
            raise ValueError("卖出费率之和必须小于 1")
        if not isinstance(self.lot_size, int) or self.lot_size < 0:
            raise ValueError("lot_size 必须是非负整数；0 表示允许零碎份额")
        if not isinstance(self.annual_days, int) or self.annual_days <= 0:
            raise ValueError("annual_days 必须是正整数")


def _target_weights(target, symbols):
    if not isinstance(target, pd.Series):
        raise TypeError("策略必须返回 pd.Series 或 None")
    if not target.index.is_unique:
        raise ValueError("目标权重包含重复证券代码")
    unknown = target.index.difference(symbols)
    if len(unknown):
        raise ValueError(f"未知证券代码: {unknown.tolist()}")
    values = target.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any() or values.sum() > 1 + 1e-12:
        raise ValueError("目标权重必须有限、非负且总和不超过 1")
    weights = target.reindex(symbols, fill_value=0.0).to_numpy(dtype=float)
    if weights.sum() > 1:
        weights /= weights.sum()
    return weights


class BacktestEngine:
    def __init__(self, data: MarketData, config: BacktestConfig | None = None):
        self.data = data
        self.config = config or BacktestConfig()

    def run(self, strategy: Strategy):
        data, config = self.data, self.config
        selected = np.ones(len(data.dates), dtype=bool)
        if config.start is not None:
            selected &= data.dates >= pd.Timestamp(config.start)
        if config.end is not None:
            selected &= data.dates <= pd.Timestamp(config.end)
        rows = np.flatnonzero(selected)
        if not len(rows):
            raise ValueError("指定回测区间内没有交易日")

        opens = data.frames["open"].to_numpy()
        closes = data.frames["close"].to_numpy()
        factors = data.frames["cumadj"].to_numpy()
        count = len(data.symbols)
        shares = np.zeros(count)
        last_price = np.full(count, np.nan)
        last_factor = np.full(count, np.nan)
        cash = config.initial_cash
        previous_equity = cash
        pending = None
        signal_date = None
        daily_records, trade_records, position_records, rejected_records = [], [], [], []

        def round_down(quantity):
            if config.lot_size:
                return np.floor(quantity / config.lot_size + 1e-12) * config.lot_size
            return quantity

        for row in rows:
            date = data.dates[row]
            factor = factors[row]
            valid_factor = np.isfinite(factor) & (factor > 0)
            # cumadj 比率等价于总收益模型中的份额调整，非真实分红到账模型。
            adjust = valid_factor & np.isfinite(last_factor)
            ratio = np.ones(count)
            ratio[adjust] = factor[adjust] / last_factor[adjust]
            shares *= ratio
            last_price /= ratio
            last_factor[valid_factor] = factor[valid_factor]

            open_price = opens[row]
            tradable = np.isfinite(open_price) & (open_price > 0) & valid_factor
            open_mark = np.where(tradable, open_price, last_price)
            open_equity = float(cash + np.sum(shares * np.nan_to_num(open_mark, nan=0.0)))
            fees = slippage_cost = traded_notional = 0.0

            def execute(indices, quantities, side):
                nonlocal cash, fees, slippage_cost, traded_notional
                direction = 1 if side == "buy" else -1
                for index in indices:
                    quantity = float(quantities[index])
                    if quantity <= 1e-10:
                        continue
                    price = float(open_price[index] * (1 + direction * config.slippage))
                    notional = quantity * price
                    commission = notional * config.commission
                    tax = notional * config.sell_tax if side == "sell" else 0.0
                    cash -= direction * notional + commission + tax
                    shares[index] += direction * quantity
                    fees += commission + tax
                    slip = quantity * abs(price - open_price[index])
                    slippage_cost += slip
                    traded_notional += quantity * open_price[index]
                    trade_records.append({
                        "date": date, "signal_date": signal_date,
                        "symbol": data.symbols[index], "side": side,
                        "quantity": quantity, "open_price": open_price[index],
                        "fill_price": price, "notional": notional,
                        "commission": commission, "sell_tax": tax,
                        "slippage_cost": slip, "cash_after": cash,
                    })

            if pending is not None:
                blocked = ~tradable & ((pending > 0) | (shares > 1e-10))
                for index in np.flatnonzero(blocked):
                    rejected_records.append({
                        "date": date, "signal_date": signal_date,
                        "symbol": data.symbols[index], "target_weight": pending[index],
                        "reason": "missing_adjustment_factor" if not valid_factor[index] else "invalid_open",
                    })
                desired = np.divide(
                    pending * open_equity, open_price,
                    out=shares.copy(), where=tradable,
                )
                sells = round_down(np.maximum(shares - desired, 0))
                # 清仓允许一次卖出全部剩余零碎份额；部分减仓遵循 lot_size。
                sells = np.where(pending == 0, shares, sells)
                sells = np.minimum(sells, shares)
                sells[~tradable] = 0
                execute(np.flatnonzero(sells > 1e-10), sells, "sell")

                buys = np.maximum(desired - shares, 0)
                buys[~tradable] = 0
                unit_cost = np.where(tradable, open_price, 0) * (1 + config.slippage) * (1 + config.commission)
                required = float(np.sum(buys * unit_cost))
                if required > cash and required > 0:
                    buys *= max(cash, 0) / required
                buys = round_down(buys)
                execute(np.flatnonzero(buys > 1e-10), buys, "buy")
                if cash < -max(1e-7, config.initial_cash * 1e-12):
                    raise RuntimeError("成交后现金为负，账户约束异常")
                cash = max(cash, 0.0)
                shares[np.abs(shares) < 1e-10] = 0

            # 今日能在开盘成交、收盘却缺价时，至少可使用今日已知开盘价估值。
            last_price[tradable] = open_price[tradable]
            valid_close = np.isfinite(closes[row]) & (closes[row] > 0) & valid_factor
            last_price[valid_close] = closes[row, valid_close]
            values = shares * np.nan_to_num(last_price, nan=0.0)
            equity = float(cash + values.sum())
            if not np.isfinite(equity) or equity <= 0:
                raise RuntimeError(f"{date}: 账户权益无效")
            stale = (shares > 1e-10) & ~valid_close
            daily_records.append({
                "date": date, "equity": equity, "cash": cash,
                "market_value": float(values.sum()), "return": equity / previous_equity - 1,
                "turnover": traded_notional / open_equity,
                "fees": fees, "slippage_cost": slippage_cost,
                "positions": int(np.count_nonzero(shares > 1e-10)),
                "stale_positions": int(stale.sum()),
            })
            weights = values / equity
            for index in np.flatnonzero(shares > 1e-10):
                position_records.append({
                    "date": date, "symbol": data.symbols[index], "shares": shares[index],
                    "mark_price": last_price[index], "market_value": values[index],
                    "weight": weights[index], "stale": bool(stale[index]),
                })
            context = StrategyContext(
                data, int(row), cash, equity,
                pd.Series(shares.copy(), index=data.symbols),
                pd.Series(weights.copy(), index=data.symbols),
            )
            target = strategy.on_close(context)
            pending = None if target is None else _target_weights(target, data.symbols)
            signal_date = date
            previous_equity = equity

        daily = pd.DataFrame(daily_records).set_index("date")
        trades = pd.DataFrame(trade_records, columns=[
            "date", "signal_date", "symbol", "side", "quantity", "open_price",
            "fill_price", "notional", "commission", "sell_tax", "slippage_cost", "cash_after",
        ])
        positions = pd.DataFrame(position_records, columns=[
            "date", "symbol", "shares", "mark_price", "market_value", "weight", "stale",
        ])
        rejected = pd.DataFrame(rejected_records, columns=[
            "date", "signal_date", "symbol", "target_weight", "reason",
        ])
        return BacktestResult(
            daily, trades, positions, rejected,
            performance(daily, config.initial_cash, config.annual_days), config,
        )
