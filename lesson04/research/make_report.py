"""仅从已保存的阶段 1 输出生成报告和标准绘图，不重跑或调优策略。"""

import argparse
import hashlib
import json
from pathlib import Path
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import numpy as np
import pandas as pd


def pct(value):
    return "—" if pd.isna(value) else f"{value:.4%}"


def table(frame, columns, percentages=()):
    lines = ["| " + " | ".join(columns.values()) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for _, row in frame.iterrows():
        values = []
        for name in columns:
            value = row[name]
            if name in percentages:
                value = pct(value)
            elif isinstance(value, (float, np.floating)) and value.is_integer():
                value = str(int(value))
            values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def subset(frame, signal="reversal", variant="buyable_cash", horizon=None):
    mask = frame["signal"].eq(signal) & frame["variant"].eq(variant)
    if horizon is not None:
        mask &= frame["horizon"].eq(horizon)
    return frame.loc[mask].sort_values("year" if "year" in frame else "horizon")


def draw_figures(directory, summary, annual, sensitivity):
    plt.rcParams.update({"svg.fonttype": "none", "axes.unicode_minus": False})
    colors = ("#087f8c", "#5876a3", "#d27859")
    filenames = []
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    for index, variant in enumerate(("close_close", "open_open", "buyable_cash")):
        data = subset(summary, variant=variant)
        x = np.arange(len(data))
        axes[0].bar(x + (index - 1) * .24, data.gross_excess, width=.23, color=colors[index], label={"close_close": "收盘到收盘", "open_open": "次日开盘入场", "buyable_cash": "门禁后、空缺留现金"}[variant])
    data = subset(summary)
    axes[0].set_xticks(np.arange(len(data)), data.horizon.astype(str))
    axes[0].set_xlabel("观察期限（交易日）")
    axes[0].set_title("收益率排序：原始毛超额")
    axes[0].axhline(.003, color="#7d8790", linestyle="--", label="双边成本下沿 0.30%")
    axes[0].legend(fontsize=8)
    x = np.arange(len(data))
    errors = np.vstack((data.net_excess - data.ci_low, data.ci_high - data.net_excess))
    axes[1].bar(x, data.net_excess, color=colors[2], yerr=errors, capsize=4)
    axes[1].set_xticks(x, data.horizon.astype(str))
    axes[1].set_xlabel("观察期限（交易日）")
    axes[1].set_title("默认成本后净超额与 HAC 95% 区间")
    for ax in axes:
        ax.axhline(0, color="#354453", linewidth=.8)
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.grid(axis="y", alpha=.2)
    fig.tight_layout()
    name = "horizon_excess.svg"
    fig.savefig(directory / name)
    filenames.append(name)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.5))
    data = subset(annual, horizon=5)
    rsi = subset(annual, signal="rsi2", horizon=5).set_index("year")
    x = np.arange(len(data))
    ax.bar(x - .25, data.gross_excess, width=.24, color=colors[0], label="收益率排序毛超额")
    ax.bar(x, data.net_excess, width=.24, color=colors[2], label="收益率排序净超额")
    ax.bar(x + .25, data.year.map(rsi.net_excess), width=.24, color=colors[1], label="RSI(2)<10 净超额")
    ax.set_xticks(x, data.year.astype(str))
    ax.axhline(0, color="#354453", linewidth=.8)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_title("按信号年份归属的 5 日超额（日期等权）")
    ax.grid(axis="y", alpha=.2)
    ax.legend(fontsize=9)
    fig.tight_layout()
    name = "annual_excess.svg"
    fig.savefig(directory / name)
    filenames.append(name)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    costs = sorted(sensitivity.one_way_cost.unique())
    x = np.arange(len(costs))
    for index, signal in enumerate(("reversal", "rsi2")):
        data = sensitivity.loc[sensitivity.signal.eq(signal)].sort_values("one_way_cost")
        ax.bar(x + (index - .5) * .3, data.net_excess, width=.28, color=colors[index], label="收益率排序" if signal == "reversal" else "RSI(2)<10")
    ax.set_xticks(x, [f"{c:.2%}" for c in costs])
    ax.set_xlabel("单边成本（另加历史卖出印花税）")
    ax.set_title("5 日信号净超额：预先固定的成本敏感性")
    ax.axhline(0, color="#354453", linewidth=.8)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.legend()
    ax.grid(axis="y", alpha=.2)
    fig.tight_layout()
    name = "cost_sensitivity.svg"
    fig.savefig(directory / name)
    filenames.append(name)
    plt.close(fig)
    return filenames


