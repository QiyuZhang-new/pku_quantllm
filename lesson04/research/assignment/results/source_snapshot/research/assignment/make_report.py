"""只从已保存的实验与独立审计结果生成新作业报告。"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .run import BASE, DATA, digest


NAMES = {"lowvol60": "60日低波动等权选股", "momentum120": "中期动量等权选股",
         "lowvol_momentum": "低波动与中期动量等权打分选股"}


def pct(number):
    return f"{number:.2%}"


def table(headers, rows):
    return "\n".join(["| " + " | ".join(map(str, headers)) + " |",
        "| " + " | ".join(["---"]*len(headers)) + " |",
        *["| " + " | ".join(map(str, row)) + " |" for row in rows]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=BASE/"ASSIGNMENT_AUDITED_REPORT.md")
    args = parser.parse_args()
    results = args.results.resolve()
    args.report = args.report.resolve()
    audit = json.loads((results/"audit.json").read_text())
    if not audit["passed"]:
        raise ValueError("独立审计未通过，不生成肯定性报告")
    choice = json.loads((results/"selection.json").read_text())
    method = choice["chosen"]
    metrics = json.loads((results/"selected/metrics.json").read_text())
    config = json.loads((results/"selected/config.json").read_text())
    training = pd.read_csv(results/"training.csv")
    experiments = pd.read_csv(results/"experiments.csv")
    comparison_metrics = pd.read_csv(results/"comparison_metrics.csv").set_index("name")
    comparison = pd.read_csv(results/"comparison.csv", parse_dates=["date"]).set_index("date")
    daily = pd.read_csv(results/"selected/equity.csv", parse_dates=["date"]).set_index("date")
    trades = pd.read_csv(results/"selected/trades.csv", parse_dates=["date", "signal_date"])
    rejects = pd.read_csv(results/"selected/rejected_orders.csv")
    selections = pd.read_csv(results/"selected/selections.csv", parse_dates=["signal_date"])
    coverage = pd.read_csv(results/"status_coverage.csv", parse_dates=["date"]).set_index("date").loc[daily.index]
    download = json.loads((DATA/"status_download_manifest.json").read_text())
    index_manifest = json.loads((DATA/"indices_manifest.json").read_text())
    relative = results.relative_to(BASE).as_posix()
    profitable = metrics["total_return"] > 0
    beats = all(metrics["total_return"] > comparison_metrics.at[b,"total_return"] for b in ["csi300", "sse"])
    liquidated = metrics["end_positions"] == 0
    if profitable and beats and liquidated:
        conclusion = "**这套策略在本次作业区间的回测中挣钱，扣除约定交易费用及基础滑点后，同时跑赢沪深300和上证综指，并在末日清空持仓。**"
    elif profitable and beats:
        conclusion = "**策略在这次回测中盈利且账面跑赢沪深300和上证综指，但末日仍有残留持仓，尚未满足老师的全部要求，不能称为完整合格结果。**"
    elif profitable:
        conclusion = "**策略在这次回测中盈利，但未同时跑赢沪深300和上证综指，不能声称已完成用户要求的策略验证。**"
    else:
        conclusion = "**这套策略在指定区间未能挣钱，应否定本次验证，不以调参掩盖结果。**"
    train_table = table(["训练候选", "累计净收益", "年化净收益", "最大回撤", "净Sharpe", "训练末残留"], [
        [NAMES[r.method], pct(r.total_return), pct(r.annualized_return), pct(r.max_drawdown), f"{r.sharpe_zero_rf:.3f}", r.end_positions]
        for r in training.itertuples()])
    names = {"strategy": NAMES[method], "csi300": "沪深300价格指数", "sse": "上证综指", "equal_weight_liquid": "同流动性股票池等权账户"}
    comparison_table = table(["组合/指数", "累计收益", "年化收益", "最大回撤", "Sharpe（无风险利率0）"], [
        [names[n], pct(r.total_return), pct(r.annualized_return), pct(r.max_drawdown), f"{r.sharpe_zero_rf:.3f}"]
        for n, r in comparison_metrics.loc[["strategy", "csi300", "sse", "equal_weight_liquid"]].iterrows()])
    sensitivity = table(["单边滑点", "累计净收益", "相对沪深300超额（百分点）", "最大回撤", "期末残留证券数"], [
        [label, pct(r.total_return), f"{100*(r.total_return-comparison_metrics.at['csi300','total_return']):+.2f}", pct(r.max_drawdown), r.end_positions]
        for label, r in zip(["5基点（主实验）", "0基点", "15基点"], experiments.iloc[:3].itertuples())])
    annual = []
    annual_frame = []
    for year in daily.index.year.unique():
        mask = comparison.index.year == year
        rows = []
        for column in ["strategy", "csi300", "sse", "equal_weight_liquid"]:
            series = comparison[column]
            returns = series.pct_change(fill_method=None)
            returns.iloc[0] = series.iloc[0]-1
            value = float((1+returns.loc[mask]).prod()-1)
            rows.append(pct(value))
            annual_frame.append({"year": int(year), "portfolio": column, "return": value})
        annual.append(["2026年1—9月" if year == 2026 else str(year), *rows])
    pd.DataFrame(annual_frame).to_csv(results/"annual.csv", index=False)
    annual_table = table(["年份", "策略", "沪深300", "上证综指", "同池等权"], annual)
    ratio = coverage.known_st_status/coverage.priced
    availability = table(["项目", "本次实测"], [
        ["查询证券数", download["symbols"]], ["请求失败/空历史证券", f"{download['failed']} / {download['empty']}"],
        ["区间每日有价证券ST状态覆盖率最小值/均值", f"{pct(ratio.min())} / {pct(ratio.mean())}"],
        ["每日状态未知且有价证券最大数", int(coverage.unknown_st_status.max())],
        ["每日ST有价证券最少/最多", f"{coverage.is_st.min()} / {coverage.is_st.max()}"],
        ["实际买入笔数", int(trades.side.eq("buy").sum())],
        ["ST持仓日累计（证券×日）", metrics["st_position_days"]],
        ["状态未知持仓日累计（证券×日）", metrics["unknown_status_position_days"]]])
    rejection_table = table(["未成交/部分成交原因", "记录条数"], [list(r) for r in rejects.reason.value_counts().items()]) if len(rejects) else "本次主实验没有受阻订单。"
    main_audit = next(r for r in audit["experiments"] if r["experiment"] == "selected")
    fee_comm = float(trades.commission.sum()); fee_tax = float(trades.sell_tax.sum())
    missing_days = int((daily.stale_positions > 0).sum())
    model_rule = {
        "lowvol60": "对每只股票计算最近60日复权日收益的样本标准差，从低到高取100只。这里计算总波动率，没有把减去市场平均收益后的波动率冒称为回归残差/特质波动率。",
        "momentum120": "计算 P(t−20)/P(t−120)−1，从高到低取100只；P为原始收盘价乘cumadj。排除最近20日，避免把短期反转与中期动量混淆。",
        "lowvol_momentum": "分别计算低60日波动和120日至20日前动量的当期池内百分位排名，两项各占50%，按综合分数从高到低取100只。"}[method]
    text = f"""# 全市场选股作业：独立审计回测报告

