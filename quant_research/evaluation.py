"""复用第五讲统计及第四讲账户；开发阶段不会构造留出期标签。"""

from dataclasses import asdict

import numpy as np
import pandas as pd

from lesson04.backtest import BacktestConfig, BacktestEngine, MarketData
from lesson05.factor_eval.core import evaluate_one, judge
from lesson05.factor_eval.data import forward_returns, universe
from lesson05.factor_eval.stats import benjamini_hochberg


def development_batch(market, factors, cfg, family_size):
    """本轮预先登记的失败候选也以 p=1 计入批内校正；不声称跨轮FDR控制。"""
    dates = market.dates
    train_end, valid_end = pd.Timestamp(cfg.train_end), pd.Timestamp(cfg.valid_end)
    if dates[-1] > valid_end or dates[0] >= train_end or not (dates > train_end).any():
        raise ValueError("开发输入必须截断到 valid_end，且训练与验证区间都有数据")
    exits = pd.Series(dates, index=dates).shift(-(cfg.horizon + 1))
    split = pd.Series("purged", index=dates, name="split")
    split.loc[(dates <= train_end) & exits.le(train_end)] = "train"
    split.loc[(dates > train_end) & exits.notna()] = "validation"
    boundaries = {"train_end": cfg.train_end, "valid_end": cfg.valid_end,
                  "purged_rows": int(split.eq("purged").sum())}
    labels = forward_returns(market, cfg.horizon)
    pool = universe(market, cfg.min_history, cfg.min_adv, cfg.top_n)
    results = {key: evaluate_one(key, factor, labels, pool, split, boundaries, cfg)
               for key, factor in factors.items()}
    for period in ("train", "validation"):
        p_values = [r.summary["periods"][period]["ic"]["p_value"] for r in results.values()]
        qs = benjamini_hochberg(p_values + [None] * (family_size - len(p_values)))
        for result, q in zip(results.values(), qs):
            result.summary["periods"][period]["ic"]["fdr_q"] = float(q)
    for result in results.values():
        # 旧判定器的test分支仅接收空区间；随后只导出开发门槛，避免误称样本外通过。
        result.summary["periods"]["test"]["ic"]["fdr_q"] = 1.
        judge(result.summary, cfg)
        gates = [g for g in result.summary["gates"] if g["period"] != "test"]
        result.summary = {"direction": result.summary["direction"],
                          "config": asdict(cfg), "boundaries": boundaries,
                          "periods": {s: result.summary["periods"][s] for s in ("train", "validation")},
                          "gates": gates, "passed": all(g["passed"] for g in gates),
                          "fdr_scope": "本轮固定候选批次；不控制自适应搜索整体错误率"}
    return results, pool


class FactorStrategy:
    """读取同一份因子与股票池；高分组等权，按评价horizon调仓。"""

    def __init__(self, score, pool, cfg, last_signal, equal_weight=False):
        self.score, self.pool, self.cfg = score, pool, cfg
        self.last_signal, self.equal_weight = last_signal, equal_weight
        self.calls = 0

    def on_close(self, context):
        rebalance = self.calls % self.cfg.horizon == 0
        self.calls += 1
        if not rebalance or context.date > self.last_signal:
            return None
        row = self.score.loc[context.date].where(self.pool.loc[context.date]).dropna()
        if len(row) < self.cfg.min_pairs or row.nunique() < self.cfg.groups:
            return pd.Series(dtype=float)
        if not self.equal_weight:
            ranks = row.rank(pct=True, method="average")
            row = row.loc[np.ceil(ranks * self.cfg.groups).eq(self.cfg.groups)]
        return pd.Series(1 / len(row), index=row.index) if len(row) else pd.Series(dtype=float)


def account_validation(market, factor, pool, direction, cfg, execution, start_signal=None):
    dates = market.dates
    signal_dates = dates[dates > pd.Timestamp(cfg.train_end)] if start_signal is None else dates[dates >= pd.Timestamp(start_signal)]
    if not len(signal_dates):
        raise ValueError("账户验证缺少信号日期")
    first = dates.get_loc(signal_dates[0])
    if len(dates) - first <= cfg.horizon + 1:
        raise ValueError("账户验证区间不足一个完整持有期")
    last_signal = dates[-cfg.horizon - 2]
    config = BacktestConfig(initial_cash=execution.initial_cash, commission=execution.commission,
        slippage=execution.slippage, sell_tax=execution.sell_tax, lot_size=execution.lot_size,
        start=str(dates[first + 1].date()), end=str(dates[-1].date()),
        execution_policy="standard", max_participation=execution.max_participation,
        prestart_signal=True, liquidate_on_last_day=True, exit_st_on_open=True,
        board_lots=True, record_positions=True)
    data = MarketData(market.frames)
    score = factor * direction
    strategy = BacktestEngine(data, config).run(FactorStrategy(score, pool, cfg, last_signal))
    baseline = BacktestEngine(data, config).run(FactorStrategy(score, pool, cfg, last_signal, equal_weight=True))
    excess = strategy.metrics["total_return"] - baseline.metrics["total_return"]
    missing = sorted({"is_st", "trade_status"} - market.frames.keys())
    gates = {
        "historical_status_available": not missing or not execution.require_status,
        "has_trades": len(strategy.trades) > 0,
        "has_baseline_trades": len(baseline.trades) > 0,
        "positive_total_return": strategy.metrics["total_return"] > execution.min_total_return,
        "positive_executable_excess": excess > execution.min_excess_return,
        "drawdown_within_budget": -strategy.metrics["max_drawdown"] <= execution.max_drawdown,
        "no_stale_valuations": strategy.metrics["stale_position_days"] == 0 and baseline.metrics["stale_position_days"] == 0,
        "liquidated": int(strategy.equity.iloc[-1].positions) == 0 and int(baseline.equity.iloc[-1].positions) == 0,
    }
    summary = {"passed": all(gates.values()), "gates": gates, "missing_status": missing,
               "excess_total_return": excess, "strategy": strategy.metrics, "baseline": baseline.metrics,
               "execution_config": asdict(config),
               "comparison": "同因子可用股票池、同费用和约束的等权账户；完整资金路径，非标签均值"}
    return summary, strategy, baseline


def save_account(result, directory):
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("equity", "trades", "positions", "rejected_orders"):
        getattr(result, name).to_csv(directory / f"{name}.csv", index=(name == "equity"))


def feedback_for(folds):
    failures = []
    for fold in folds:
        failures.extend(g["metric"] for g in fold["evaluation"]["gates"] if not g["passed"])
        failures.extend(k for k, passed in fold["account"]["gates"].items() if not passed)
    if not failures:
        reason = "开发窗口通过；下一步检验相似性及冻结后的历史审核"
    elif any("coverage" in name or name == "valid_days" for name in failures):
        reason = "样本或覆盖不足，优先检查字段和历史窗口"
    elif any(name in failures for name in ("long_excess_net", "long_short_net", "positive_executable_excess")):
        reason = "预测排序未稳定转化为扣费后的组合增益"
    else:
        reason = "预测强度、时间稳定性或交易风险未通过"
    value = folds[-1]["evaluation"]["periods"]["validation"]["long_excess_net"]["mean"]
    return {"reason": reason, "failed_metrics": sorted(set(failures)),
            "development_score": value, "passed": not failures,
            "evidence_level": "反复探索的开发证据，不能作为独立样本外证明"}
