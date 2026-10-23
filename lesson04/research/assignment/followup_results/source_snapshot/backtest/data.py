"""本地日频矩阵及仅允许读取当日以前数据的策略接口。"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


FIELDS = ("open", "high", "low", "close", "amt", "num_trades", "cumadj")
PRICE_FIELDS = ("open", "high", "low", "close")


class MarketData:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        missing = {"open", "close", "cumadj"} - frames.keys()
        if missing:
            raise ValueError(f"缺少必要行情字段: {sorted(missing)}")
        reference = frames["close"]
        if not isinstance(reference.index, pd.DatetimeIndex):
            raise ValueError("行情索引必须为 DatetimeIndex")
        if reference.empty or reference.index.hasnans:
            raise ValueError("行情不能为空，日期不能缺失")
        if not reference.index.is_unique or not reference.index.is_monotonic_increasing:
            raise ValueError("日期必须唯一且按升序排列")
        if not reference.columns.is_unique or not all(isinstance(c, str) for c in reference.columns):
            raise ValueError("证券代码必须为不重复的字符串")
        self.frames = {}
        for field, frame in frames.items():
            if not frame.index.equals(reference.index) or not frame.columns.equals(reference.columns):
                raise ValueError(f"{field} 的日期或证券列与 close 不一致")
            values = frame.to_numpy(dtype=float, copy=True)
            if np.isinf(values).any():
                raise ValueError(f"{field} 包含无穷值")
            self.frames[field] = pd.DataFrame(values, index=reference.index, columns=reference.columns)
        self.dates = reference.index.copy()
        self.symbols = reference.columns.copy()

    @classmethod
    def from_feathers(cls, directory="feathers", symbols=None):
        directory = Path(directory)
        frames = {}
        for field in FIELDS:
            path = directory / f"{field}.feather"
            if not path.exists() and field not in {"open", "close", "cumadj"}:
                continue
            columns = None if symbols is None else ["date", *symbols]
            frames[field] = pd.read_feather(path, columns=columns).set_index("date")
        return cls(frames)


@dataclass(frozen=True)
class StrategyContext:
    """公开 history 接口只返回截至 date 收盘的副本；仓位为当日收盘状态。"""

    _data: MarketData
    _row: int
    cash: float
    equity: float
    positions: pd.Series
    weights: pd.Series

    @property
    def date(self):
        return self._data.dates[self._row]

    def history(self, field: str, lookback: int | None = None, *, adjusted=False):
        if lookback is not None and (not isinstance(lookback, int) or lookback <= 0):
            raise ValueError("lookback 必须为正整数或 None")
        if adjusted and field not in PRICE_FIELDS:
            raise ValueError("只有 OHLC 价格字段支持 adjusted=True")
        start = 0 if lookback is None else max(0, self._row + 1 - lookback)
        frame = self._data.frames[field].iloc[start:self._row + 1]
        if adjusted:
            factor = self._data.frames["cumadj"].iloc[start:self._row + 1]
            frame = frame * factor.where(factor > 0)
        return frame.copy()