{conclusion}

最终方案：**{NAMES[method]}**。初始资金10亿元，期末权益 **{metrics['final_equity']/1e8:.4f}亿元**，累计净收益 **{pct(metrics['total_return'])}**。
相对沪深300累计收益高 **{100*(metrics['total_return']-comparison_metrics.at['csi300','total_return']):.2f}个百分点**，
相对上证综指高 **{100*(metrics['total_return']-comparison_metrics.at['sse','total_return']):.2f}个百分点**。
以上只说明这个历史样本的回测结果，不证明未来一定获利或存在稳定选股alpha。

## 1. 作业约束与实际执行

| 老师要求 | 本次实际 |
| --- | --- |
| 非ST全市场选股 | 对课程沪深5451列计算候选信号；按逐日历史ST剔除，未知状态不买，另设上市时间和流动性门槛 |
| 10亿元初始资金 | 1,000,000,000元，无杠杆、无做空 |
| 佣金单边万一 | 买卖实际金额各收0.0001 |
| 印花税万五 | 仅卖出实际金额收0.0005 |
| 2025年第一个交易日建仓 | {daily.index[0].date()}开盘，实际首日买入{metrics['first_day_buys']}笔；信号截至2024-12-31 |
| 2026年9月最后交易日清仓 | {daily.index[-1].date()}开盘尝试清仓；期末残留{metrics['end_positions']}只 |
| 执行改进 | 单边基础滑点0.0005，主板/创业板100份、科创最低200份且1份递增，涨停不买/跌停不卖，历史成交额5%的单笔金额上限 |

