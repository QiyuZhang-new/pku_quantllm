"""研究协议使用明确日历边界；搜索预算和交易准入也在运行前冻结。"""

from dataclasses import asdict, dataclass, field
import hashlib
import json

import numpy as np
import pandas as pd

from lesson04.backtest import BacktestConfig
from lesson05.factor_eval import Config


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def calendar_date(value):
    date = pd.Timestamp(value)
    if pd.isna(date) or date.tz is not None or date != date.normalize():
        raise ValueError("协议边界必须是无时区的日历日期")
    return date


@dataclass(frozen=True)
class Fold:
    train_end: str
    valid_end: str

    def __post_init__(self):
        if calendar_date(self.train_end) >= calendar_date(self.valid_end):
            raise ValueError("每个窗口须满足 train_end < valid_end")


@dataclass(frozen=True)
class Execution:
    initial_cash: float = 1_000_000.0
    commission: float = .0001
    slippage: float = .0005
    sell_tax: float = .0005
    lot_size: int = 100
    max_participation: float = .05
    require_status: bool = True
    max_drawdown: float = .25
    min_total_return: float = 0.
    min_excess_return: float = 0.

    def __post_init__(self):
        BacktestConfig(initial_cash=self.initial_cash, commission=self.commission,
                       slippage=self.slippage, sell_tax=self.sell_tax,
                       lot_size=self.lot_size, max_participation=self.max_participation)
        if type(self.require_status) is not bool:
            raise ValueError("require_status 必须为布尔值")
        if not np.isfinite([self.max_drawdown, self.min_total_return, self.min_excess_return]).all():
            raise ValueError("账户准入阈值必须有限")
        if not 0 < self.max_drawdown <= 1:
            raise ValueError("max_drawdown 必须在 (0,1] 内")


@dataclass(frozen=True)
class Protocol:
    folds: tuple[Fold, ...]
    holdout_end: str
    evaluation: dict = field(default_factory=dict)
    execution: Execution = field(default_factory=Execution)
    max_rounds: int = 3
    batch_size: int = 4
    max_candidates: int = 12
    max_finalists: int = 3
    max_correlation: float = .98

    def __post_init__(self):
        if not self.folds:
            raise ValueError("至少需要一个开发窗口")
        for previous, current in zip(self.folds, self.folds[1:]):
            if (calendar_date(previous.valid_end) > calendar_date(current.train_end)
                    or calendar_date(previous.valid_end) >= calendar_date(current.valid_end)):
                raise ValueError("窗口须向前推进，后一验证期不能重复使用前一验证期")
        if calendar_date(self.holdout_end) <= calendar_date(self.development_end):
            raise ValueError("holdout_end 必须晚于所有开发窗口")
        if {"train_end", "valid_end"} & self.evaluation.keys():
            raise ValueError("评价时间边界只能由 folds 指定")
        Config(**self.evaluation)
        for name in ("max_rounds", "batch_size", "max_candidates", "max_finalists"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正整数")
        if self.max_rounds > 8 or self.max_candidates > 64 or self.batch_size > 16:
            raise ValueError("首版预算上限：8轮、64候选、每批16个")
        if not 0 < self.max_correlation <= 1:
            raise ValueError("max_correlation 必须在 (0,1] 内")

    @property
    def development_end(self):
        return self.folds[-1].valid_end

    def config(self, fold=None):
        fold = fold or self.folds[-1]
        return Config(**self.evaluation, train_end=fold.train_end, valid_end=fold.valid_end)

    def to_dict(self):
        result = asdict(self)
        result["evaluation"] = {k: v for k, v in asdict(Config(**self.evaluation)).items()
                                if k not in {"train_end", "valid_end"}}
        return result

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["folds"] = tuple(Fold(**fold) for fold in value["folds"])
        value["execution"] = Execution(**value.get("execution", {}))
        return cls(**value)
