"""从逐证券原始端点独立复算已保存统计，核对结果，不调参数。"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="research/results/reversal_default")
    args = parser.parse_args()
    directory = Path(args.results)
    manifest = json.loads((directory / "manifest.json").read_text())
    cfg = manifest["config"]
    observations = pd.read_feather(directory / "signal_observations.feather")
    pools = pd.read_feather(directory / "pool_members.feather")
    daily = pd.read_csv(directory / "daily.csv", parse_dates=["date"])
    frames = {}
    checks = 0

    def check(condition, message):
        nonlocal checks
        if not condition:
            raise AssertionError(message)
        checks += 1

    def close(actual, expected, message):
        check(np.allclose(actual, expected, rtol=1e-9, atol=1e-12, equal_nan=True), message)

    for name, entry in manifest["inputs"].items():
        path = Path(entry["path"])
        check(hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], f"输入改变: {name}")
        frames[name] = pd.read_feather(path).set_index("date")
    source_map = {"signal_health.py": Path("research/signal_health.py"), "PROTOCOL.md": Path("research/PROTOCOL.md"),
                  "market_rules.py": Path("backtest/market_rules.py"), "STRATEGY.md": Path("STRATEGY.md")}
    for name, expected in manifest["source"].items():
        check(hashlib.sha256(source_map[name].read_bytes()).hexdigest() == expected, f"分析源码改变: {name}")
    dates = frames["close"].index
    arrays = {name: frame.to_numpy() for name, frame in frames.items()}
    row, col = observations.row.to_numpy(), observations.col.to_numpy()
    check(np.array_equal(frames["close"].columns.to_numpy()[col], observations.symbol.to_numpy()), "行列和证券代码不符")
    check((observations.loc[observations.signal.eq("reversal"), "group_size"] == cfg["K"]).all(), "主信号原定数量不等于 K")
    entry_row = np.minimum(row + 1, len(dates) - 1)
    opening = arrays["open"][entry_row, col]
    previous = arrays["close"][row, col]
    factor = arrays["cumadj"][entry_row, col]
    rate = np.array([.2 if s.startswith(("300", "301", "688")) else .1 for s in observations.symbol])
    known = row + 1 < len(dates)
    down = np.isfinite(opening) & (opening > 0) & np.isfinite(previous) & (previous > 0) & (opening <= previous * (1 - rate) + abs(previous) * 1e-12)
    admitted = known & np.isfinite(opening) & (opening > 0) & np.isfinite(factor) & (factor > 0) & np.isfinite(previous) & (previous > 0) & ~down
    check(np.array_equal(down & known, observations.entry_limit_down), "跌停计数错误")
    check(np.array_equal(admitted, observations.entry_admitted), "可入场掩码错误")

    def stock_return(frame, rows, cols, horizon, lag):
        start = rows + lag
        end = start + horizon
        available = end < len(dates)
        start = np.minimum(start, len(dates) - 1)
        end = np.minimum(end, len(dates) - 1)
        numerator = arrays[frame][end, cols] * arrays["cumadj"][end, cols]
        denominator = arrays[frame][start, cols] * arrays["cumadj"][start, cols]
        valid = available & np.isfinite(numerator) & np.isfinite(denominator) & (numerator > 0) & (denominator > 0)
        result = np.full(len(rows), np.nan)
        result[valid] = numerator[valid] / denominator[valid] - 1
        return result

    for horizon in cfg["horizons"]:
        closed = stock_return("close", row, col, horizon, 0)
        opened = stock_return("open", row, col, horizon, 1)
        cash = opened.copy()
        matured = row + 1 + horizon < len(dates)
        cash[~admitted & known & matured] = 0
        cash[~matured] = np.nan
        close(observations[f"return_close_{horizon}"], closed, "收盘端点计算错误")
        close(observations[f"return_open_{horizon}"], opened, "开盘端点计算错误")
        close(observations[f"return_buyable_{horizon}"], cash, "现金份额或缺失端点处理错误")
        for variant, values, field, lag in (("close_close", closed, "close", 0), ("open_open", opened, "open", 1), ("buyable_cash", cash, "open", 1)):
            exported = daily.loc[daily.variant.eq(variant) & daily.horizon.eq(horizon)].set_index(["signal", "row"]).sort_index()
            samples = observations[["signal", "row"]].copy()
            samples["return"] = values
            end_row = np.minimum(row + horizon + lag, len(dates) - 1)
            cost = 2 * cfg["one_way_cost"] + np.where(dates[end_row] < pd.Timestamp("2023-08-28"), .001, .0005)
            samples["cost"] = cost * admitted if variant == "buyable_cash" else cost
            grouped = samples.groupby(["signal", "row"]).agg(count=("return", "count"), size=("return", "size"), mean=("return", "mean"), cost=("cost", "mean")).sort_index()
            grouped.loc[grouped["count"] != grouped["size"], "mean"] = np.nan
            close(exported.gross_return, grouped["mean"], "组均值或完整样本口径错误")
            close(exported.estimated_cost, grouped["cost"], "历史税率/现金成本错误")
            pool_return = stock_return(field, pools.row.to_numpy(), pools.col.to_numpy(), horizon, lag)
            pool_series = pd.Series(pool_return).groupby(pools.row).mean()
            pool_means = exported.index.get_level_values("row").map(pool_series)
            close(exported.pool_return, pool_means, "池平均收益错误")
            close(exported.gross_excess, grouped["mean"].to_numpy() - np.asarray(pool_means), "毛超额错误")
            close(exported.net_excess, exported.gross_excess - exported.estimated_cost, "净超额错误")

    for name, keys in (("summary", ["signal", "variant", "horizon"]), ("annual", ["signal", "variant", "horizon", "year"])):
        aggregate = pd.read_csv(directory / f"{name}.csv").set_index(keys)
        for key, group in daily.groupby(keys):
            valid = group.loc[group.net_excess.notna()]
            if valid.empty:
                continue
            exported = aggregate.loc[key]
            close(exported.net_excess, valid.net_excess.mean(), "全期/年度净超额不符")
            close(exported.gross_excess, valid.gross_excess.mean(), "全期/年度毛超额不符")
            check(int(exported.n_dates) == len(valid), "有效日期分母不符")
    paired = pd.read_csv(directory / "paired.csv")
    for record in paired.itertuples():
        group = daily.loc[daily.variant.eq(record.variant) & daily.horizon.eq(record.horizon)]
        sample = group.pivot(index="row", columns="signal", values=record.measure)[["reversal", "rsi2"]].dropna()
        check(len(sample) == record.n_paired_dates, "配对日期数量不符")
        close(record.rsi_minus_reversal, (sample.rsi2 - sample.reversal).mean(), "配对差额不符")
    gate = json.loads((directory / "gate.json").read_text())
    primary = daily.loc[daily.signal.eq("reversal") & daily.variant.eq("buyable_cash") & daily.horizon.eq(5)]
    close(gate["primary_net_excess"], primary.net_excess.mean(), "闸门值不符")
    check(not gate["passed"] and primary.net_excess.mean() <= 0, "未在默认成本失败后停止")
    target = observations.loc[observations.signal.eq("reversal") & observations.entry_observed]
    close(gate["limit_down_ratio"], target.entry_limit_down.mean(), "闸门跌停分母不符")
    output_hashes = {}
    for path in sorted(directory.iterdir()):
        if path.suffix in (".csv", ".feather") or path.name in ("gate.json", "manifest.json"):
            output_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {"passed": True, "checks": checks, "scope": "原始端点→逐目标→逐日→年度/全期/配对→闸门", "outputs": output_hashes}
    (directory / "audit.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    print(f"独立复核通过：{checks} 项；阶段 1 失败状态一致。")


if __name__ == "__main__":
    main()
