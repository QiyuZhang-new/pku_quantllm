"""横截面因子评价。先冻结方向，再分别检查验证集和测试集。"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .data import forward_returns, universe, validate_frame
from .stats import benjamini_hochberg, hac_mean, row_correlation


@dataclass(frozen=True)
class Config:
    horizon: int = 5
    groups: int = 5
    min_pairs: int = 100
    min_history: int = 120
    min_adv: float = 1e8
    top_n: int = 1000
    train_end: str | None = None
    valid_end: str | None = None
    min_days: int = 126
    min_factor_coverage: float = 0.8
    min_label_coverage: float = 0.98
    min_valid_ratio: float = 0.8
    min_rank_ic: float = 0.02
    min_hac_t: float = 2.0
    max_fdr: float = 0.05
    min_monotonicity: float = 0.6
    min_month_positive: float = 0.55
    min_offset_positive: float = 0.6
    one_way_bps: float = 10.
    hac_lags: int = 10

    def __post_init__(self):
        integers = ("horizon", "groups", "min_pairs", "min_history", "top_n", "min_days", "hac_lags")
        if any(type(getattr(self, n)) is not int for n in integers):
            raise ValueError("窗口、组数、样本数与 HAC 滞后数必须为整数")
        if self.horizon < 1 or self.groups < 2 or self.min_pairs < self.groups or self.min_days < 3:
            raise ValueError("horizon>=1、groups>=2、min_pairs>=groups、min_days>=3")
        if self.min_history < 1 or self.top_n < 0 or self.hac_lags < self.horizon - 1:
            raise ValueError("历史长度须为正、top_n>=0，HAC 至少覆盖 horizon-1 阶重叠")
        if not np.isfinite([self.min_adv, self.one_way_bps, self.min_rank_ic, self.min_hac_t]).all():
            raise ValueError("阈值必须有限")
        if self.min_adv < 0 or self.one_way_bps < 0 or self.min_hac_t <= 0 or self.min_rank_ic < 0:
            raise ValueError("成交额、成本、IC 阈值不可为负，t 阈值必须为正")
        for name in ("min_factor_coverage", "min_label_coverage", "min_valid_ratio", "max_fdr",
                     "min_month_positive", "min_offset_positive", "min_monotonicity"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} 必须位于 [0,1]")
        if bool(self.train_end) != bool(self.valid_end):
            raise ValueError("train_end 与 valid_end 必须同时指定")


@dataclass
class Evaluation:
    name: str
    summary: dict
    daily: pd.DataFrame
    monthly: pd.DataFrame
    yearly: pd.DataFrame
    offsets: pd.DataFrame


def split_dates(dates, cfg):
    if len(dates) < 30:
        raise ValueError("至少需要 30 个交易日")
    if cfg.train_end:
        train_end, valid_end = pd.Timestamp(cfg.train_end), pd.Timestamp(cfg.valid_end)
    else:
        train_end = dates[int(len(dates) * .5) - 1]
        valid_end = dates[int(len(dates) * .75) - 1]
    if not dates[0] <= train_end < valid_end < dates[-1]:
        raise ValueError("切分边界必须在数据区间内，且 train_end < valid_end < 数据末日")
    # 标签的退出开盘时点必须落在所属区间内。
    exit_dates = pd.Series(dates, index=dates).shift(-(cfg.horizon + 1))
    split = pd.Series("purged", index=dates, name="split")
    split.loc[(dates <= train_end) & exit_dates.le(train_end)] = "train"
    split.loc[(dates > train_end) & (dates <= valid_end) & exit_dates.le(valid_end)] = "validation"
    split.loc[(dates > valid_end) & exit_dates.notna()] = "test"
    return split, {"train_end": str(train_end.date()), "valid_end": str(valid_end.date()),
                   "purged_rows": int(split.eq("purged").sum())}


def group_diagnostics(score, labels, cfg):
    # 先用信号时点的全部有效因子分组，再查看未来收益；缺收益不重排、不补仓。
    ranks = score.rank(axis=1, pct=True, method="average")
    groups = np.ceil(ranks * cfg.groups)
    usable = score.notna().sum(axis=1).ge(cfg.min_pairs) & score.nunique(axis=1).ge(cfg.groups)
    groups = groups.where(usable, axis=0)
    result = pd.DataFrame(index=score.index)
    for k in range(1, cfg.groups + 1):
        members = groups.eq(k)
        expected = members.sum(axis=1)
        observed = (members & labels.notna()).sum(axis=1)
        # 计划等权，只有整组结果完整时才发布收益，不能自动剔除缺失股票。
        result[f"q{k}"] = labels.where(members).sum(axis=1, min_count=1).div(expected).where(
            observed.eq(expected) & expected.gt(0))
        result[f"q{k}_count"] = expected
        result[f"q{k}_coverage"] = observed.div(expected.where(expected.gt(0)))
    members = score.notna()
    expected = members.sum(axis=1)
    observed = (members & labels.notna()).sum(axis=1)
    result["benchmark"] = labels.where(members).sum(axis=1, min_count=1).div(expected).where(
        observed.eq(expected) & expected.ge(cfg.min_pairs) & usable)
    result["long_short_gross"] = result[f"q{cfg.groups}"] - result.q1
    result["long_excess_gross"] = result[f"q{cfg.groups}"] - result.benchmark
    cost = cfg.one_way_bps / 10000
    # 每期独立建仓且全部平仓，不靠低估换手成本制造盈利。
    result["long_net"] = result[f"q{cfg.groups}"] - 2 * cost
    result["long_excess_net"] = result.long_excess_gross - 2 * cost
    result["long_short_net"] = result.long_short_gross - 4 * cost
    result["long_excess_stress"] = result.long_excess_gross - 4 * cost
    result["long_short_stress"] = result.long_short_gross - 8 * cost
    top = groups.eq(cfg.groups)
    prior = top.shift(cfg.horizon, fill_value=False)
    previous_count = prior.sum(axis=1)
    result["top_name_turnover"] = (1 - (top & prior).sum(axis=1).div(previous_count)).where(
        previous_count.gt(0) & top.sum(axis=1).gt(0))
    return result


def _value(series, operation="mean"):
    value = getattr(series, operation)()
    return float(value) if pd.notna(value) and np.isfinite(value) else None


def _gt(value, threshold):
    return value is not None and np.isfinite(value) and value >= threshold - 1e-12


def _positive(value):
    return value is not None and np.isfinite(value) and value > 0


def period_summary(frame, cfg):
    ic = hac_mean(frame.rank_ic, cfg.hac_lags)
    qmeans = pd.Series([frame[f"q{k}"].mean() for k in range(1, cfg.groups + 1)])
    # 仅用所有组结果齐全的共同日期比较，避免不同日期组成制造单调性。
    common = frame[[f"q{k}" for k in range(1, cfg.groups + 1)]].dropna()
    if len(common):
        qmeans = common.mean()
    else:
        qmeans[:] = np.nan
    if qmeans.notna().all() and qmeans.nunique() > 1:
        monotonicity = float(qmeans.rank().corr(pd.Series(np.arange(cfg.groups), index=qmeans.index)))
    else:
        monotonicity = None
    monthly = frame.rank_ic.resample("ME").agg(["mean", "count"])
    monthly = monthly.loc[monthly["count"].ge(10)]
    active = frame.pool_count.ge(cfg.min_pairs)
    out = {
        "signal_days": len(frame), "active_days": int(active.sum()),
        "ic": ic, "pearson_ic": hac_mean(frame.ic, cfg.hac_lags),
        "factor_coverage": _value(frame.loc[active, "factor_coverage"]),
        "label_coverage": _value(frame.loc[active, "label_coverage"]),
        "ic_valid_ratio": float(frame.loc[active, "rank_ic"].notna().mean()) if active.any() else None,
        "portfolio_valid_ratio": float(frame.loc[active, "long_excess_net"].notna().mean()) if active.any() else None,
        "median_pairs": _value(frame.pairs, "median"),
        "monotonicity": monotonicity,
        "group_means_5dr": [float(v) if pd.notna(v) else None for v in qmeans],
        "positive_month_ratio": float(monthly["mean"].gt(0).mean()) if len(monthly) else None,
        "eligible_months": len(monthly),
        "rank_autocorrelation": _value(frame.rank_autocorrelation),
        "top_name_turnover": _value(frame.top_name_turnover),
    }
    for name in ("long_short_gross", "long_excess_gross", "long_net", "long_excess_net",
                 "long_short_net", "long_excess_stress", "long_short_stress"):
        out[name] = hac_mean(frame[name], cfg.hac_lags)
    # 多空价差以每腿 1 元计，不能称为全额资金收益或复利净值。
    out["long_short_linear_annual"] = (out["long_short_net"]["mean"] * 252 / cfg.horizon
                                       if out["long_short_net"]["mean"] is not None else None)
    out["break_even_one_way_bps"] = (out["long_short_gross"]["mean"] / 4 * 10000
                                      if out["long_short_gross"]["mean"] is not None else None)
    return out


def offset_diagnostics(daily, cfg):
    rows = []
    for split in ("train", "validation", "test"):
        for offset in range(cfg.horizon):
            # 用完整行情行号固定锚点；不得先 dropna 再每五行取样。
            sample = daily.loc[daily.split.eq(split) & daily.calendar_row.mod(cfg.horizon).eq(offset)]
            for metric in ("long_excess_net", "long_short_net"):
                stats = hac_mean(sample[metric], max(0, cfg.hac_lags // cfg.horizon))
                rows.append({"split": split, "offset": offset, "metric": metric, **stats})
    return pd.DataFrame(rows)


def evaluate_one(name, factor, labels, pool, split, boundaries, cfg):
    validate_frame(factor, name)
    if not factor.index.equals(labels.index) or not factor.columns.equals(labels.columns):
        raise ValueError(f"{name}: 因子必须与行情严格对齐；请显式 reindex 后再传入")
    raw = factor.where(pool)
    raw_ic = row_correlation(raw, labels.where(pool), cfg.min_pairs, rank=True)
    train = raw_ic.loc[split.eq("train")].dropna()
    direction = -1 if len(train) and train.mean() < 0 else 1
    score = raw * direction
    daily = group_diagnostics(score, labels, cfg)
    daily["ic"] = row_correlation(score, labels.where(pool), cfg.min_pairs)
    daily["rank_ic"] = raw_ic * direction
    daily["raw_rank_ic"] = raw_ic
    daily["pool_count"] = pool.sum(axis=1)
    daily["factor_count"] = score.notna().sum(axis=1)
    daily["pairs"] = (score.notna() & labels.notna()).sum(axis=1)
    daily["factor_coverage"] = daily.factor_count.div(daily.pool_count.where(daily.pool_count.gt(0)))
    daily["label_coverage"] = daily.pairs.div(daily.factor_count.where(daily.factor_count.gt(0)))
    daily["rank_autocorrelation"] = row_correlation(score, score.shift(cfg.horizon), cfg.min_pairs, rank=True)
    daily["split"] = split
    daily["calendar_row"] = np.arange(len(daily))
    daily.index.name = "date"
    offsets = offset_diagnostics(daily, cfg)
    periods = {s: period_summary(daily.loc[split.eq(s)], cfg) for s in ("train", "validation", "test")}
    for s, stats in periods.items():
        o = offsets.loc[offsets.split.eq(s) & offsets.metric.eq("long_excess_net")]
        qualified = o["n"].ge(12) & o["mean"].notna()
        # 未达到最小样本的相位算不通过，不能靠丢掉坏相位提高比例。
        stats["positive_offset_ratio"] = float((qualified & o["mean"].gt(0)).sum() / cfg.horizon)
        stats["qualified_offsets"] = int(qualified.sum())
    tables = []
    for frequency in ("ME", "YE"):
        rows = []
        for s in periods:
            for period, frame in daily.loc[split.eq(s)].resample(frequency):
                if frame.empty:
                    continue
                rows.append({"split": s, "period": str(period.date()),
                             "n": int(frame.rank_ic.notna().sum()), "rank_ic": _value(frame.rank_ic),
                             "long_excess_net": _value(frame.long_excess_net),
                             "long_short_net": _value(frame.long_short_net)})
        tables.append(pd.DataFrame(rows))
    summary = {"factor": name, "direction": direction,
               "direction_source": "train mean raw RankIC only",
               "direction_train_ic": _value(train), "boundaries": boundaries,
               "config": asdict(cfg), "periods": periods}
    return Evaluation(name, summary, daily, tables[0], tables[1], offsets)


def judge(summary, cfg):
    """分数用于定位问题；硬门槛决定结论，不能用加权分掩盖失败。"""
    p = summary["periods"]
    gates = []

    def gate(category, period, metric, actual, threshold, passed, explanation):
        gates.append({"category": category, "period": period, "metric": metric,
                      "actual": actual, "threshold": threshold, "passed": bool(passed),
                      "explanation": explanation})

    gate("data", "train", "direction_days", p["train"]["ic"]["n"], cfg.min_days,
         p["train"]["ic"]["n"] >= cfg.min_days, "训练期有效日期不足时，方向不可依赖")
    for s in ("validation", "test"):
        stats = p[s]
        for metric, value, threshold, explanation in (
            ("valid_days", stats["ic"]["n"], cfg.min_days, "至少半个交易年的有效 IC"),
            ("factor_coverage", stats["factor_coverage"], cfg.min_factor_coverage, "信号时点股票池内的因子覆盖"),
            ("label_coverage", stats["label_coverage"], cfg.min_label_coverage, "未来标签缺失会造成选择偏差"),
            ("ic_valid_ratio", stats["ic_valid_ratio"], cfg.min_valid_ratio, "常数或退化横截面不提供排序证据"),
            ("portfolio_valid_ratio", stats["portfolio_valid_ratio"], cfg.min_valid_ratio, "组合缺失收益不得重新分配权重"),
        ):
            gate("data", s, metric, value, threshold, _gt(value, threshold), explanation)
        for metric, value, threshold, explanation in (
            ("rank_ic", stats["ic"]["mean"], cfg.min_rank_ic, "方向冻结后的预测强度"),
            ("hac_t", stats["ic"]["hac_t"], cfg.min_hac_t, "考虑五日重叠的 HAC 显著性"),
        ):
            gate("prediction", s, metric, value, threshold, _gt(value, threshold), explanation)
        q = stats["ic"]["fdr_q"]
        gate("prediction", s, "fdr_q", q, cfg.max_fdr, q <= cfg.max_fdr, "本次批量候选的 BH 校正")
        for metric in ("long_excess_net", "long_short_net"):
            value = stats[metric]["mean"]
            gate("returns", s, metric, value, ">0", _positive(value),
                 "每期完整往返成本；多头须战胜同池未扣费等权基准，多空须有正价差")
        for metric, threshold in (("monotonicity", cfg.min_monotonicity),
                                  ("positive_month_ratio", cfg.min_month_positive),
                                  ("positive_offset_ratio", cfg.min_offset_positive)):
            value = stats[metric]
            gate("stability", s, metric, value, threshold, _gt(value, threshold),
                 "分层、跨月与五个固定非重叠起点的方向一致性")
    by_category = {c: [g for g in gates if g["category"] == c]
                   for c in ("data", "prediction", "returns", "stability")}
    scores = {c: round(weight * sum(g["passed"] for g in group) / len(group), 1)
              for (c, group), weight in zip(by_category.items(), (10, 40, 30, 20))}
    data_ok = all(g["passed"] for g in by_category["data"])
    all_ok = all(g["passed"] for g in gates)
    validation_ok = all(g["passed"] for g in gates if g["period"] != "test")
    test_positive = _positive(p["test"]["ic"]["mean"])
    if not data_ok:
        verdict = "证据不足"
    elif all_ok:
        verdict = "好因子（当前研究样本通过）"
    elif validation_ok and test_positive:
        verdict = "有潜力，但样本外未完全通过"
    else:
        verdict = "不合格（当前定义与样本）"
    summary.update(verdict=verdict, score=round(sum(scores.values()), 1), score_components=scores,
                   validation_approved=validation_ok, gates=gates,
                   failed_gates=[g for g in gates if not g["passed"]],
                   cost_stress_passed=all(_positive(p[s]["long_excess_stress"]["mean"]) and
                                          _positive(p[s]["long_short_stress"]["mean"])
                                          for s in ("validation", "test")))


def evaluate_batch(market, factors, cfg=None):
    cfg = cfg or Config()
    if not factors:
        raise ValueError("至少提供一个因子")
    labels = forward_returns(market, cfg.horizon)
    pool = universe(market, cfg.min_history, cfg.min_adv, cfg.top_n)
    split, boundaries = split_dates(market.dates, cfg)
    results = [evaluate_one(name, factor, labels, pool, split, boundaries, cfg)
               for name, factor in factors.items()]
    for s in ("train", "validation", "test"):
        qs = benjamini_hochberg([r.summary["periods"][s]["ic"]["p_value"] for r in results])
        for result, q in zip(results, qs):
            result.summary["periods"][s]["ic"]["fdr_q"] = q
    for result in results:
        judge(result.summary, cfg)
    return results