本地没有北交所数据，**“全市场”仅指给定沪深面板内选股**，不冒称覆盖所有历史A股。区间共{len(daily)}个交易日。

## 2. 为什么研究这个方向、怎样确定方案

MSCI官方研究将低波动和动量列入常见因子，支持作为研究先验，但并不保证本地样本收益。
本轮运行候选账户前写下 [固定研究规则](research/assignment/PROTOCOL.md)，仅比较3个固定候选，
按2023—2024年的扣费Sharpe最高选定方案，之后在作业窗口不再选择胜者或调参数。
训练阶段也统一使用老师的固定费率，并非复刻2023年印花税制度变化。

{train_table}

选定方案为 `{method}`，具体信号：{model_rule}

全池先剔除历史ST/未知状态、上市有价观测不足120日、信号日无有效原始收盘/复权因子、
过去60日日收益不完整、过去20日无完整正成交额或平均成交额低于2亿元的证券。
持仓等权，每20个交易日收盘更新目标，下一交易日开盘执行；重合持仓只交易权重差额。
没有借助成交日的收盘价或全日成交额决定开盘选股。ST生效日盘前状态仅用于禁止买入及退出。

**样本使用限制：** 目录里已有 `verify/` 研究事先查看过作业区间。本轮虽以2023—2024年选择候选，
也不能把2025—2026年冒称为完全未接触过的样本外检验。低波动方向也不是本轮首次发现。

## 3. 收益、风险与基准

{comparison_table}

![策略与真实指数、同池等权账户净值及回撤]({relative}/comparison.png)

真实指数均按**首日开盘买入、末日开盘退出**计算；中间按当日收盘标记。指数不扣交易费。
沪深300/上证综指若用末日收盘退出，收益分别为{pct(comparison.csi300_last_close.iloc[-1]-1)}和{pct(comparison.sse_last_close.iloc[-1]-1)}，不作为挑选有利结论的替代端点。
指数行情来自腾讯，并与BaoStock逐日核对；各指数核对{index_manifest[0]['crosscheck_rows']}行，最大价格差{index_manifest[0]['crosscheck_max_abs_price_difference']:.4f}点（显示精度差异）。

**口径限制：股票是含分红再投资的复权总收益模型，上述指数是价格指数，分红口径不完全一致。**
因此“跑赢大盘”仅指跑赢这两个明确列出的价格基准；未据此证明对相同总收益指数有alpha。
辅助等权账户使用相同ST/流动性/收益完整性筛选、调仓日期、费用、滑点和成交上限，其期末残留为{int(experiments.iloc[-1].end_positions)}只。
辅助账户如未清仓，其末日是残留持仓市值加现金，不能冒称可变现收益。

{annual_table}

## 4. 换手、成本、容量与不可成交

- 年化双向换手：{metrics['annualized_two_way_turnover']:.2f}倍（买入加卖出金额/开盘权益，日均乘252）。
- 佣金：{fee_comm:,.2f}元；印花税：{fee_tax:,.2f}元；费用合计：{metrics['total_fees']:,.2f}元。
- 滑点成本：{metrics['total_slippage_cost']:,.2f}元，已反映在成交价中，不二次扣除。
- 总费用加滑点占初始资金：{pct(metrics['fees_and_slippage_initial_capital_ratio'])}；不是用利润作分母。
- 涨跌停拒绝{metrics['limit_rejected_orders']}次，占所有取整前意图尝试{pct(metrics['limit_rejected_ratio'])}；停牌尝试比例{pct(metrics['suspended_attempt_ratio'])}。
- 逐笔审计得到最大成交原始金额/成交前20日均成交额={pct(main_audit['max_trade_over_prior_adv'])}；上限为5%。
- 有缺价持仓的交易日{missing_days}日，累计缺价持仓{metrics['stale_position_days']}个证券日；不会把缺价股票从账户里删除。

{rejection_table}

单笔金额上限只是基于历史日均成交额的计划容量约束，**不代表开盘集合竞价有这么多可成交量**。
10亿元下仍可能有冲击、排队或滑点扩大。涨跌停按原始开盘价和BaoStock未复权`preclose`参考值判断，
不能用复权价判限；板块按300/301/302/688/689前缀识别20%，其余按10%，主板ST在2026-07-06前5%、之后10%。
价格取到分，但没有交易所逐日官方限价、特殊恢复上市制度和完整盘口，因此仍是近似。

