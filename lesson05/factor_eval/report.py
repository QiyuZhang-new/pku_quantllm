"""可审计的 Markdown、CSV、JSON 和无需联网的 HTML 报告。"""

import base64
import html
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


def fmt(value, percent=False):
    if value is None or not isinstance(value, (int, float, np.number)) or not np.isfinite(value):
        return "—"
    return f"{value:.2%}" if percent else f"{value:.4f}"


def _table(headers, rows):
    safe = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(map(safe, headers)) + " |",
                       "| " + " | ".join(["---"] * len(headers)) + " |"] +
                      ["| " + " | ".join(map(safe, row)) + " |" for row in rows])


def plot_result(result, path):
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/quantllm-lesson05-matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    daily, summary = result.daily, result.summary
    cfg = summary["config"]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    ax = axes[0, 0]
    for split, color in (("train", "#718096"), ("validation", "#d97706"), ("test", "#2563eb")):
        series = daily.rank_ic.where(daily.split.eq(split)).rolling(40, min_periods=15).mean()
        ax.plot(daily.index, series, label=split, color=color)
    ax.axhline(0, color="black", lw=.7)
    ax.set_title("40-session mean oriented RankIC (not annualized)")
    locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.legend()
    ax = axes[0, 1]
    x = np.arange(cfg["groups"])
    for split, shift in (("validation", -.18), ("test", .18)):
        means = summary["periods"][split]["group_means_5dr"]
        ax.bar(x + shift, [np.nan if v is None else v * 100 for v in means], .36, label=split)
    ax.axhline(0, color="black", lw=.7)
    ax.set_xticks(x, [f"Q{k}" for k in range(1, cfg["groups"] + 1)])
    ax.set_title(f"Mean forward {cfg['horizon']}-session return (%)")
    ax.legend()
    ax = axes[1, 0]
    for offset in range(cfg["horizon"]):
        sample = daily.loc[daily.split.eq("test") & daily.calendar_row.mod(cfg["horizon"]).eq(offset)]
        # cumsum 保持缺失点断线，明确为已观测收益之和，非资金净值。
        series = sample.long_excess_net.cumsum() * 100
        ax.plot(sample.index, series, label=f"offset {offset}", lw=1)
    ax.axhline(0, color="black", lw=.7)
    ax.set_title("Test: observed net excess sums (%); gaps = unknown, NOT NAV")
    locator = mdates.AutoDateLocator(minticks=4, maxticks=6)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.legend(fontsize=8)
    ax = axes[1, 1]
    years = result.yearly.loc[result.yearly.split.ne("train")]
    labels = [f"{r.period[:4]} {r.split[:3]}" for r in years.itertuples()]
    ax.bar(np.arange(len(years)), years.rank_ic.fillna(np.nan))
    ax.set_xticks(np.arange(len(years)), labels, rotation=30, ha="right")
    ax.axhline(0, color="black", lw=.7)
    ax.set_title("Yearly oriented RankIC (validation / test)")
    for ax in axes.flat:
        ax.grid(axis="y", alpha=.2)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def write_reports(results, output, manifest):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    leaderboard = []
    sections = []
    html_sections = []
    for i, result in enumerate(results, 1):
        summary = result.summary
        directory = output / f"factor_{i:02d}"
        directory.mkdir(exist_ok=True)
        for name in ("daily", "monthly", "yearly", "offsets"):
            frame = getattr(result, name)
            frame.to_csv(directory / f"{name}.csv", index=(name == "daily"))
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                           allow_nan=False), encoding="utf-8")
        pd.DataFrame(summary["gates"]).to_csv(directory / "gates.csv", index=False)
        plot_result(result, directory / "diagnostics.png")
        test = summary["periods"]["test"]
        val = summary["periods"]["validation"]
        leaderboard.append({"factor": result.name, "verdict": summary["verdict"], "score": summary["score"],
                            "direction": summary["direction"], "validation_approved": summary["validation_approved"],
                            "validation_rank_ic": val["ic"]["mean"], "test_rank_ic": test["ic"]["mean"],
                            "test_hac_t": test["ic"]["hac_t"], "test_fdr_q": test["ic"]["fdr_q"],
                            "test_long_excess_net_5dr": test["long_excess_net"]["mean"],
                            "test_long_short_net_5dr": test["long_short_net"]["mean"],
                            "cost_stress_passed": summary["cost_stress_passed"], "detail": directory.name})
        rows = []
        for s, p in summary["periods"].items():
            rows.append([s, p["ic"]["n"], fmt(p["ic"]["mean"]), fmt(p["ic"]["ir"]),
                         fmt(p["ic"]["hac_t"]), fmt(p["ic"]["fdr_q"]),
                         fmt(p["long_excess_net"]["mean"], True), fmt(p["long_short_net"]["mean"], True),
                         fmt(p["monotonicity"]), fmt(p["positive_month_ratio"], True),
                         fmt(p["positive_offset_ratio"], True)])
        headers = ["区间", "IC天数", "RankIC", "ICIR", "HAC t", "FDR q", "净多头超额/期",
                   "净多空价差/期", "单调性", "正IC月份", "正超额相位"]
        reasons = [f"{g['period']} / {g['metric']}：实测 {fmt(g['actual'])}，门槛 {g['threshold']}；{g['explanation']}"
                   for g in summary["failed_gates"]]
        why = "\n".join(f"- {r}" for r in reasons) if reasons else "所有硬门槛通过。"
        sections.append(f"## {result.name}：{summary['verdict']}\n\n"
                        f"诊断分数 {summary['score']}/100；训练期冻结方向 {summary['direction']:+d}；"
                        f"验证阶段准入：{'通过' if summary['validation_approved'] else '未通过'}；"
                        f"双倍成本压力：{'通过' if summary['cost_stress_passed'] else '未通过'}。\n\n"
                        + _table(headers, rows) + "\n\n判定依据：\n\n" + why +
                        f"\n\n![诊断图]({directory.name}/diagnostics.png)\n\n"
                        f"逐日、分月、分年、非重叠相位、全部门槛见 `{directory.name}/`。\n")
        image = base64.b64encode((directory / "diagnostics.png").read_bytes()).decode()
        cells = lambda r, tag: "<tr>" + "".join(f"<{tag}>{html.escape(str(c))}</{tag}>" for c in r) + "</tr>"
        html_sections.append(f"<section><h2>{html.escape(result.name)}：{html.escape(summary['verdict'])}</h2>"
                             f"<p>诊断分数 {summary['score']}/100；训练期冻结方向 {summary['direction']:+d}</p>"
                             "<div class='scroll'><table>" + cells(headers, "th") +
                             "".join(cells(row, "td") for row in rows) + "</table></div><ul>" +
                             "".join(f"<li>{html.escape(r)}</li>" for r in reasons) + "</ul>" +
                             f"<img alt='Factor diagnostics' src='data:image/png;base64,{image}'></section>")
    board = pd.DataFrame(leaderboard)
    # 保持输入顺序；不能把看完测试集后的名次包装成事前选因子。
    board.to_csv(output / "leaderboard.csv", index=False)
    preface = (
        "# 因子评价报告\n\n"
        "**结论针对本次研究样本；诊断分数不替代硬门槛。** 候选按输入顺序展示，"
        "测试结果不用于重新选方向或调阈值。历史时间留出不等于前瞻验证；"
        "默认课程数据已在 lesson04 中使用。合成数据结果不能作为市场有效性证据。\n\n"
        "标签：t 收盘因子 → t+1 复权开盘进入 → t+1+h 复权开盘退出；默认 h=5。"
        "train/validation 边界剔除跨区间标签；方向仅取训练期原始 RankIC 均值的符号。\n\n"
        "分组按信号日有效因子排序；Q1 为低分组，Q5 为高分组（默认五组）。"
        "并列因子取平均秩，不人为拆开；分组不一定等大。基准为相同有效因子股票池的等权收益。"
        "缺任何计划成分的标签，该组当期收益不可用；不补仓、不事后重排。\n\n"
        "每期假设独立完整买卖：多头扣 2×单边成本，多空价差扣 4×单边成本；"
        "基准不扣费。成本是配置情景，非税率或券商报价。多空价差为多空各 1 元名义头寸的差，"
        "未计融券费用，也不是资金组合收益。每日五日收益相互重叠，不能连乘为每日净值；"
        "图中只对各固定相位的非重叠观测超额求和，并明确标注缺失。\n\n"
        "HAC 使用 Bartlett 权重，默认 10 阶（至少 h−1 阶），p 值为大样本双侧正态近似；"
        "ICIR 为均值/标准差，不做重叠样本的机械年化。FDR 仅校正本批次因子，"
        "不能涵盖过去未披露的筛选或反复调参；train 的显著性受方向拟合影响，仅作描述。\n\n"
        "当前默认池只按历史价格长度和成交额筛选。未自动接入历史 ST、涨跌停、行业、市值、"
        "做空可得性与逐笔成交；未做行业/规模中性化。因此“好因子”表示研究指标通过，"
        "不能证明独立 alpha 或可实盘成交。可提供信号日 eligible 掩码以排除不合格证券。\n\n"
    )
    (output / "REPORT.md").write_text(preface + "\n".join(sections), encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                                    allow_nan=False), encoding="utf-8")
    (output / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>五日收益因子评价报告</title><style>"
        "body{font:15px/1.7 system-ui,sans-serif;color:#182333;background:#f5f7fa;margin:0 auto;max-width:1200px;padding:24px}"
        "section{background:white;padding:24px;margin:24px 0;border-radius:12px}"
        "table{border-collapse:collapse;white-space:nowrap;width:100%;font-size:13px}"
        "th,td{padding:9px;border-bottom:1px solid #ddd;text-align:right}.scroll{overflow:auto}"
        "img{max-width:100%;height:auto}pre{white-space:pre-wrap;font-family:inherit}"
        "</style><h1>因子评价：能否预测未来五日收益</h1><details><summary>评价口径与边界（点击展开）</summary><pre>" +
        html.escape(preface) + "</pre></details>" + "".join(html_sections) + "</html>", encoding="utf-8")
    return board
