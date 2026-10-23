"""独立信号体检：不创建账户、不调用 BacktestEngine。"""

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from math import erfc, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.market_rules import opening_limits, stamp_tax


@dataclass(frozen=True)
class SignalConfig:
    N: int = 500
    K: int = 20
    L: int = 5
    H: int = 5
    MA: int = 200
    MIN_LISTED: int = 60
    RSI_PERIOD: int = 2
    RSI_THRESHOLD: float = 10.0
    horizons: tuple = (1, 3, 5, 10, 20)
    one_way_cost: float = .0015
    cost_levels: tuple = (.0005, .0015, .003)
    hac_lags: int = 20
    max_limit_down_ratio: float = .20

    def __post_init__(self):
        for name in ("N", "K", "L", "H", "MA", "MIN_LISTED", "RSI_PERIOD"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正整数")
        if self.K > self.N or self.H not in self.horizons:
            raise ValueError("要求 K<=N 且 H 位于观察期限中")


def wilder_rsi(prices, period=2):
    """逐时间向前递推；遇到缺价重置种子，绝不填充。"""
    values = np.asarray(prices, dtype=float)
    result = np.full_like(values, np.nan)
    previous = np.full(values.shape[1], np.nan)
    gains = np.zeros(values.shape[1])
    losses = np.zeros(values.shape[1])
    counts = np.zeros(values.shape[1], dtype=int)
    for row, price in enumerate(values):
        valid = np.isfinite(price) & (price > 0)
        pair = valid & np.isfinite(previous)
        reset = ~pair
        gains[reset] = losses[reset] = 0
        counts[reset] = 0
        delta = np.zeros_like(price)
        np.subtract(price, previous, out=delta, where=pair)
        gain, loss = np.maximum(delta, 0), np.maximum(-delta, 0)
        seed = pair & (counts < period)
        smooth = pair & ~seed
        gains[seed] += gain[seed] / period
        losses[seed] += loss[seed] / period
        gains[smooth] = ((period - 1) * gains[smooth] + gain[smooth]) / period
        losses[smooth] = ((period - 1) * losses[smooth] + loss[smooth]) / period
        counts[pair] += 1
        ready = pair & (counts >= period)
        total = gains + losses
        result[row, ready] = np.divide(100 * gains[ready], total[ready], out=np.full(int(ready.sum()), 50.0), where=total[ready] > 0)
        previous = np.where(valid, price, np.nan)
    return result


def build_signals(frames, config):
    close, factor, amount = (frames[name] for name in ("close", "cumadj", "amt"))
    adjusted = (close * factor).where((close > 0) & (factor > 0))
    lag_return = adjusted.pct_change(config.L, fill_method=None).to_numpy()
    rsi = wilder_rsi(adjusted.to_numpy(), config.RSI_PERIOD)
    daily_market = adjusted.pct_change(fill_method=None).mean(axis=1)
    daily_market.iloc[0] = 0.0  # 指数起点基值，不是缺价收益填零。
    proxy = (1 + daily_market).cumprod()
    average = proxy.rolling(config.MA, min_periods=config.MA).mean()
    regime = (proxy > average).to_numpy(copy=True)
    regime[:config.MA] = False
    market = pd.DataFrame({"market_return": daily_market, "market_proxy": proxy, "moving_average": average, "regime_on": regime})
    market.index.name = "date"
    symbols = close.columns.to_numpy()
    first_seen = np.full(len(symbols), -1, dtype=int)
    pool_frames, selection_frames = [], []
    raw = close.to_numpy()
    factors, amounts = factor.to_numpy(), amount.to_numpy()
    for row, date in enumerate(close.index):
        observed = np.isfinite(raw[row]) & (raw[row] > 0)
        first_seen[(first_seen < 0) & observed] = row
        age = row - first_seen + 1
        eligible = observed & (first_seen >= 0) & (age >= config.MIN_LISTED)
        eligible &= np.isfinite(factors[row]) & (factors[row] > 0) & np.isfinite(amounts[row]) & (amounts[row] > 0)
        candidates = np.flatnonzero(eligible)
        ordered = np.lexsort((symbols[candidates], -amounts[row, candidates]))
        pool = candidates[ordered[:config.N]]
        if not len(pool):
            continue
        pool_frames.append(pd.DataFrame({
            "date": date, "row": row, "col": pool, "symbol": symbols[pool],
            "amount": amounts[row, pool], "listed_age": age[pool],
            "return_L": lag_return[row, pool], "rsi2": rsi[row, pool], "regime_on": regime[row],
        }))
        valid_return = pool[np.isfinite(lag_return[row, pool])]
        reversal = valid_return[np.lexsort((symbols[valid_return], lag_return[row, valid_return]))[:config.K]]
        if len(reversal) < config.K:
            reversal = np.array([], dtype=int)
        candidates_rsi = pool[np.isfinite(rsi[row, pool]) & (rsi[row, pool] < config.RSI_THRESHOLD)]
        selected_rsi = candidates_rsi[np.lexsort((symbols[candidates_rsi], rsi[row, candidates_rsi]))[:config.K]]
        for name, chosen in (("reversal", reversal), ("rsi2", selected_rsi)):
            if len(chosen):
                selection_frames.append(pd.DataFrame({
                    "date": date, "row": row, "col": chosen, "symbol": symbols[chosen], "signal": name,
                    "return_L": lag_return[row, chosen], "rsi2": rsi[row, chosen],
                    "group_size": len(chosen), "pool_size": len(pool), "regime_on": regime[row],
                }))
    if not selection_frames:
        raise ValueError("没有可构造的信号组，不能判断盈利能力")
    return pd.concat(pool_frames, ignore_index=True), pd.concat(selection_frames, ignore_index=True), market


def forward_panel(prices, horizon, entry_lag=0):
    """行 t 的结果始终为 price[t+entry_lag+h]/price[t+entry_lag]-1。"""
    prices = np.asarray(prices, dtype=float)
    result = np.full_like(prices, np.nan)
    count = len(prices) - horizon - entry_lag
    if count > 0:
        entry = prices[entry_lag:entry_lag + count]
        exit_price = prices[entry_lag + horizon:entry_lag + horizon + count]
        valid = np.isfinite(entry) & (entry > 0) & np.isfinite(exit_price) & (exit_price > 0)
        values = np.full_like(entry, np.nan)
        np.divide(exit_price, entry, out=values, where=valid)
        result[:count] = values - 1
    return result


def hac_mean(values, lags=20):
    values = np.asarray(values, dtype=float)
    finite = np.isfinite(values)
    n = int(finite.sum())
    if not n:
        return np.nan, np.nan
    mean = float(values[finite].mean())
    residual = np.zeros(len(values))
    residual[finite] = values[finite] - mean
    long_variance = float(residual @ residual) / n
    for lag in range(1, min(lags, len(values) - 1) + 1):
        covariance = float(residual[:-lag] @ residual[lag:]) / n
        long_variance += 2 * (1 - lag / (lags + 1)) * covariance
    return mean, sqrt(max(0.0, long_variance) / n)


def summarize(daily, config, annual=False):
    keys = ["signal", "variant", "horizon"] + (["year"] if annual else [])
    records = []
    for key, group in daily.groupby(keys, sort=True):
        valid = group.loc[group["net_excess"].notna()]
        if valid.empty:
            continue
        ordered = group.set_index("row").sort_index()
        calendar = ordered.reindex(range(int(ordered.index.min()), int(ordered.index.max()) + 1))
        mean, se = hac_mean(calendar["net_excess"], config.hac_lags)
        gross, gross_se = hac_mean(calendar["gross_excess"], config.hac_lags)
        record = dict(zip(keys, key))
        record.update({
            "n_dates": len(valid), "n_targets": int(valid["selected_n"].sum()),
            "selected_return": float(valid["gross_return"].mean()),
            "pool_return": float(valid["pool_return"].mean()),
            "gross_excess": gross, "estimated_cost": float(valid["estimated_cost"].mean()),
            "net_excess": mean, "hac_se": se, "ci_low": mean - 1.96 * se, "ci_high": mean + 1.96 * se,
            "gross_hac_se": gross_se, "gross_ci_low": gross - 1.96 * gross_se, "gross_ci_high": gross + 1.96 * gross_se,
            "p_value_normal": erfc(abs(mean / se) / sqrt(2)) if se > 0 else np.nan,
            "incomplete_dates": int((group["priced_n"] < group["selected_n"]).sum()),
            "right_censored_dates": int(group["right_censored"].sum()),
            "missing_endpoint_dates": int(((group["priced_n"] < group["selected_n"]) & ~group["right_censored"]).sum()),
            "mean_fill_ratio": float(valid["fill_ratio"].mean()),
        })
        records.append(record)
    return pd.DataFrame(records)


def analyze(frames, config):
    pools, observations, market = build_signals(frames, config)
    dates = frames["close"].index
    symbols = frames["close"].columns
    raw_open = frames["open"].to_numpy()
    raw_close = frames["close"].to_numpy()
    factor = frames["cumadj"].to_numpy()
    adjusted_open = np.where((raw_open > 0) & (factor > 0), raw_open * factor, np.nan)
    adjusted_close = np.where((raw_close > 0) & (factor > 0), raw_close * factor, np.nan)
    down = np.zeros_like(raw_open, dtype=bool)
    up = np.zeros_like(raw_open, dtype=bool)
    reference_valid = np.zeros_like(raw_open, dtype=bool)
    down[1:], up[1:], reference_valid[1:] = opening_limits(raw_open[1:], raw_close[:-1], symbols)
    row, col = observations["row"].to_numpy(), observations["col"].to_numpy()
    entry = np.minimum(row + 1, len(dates) - 1)
    entry_observed = row + 1 < len(dates)
    raw_valid = np.isfinite(raw_open[entry, col]) & (raw_open[entry, col] > 0)
    factor_valid = np.isfinite(factor[entry, col]) & (factor[entry, col] > 0)
    reasons = np.full(len(row), "", dtype=object)
    reasons[down[entry, col]] = "limit_down"
    reasons[~reference_valid[entry, col]] = "missing_previous_close"
    reasons[~factor_valid] = "missing_factor"
    reasons[~raw_valid] = "missing_open"
    reasons[~entry_observed] = "right_censored"
    admitted = reasons == ""
    observations["entry_date"] = dates[entry].to_numpy()
    observations.loc[~entry_observed, "entry_date"] = pd.NaT
    observations["entry_observed"] = entry_observed
    observations["entry_reason"] = reasons
    observations["entry_limit_down"] = down[entry, col] & entry_observed
    observations["entry_admitted"] = admitted
    daily_frames = []
    group_keys = ["signal", "row"]
    taxes = np.array([stamp_tax(date) for date in dates])
    for horizon in config.horizons:
        close_forward = forward_panel(adjusted_close, horizon)
        open_forward = forward_panel(adjusted_open, horizon, 1)
        end = row + 1 + horizon
        matured = end < len(dates)
        safe_end = np.minimum(end, len(dates) - 1)
        observations[f"matured_{horizon}"] = matured
        observations[f"exit_date_{horizon}"] = dates[safe_end].to_numpy()
        observations.loc[~matured, f"exit_date_{horizon}"] = pd.NaT
        observations[f"exit_upper_{horizon}"] = up[safe_end, col] & admitted & matured
        observations[f"return_close_{horizon}"] = close_forward[row, col]
        observations[f"return_open_{horizon}"] = open_forward[row, col]
        buyable = open_forward[row, col].copy()
        # 已知未入场份额是现金贡献 0，不是把任何缺失行情填成 0。
        buyable[(~admitted) & entry_observed & matured] = 0.0
        buyable[~matured] = np.nan
        observations[f"return_buyable_{horizon}"] = buyable
        for variant, values, panel, cost_end in (
            ("close_close", close_forward[row, col], close_forward, np.minimum(row + horizon, len(dates) - 1)),
            ("open_open", open_forward[row, col], open_forward, safe_end),
            ("buyable_cash", buyable, open_forward, safe_end),
        ):
            cost = 2 * config.one_way_cost + taxes[cost_end]
            if variant == "buyable_cash":
                cost = cost * admitted
            pool_values = panel[pools["row"].to_numpy(), pools["col"].to_numpy()]
            pool_stats = pd.DataFrame({"row": pools["row"], "return": pool_values}).groupby("row")["return"].agg(["mean", "count"])
            sample = observations[["date", "signal", "row", "group_size", "pool_size", "regime_on"]].copy()
            sample["value"] = values
            sample["cost"] = cost
            sample["fill"] = admitted if variant == "buyable_cash" else 1.0
            grouped = sample.groupby(group_keys, sort=False).agg(
                date=("date", "first"), selected_n=("group_size", "first"), pool_n=("pool_size", "first"),
                regime_on=("regime_on", "first"), priced_n=("value", "count"), gross_return=("value", "mean"),
                estimated_cost=("cost", "mean"), fill_ratio=("fill", "mean"),
            ).reset_index()
            grouped["pool_return"] = grouped["row"].map(pool_stats["mean"])
            grouped["pool_priced_n"] = grouped["row"].map(pool_stats["count"])
            grouped.loc[grouped["priced_n"] < grouped["selected_n"], "gross_return"] = np.nan
            grouped["gross_excess"] = grouped["gross_return"] - grouped["pool_return"]
            grouped["net_excess"] = grouped["gross_excess"] - grouped["estimated_cost"]
            grouped["year"] = grouped["date"].dt.year
            grouped["horizon"] = horizon
            grouped["variant"] = variant
            grouped["right_censored"] = grouped["row"] + horizon + (variant != "close_close") >= len(dates)
            daily_frames.append(grouped)
    daily = pd.concat(daily_frames, ignore_index=True)
    summary = summarize(daily, config)
    annual = summarize(daily, config, annual=True)
    comparisons = []
    for (variant, horizon), group in daily.groupby(["variant", "horizon"]):
        for measure in ("gross_excess", "net_excess"):
            paired = group.pivot(index="row", columns="signal", values=measure)
            if not {"reversal", "rsi2"}.issubset(paired.columns):
                continue
            paired = paired[["reversal", "rsi2"]].dropna()
            if not len(paired):
                continue
            comparisons.append({
                "variant": variant, "horizon": horizon, "measure": measure, "n_paired_dates": len(paired),
                "reversal": float(paired["reversal"].mean()), "rsi2": float(paired["rsi2"].mean()),
                "rsi_minus_reversal": float((paired["rsi2"] - paired["reversal"]).mean()),
            })
    availability = []
    for (signal, year), group in observations.assign(year=observations["date"].dt.year).groupby(["signal", "year"]):
        known = group.loc[group["entry_observed"]]
        availability.append({
            "signal": signal, "year": year, "targets": len(known),
            "limit_down": int(known["entry_limit_down"].sum()),
            "limit_down_ratio": float(known["entry_limit_down"].mean()),
            "admitted_ratio": float(known["entry_admitted"].mean()),
            "missing_open": int((known["entry_reason"] == "missing_open").sum()),
            "missing_factor": int((known["entry_reason"] == "missing_factor").sum()),
            "missing_previous_close": int((known["entry_reason"] == "missing_previous_close").sum()),
        })
    sensitivity = []
    base = daily.loc[(daily["variant"] == "buyable_cash") & (daily["horizon"] == config.H)]
    for signal, group in base.groupby("signal"):
        valid = group.loc[group["net_excess"].notna()]
        for cost in config.cost_levels:
            net = valid["net_excess"] - 2 * (cost - config.one_way_cost) * valid["fill_ratio"]
            sensitivity.append({"signal": signal, "one_way_cost": cost, "n_dates": len(valid), "net_excess": float(net.mean())})
    primary = summary.loc[(summary["signal"] == "reversal") & (summary["variant"] == "buyable_cash") & (summary["horizon"] == config.H)]
    yearly = annual.loc[(annual["signal"] == "reversal") & (annual["variant"] == "buyable_cash") & (annual["horizon"] == config.H)]
    target = observations.loc[(observations["signal"] == "reversal") & observations["entry_observed"]]
    failures = []
    if primary.empty or not np.isfinite(primary.iloc[0]["net_excess"]):
        failures.append("没有可估计的主闸门样本")
    elif primary.iloc[0]["net_excess"] <= 0:
        failures.append("默认成本下 5 日平均净超额不大于 0")
    positive_years = int((yearly["gross_excess"] > 0).sum())
    if len(yearly) >= 3 and positive_years <= 2:
        failures.append("原始超额至多只有 1–2 年为正，其余非正")
    down_ratio = float(target["entry_limit_down"].mean())
    if down_ratio > config.max_limit_down_ratio:
        failures.append("次日开盘跌停不可买比例超过 20%")
    gate = {
        "passed": not failures, "failures": failures,
        "primary_signal": "reversal", "primary_variant": "buyable_cash", "horizon": config.H,
        "one_way_cost": config.one_way_cost, "limit_down_ratio": down_ratio,
        "positive_gross_years": positive_years,
        "primary_net_excess": None if primary.empty else float(primary.iloc[0]["net_excess"]),
        "stage2_benchmark_gate": "阶段 1 通过后才检验，阶段 1 不能宣称已通过净值基准判据",
    }
    return {"pools": pools, "observations": observations, "market": market, "daily": daily,
            "summary": summary, "annual": annual, "paired": pd.DataFrame(comparisons),
            "summary_regime": summarize(daily.loc[daily["regime_on"]], config),
            "annual_regime": summarize(daily.loc[daily["regime_on"]], config, annual=True),
            "availability": pd.DataFrame(availability), "sensitivity": pd.DataFrame(sensitivity), "gate": gate}


def main():
    parser = argparse.ArgumentParser(description="STRATEGY.md 阶段 1 信号体检；不使用回测账户")
    parser.add_argument("--data", default="feathers")
    parser.add_argument("--output", default="research/results/reversal_default")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("输出目录已存在，请指定新目录以保留原始实验")
    config = SignalConfig()
    frames = {name: pd.read_feather(Path(args.data) / f"{name}.feather").set_index("date") for name in ("open", "close", "amt", "cumadj")}
    print("按事前固定口径运行独立信号体检；未调用回测引擎。", flush=True)
    result = analyze(frames, config)
    output.mkdir(parents=True)
    result["pools"].to_feather(output / "pool_members.feather")
    result["observations"].to_feather(output / "signal_observations.feather")
    result["market"].to_csv(output / "market_proxy.csv", index_label="date")
    for name in ("daily", "summary", "annual", "paired", "availability", "sensitivity", "summary_regime", "annual_regime"):
        result[name].to_csv(output / f"{name}.csv", index=False)
    (output / "gate.json").write_text(json.dumps(result["gate"], ensure_ascii=False, indent=2, allow_nan=False))
    manifest = {"config": asdict(config), "inputs": {}, "source": {}}
    for name in ("open", "close", "amt", "cumadj"):
        path = Path(args.data) / f"{name}.feather"
        manifest["inputs"][name] = {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    for path in (Path(__file__), Path("research/PROTOCOL.md"), Path("backtest/market_rules.py"), Path("STRATEGY.md")):
        manifest["source"][str(path.name)] = hashlib.sha256(path.read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(result["gate"], ensure_ascii=False, indent=2, allow_nan=False), flush=True)
    print(result["summary"].loc[(result["summary"]["signal"] == "reversal") & (result["summary"]["variant"] == "buyable_cash")].to_string(index=False), flush=True)
    print("中间输出：", output.resolve(), flush=True)


if __name__ == "__main__":
    main()
