"""按 STRATEGY.md 使用的研究近似；所有门禁只读取开盘及之前信息。"""

import numpy as np
import pandas as pd


def stamp_tax(date):
    """样本期卖出印花税；日期按实际成交日。"""
    return 0.001 if pd.Timestamp(date) < pd.Timestamp("2023-08-28") else 0.0005


def limit_rates(symbols):
    """无 ST 字段，300/301/688 近似 20%，其余近似 10%。"""
    return np.array([0.20 if str(s).startswith(("300", "301", "688")) else 0.10 for s in symbols])


def standard_limit_rates(symbols, date, is_st=None):
    """本轮2023年后区间：科创/创业20%，主板10%，主板ST历史分段。

    沪深2026年交易规则自2026-07-06起将主板ST涨跌幅从5%调整为10%。
    689前缀为科创板存托凭证。旧反转实验仍使用 limit_rates，保持回放。
    """
    rates = np.array([.2 if str(s).startswith(("300", "301", "302", "688", "689")) else .1 for s in symbols])
    if is_st is not None and pd.Timestamp(date) < pd.Timestamp("2026-07-06"):
        rates = np.where((np.asarray(is_st) == 1) & (rates == .1), .05, rates)
    return rates


def opening_limits(open_price, previous_close, symbols):
    """返回跌停、涨停、有效前收掩码；严格按规格方向，不能交换。"""
    open_price = np.asarray(open_price, dtype=float)
    previous_close = np.asarray(previous_close, dtype=float)
    valid = np.isfinite(previous_close) & (previous_close > 0)
    valid_open = np.isfinite(open_price) & (open_price > 0)
    rates = limit_rates(symbols)
    # 这里只加浮点误差容差，不假装拥有交易所官方涨跌停价或除权参考价。
    tolerance = np.abs(previous_close) * 1e-12
    down = valid & valid_open & (open_price <= previous_close * (1 - rates) + tolerance)
    up = valid & valid_open & (open_price >= previous_close * (1 + rates) - tolerance)
    return down, up, valid
