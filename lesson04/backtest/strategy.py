"""策略仅产生目标权重，不直接操作成交和资金账户。"""

from typing import Protocol

import pandas as pd

from .data import StrategyContext


class Strategy(Protocol):
    def on_close(self, context: StrategyContext) -> pd.Series | None:
        """None 表示不调仓；Series 中未列出的证券目标权重为零。"""
        ...


class EqualWeightStrategy:
    """用于演示接口的等权策略；按已观察到的日历周期首日收盘发信号。"""

    def __init__(self, rebalance="monthly"):
        if rebalance not in {"daily", "weekly", "monthly"}:
            raise ValueError("rebalance 必须是 daily、weekly 或 monthly")
        self.rebalance = rebalance
        self._period = None

    def on_close(self, context):
        date = context.date
        period = date if self.rebalance == "daily" else date.to_period(
            "W-FRI" if self.rebalance == "weekly" else "M"
        )
        if self._period == period:
            return None
        self._period = period
        close = context.history("close", 1).iloc[-1]
        factor = context.history("cumadj", 1).iloc[-1]
        eligible = close.gt(0) & factor.gt(0) & close.notna() & factor.notna()
        weights = pd.Series(0.0, index=close.index)
        if eligible.any():
            weights.loc[eligible] = 1.0 / eligible.sum()
        return weights
