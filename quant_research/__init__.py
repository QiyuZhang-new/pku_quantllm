"""固定协议下的因子研究、账户验证与可追溯搜索。"""

from .contracts import Fold, Protocol
from .factors import FactorSpec, compute_factor

__all__ = ["Fold", "Protocol", "FactorSpec", "compute_factor"]
