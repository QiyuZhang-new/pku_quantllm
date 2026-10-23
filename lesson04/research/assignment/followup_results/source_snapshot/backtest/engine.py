"""收盘信号、下一日开盘成交的多资产现金账户。"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import MarketData, StrategyContext
from .result import BacktestResult, performance
from .market_rules import opening_limits, stamp_tax, standard_limit_rates
from .strategy import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    commission: float = 0.0003
    sell_tax: float | None = None
    slippage: float = 0.0005
    lot_size: int = 100
    annual_days: int = 252
    start: str | None = None
    end: str | None = None
    apply_price_limits: bool = True
    defer_suspended_orders: bool = True
    execution_policy: str = "legacy_reversal"
    prestart_signal: bool = False
    liquidate_on_last_day: bool = False
    max_participation: float | None = None
    volume_lookback: int = 20
    record_positions: bool = True
    exit_st_on_open: bool = False
    board_lots: bool = False

    def __post_init__(self):
        if not np.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash 必须为有限正数")
        for name in ("commission", "sell_tax", "slippage"):
            value = getattr(self, name)
            if name == "sell_tax" and value is None:
                continue
            if not np.isfinite(value) or not 0 <= value < 1:
                raise ValueError(f"{name} 必须在 [0, 1) 内")
        if self.commission + (0.001 if self.sell_tax is None else self.sell_tax) >= 1:
            raise ValueError("卖出费率之和必须小于 1")
        if not isinstance(self.lot_size, int) or self.lot_size < 0:
            raise ValueError("lot_size 必须是非负整数；0 表示允许零碎份额")
        if not isinstance(self.annual_days, int) or self.annual_days <= 0:
            raise ValueError("annual_days 必须是正整数")
        if self.execution_policy not in {"legacy_reversal", "standard"}:
            raise ValueError("execution_policy 必须是 legacy_reversal 或 standard")
        if self.max_participation is not None and not 0 < self.max_participation <= 1:
            raise ValueError("max_participation 必须在 (0, 1] 内")
        if not isinstance(self.volume_lookback, int) or self.volume_lookback <= 0:
            raise ValueError("volume_lookback 必须是正整数")


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
    weights = target.reindex(symbols, fill_value=0.0).to_numpy(dtype=float, copy=True)
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
        deferred_weights = np.full(count, np.nan)
        deferred_dates = np.full(count, None, dtype=object)
        order_keys, deferred_keys = set(), set()
        daily_records, trade_records, position_records, rejected_records = [], [], [], []
        if config.max_participation is not None and "amt" not in data.frames:
            raise ValueError("成交参与率限制需要 amt 字段")
        adv = None if config.max_participation is None else data.frames["amt"].where(
            data.frames["amt"] > 0
        ).rolling(config.volume_lookback, min_periods=config.volume_lookback).mean().shift(1).to_numpy()
        st_flags = data.frames.get("is_st")
        st_flags = None if st_flags is None else st_flags.to_numpy()
        trading_status = data.frames.get("trade_status")
        trading_status = None if trading_status is None else trading_status.to_numpy()
        official_reference = data.frames.get("preclose")
        official_reference = None if official_reference is None else official_reference.to_numpy()
        if config.prestart_signal:
            if rows[0] == 0:
                raise ValueError("首日开盘建仓需要至少一行区间前的信号数据")
            zeros = pd.Series(0.0, index=data.symbols)
            context = StrategyContext(data, int(rows[0] - 1), cash, cash, zeros.copy(), zeros.copy())
            target = strategy.on_close(context)
            pending = None if target is None else _target_weights(target, data.symbols)
            signal_date = context.date

        def round_down(quantity):
            if config.lot_size:
                rounded = np.floor(quantity / config.lot_size + 1e-12) * config.lot_size
            else:
                rounded = quantity.copy()
            if config.board_lots:
                star = np.array([s.startswith(("688", "689")) for s in data.symbols])
                units = np.floor(quantity + 1e-12)
                rounded = np.where(star, np.where(units >= 200, units, 0), rounded)
            return rounded

        for row in rows:
            date = data.dates[row]
            if config.liquidate_on_last_day and row == rows[-1]:
                pending = np.zeros(count)
                signal_date = data.dates[row - 1]
            tax_rate = stamp_tax(date) if config.sell_tax is None else config.sell_tax
            factor = factors[row]
            valid_factor = np.isfinite(factor) & (factor > 0)
            # cumadj 比率等价于总收益模型中的份额调整，非真实分红到账模型。
            adjust = valid_factor & np.isfinite(last_factor)
            ratio = np.ones(count)
            ratio[adjust] = factor[adjust] / last_factor[adjust]
            shares *= ratio
            last_price /= ratio
            last_factor[valid_factor] = factor[valid_factor]
            # 当天买入的份额不能增加当天的可卖数量，显式维持日频 T+1。
            sellable_shares = shares.copy()

            open_price = opens[row]
            tradable = np.isfinite(open_price) & (open_price > 0) & valid_factor
            if trading_status is not None:
                tradable &= trading_status[row] == 1
            open_mark = np.where(tradable, open_price, last_price)
            open_equity = float(cash + np.sum(shares * np.nan_to_num(open_mark, nan=0.0)))
            fees = slippage_cost = traded_notional = 0.0
            attempted_orders = limit_rejections = suspended_orders = deferred_fills = 0
            order_dates = np.full(count, signal_date, dtype=object)
            was_deferred = np.zeros(count, dtype=bool)

            def execute(indices, quantities, side):
                nonlocal cash, fees, slippage_cost, traded_notional, deferred_fills
                direction = 1 if side == "buy" else -1
                for index in indices:
                    quantity = float(quantities[index])
                    if quantity <= 1e-10:
                        continue
                    price = float(open_price[index] * (1 + direction * config.slippage))
                    notional = quantity * price
                    commission = notional * config.commission
                    tax = notional * tax_rate if side == "sell" else 0.0
                    cash -= direction * notional + commission + tax
                    shares[index] += direction * quantity
                    fees += commission + tax
                    slip = quantity * abs(price - open_price[index])
                    slippage_cost += slip
                    traded_notional += quantity * open_price[index]
                    trade_records.append({
                        "date": date, "signal_date": order_dates[index],
                        "symbol": data.symbols[index], "side": side,
                        "quantity": quantity, "open_price": open_price[index],
                        "fill_price": price, "notional": notional,
                        "commission": commission, "sell_tax": tax,
                        "slippage_cost": slip, "cash_after": cash,
                        "sell_tax_rate": tax_rate if side == "sell" else 0.0,
                        "deferred": bool(was_deferred[index]),
                    })
                    deferred_fills += int(was_deferred[index])

            st_exit = ((shares > 1e-10) & (st_flags[row] == 1)) if config.exit_st_on_open and st_flags is not None else np.zeros(count, dtype=bool)
            if pending is not None or np.isfinite(deferred_weights).any() or st_exit.any():
                if pending is not None:
                    targets = pending
                    # 新信号替代旧目标，停牌旧单不能在新目标之后意外成交。
                    deferred_weights[:] = np.nan
                    deferred_dates[:] = None
                else:
                    targets = deferred_weights.copy()
                    was_deferred = np.isfinite(targets)
                    order_dates = deferred_dates.copy()
                    deferred_weights[:] = np.nan
                    deferred_dates[:] = None
                if st_exit.any():
                    targets = targets.copy()
                    targets[st_exit] = 0
                    # isST为当日生效、盘前已公告的状态；不使用当日收盘价格。
                    order_dates[st_exit] = data.dates[row-1]
                active = np.isfinite(targets)
                previous_close = closes[row - 1] if row > 0 else np.full(count, np.nan)
                if config.execution_policy == "standard" and official_reference is not None:
                    previous_close = official_reference[row]
                down, up, reference_valid = opening_limits(open_price, previous_close, data.symbols)
                if config.execution_policy == "standard":
                    # 价格取到分；无官方除权参考价时仍是原始前收近似。
                    rates = standard_limit_rates(data.symbols, date, None if st_flags is None else st_flags[row])
                    down = reference_valid & (open_price <= np.floor(previous_close * (1-rates) * 100 + .5) / 100 + 1e-9)
                    up = reference_valid & (open_price >= np.floor(previous_close * (1+rates) * 100 + .5) / 100 - 1e-9)
                if not config.apply_price_limits:
                    down[:] = up[:] = False
                    reference_valid[:] = True
                raw_open_valid = np.isfinite(open_price) & (open_price > 0)
                executable = tradable & reference_valid
                desired = np.divide(
                    np.where(active, targets, 0) * open_equity, open_price,
                    out=shares.copy(), where=executable & active,
                )
                buy_intent = active & (desired > shares + 1e-10)
                sell_intent = active & (desired < shares - 1e-10)
                unknown_intent = active & ~executable & ((targets > 0) | (shares > 1e-10))
                attempted_orders = int((buy_intent | sell_intent | unknown_intent).sum())
                for index in np.flatnonzero(buy_intent | sell_intent | unknown_intent):
                    order_keys.add((order_dates[index], data.symbols[index]))
                buy_limit, sell_limit = (up, down) if config.execution_policy == "standard" else (down, up)
                limit_block = (buy_intent & buy_limit) | (sell_intent & sell_limit)
                status_block = buy_intent & (st_flags[row] != 0) if st_flags is not None else np.zeros(count, dtype=bool)
                blocked = unknown_intent | limit_block | status_block
                limit_rejections = int(limit_block.sum())
                suspended_orders = int((unknown_intent & ~raw_open_valid).sum())
                for index in np.flatnonzero(blocked):
                    if limit_block[index]:
                        if config.execution_policy == "standard":
                            reason = "limit_up_buy" if buy_intent[index] else "limit_down_sell"
                        else:
                            reason = "limit_down_buy" if buy_intent[index] else "limit_up_sell"
                    elif status_block[index]:
                        reason = "st_or_unknown_status"
                    elif not raw_open_valid[index]:
                        reason = "invalid_open"
                    elif not valid_factor[index]:
                        reason = "missing_adjustment_factor"
                    elif trading_status is not None and trading_status[row, index] != 1:
                        reason = "suspended_or_unknown_trade_status"
                    else:
                        reason = "missing_previous_close"
                    defer = config.defer_suspended_orders and (
                        reason in {"invalid_open", "suspended_or_unknown_trade_status"}
                        or (was_deferred[index] and reason == "missing_previous_close")
                    )
                    if defer:
                        deferred_weights[index] = targets[index]
                        deferred_dates[index] = order_dates[index]
                        deferred_keys.add((order_dates[index], data.symbols[index]))
                    rejected_records.append({
                        "date": date, "signal_date": order_dates[index],
                        "symbol": data.symbols[index], "target_weight": targets[index],
                        "reason": reason, "deferred": bool(defer),
                    })
                sells = round_down(np.maximum(shares - desired, 0))
                # 清仓允许一次卖出全部剩余零碎份额；部分减仓遵循 lot_size。
                sells = np.where(active & (targets == 0), shares, sells)
                sells = np.minimum(sells, sellable_shares)
                sells[~executable | ~active | sell_limit] = 0
                capacity = None
                if adv is not None:
                    capacity = np.divide(
                        adv[row] * config.max_participation, open_price,
                        out=np.zeros(count), where=executable & np.isfinite(adv[row]),
                    )
                    capped = sells > capacity + 1e-10
                    sells = np.where(capped, round_down(capacity), sells)
                    for index in np.flatnonzero(capped):
                        rejected_records.append({"date": date, "signal_date": order_dates[index],
                            "symbol": data.symbols[index], "target_weight": targets[index],
                            "reason": "sell_participation_cap", "deferred": False})
                execute(np.flatnonzero(sells > 1e-10), sells, "sell")

                buys = np.maximum(desired - shares, 0)
                buys[~executable | ~active | buy_limit | status_block] = 0
                if capacity is not None:
                    capped = buys > capacity + 1e-10
                    buys = np.minimum(buys, capacity)
                    for index in np.flatnonzero(capped):
                        rejected_records.append({"date": date, "signal_date": order_dates[index],
                            "symbol": data.symbols[index], "target_weight": targets[index],
                            "reason": "buy_participation_cap", "deferred": False})
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
                "attempted_orders": attempted_orders,
                "limit_rejected_orders": limit_rejections,
                "suspended_orders": suspended_orders,
                "deferred_fills": deferred_fills,
                "st_positions": int(((shares > 1e-10) & (st_flags[row] == 1)).sum()) if st_flags is not None else 0,
                "unknown_status_positions": int(((shares > 1e-10) & ~np.isin(st_flags[row], [0, 1])).sum()) if st_flags is not None else 0,
            })
            weights = values / equity
            for index in (np.flatnonzero(shares > 1e-10) if config.record_positions else []):
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
            "sell_tax_rate", "deferred",
        ])
        positions = pd.DataFrame(position_records, columns=[
            "date", "symbol", "shares", "mark_price", "market_value", "weight", "stale",
        ])
        rejected = pd.DataFrame(rejected_records, columns=[
            "date", "signal_date", "symbol", "target_weight", "reason", "deferred",
        ])
        metrics = performance(daily, config.initial_cash, config.annual_days)
        metrics.update({
            "unique_order_intents": len(order_keys), "suspended_unique_orders": len(deferred_keys),
            "suspended_unique_ratio": len(deferred_keys) / len(order_keys) if order_keys else 0.0,
        })
        return BacktestResult(
            daily, trades, positions, rejected,
            metrics, config,
        )
