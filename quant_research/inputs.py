"""适配现有 Feather 行情及第四讲逐证券历史状态缓存。"""

from pathlib import Path

import numpy as np
import pandas as pd

from lesson05.factor_eval import Market
from .storage import digest, write_json


def load_inputs(directory, end, symbols=None, status_directory=None):
    directory = Path(directory).resolve()
    frames, hashes = {}, {}
    marker = directory / "SYNTHETIC.json"
    if marker.exists():
        hashes[str(marker)] = digest(marker)
    columns = None if symbols is None else ["date", *symbols]
    for name in ("open", "high", "low", "close", "cumadj", "amt", "eligible", "is_st", "trade_status", "preclose"):
        path = directory / f"{name}.feather"
        if not path.exists() and name not in {"open", "close", "cumadj", "amt"}:
            continue
        hashes[str(path)] = digest(path)
        frame = pd.read_feather(path, columns=columns).set_index("date")
        frames[name] = frame.loc[:pd.Timestamp(end)].astype(float)
    market = Market(frames)
    if status_directory is not None:
        if {"is_st", "trade_status", "preclose"} & frames.keys():
            raise ValueError("宽表状态与逐证券状态目录不能同时指定")
        status_directory = Path(status_directory).resolve()
        ref = frames["close"]
        for name in ("is_st", "trade_status", "preclose"):
            frames[name] = pd.DataFrame(np.nan, index=ref.index, columns=ref.columns)
        for symbol in ref.columns:
            path = status_directory / f"{symbol}.feather"
            if not path.exists():
                continue
            hashes[str(path)] = digest(path)
            history = pd.read_feather(path)
            history["date"] = pd.to_datetime(history["date"])
            if history.date.duplicated().any():
                raise ValueError(f"{symbol} 状态日期重复")
            history = history.set_index("date").reindex(ref.index)
            for name, source in (("is_st", "isST"), ("trade_status", "tradestatus"), ("preclose", "preclose")):
                frames[name][symbol] = pd.to_numeric(history[source], errors="raise")
    for name in ("is_st", "trade_status", "eligible"):
        if name in frames and not np.isin(frames[name].to_numpy()[frames[name].notna()], [0, 1]).all():
            raise ValueError(f"{name} 只能包含0、1或缺失")
    # 评价与账户回测共用信号日状态；未知不自动视为非ST或正常交易。
    eligible = frames.get("eligible", pd.DataFrame(1., index=market.dates, columns=frames["close"].columns)).eq(1)
    if "is_st" in frames:
        eligible &= frames["is_st"].eq(0)
    if "trade_status" in frames:
        eligible &= frames["trade_status"].eq(1)
    frames["eligible"] = eligible.astype(float)
    return Market(frames), hashes


def demo_inputs(directory):
    """持久横截面漂移的合成正对照；仅供验证管线，不能作为市场有效性证据。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "SYNTHETIC.json", {"synthetic": True, "generator": "persistent-drift-v1", "seed": 817})
    rng = np.random.default_rng(817)
    days, count = 900, 40
    dates = pd.bdate_range("2020-01-02", periods=days)
    columns = [f"{i + 1:06d}.SZ" for i in range(count)]
    drift = np.linspace(-.0025, .0035, count)
    changes = drift + rng.normal(0, .006, (days, count))
    prices = 20 * np.exp(np.cumsum(changes, axis=0))
    values = {"open": prices, "close": prices.copy(), "high": prices * 1.01, "low": prices * .99,
              "cumadj": np.ones_like(prices),
              "amt": np.full_like(prices, 1e9), "is_st": np.zeros_like(prices),
              "trade_status": np.ones_like(prices)}
    for name, value in values.items():
        pd.DataFrame(value, index=pd.Index(dates, name="date"), columns=columns).reset_index().to_feather(directory / f"{name}.feather")
    return dates
