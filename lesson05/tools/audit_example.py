"""独立从原始行情抽查默认 5dr 报告，不调用评价引擎的指标函数。"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    base = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=base.parent / "lesson04" / "feathers")
    parser.add_argument("--report", type=Path, default=base / "examples" / "course_default")
    args = parser.parse_args()
    summary = json.loads((args.report / "factor_01" / "summary.json").read_text())
    if summary["factor"] != "5dr":
        raise ValueError("本抽查入口要求默认报告的第一个因子为 5dr")
    cfg = summary["config"]
    frames = {name: pd.read_feather(args.data / f"{name}.feather").set_index("date")
              for name in ("open", "close", "cumadj", "amt")}
    dates = frames["close"].index
    daily = pd.read_csv(args.report / "factor_01" / "daily.csv", parse_dates=["date"]).set_index("date")
    # 独立展开公式，未复用 forward_returns、universe、row_correlation、group_diagnostics。
    adjusted_close = (frames["close"] * frames["cumadj"]).where(
        frames["close"].gt(0) & frames["cumadj"].gt(0))
    adjusted_open = (frames["open"] * frames["cumadj"]).where(
        frames["open"].gt(0) & frames["cumadj"].gt(0))
    raw_factor = adjusted_close / adjusted_close.shift(5) - 1
    valid = frames["close"].gt(0) & frames["cumadj"].gt(0)
    adv = frames["amt"].where(frames["amt"].gt(0)).rolling(20).mean()
    pool = valid & valid.cumsum().ge(cfg["min_history"]) & frames["amt"].gt(0) & adv.ge(cfg["min_adv"])
    if cfg["top_n"]:
        pool &= adv.where(pool).rank(axis=1, ascending=False, method="first").le(cfg["top_n"])
    checks = []
    for split in ("train", "validation", "test"):
        eligible = daily.loc[daily.split.eq(split) & daily.rank_ic.notna()]
        chosen = eligible.index[np.linspace(0, len(eligible) - 1, 5, dtype=int)]
        for date in chosen:
            row = dates.get_loc(date)
            score = raw_factor.loc[date].where(pool.loc[date]) * summary["direction"]
            returns = adjusted_open.iloc[row + cfg["horizon"] + 1] / adjusted_open.iloc[row + 1] - 1
            matched = score.notna() & returns.notna()
            ic = score[matched].rank().corr(returns[matched].rank())
            np.testing.assert_allclose(ic, daily.loc[date, "rank_ic"], rtol=1e-10, atol=1e-12)
            groups = np.ceil(score.rank(pct=True, method="average") * cfg["groups"])
            for k in range(1, cfg["groups"] + 1):
                selected = returns.loc[groups.eq(k)]
                value = selected.mean() if selected.notna().all() and len(selected) else np.nan
                np.testing.assert_allclose(value, daily.loc[date, f"q{k}"], equal_nan=True, atol=1e-12)
            constituents = returns.loc[score.notna()]
            benchmark = constituents.mean() if constituents.notna().all() else np.nan
            top = daily.loc[date, f"q{cfg['groups']}"]
            net = top - benchmark - 2 * cfg["one_way_bps"] / 10000
            np.testing.assert_allclose(net, daily.loc[date, "long_excess_net"], equal_nan=True, atol=1e-12)
            checks.append({"date": str(date.date()), "split": split, "rank_ic": float(ic)})
    manifest = json.loads((args.report / "manifest.json").read_text())
    for name, expected in manifest["code_sha256"].items():
        actual = hashlib.sha256((base / "factor_eval" / name).read_bytes()).hexdigest()
        if actual != expected:
            raise AssertionError(f"源码哈希不符：{name}；报告与当前代码不是同一版本")
    print(json.dumps({"passed": True, "sampled_dates": len(checks),
                      "checked": ["oriented_rank_ic", "all_groups", "benchmark_and_round_trip_cost", "code_hashes"],
                      "samples": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
