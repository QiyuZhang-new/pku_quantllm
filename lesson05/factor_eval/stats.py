"""IC、保持交易日间隔的 HAC 均值检验、批量 FDR。"""

from math import erfc, sqrt

import numpy as np
import pandas as pd


def row_correlation(x, y, min_pairs=2, rank=False):
    matched = x.notna() & y.notna()
    x, y = x.where(matched), y.where(matched)
    if rank:
        x, y = x.rank(axis=1), y.rank(axis=1)
    dx = x.sub(x.mean(axis=1), axis=0)
    dy = y.sub(y.mean(axis=1), axis=0)
    den = ((dx * dx).sum(axis=1) * (dy * dy).sum(axis=1)).pow(0.5)
    result = (dx * dy).sum(axis=1).div(den.where(den.gt(0)))
    return result.where(matched.sum(axis=1).ge(min_pairs))


def hac_mean(series, lags=4):
    """截距模型、Bartlett 核、无小样本修正；缺失日不压缩为相邻日。"""
    values = np.asarray(series, dtype=float)
    finite = np.isfinite(values)
    n = int(finite.sum())
    out = {"n": n, "mean": None, "std": None, "ir": None,
           "hac_t": None, "p_value": None, "ci_low": None, "ci_high": None,
           "positive_ratio": None, "hac_lags": min(lags, max(len(values) - 1, 0))}
    if n == 0:
        return out
    mean = float(values[finite].mean())
    out.update(mean=mean, positive_ratio=float((values[finite] > 0).mean()))
    if n < 3:
        return out
    std = float(values[finite].std(ddof=1))
    out["std"] = std
    out["ir"] = mean / std if std > 1e-14 else None
    centered = np.where(finite, values - mean, 0.)
    meat = float(centered @ centered)
    for lag in range(1, out["hac_lags"] + 1):
        meat += 2 * (1 - lag / (out["hac_lags"] + 1)) * float(centered[lag:] @ centered[:-lag])
    variance = max(meat, 0) / n**2
    if variance <= 1e-24:
        # 常数序列不制造无穷 t 值或虚假的确定性。
        return out
    se = sqrt(variance)
    t = mean / se
    out.update(hac_t=t, p_value=erfc(abs(t) / sqrt(2)),
               ci_low=mean - 1.96 * se, ci_high=mean + 1.96 * se)
    return out


def benjamini_hochberg(p_values):
    """无效候选按 p=1 纳入检验家族，避免缩小校正数量。"""
    values = np.array([1. if p is None or not np.isfinite(p) else p for p in p_values])
    if not len(values):
        return []
    order = np.argsort(values)
    adjusted = values[order] * len(values) / np.arange(1, len(values) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1].clip(0, 1)
    result = np.empty(len(values))
    result[order] = adjusted
    return result.tolist()
