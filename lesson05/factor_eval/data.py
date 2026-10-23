"""严格对齐的日频数据、因果因子与五日标签。"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


def validate_frame(frame, name="matrix"):
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{name}: 需要非空 DatetimeIndex 宽表")
    if frame.index.hasnans or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError(f"{name}: 日期必须无缺失、唯一、升序")
    if frame.index.tz is not None or not frame.index.equals(frame.index.normalize()):
        raise ValueError(f"{name}: 日期必须是无时区的交易日日期")
    if not frame.columns.is_unique or not all(isinstance(c, str) for c in frame.columns):
        raise ValueError(f"{name}: 证券代码必须为唯一字符串")
    values = frame.to_numpy(dtype=float)
    if np.isinf(values).any():
        raise ValueError(f"{name}: 不接受无穷值")


def read_matrix(path):
    path = Path(path)
    if path.suffix == ".csv":
        frame = pd.read_csv(path)
    elif path.suffix == ".feather":
        frame = pd.read_feather(path)
    elif path.suffix == ".parquet":
        frame = pd.read_parquet(path)
    else:
        raise ValueError("矩阵文件仅支持 csv / feather / parquet")
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        frame = frame.set_index("date")
    validate_frame(frame, str(path))
    return frame.astype(float)


@dataclass
class Market:
    frames: dict[str, pd.DataFrame]

    def __post_init__(self):
        required = {"open", "close", "cumadj", "amt"}
        if required - self.frames.keys():
            raise ValueError(f"缺少行情: {sorted(required - self.frames.keys())}")
        ref = self.frames["close"]
        for name, frame in self.frames.items():
            validate_frame(frame, name)
            if not frame.index.equals(ref.index) or not frame.columns.equals(ref.columns):
                raise ValueError(f"{name}: 日期或证券列与 close 不一致")

    @property
    def dates(self):
        return self.frames["close"].index

    def adjusted(self, field):
        raw, adj = self.frames[field], self.frames["cumadj"]
        return (raw * adj).where(raw.gt(0) & adj.gt(0))


def load_market(directory):
    return Market({f: read_matrix(Path(directory) / f"{f}.feather")
                   for f in ("open", "close", "cumadj", "amt")})


def forward_returns(market, horizon=5):
    """t 收盘信号，t+1 开盘进入，t+1+h 开盘退出；尾部保持 NaN。"""
    if not isinstance(horizon, int) or horizon < 1:
        raise ValueError("horizon 必须为正整数")
    price = market.adjusted("open")
    return price.shift(-(horizon + 1)).div(price.shift(-1)).sub(1)


def build_factors(market):
    """仅使用截至 t 收盘的信息；5dr 指过去五交易日复权收盘收益。"""
    close = market.adjusted("close")
    daily = close.pct_change(fill_method=None)
    return {
        "5dr": close.div(close.shift(5)).sub(1),
        "momentum20": close.div(close.shift(20)).sub(1),
        "volatility20": daily.rolling(20, min_periods=20).std(),
    }


def universe(market, min_history=120, min_adv=1e8, top_n=1000):
    f = market.frames
    close, adj, amt = (f[n] for n in ("close", "cumadj", "amt"))
    valid_price = close.gt(0) & adj.gt(0)
    # 累计有效观测数是保守的历史长度代理，不伪称真实上市年龄。
    age = valid_price.cumsum()
    adv = amt.where(amt.gt(0)).rolling(20, min_periods=20).mean()
    pool = valid_price & amt.gt(0) & age.ge(min_history) & adv.ge(min_adv)
    if top_n:
        # 同成交额按输入证券列顺序处理，完全不读取未来收益。
        pool &= adv.where(pool).rank(axis=1, ascending=False, method="first").le(top_n)
    if "eligible" in f:
        pool &= f["eligible"].eq(1)
    return pool


def demo_market(seed=7, days=900, symbols=160):
    """因果的合成环境：signal 在当日产生，影响未来开盘收益。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=days)
    columns = [f"DEMO{i:04d}" for i in range(symbols)]
    signal = rng.normal(size=(days, symbols))
    ret = rng.normal(0.00015, 0.009, size=signal.shape)
    for lag in range(2, 7):
        ret[lag:] += 0.001 * signal[:-lag]
    price = 20 * np.exp(np.cumsum(ret, axis=0))
    frame = lambda a: pd.DataFrame(a, index=dates, columns=columns)
    market = Market({"open": frame(price), "close": frame(price.copy()),
                     "cumadj": frame(np.ones_like(price)),
                     "amt": frame(np.full_like(price, 1e9))})
    factors = {"synthetic_signal": frame(signal),
               "synthetic_noise": frame(rng.normal(size=signal.shape)),
               "synthetic_constant": frame(np.ones_like(price))}
    return market, factors
