"""低市场偏离波动选股；独立于第一轮失败的总波动率策略。"""

import numpy as np
import pandas as pd


class MarketDeviationStrategy:
    def __init__(self, k=200, holding=20, window=20, min_listed=120,
                 min_adv=100_000_000., notices=None, liquidation_signals=None, equal_weight=False):
        if min(k, holding, window, min_listed) <= 0 or min_adv < 0:
            raise ValueError("参数必须为正，成交额门槛非负")
        self.k, self.holding, self.window = k, holding, window
        self.min_listed, self.min_adv = min_listed, min_adv
        self.notices = {} if notices is None else {s:pd.Timestamp(d) for s,d in notices.items()}
        self.liquidation_signals = {} if liquidation_signals is None else liquidation_signals
        self.equal_weight = equal_weight
        self._age = None
        self._calls = 0
        self.selections = []
        self.controls = []

    def on_close(self, context):
        raw = context.history("close", 1).iloc[-1]
        status = context.history("is_st", 1).iloc[-1]
        self._age = (context.history("close").gt(0).sum() if self._age is None
                     else self._age.add(raw.gt(0).astype(int), fill_value=0))
        rebalance = self._calls % self.holding == 0
        self._calls += 1
        excluded = raw.index.isin([s for s,date in self.notices.items() if date <= context.date])
        if context.date in self.liquidation_signals:
            remaining = self.liquidation_signals[context.date]
            target = context.weights * ((remaining-1)/remaining)
            target.loc[excluded | status.eq(1)] = 0.
            self.controls.append({"signal_date": context.date, "action": "scheduled_liquidation", "remaining": remaining})
            return target
        if not rebalance:
            exits = (excluded | status.eq(1)) & context.positions.gt(0)
            if exits.any():
                self.controls.append({"signal_date": context.date, "action": "announced_risk_exit", "remaining": None})
                return context.weights.where(~exits, 0)
            return None
        prices = context.history("close", self.window+1, adjusted=True).where(lambda f:f>0)
        st_history = context.history("is_st", self.window)
        returns = prices.pct_change(fill_method=None).tail(self.window)
        if len(returns) < self.window:
            return pd.Series(dtype=float)
        # 各历史日仅使用该日的非ST状态，不能用今天名单覆盖整段历史。
        market = returns.where(st_history.eq(0) & np.isfinite(returns)).mean(axis=1)
        deviation = returns.sub(market, axis=0).std(ddof=1)
        amount = context.history("amt", 20)
        adv = amount.where(amount>0).mean().where(amount.gt(0).sum() == 20)
        factor = context.history("cumadj", 1).iloc[-1]
        eligible = (status.eq(0) & ~excluded & raw.gt(0) & factor.gt(0) & self._age.ge(self.min_listed)
            & np.isfinite(returns).all() & returns.notna().all() & np.isfinite(deviation) & adv.ge(self.min_adv))
        pool = raw.index[eligible]
        scores = (-deviation.loc[pool]).sort_index().sort_values(ascending=False, kind="stable")
        selected = pool if self.equal_weight else scores.head(self.k).index
        if not len(selected):
            return pd.Series(dtype=float)
        target = pd.Series(1/len(selected), index=selected)
        for symbol in selected:
            self.selections.append({"signal_date": context.date, "symbol": symbol, "score": float(-deviation[symbol]),
                "weight": float(target[symbol]), "adv20": float(adv[symbol]), "st": int(status[symbol]),
                "pool_size": len(pool)})
        return target