def main():
    parser = argparse.ArgumentParser(description="从固定的阶段 1 输出生成 REPORT.md")
    parser.add_argument("--results", default="research/results/reversal_default")
    parser.add_argument("--report", default="REPORT.md")
    args = parser.parse_args()
    directory = Path(args.results)
    report_path = Path(args.report)
    summary, annual, paired, availability, sensitivity, regime = [pd.read_csv(directory / f"{name}.csv") for name in ("summary", "annual", "paired", "availability", "sensitivity", "summary_regime")]
    gate = json.loads((directory / "gate.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    if gate["passed"]:
        raise SystemExit("闸门通过时应先完成阶段 2，本报告模板不伪造阶段 2 结果")
    observations = pd.read_feather(directory / "signal_observations.feather")
    primary = subset(summary, horizon=5).iloc[0]
    reversal = observations.loc[observations.signal.eq("reversal")]
    rsi = observations.loc[observations.signal.eq("rsi2")]
    known = reversal.loc[reversal.entry_observed]
    rsi_known = rsi.loc[rsi.entry_observed]
    complete = reversal.loc[reversal.matured_5 & reversal.entry_admitted]
    rsi_complete = rsi.loc[rsi.matured_5 & rsi.entry_admitted]
    yearly = subset(annual, horizon=5).copy()
    yearly = yearly.merge(subset(annual, signal="rsi2", horizon=5)[["year", "n_dates", "gross_excess", "net_excess"]].rename(columns={"n_dates": "rsi_n_dates", "gross_excess": "rsi_gross", "net_excess": "rsi_net"}), on="year")
    test_file = directory / "verification.json"
    verification = json.loads(test_file.read_text()) if test_file.exists() else {}
    audit_file = directory / "audit.json"
    audit = json.loads(audit_file.read_text()) if audit_file.exists() else {}
    # SVG 保留中文 text，由阅读器的系统字体回退渲染；不把缺字转成空白字形路径。
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Glyph .* missing from font")
        figures = draw_figures(directory, summary, annual, sensitivity)
    link = directory.as_posix()
    chunks = [
        "# 短期反转策略验证报告\n",
        "## 结论：默认策略不能通过盈利闸门，停止投入阶段 2\n",
        f"按 `STRATEGY.md` §7，**本次策略方向淘汰，不认定为能挣钱**。成交额前 500 中取过去 5 日最跌 20 只，次日开盘入场、观察 5 日、跌停份额留现金后，平均毛超额仅 **{pct(primary.gross_excess)}**，小于估计双边成本 **{pct(primary.estimated_cost)}**；净超额 **{pct(primary.net_excess)}**。Newey–West 20 滞后的 95% 区间为 **[{pct(primary.ci_low)}, {pct(primary.ci_high)}]**，完全低于零。\n",
        f"这不仅是跑输池均值：该口径的 5 日组平均毛收益为 {pct(primary.selected_return)}，扣估计成本后为 {pct(primary.selected_return - primary.estimated_cost)}；池均值为 {pct(primary.pool_return)}。这些是重叠信号组的期限收益均值，**不是资金账户的累计、年化收益或净值**，不能累乘成策略曲线。结论限定于本规格、默认参数、历史样本与成本模型，不是断言所有反转策略永远亏损。\n",
        "阶段 1 已触发第一条失败条件，因此**未开发 `RollingReversalStrategy`、未运行阶段 2、未生成策略净值/年化/Sharpe/换手率或三基准回测图**。§7 第四条净值基准判据尚未检验，不能写成已通过。后面的 RSI 和 regime 表只解释既定体检，不替换失败的主策略。\n",
        "## 一、事前固定方法与数据边界\n",
        "策略权威：[STRATEGY.md](STRATEGY.md)。分析前固定口径：[research/PROTOCOL.md](research/PROTOCOL.md)。脚本：[research/signal_health.py](research/signal_health.py)，不调用回测引擎；读取本地 `open/close/amt/cumadj` 四个矩阵。数据形状、日期、复权乘法和幸存者偏差直接采用任务提供的已核实事实，不重复做数据口径研究。\n",
        f"首个实际信号日为 **{reversal.date.min():%Y-%m-%d}**，最后一个为 **{reversal.date.max():%Y-%m-%d}**。先按已观测上市年龄筛选，再对当日正成交额排名取池；未来是否可买只影响执行诊断，不能反过来更换入选股票。收益率排序共 {reversal.date.nunique():,} 个信号日、{len(reversal):,} 个目标；有下一开盘的目标为 {len(known):,}，5 日期限完整的目标为 {int(reversal.matured_5.sum()):,}。\n",
        "所有价格比率使用 `(原始价 × 当日 cumadj)` 的端点比，成交门禁只用原始前收和开盘价。主口径为 **t+1 开盘 → t+1+h 开盘**，h=5 代表买入后跨 5 个交易日间隔；h=1 也不在买入当天卖出。另列 t 收盘→t+h 收盘的原始描述，避免把两者混为一谈。\n",
        "每个信号日先算所选组等权收益减信号日固定池的同期均值，再对日期等权平均。池内缺未来端点者不进入该日池收益分母，原定已买组只要有未知期限收益则整组记缺失；缺价绝不填零或前填成可交易。只有明确未买入的份额才记录现金收益 0，保留原目标权重。末端未到期另记右删失。成本只扣在信号组，池均值不扣成本，这是相对严格的成本覆盖检验。\n",
        "## 二、1/3/5/10/20 日超额与成本覆盖\n",
        "下表为主口径：次日开盘可入场者持有，不可入场的原定份额留现金。金额比例均为每个期限的收益率，不是百分点以外的累计倍数。\n",
        table(subset(summary), {"horizon":"期限", "n_dates":"有效信号日", "selected_return":"组毛收益", "pool_return":"池毛收益", "gross_excess":"毛超额", "estimated_cost":"估计双边成本", "net_excess":"净超额", "ci_low":"净超额95%下界", "ci_high":"95%上界"}, ("selected_return","pool_return","gross_excess","estimated_cost","net_excess","ci_low","ci_high")),
        "\n主口径的缺失与末端删失计数（按信号组日期，不按个股）：\n",
        table(subset(summary), {"horizon":"期限", "n_dates":"有效信号日", "right_censored_dates":"未到期日期", "missing_endpoint_dates":"已到期但估值缺失日期"}),
        "\n原始收盘到收盘描述（不意味着可按信号日收盘价成交）：\n",
        table(subset(summary, variant="close_close"), {"horizon":"期限", "n_dates":"有效日期", "gross_excess":"收盘到收盘毛超额", "net_excess":"减成本后的诊断值"}, ("gross_excess","net_excess")),
        "\n次日开盘但尚未施加门禁的描述：\n",
        table(subset(summary, variant="open_open"), {"horizon":"期限", "n_dates":"有效日期", "gross_excess":"开盘到开盘毛超额", "net_excess":"减成本后的诊断值"}, ("gross_excess","net_excess")),
        f"\n默认单边 0.15% 加历史卖出税，对实际投入份额的名义双边门槛为税改前 0.40%、税改后 0.35%；组平均成本因少量份额留现金为 {pct(primary.estimated_cost)}。5 日毛超额连较宽松的 0.30% 门槛也未达到，其毛超额 95% 上界为 {pct(primary.gross_ci_high)}。不是通过降低成本、改持有期或挑年份才作出默认结论。\n",
        f"![期限超额比较]({link}/{figures[0]})\n",
        "## 三、年度稳定性：2020—2026\n",
        "按信号年份归属；到期跨年不挪动信号年份。2020 受左端上市年龄预热影响，2026 为截至给定数据末日的部分年度，并排除未到期观察。\n",
        table(yearly, {"year":"年份", "n_dates":"收益率组日期", "gross_excess":"收益率排序毛超额", "estimated_cost":"平均成本", "net_excess":"收益率排序净超额", "rsi_n_dates":"RSI日期", "rsi_gross":"RSI毛超额", "rsi_net":"RSI净超额"}, ("gross_excess","estimated_cost","net_excess","rsi_gross","rsi_net")),
        f"\n主信号有 {int((yearly.gross_excess > 0).sum())}/7 个年份毛超额为正，故没有触发“只有 1–2 年贡献”这一特定失败项；但是 **{int((yearly.net_excess < 0).sum())}/7 年默认成本后净超额为负**，不能认为年度盈利稳定。不能把某条失败项未触发解释为整体通过。\n",
    ]
    for measure, title in (("gross_excess", "各期限分年度毛超额"), ("net_excess", "各期限分年度净超额")):
        pivot = subset(annual).pivot(index="year", columns="horizon", values=measure).reset_index()
        columns = {"year":"年份", **{h:f"{h}日" for h in (1,3,5,10,20)}}
        chunks += [f"\n### {title}\n", table(pivot, columns, (1,3,5,10,20))]
    chunks += [
        f"\n![年度超额比较]({link}/{figures[1]})\n",
        "## 四、跌停无法买入与可执行性\n",
        f"收益率排序在有入场开盘的 {len(known):,} 个目标中，**{int(known.entry_limit_down.sum())} 个次日开盘触发跌停不买，占 {pct(known.entry_limit_down.mean())}**。RSI 对照为 {int(rsi_known.entry_limit_down.sum())}/{len(rsi_known):,}，占 {pct(rsi_known.entry_limit_down.mean())}。分母是证券目标事件，不是去重证券数，也不是交易日数；样本最后一天没有下一开盘，不混入该分母。\n",
        table(availability, {"signal":"信号", "year":"年份", "targets":"已观测开盘目标", "limit_down":"跌停目标", "limit_down_ratio":"跌停比例", "admitted_ratio":"可入场比例", "missing_open":"缺开盘", "missing_factor":"缺因子", "missing_previous_close":"缺前收"}, ("limit_down_ratio","admitted_ratio")),
        f"\n原始数量低于事前 20% 红线，不是本次失败的主因。按默认 5 日期限，可入场的 {len(complete):,} 个收益率排序目标中，到期原始开盘触发涨停不卖的有 {int(complete.exit_upper_5.sum())} 个（{pct(complete.exit_upper_5.mean())}）；RSI 为 {int(rsi_complete.exit_upper_5.sum())}/{len(rsi_complete):,}（{pct(rsi_complete.exit_upper_5.mean())}）。体检只是到期市值诊断，**没有把这些目标伪装成已卖出成交**。完整退出账本只属于闸门通过后的阶段 2。\n",
        "## 五、RSI(2)<10 对照\n",
        f"Wilder(2) 连续价差初始化，缺价重置，双零时 50；阈值固定 10，最多取 20 只。RSI 共 {rsi.date.nunique():,} 个信号日，每日平均 {rsi.groupby('date').size().mean():.2f} 只，少于 20 时按实际数量等权。因此须同时报告共有日期配对，而不能仅比较各自全样本均值。\n",
        table(paired.loc[paired.variant.eq("buyable_cash") & paired.measure.eq("gross_excess")], {"horizon":"期限", "n_paired_dates":"共有有效信号日", "reversal":"收益率排序毛超额", "rsi2":"RSI毛超额", "rsi_minus_reversal":"RSI减收益率排序"}, ("reversal","rsi2","rsi_minus_reversal")),
        "\n在相同有效日期上，RSI 的毛超额更高，但这不表示默认成本下可以赚钱；两组持仓数量也并非完全相同。默认成本的 5 日配对净超额如下：\n",
        table(paired.loc[paired.variant.eq("buyable_cash") & paired.horizon.eq(5) & paired.measure.eq("net_excess")], {"n_paired_dates":"共有有效日期", "reversal":"收益率排序净超额", "rsi2":"RSI净超额", "rsi_minus_reversal":"RSI相对改善"}, ("reversal","rsi2","rsi_minus_reversal")),
        "## 六、事前固定成本敏感性与默认 regime 诊断\n",
        "以下是**信号层成本门槛敏感性，不是阶段 2 回测**。没有搜索收益最优的 N/K/L/H/MA；没有因 RSI 在低成本档改善就替换主策略。\n",
        table(sensitivity, {"signal":"信号", "one_way_cost":"单边成本", "n_dates":"有效日期", "net_excess":"5日净超额"}, ("one_way_cost","net_excess")),
        "\n原策略在最低 0.05% 单边成本档仍为负。RSI 在最低成本档的正值只说明其成本容忍度较高，不能覆盖默认成本，也不作为本任务继续开发的依据。\n",
        f"![成本敏感性]({link}/{figures[2]})\n",
        "合成市场代理以全市场相邻有效复权价日收益等权累乘、基值 1，前 200 日不允许 regime=True。此指数不是实际可投资指数，不含真实指数成分和交易摩擦；缺价改变每日参与证券数，且仍受幸存者偏差影响。仅在默认 `M>MA200` 日期内的 5 日诊断如下，不改变主闸门：\n",
        table(regime.loc[regime.horizon.eq(5) & regime.variant.eq("buyable_cash")], {"signal":"信号", "n_dates":"趋势内有效日期", "gross_excess":"毛超额", "net_excess":"默认成本后净超额", "ci_low":"净超额95%下界", "ci_high":"95%上界"}, ("gross_excess","net_excess","ci_low","ci_high")),
        "\n该表也不构成持有到期的资金账户。因主闸门失败，本次未选择任何进一步 regime 优化、子组合或择时参数。\n",
        "## 七、参数：默认、实际值与依据\n",
        "| 参数 | 默认/本次实际 | 依据与性质 |\n| --- | --- | --- |\n"
        "| N / K / L | 500 / 20 / 5 | 规格经验起点：流动性、分散和短期反转；均未由本数据优化 |\n"
        "| H | 5 | 规格默认；本次为固定期限信号诊断，不是已执行持有账本 |\n"
        "| MA | 200 | 规格长期趋势经验窗口；只作默认 regime 子样本诊断 |\n"
        "| MIN_LISTED | 60 | 规格经验门槛；实际用面板首次有效收盘至今的交易日年龄近似真实上市年龄 |\n"
        "| 首 200 日 | regime=False | 规格明确预热；原始信号体检仍从满足 MIN_LISTED 时开始 |\n"
        "| RSI_PERIOD / RSI_THRESHOLD | 2 / 10 | 规格固定备选；Wilder 初始化、缺口重置和最多 K 只是事前实现约定 |\n"
        "| 各期限 | 1 / 3 / 5 / 10 / 20 | 任务指定，未择优选择其中一期替代 H=5 |\n"
        "| 每边成本 | 0.15% | 规格默认，佣金/过户/滑点合计的研究假设，不是数据测得实际费用 |\n"
        "| 成本敏感性 | 0.05% / 0.15% / 0.30% | 任务指定的三档，不挑最好的一档作主结论 |\n"
        "| 卖出印花税 | 0.1% / 0.05%，2023-08-28 切换 | 任务提供的已确认历史事实；按到期日计，买入不收 |\n"
        "| 板块涨跌幅 | 300/301/688=20%，其余=10% | 任务指定代码近似，不是已知真实证券状态；ST 5% 无法识别 |\n"
        "| 跌停比率红线 | >20% | 规格举例，事前固定为停止红线；本次未触发 |\n"
        "| HAC 滞后 / 区间 | 20 / 正态近似95% | 事前统计约定，覆盖最长观察窗；不是独立同分布 t 检验 |\n"
        "| 日度/组内权重 | 日期等权 / 原定组等权 | 事前约定；缺席份额留现金，不补标的 |\n"
        "| RSI 不足20只 | 实际数量等权 | 事前对照约定；因此另报共有信号日对照 |\n"
        "| 买入/卖出端点 | t+1 / t+1+h 原始开盘对应复权收益比 | 无未来选股；真实价格与收益率口径分离 |\n"
        "| 初始资金/手数/账户费率 | 阶段1不使用 | 没有运行账户；不把通用引擎100万、100份及较低成本默认值混入本分析 |\n",
        f"\n完整机器可读参数与输入/代码哈希：[manifest.json]({link}/manifest.json)。有限敏感性只涉及预先指定成本，未调主信号其他参数。\n",
        "## 八、系统修复与验证\n",
        "- `backtest/market_rules.py`：历史印花税与原始价格门禁。严格保留规格方向：跌停不买、涨停不卖；不能将其偷偷替换成另一套门禁。\n"
        "- `backtest/engine.py`：默认启用门禁；涨跌停不补仓、不顺延；停牌单独顺延，新目标替代旧目标；买单预算不向其他标的再分配；卖出数量不超过开盘前持仓；记录实际卖出税率、受阻原因及顺延。\n"
        "- 引擎统计门禁受阻/尝试比例和停牌目标去重顺延比例。前收缺失时不猜涨跌停价，已顺延订单继续等待可判定的开盘；复权缺失不成交。\n"
        "- CLI 和工作台默认历史印花税，也保留明确选择固定费率的对照模式；旧实验固定税率不被篡改。工作台展示门禁和顺延统计。\n",
        f"验证记录：完整测试返回码 **{verification.get('returncode', '尚未记录')}**，测试数 **{verification.get('tests', '尚未记录')}**；中间输出独立核对 **{audit.get('checks', '尚未记录')}** 项，结果 **{audit.get('passed', '尚未记录')}**。日志见 [tests.log]({link}/tests.log)、[verification.json]({link}/verification.json)、[audit.json]({link}/audit.json)。\n",
        "## 九、诚实限制与实盘可能更差的原因\n",
        "1. **已确认幸存者偏差**：面板仅当前仍在的 5451 只，退市失败样本缺失，历史收益可能偏乐观。没有退市标记，不能用该结果证明覆盖退市损失。\n"
        "2. **没有真实指数、成分、市值或行业**：M 是合成近似，成交额前500是流动性池而非大盘股/指数成分；没有行业中性化或市场风险匹配，超额不是回归 alpha。\n"
        "3. **涨跌幅近似不完整**：无 ST 标记导致真实5%限制漏判；未重建创业板历史制度变化、IPO无涨跌幅期、除权官方参考价与分币取整。原始前收除权跳变也可能被门禁误识别，不能因此改用复权价判停。\n"
        "4. **规格门禁并非实盘成交保证**：只实现指定的跌停买入、涨停卖出禁令；其他封板方向、排队、开盘集合竞价成交容量与时间戳误差均可能使实际成交更差。high/low 是盘后信息，不能拿来改善开盘撮合。\n"
        "5. **信号层而非完整交易账本**：逐日信号期限互相重叠；没有把它们当作每日可全额重建的独立本金。没有阶段2净值，不能引用年化、Sharpe、最大回撤或换手率来宣传。体检入场缺席留现金，停牌真正顺延与到期退出受阻仍可能进一步降低真实表现。\n"
        "6. **成本是假设不是报价**：单边合计成本线性扣减，未计最低佣金、个股冲击、容量、价差差异和成本随成交量变化；这些可能使成本更高。印花税按任务确认的真实分段处理。\n"
        "7. **复权是总收益口径**：cumadj 只计算收益端点比；真实买入价格绝不是 close*cumadj 的数值。通用引擎既有因子比率合成份额模型不等于分红到账、税差、配股及再投资的真实路径。\n"
        "8. **上市日期近似与缺失样本**：面板左侧不可见真实上市历史，MIN_LISTED保守预热；池均值仅用当期限有效端点，其缺失分母可能改变比较。样本末端未到期单独删失，绝不补零，完整计数见 daily/summary 和逐目标记录。\n"
        "9. **统计不确定性**：跨年可能变号、信号自相关、重叠收益、样本长度有限及合成池风险暴露均影响显著性；HAC区间只是固定口径的近似，不消除选择偏差或市场非平稳性。\n",
        "## 十、可追溯产物与复现\n",
        "```bash\nuv run python -m research.signal_health --data feathers --output research/results/reversal_reproduction\nuv run python -m research.audit_outputs --results research/results/reversal_reproduction\nMPLCONFIGDIR=/tmp/quantllm-lesson04-matplotlib uv run python -m unittest discover -s tests -v\nuv run python -m research.make_report --results research/results/reversal_default\n```\n",
        "分析输出目录必须是新目录。最后一条从首次固定结果重新生成本报告，不覆盖数据或重新选择参数。新增复现目录也可传入 `make_report --results ... --report ...`；测试状态需另保存验证记录，不能复用首次记录冒充新实验。\n",
        "| 产物 | 用途 |\n| --- | --- |\n"
        f"| [pool_members.feather]({link}/pool_members.feather) | 每日原始池、金额、年龄、L日收益、RSI、行列索引 |\n"
        f"| [signal_observations.feather]({link}/signal_observations.feather) | 每个入选目标、入场原因、各期限端点与收益、退出涨停标记 |\n"
        f"| [daily.csv]({link}/daily.csv) | 三口径逐日组收益、池均值、净超额、有效分母、右删失 |\n"
        f"| [summary.csv]({link}/summary.csv)、[annual.csv]({link}/annual.csv) | 全期与分年度汇总及HAC区间 |\n"
        f"| [paired.csv]({link}/paired.csv) | 两信号共有日期的直接对照 |\n"
        f"| [availability.csv]({link}/availability.csv) | 按年份的目标、跌停及缺价计数 |\n"
        f"| [sensitivity.csv]({link}/sensitivity.csv) | 预定三档单边成本门槛 |\n"
        f"| [market_proxy.csv]({link}/market_proxy.csv)、[summary_regime.csv]({link}/summary_regime.csv) | 合成指数与默认regime诊断，非真实指数回测 |\n"
        f"| [gate.json]({link}/gate.json) | 明确通过/失败及停止原因 |\n",
    ]
    report_path.write_text("\n\n".join(chunk.strip() for chunk in chunks) + "\n", encoding="utf-8")
    report_manifest = {"report": str(report_path), "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                       "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "results": str(directory)}
    (directory / "report_manifest.json").write_text(json.dumps(report_manifest, ensure_ascii=False, indent=2))
    print("报告已生成：", report_path.resolve())
    print("结论：默认策略未通过阶段 1；未进入阶段 2。")


if __name__ == "__main__":
    main()