## 5. 滑点敏感性（佣金、印花税保持老师指定值）

{sensitivity}

只做事先写下的3档滑点，没有在作业区间搜索K、持有期或窗口来选好看的参数。

## 6. ST数据调研与覆盖

使用免费BaoStock `query_history_k_data_plus` 的 `isST` 字段逐日对齐，下载起点2022-01-01，
终点2026-09-30。**不使用当前名称、当前ST名单或5%涨跌幅反推历史ST状态。**
状态未知的证券不能新买；持仓转ST时在生效日开盘尝试卖出，无法成交则保留并报告。

{availability}

例：001270.SZ的缓存与原始公告核对一致：2025-04-23非ST且停牌，2025-04-24起ST；
2026-05-19仍ST且停牌，2026-05-20撤销。交易所[实施风险警示公告](https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-04-23/1fc0c56b-fbc2-43c1-bd4c-aca59b26b088.PDF)
及[撤销风险警示公告](https://disc.static.szse.cn/disc/disk03/finalpage/2026-05-19/7dcd926a-5a98-4b8b-b5cb-185bc679a713.PDF)支持这些生效日。
这个抽样核对不能保证供应商全部历史标记零误差。数据源没有逐条公告发布时间，回测假设生效状态在开盘前可知，
不使用未来摘帽信息提前入池。Tushare也有[逐日ST接口](https://tushare.pro/document/2?doc_id=397)，本机无授权token，未调用该付费权限接口。

## 7. 参数默认值、实际值与依据

| 参数 | 类/配置默认值 | 主实验实际值 | 依据 |
| --- | --- | --- | --- |
| method | lowvol60 | {method} | 3候选中按2023—2024净Sharpe选择 |
| initial_cash | 通用引擎100万 | 10亿元 | 老师给定 |
| commission | 通用引擎0.0003 | 0.0001/边 | 老师给定 |
| sell_tax | 通用引擎历史分段 | 固定0.0005，仅卖出 | 老师给定 |
| start/end | 通用引擎无 | 2025-01-02/2026-09-30 | 老师给定、真实交易日 |
| K | 100 | 100 | 研究者经验选择，用于分散；未优化 |
| holding | 20 | 20交易日 | 研究者取约月度，控制换手；未优化 |
| min_listed | 120 | 120个有价观测 | 研究者用于排除新股；没有真实IPO日期 |
| volatility_window | 60 | 60交易日 | 研究者取约一季度；文献只支持方向、不支持此数最优 |
| momentum_window/skip | 120/20 | 120/20交易日 | 研究者取约半年且剔除近一月；非寻优结果 |
| 组合因子权重 | 0.5/0.5 | 仅组合候选使用0.5/0.5 | 研究者等权，不拟合 |
| min_adv | 2亿元 | 前20日均成交额≥2亿元 | 10亿/100只≈1000万，约为2亿的5%；经验容量门槛 |
| max_participation | 通用引擎无 | 5% | 经验上限，非盘口可成交保证 |
| volume_lookback | 20 | 20交易日 | 与流动性筛选一致，研究者选择 |
| slippage | 0.0005 | 0.0005/边 | 研究者假设；另测0、0.0015 |
| lot_size/board_lots | 100/False | 100/True | 主板/创业板100份，科创最低200份且1份递增；全清合成复权零碎份额 |
| annual_days | 252 | 252 | 年化统计惯例 |
| execution_policy | legacy_reversal | standard | 新作业撤销旧反转方向，改用正常涨跌停门禁 |
| prestart_signal/liquidate_on_last_day | False/False | True/True | 首日建仓、末日退出要求 |
| defer_suspended_orders | True | True | 缺价不虚构成交，新目标覆盖旧目标 |
| exit_st_on_open | False | True | 生效日尝试退出新ST，不能保证跌停时退出 |

## 8. 诚实限制

1. **幸存者/覆盖偏差**：不能证明课程面板涵盖所有历史退市证券。即使发现若干历史退市代码，也不能据此排除幸存者偏差；收益可能偏乐观。北交所不在样本内。
2. **历史ST**来自外部供应商，未知状态明确排除；抽样公告核验不能替代全量人工审计，也没有完整公告发布时间数据。
3. **复权持仓**以cumadj比率调整合成份额，表示总收益/分红再投资，不是券商实际股数与现金分红到账；忽略真实分红到账延迟、税差、配股等。
4. **指数基准**是价格收益，股票为复权总收益；没有行业、市值数据做中性化，可能存在行业/规模风格暴露。指数涨幅以上述缓存真实行情计算，不是合成等权冒充大盘。
5. **执行**缺盘口、开盘量和完整交易制度，主板/创业板100份、科创最低200份及1份递增；合成复权持仓全清仍允许零碎份额。5%历史成交额上限仍可能过于乐观。未另加老师未指定的过户费、最低佣金等。
6. **统计**只有约1.7年作业窗口，存在旧研究对该窗口的查看，不宣称显著alpha或未来必胜。没有挑掉亏损日期、股票或不利实验。
7. **清仓**受阻时保留真实账面持仓。主实验期末残留={metrics['end_positions']}，必须据此判断是否满足作业，不能单凭收益为正打勾。

## 9. 可复现产物与验证

- [研究协议](research/assignment/PROTOCOL.md)、[策略类](backtest/factor_strategy.py)、[运行脚本](research/assignment/run.py)、[独立审计](research/assignment/audit.py)。
- [{relative}/training.csv]({relative}/training.csv)：全部训练候选；[selection.json]({relative}/selection.json)：在运行作业账户前写下的选择。
- [comparison.csv]({relative}/comparison.csv)、[comparison_metrics.csv]({relative}/comparison_metrics.csv)、[annual.csv]({relative}/annual.csv)：图表、基准和年度数据。
- [主实验逐笔成交]({relative}/selected/trades.csv)、[逐日账户]({relative}/selected/equity.csv)、[持仓]({relative}/selected/positions.csv)、[选股]({relative}/selected/selections.csv)、[未成交]({relative}/selected/rejected_orders.csv)。
- [审计结果]({relative}/audit.json)：独立重新建账、历史非ST、T+1、容量、费用、原始开盘价和数据/代码哈希，**{audit['checks']:,}项核验通过**。
- [数据与源码清单]({relative}/manifest.json)、[逐证券状态文件哈希]({relative}/status_file_hashes.csv)、[状态覆盖]({relative}/status_coverage.csv)。
- 单元测试命令：`MPLCONFIGDIR=/tmp/quantllm-lesson04-matplotlib uv run python -m unittest discover -s tests -v`，最近执行日志见 [research/assignment/tests.log](research/assignment/tests.log)。

```bash
uv sync --locked
uv run python -m research.assignment.fetch_data --workers 4
uv run python -m research.assignment.fetch_indices
uv run python -m research.assignment.data_quality
MPLCONFIGDIR=/tmp/quantllm-lesson04-matplotlib uv run python -m research.assignment.run --output research/assignment/results_reproduction
uv run python -m research.assignment.audit --results research/assignment/results_reproduction
uv run python -m research.assignment.make_report --results research/assignment/results_reproduction --report ASSIGNMENT_REPRODUCTION_REPORT.md
```

## 10. 调研来源

- [MSCI：因子投资与常见因子](https://www.msci.com/research-and-insights/blog-post/what-is-factor-investing)。
- [MSCI：低波动与市场周期](https://www.msci.com/research-and-insights/paper/low-volatility-over-the-market-cycle-understanding-factor-investing)。
- [BaoStock官方平台](https://www.baostock.com/)；查询方法和实际字段见下载脚本及缓存，不需要账号密钥。
- [Tushare逐日ST接口说明](https://tushare.pro/document/2?doc_id=397)。
- [上交所2026年交易规则修订说明](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml)及[2026-07-06正式生效通知](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)。
- [深交所风险警示板规则配套通知](https://www.szse.cn/lawrules/service/member/t20260630_621404.html)。
- [上交所科创板交易数量与涨跌幅投教说明](https://edu.sse.com.cn/tib/)。

生成时间：{datetime.now(timezone.utc).isoformat()}。旧 `ASSIGNMENT_REPORT.md`、`STRATEGY_ASSIGNMENT.md` 和 `verify/` 保留作历史材料，其数字未直接移植为本轮结论。
"""
    args.report.write_text(text, encoding="utf-8")
    (results/"report_manifest.json").write_text(json.dumps({"report": str(args.report.relative_to(BASE)),
        "sha256": digest(args.report), "generator_sha256": digest(Path(__file__)),
        "independent_audit_sha256": digest(results/"audit.json")}, ensure_ascii=False, indent=2))
    print(args.report, conclusion, flush=True)


if __name__ == "__main__":
    main()
