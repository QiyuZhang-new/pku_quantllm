"""网页工作台使用的实验服务；与页面框架解耦，复用同一回测引擎。"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from typing import Callable
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import pyarrow as pa
import pyarrow.feather as feather

from . import BacktestConfig, BacktestEngine, BacktestResult, EqualWeightStrategy, MarketData, Strategy
from .data import FIELDS
from .result import performance


@dataclass(frozen=True)
class StrategySpec:
    description: str
    factory: Callable[[str], Strategy]


# 后续策略在此注册；每次回测都由 factory 创建一个新实例。
STRATEGIES = {
    "等权示例": StrategySpec(
        "对当日具有有效收盘价与复权因子的证券等权配置，按所选周期调仓。",
        EqualWeightStrategy,
    ),
}


@dataclass
class DashboardRun:
    result: BacktestResult
    directory: Path
    experiment: dict


def data_stamp(directory):
    """文件变化时使页面的轻量元数据缓存失效。"""
    directory = Path(directory)
    return tuple(
        (field, path.stat().st_mtime_ns, path.stat().st_size)
        for field in FIELDS
        if (path := directory / f"{field}.feather").exists()
    )


def inspect_data(directory):
    directory = Path(directory)
    for name in ("open", "close", "cumadj"):
        if not (directory / f"{name}.feather").is_file():
            raise FileNotFoundError(f"行情目录缺少 {name}.feather")
    path = directory / "close.feather"
    with pa.memory_map(str(path), "r") as source:
        columns = pa.ipc.open_file(source).schema.names
    dates = pd.DatetimeIndex(feather.read_table(path, columns=["date"]).column("date").to_pandas())
    if not len(dates) or dates.hasnans or not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError("行情日期必须非空、无缺失、唯一且按升序排列")
    return {"symbols": tuple(c for c in columns if c != "date"), "dates": dates}


def execute_experiment(data_directory, symbols, config, strategy_name, rebalance, output_parent):
    if strategy_name not in STRATEGIES:
        raise ValueError(f"未知策略: {strategy_name}")
    if symbols is not None and not symbols:
        raise ValueError("请至少选择一只证券，或选择全部证券")
    data_directory = Path(data_directory).resolve()
    stamp = data_stamp(data_directory)
    data = MarketData.from_feathers(data_directory, symbols)
    strategy = STRATEGIES[strategy_name].factory(rebalance)
    result = BacktestEngine(data, config).run(strategy)
    if stamp != data_stamp(data_directory):
        raise RuntimeError("回测过程中行情文件发生变化，请待数据更新完成后重试")
    directory = Path(output_parent) / datetime.now(timezone.utc).strftime("web_%Y%m%dT%H%M%S%fZ")
    experiment = {
        "strategy": strategy_name,
        "rebalance": rebalance,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_directory": str(data_directory),
        "data_files": [{"field": name, "mtime_ns": mtime, "bytes": size} for name, mtime, size in stamp],
        "symbols": data.symbols.tolist(),
        "first_data_date": str(data.dates[0]),
        "last_data_date": str(data.dates[-1]),
        "config": asdict(config),
    }
    result.save(directory)
    (directory / "experiment.json").write_text(
        json.dumps(experiment, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    return DashboardRun(result, directory, experiment)


def saved_experiments(parent):
    required = ("equity.csv", "trades.csv", "positions.csv", "rejected_orders.csv", "config.json", "metrics.json")
    parent = Path(parent)
    if not parent.exists():
        return []
    return sorted(
        [p for p in parent.iterdir() if p.is_dir() and all((p / name).is_file() for name in required)],
        key=lambda p: (p / "equity.csv").stat().st_mtime_ns,
        reverse=True,
    )


def load_experiment(directory):
    directory = Path(directory)
    config = BacktestConfig(**json.loads((directory / "config.json").read_text(encoding="utf-8")))
    equity = pd.read_csv(directory / "equity.csv", index_col="date", parse_dates=["date"])
    trades = pd.read_csv(directory / "trades.csv", parse_dates=["date", "signal_date"])
    positions = pd.read_csv(directory / "positions.csv", parse_dates=["date"])
    rejected = pd.read_csv(directory / "rejected_orders.csv", parse_dates=["date", "signal_date"])
    # 重新计算使较早的 CLI 结果也可显示新增指标，例如平均仓位。
    metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    metrics.update(performance(equity, config.initial_cash, config.annual_days))
    result = BacktestResult(equity, trades, positions, rejected, metrics, config)
    metadata = directory / "experiment.json"
    experiment = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else {}
    return DashboardRun(result, directory, experiment)


def archive_experiment(directory):
    """按需打包已有产物，不重复执行回测，不写入额外文件。"""
    directory = Path(directory)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name in (
            "equity.csv", "trades.csv", "positions.csv", "rejected_orders.csv",
            "metrics.json", "config.json", "experiment.json", "equity.png",
        ):
            path = directory / name
            if path.is_file():
                archive.write(path, arcname=name)
    return buffer.getvalue()
