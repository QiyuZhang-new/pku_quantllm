"""全市场低波动/动量候选；通过公开 context.history 限制数据时点。"""

import numpy as np
import pandas as pd


class FactorSelectionStrategy:
    """历史非ST、流动性门槛、等权、每20交易日调仓。"""

    METHODS = ("lowvol60", "momentum120", "lowvol_momentum", "equal_weight")

    def __init__(self, method="lowvol60", k=100, holding=20, min_listed=120,
                 min_adv=200_000_000.0, volatility_window=60,
                 momentum_window=120, momentum_skip=20):
        if method not in self.METHODS:
            raise ValueError(f"未知因子 {method}")
        if min(k, holding, min_listed, volatility_window, momentum_skip) <= 0:
            raise ValueError("窗口和持仓数必须为正整数")
        if momentum_window <= momentum_skip or min_adv < 0:
            raise ValueError("动量长窗口必须大于跳过窗口，成交额门槛非负")
        self.method, self.k, self.holding = method, k, holding
        self.min_listed, self.min_adv = min_listed, min_adv
        self.volatility_window = volatility_window
        self.momentum_window, self.momentum_skip = momentum_window, momentum_skip
        self._calls = 0
        self._age = None
        self._last_date = None
        self.selections = []

    def on_close(self, context):
        if self._last_date is not None and context.date <= self._last_date:
            raise ValueError("策略上下文必须按交易日严格递增")
        self._last_date = context.date
        raw = context.history("close", 1).iloc[-1]
        if self._age is None:
            self._age = context.history("close").gt(0).sum()
        else:
            self._age = self._age.add(raw.gt(0).astype(int), fill_value=0)
        status = context.history("is_st", 1).iloc[-1]
        rebalance = self._calls % self.holding == 0
        self._calls += 1
        if not rebalance:
            # 确认成为ST后在下一开盘退出；不把状态未知直接标成非ST。
            exits = status.eq(1) & context.positions.gt(0)
            if exits.any():
                return context.weights.where(~exits, 0)
            return None
        window = max(self.volatility_window, self.momentum_window) + 1
        close = context.history("close", window, adjusted=True).where(lambda frame: frame > 0)
        amt = context.history("amt", 20)
        factor = context.history("cumadj", 1).iloc[-1]
        if len(close) < window or len(amt) < 20:
            return pd.Series(dtype=float)
        returns = close.pct_change(fill_method=None).iloc[-self.volatility_window:]
        volatility = returns.std(ddof=1)
        momentum = close.iloc[-self.momentum_skip-1] / close.iloc[-self.momentum_window-1] - 1
        adv = amt.where(amt > 0).mean().where(amt.gt(0).sum() == 20)
        eligible = (status.eq(0) & raw.gt(0) & factor.gt(0) & self._age.ge(self.min_listed)
                    & returns.notna().all() & np.isfinite(returns).all() & np.isfinite(volatility)
                    & momentum.notna() & np.isfinite(momentum)
                    & adv.ge(self.min_adv))
        pool = raw.index[eligible]
        if self.method == "equal_weight":
            selected = pool
            score = pd.Series(0.0, index=pool)
        else:
            if self.method == "lowvol60":
                score = -volatility.loc[pool]
            elif self.method == "momentum120":
                score = momentum.loc[pool]
            else:
                score = ((-volatility.loc[pool]).rank(pct=True)
                         + momentum.loc[pool].rank(pct=True)) / 2
            # 股票代码作为平分时的确定性次序，不依赖未来或随机种子。
            score = score.sort_index().sort_values(ascending=False, kind="stable")
            selected = score.head(self.k).index
        weights = pd.Series(1 / len(selected), index=selected) if len(selected) else pd.Series(dtype=float)
        for symbol in selected:
            self.selections.append({"signal_date": context.date, "symbol": symbol,
                "score": float(score.loc[symbol]), "weight": float(weights.loc[symbol]),
                "adv20": float(adv.loc[symbol]), "st": int(status.loc[symbol]),
                "volatility": float(volatility.loc[symbol]), "momentum": float(momentum.loc[symbol]),
                "pool_size": len(pool)})
        return weights
