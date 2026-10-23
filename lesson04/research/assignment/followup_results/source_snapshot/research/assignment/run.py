"""先固定候选训练选择，再运行老师区间；保存全部实验而非只保存胜者。"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from backtest import BacktestConfig, BacktestEngine, MarketData
from backtest.factor_strategy import FactorSelectionStrategy


BASE = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent / "data"
CANDIDATES = ("lowvol60", "momentum120", "lowvol_momentum")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_data():
    frames = {f: pd.read_feather(BASE / "feathers" / f"{f}.feather").set_index("date")
              for f in ["open", "close", "cumadj", "amt"]}
    close = frames["close"]
    matrices = {f: np.full(close.shape, np.nan) for f in ["is_st", "trade_status", "preclose"]}
    hashes = []
    for col, symbol in enumerate(close.columns):
        path = DATA / "historical_status" / f"{symbol}.feather"
        if not path.exists():
            continue
        history = pd.read_feather(path)
        dates = pd.to_datetime(history.date)
        if dates.duplicated().any():
            raise ValueError(f"{symbol}历史状态日期重复")
        rows = close.index.get_indexer(dates)
        mask = rows >= 0
        for field, source in [("is_st", "isST"), ("trade_status", "tradestatus"), ("preclose", "preclose")]:
            values = pd.to_numeric(history[source], errors="coerce").to_numpy()
            if field != "preclose" and not np.isin(values[np.isfinite(values)], [0, 1]).all():
                raise ValueError(f"{symbol}的{source}出现未定义状态")
            matrices[field][rows[mask], col] = values[mask]
        hashes.append({"symbol": symbol, "sha256": digest(path), "rows": len(history)})
    for field, matrix in matrices.items():
        frames[field] = pd.DataFrame(matrix, index=close.index, columns=close.columns)
    # 数据覆盖与未知状态独立留档，不把无记录解释为非ST。
    known = frames["is_st"].isin([0, 1])
    valid = close.gt(0)
    coverage = pd.DataFrame({"priced": valid.sum(axis=1), "known_st_status": (valid & known).sum(axis=1),
        "unknown_st_status": (valid & ~known).sum(axis=1), "is_st": (valid & frames["is_st"].eq(1)).sum(axis=1)})
    return MarketData(frames), coverage, hashes


def configuration(start, end, slippage=.0005, record_positions=True, max_participation=.05):
    return BacktestConfig(initial_cash=1e9, commission=.0001, sell_tax=.0005, slippage=slippage,
        lot_size=100, start=start, end=end, execution_policy="standard", prestart_signal=True,
        liquidate_on_last_day=True, max_participation=max_participation, volume_lookback=20,
        record_positions=record_positions, exit_st_on_open=True, board_lots=True)


def run_case(data, directory, method, start, end, slippage=.0005, min_adv=200_000_000.0,
             record_positions=True, k=100, holding=20, max_participation=.05):
    strategy = FactorSelectionStrategy(method=method, min_adv=min_adv, k=k, holding=holding)
    result = BacktestEngine(data, configuration(start, end, slippage, record_positions, max_participation)).run(strategy)
    result.metrics.update({"end_positions": int(result.equity.iloc[-1].positions),
        "first_day_buys": int(((result.trades.date == result.equity.index[0]) & result.trades.side.eq("buy")).sum()),
        "st_position_days": int(result.equity.st_positions.sum()),
        "unknown_status_position_days": int(result.equity.unknown_status_positions.sum()),
        "fees_and_slippage_initial_capital_ratio": (result.metrics["total_fees"]+result.metrics["total_slippage_cost"])/1e9,
        "annualized_two_way_turnover": float(result.equity.turnover.mean()*252)})
    result.save(directory)
    pd.DataFrame(strategy.selections).to_csv(directory / "selections.csv", index=False)
    (directory / "strategy.json").write_text(json.dumps({"method": method, "k": k, "holding": holding,
        "min_listed": 120, "min_adv": min_adv, "volatility_window": 60,
        "momentum_window": 120, "momentum_skip": 20}, ensure_ascii=False, indent=2))
    print(directory.name, json.dumps({k: result.metrics[k] for k in ["total_return", "sharpe_zero_rf", "max_drawdown", "end_positions"]}), flush=True)
    return result


def benchmark_series(dates):
    results = pd.DataFrame(index=dates)
    for name in ["csi300", "sse"]:
        frame = pd.read_csv(DATA / f"{name}.csv", parse_dates=["date"]).set_index("date")
        aligned = frame.reindex(dates)
        if aligned[["open", "close"]].isna().any().any():
            raise ValueError(f"{name} 在作业区间缺行情，不能前填")
        nav = aligned.close / aligned.open.iloc[0]
        nav.iloc[-1] = aligned.open.iloc[-1] / aligned.open.iloc[0]
        results[name] = nav
        results[name+"_last_close"] = aligned.close / aligned.open.iloc[0]
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BASE / "research/assignment/results")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.output.exists():
        raise FileExistsError("请用新输出目录保留已有结果")
    manifest_path = DATA / "status_download_manifest.json"
    meta = json.loads(manifest_path.read_text())
    if meta["symbols"] != 5451:
        raise ValueError("全市场状态下载尚未完成，不能在部分下载上挑策略")
    if not json.loads((DATA/"data_crosschecks.json").read_text())["passed"]:
        raise ValueError("行情单位和历史ST公告样本校验未通过")
    args.output.mkdir(parents=True)
    started = time.monotonic()
    data, coverage, hashes = load_data()
    coverage.to_csv(args.output / "status_coverage.csv", index_label="date")
    pd.DataFrame(hashes).to_csv(args.output / "status_file_hashes.csv", index=False)
    # 训练结果全部保存，选择只读取训练区间Sharpe。
    training = []
    for method in CANDIDATES:
        result = run_case(data, args.output / ("train_"+method), method, "2023-01-03", "2024-12-31")
        training.append({"method": method, **result.metrics})
    pd.DataFrame(training).to_csv(args.output / "training.csv", index=False)
    winner = max(training, key=lambda r: r["sharpe_zero_rf"] if r["sharpe_zero_rf"] is not None else -np.inf)["method"]
    selection = {"chosen": winner, "criterion": "2023-2024净Sharpe最高，平分按候选顺序", "candidates": list(CANDIDATES),
        "chosen_at_utc": datetime.now(timezone.utc).isoformat(), "assignment_window_used_to_select": False}
    (args.output / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2))
    results = []
    main_result = None
    for label, slip in [("selected", .0005), ("slip_0", 0.), ("slip_15bp", .0015)]:
        result = run_case(data, args.output / label, winner, "2025-01-02", "2026-09-30", slippage=slip)
        results.append({"experiment": label, "method": winner, **result.metrics})
        if label == "selected":
            main_result = result
    equal = run_case(data, args.output / "equal_weight_liquid", "equal_weight", "2025-01-02", "2026-09-30", record_positions=False)
    results.append({"experiment": "equal_weight_liquid", "method": "equal_weight", **equal.metrics})
    pd.DataFrame(results).to_csv(args.output / "experiments.csv", index=False)
    benchmarks = benchmark_series(main_result.equity.index)
    benchmarks["strategy"] = main_result.equity.equity / 1e9
    benchmarks["equal_weight_liquid"] = equal.equity.equity / 1e9
    benchmarks.to_csv(args.output / "comparison.csv", index_label="date")
    lines = []
    for name in ["csi300", "sse", "strategy", "equal_weight_liquid"]:
        nav = benchmarks[name]
        ret = nav.pct_change(fill_method=None)
        ret.iloc[0] = nav.iloc[0]-1
        lines.append({"name": name, "total_return": float(nav.iloc[-1]-1),
            "annualized_return": float(nav.iloc[-1]**(252/len(nav))-1),
            "max_drawdown": float((nav/nav.cummax().clip(lower=1)-1).min()),
            "sharpe_zero_rf": float(ret.mean()/ret.std(ddof=1)*np.sqrt(252))})
    pd.DataFrame(lines).to_csv(args.output / "comparison_metrics.csv", index=False)
    manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "runtime_seconds": time.monotonic()-started,
        "raw_inputs": {f: digest(BASE / "feathers" / f"{f}.feather") for f in ["open", "close", "cumadj", "amt"]},
        "source_code": {f: digest(BASE/f) for f in ["backtest/engine.py", "backtest/factor_strategy.py",
            "backtest/market_rules.py", "backtest/data.py", "backtest/result.py", "research/assignment/run.py",
            "research/assignment/fetch_data.py", "research/assignment/fetch_indices.py",
            "research/assignment/data_quality.py", "research/assignment/PROTOCOL.md", "tests/test_assignment.py"]},
        "index_manifest_sha256": digest(DATA/"indices_manifest.json"),
        "status_manifest_sha256": digest(manifest_path), "status_files": len(hashes),
        "data_crosschecks_sha256": digest(DATA/"data_crosschecks.json"),
        "external_inputs": {f: digest(DATA/f) for f in ["csi300.csv", "sse.csv", "csi300_tencent.json", "sse_tencent.json", "indices_manifest.json", "data_crosschecks.json"]},
        "chosen_method": winner, "config": asdict(main_result.config)}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "SimHei"
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    labels = {"strategy": "选定策略", "csi300": "沪深300价格指数", "sse": "上证综指", "equal_weight_liquid": "同池等权账户"}
    for name in ["strategy", "csi300", "sse", "equal_weight_liquid"]:
        nav = benchmarks[name]
        axes[0].plot(nav.index, nav.values, label=labels[name])
        axes[1].plot(nav.index, nav/nav.cummax().clip(lower=1)-1, label=name)
    axes[0].set_ylabel("净值（初始资金归一为1）")
    axes[1].set_ylabel("回撤")
    axes[0].set_title("10亿元选股回测：2025-01-02至2026-09-30")
    axes[0].legend(); axes[0].grid(alpha=.3); axes[1].grid(alpha=.3)
    fig.tight_layout(); fig.savefig(args.output / "comparison.png", dpi=160); plt.close(fig)
    print("CHOSEN", winner, "runtime", time.monotonic()-started, flush=True)


if __name__ == "__main__":
    main()
