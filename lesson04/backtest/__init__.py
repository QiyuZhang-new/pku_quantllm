"""日频回测公共接口。"""

from .data import MarketData, StrategyContext
from .engine import BacktestConfig, BacktestEngine
from .result import BacktestResult
from .strategy import EqualWeightStrategy, Strategy

__all__ = [
    "MarketData", "StrategyContext", "BacktestConfig", "BacktestEngine",
    "BacktestResult", "EqualWeightStrategy", "Strategy",
]
