"""量化交易主工作台中的因子演化页面。"""

import json
import os
from pathlib import Path
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from research_ui.data import (
    attempt_frame, contained, curve_frame, label, lineage_dot, list_campaigns, load_campaign, read_json,
)
from research_ui.evolution import replay
from research_ui.branding import DISPLAY_NAME, TAGLINE
from research_ui.workspace import alpha_catalog, evolution_panel, mining_examples, new_study, task_monitor


ROOT = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("QUANTLLM_RESEARCH_RUNS_DIR", ROOT / "research_runs"))
PERIODS = {"train": "训练期", "validation": "开发验证期", "test": "历史审核期"}
METRICS = {
    "direction_days": "训练方向样本数", "valid_days": "有效样本天数",
    "factor_coverage": "因子覆盖率", "label_coverage": "标签覆盖率",
    "ic_valid_ratio": "有效IC日期比例", "portfolio_valid_ratio": "完整组合日期比例",
    "rank_ic": "RankIC", "hac_t": "HAC t值", "fdr_q": "批内FDR q值",
    "long_excess_net": "净多头超额", "long_short_net": "净多空价差",
    "monotonicity": "分层单调性", "positive_month_ratio": "正IC月份比例",
    "positive_offset_ratio": "调仓起点稳定性", "historical_status_available": "历史状态可用",
    "has_trades": "策略有实际成交", "has_baseline_trades": "基准有实际成交",
    "positive_total_return": "账户净收益", "positive_executable_excess": "相对可执行基准超额",
    "drawdown_within_budget": "回撤预算", "no_stale_valuations": "无陈旧估值", "liquidated": "策略与基准均清仓",
}


def percent(value):
    return "—" if value is None or pd.isna(value) else f"{value:.2%}"


def display_attempts(frame):
    table = frame[["id", "round", "name", "status", "lifecycle", "score", "reason"]].copy()
    table["score"] = table.score.map(percent)
    return table.rename(columns={"id": "尝试", "round": "轮次", "name": "因子", "status": "开发状态",
                                 "lifecycle": "当前阶段", "score": "末窗口净标签超额", "reason": "开发诊断"})


def overview(campaign, frame):
    st.subheader("研究进度")
    left, right = st.columns(2)
    with left:
        rounds = frame.groupby(["round", "status"]).size().reset_index(name="count")
        plot = alt.Chart(rounds).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("round:O", title="研究轮次"), y=alt.Y("count:Q", title="尝试数量", axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("status:N", title="开发状态"),
            tooltip=[alt.Tooltip("round:O", title="轮次"), alt.Tooltip("status:N", title="状态"), alt.Tooltip("count:Q", title="数量")],
        ).properties(height=250)
        st.altair_chart(plot, width="stretch")
    with right:
        scores = frame.dropna(subset=["score"])
        if len(scores):
            plot = alt.Chart(scores).mark_circle(size=100).encode(
                x=alt.X("id:O", title="候选尝试"), y=alt.Y("score:Q", title="末窗口平均净标签超额", axis=alt.Axis(format=".1%")),
                color=alt.Color("status:N", title="开发状态"),
                tooltip=[alt.Tooltip("name:N", title="因子"), alt.Tooltip("score:Q", title="净标签超额", format=".3%"),
                         alt.Tooltip("status:N", title="状态")],
            ).properties(height=250)
            st.altair_chart(plot, width="stretch")
        else:
            st.info("尚无完整的开发评分。")
    st.caption("评分用于开发搜索；需要所有窗口的硬门槛通过，才会冻结为审核候选。")
    status = st.multiselect("筛选开发状态", frame.status.unique().tolist(), key="status_filter")
    filtered = frame.loc[frame.status.isin(status)] if status else frame
    st.dataframe(display_attempts(filtered), hide_index=True, width="stretch")
    failures = []
    for attempt in campaign["attempts"]:
        failures.extend((attempt["feedback"] or {}).get("failed_metrics", []))
    if failures:
        st.subheader("候选主要卡在哪里")
        table = pd.Series(failures).value_counts().rename_axis("metric").reset_index(name="count")
        table["metric"] = table.metric.map(lambda item: METRICS.get(item, item))
        st.altair_chart(alt.Chart(table).mark_bar(color="#ba6b56").encode(
            x=alt.X("count:Q", title="涉及候选数", axis=alt.Axis(tickMinStep=1)),
            y=alt.Y("metric:N", title=None, sort="-x"),
            tooltip=[alt.Tooltip("metric:N", title="未通过项"), alt.Tooltip("count:Q", title="候选数")],
        ).properties(height=max(180, 23 * len(table))), width="stretch")


def account_charts(root, folder, account):
    cash = account["execution_config"]["initial_cash"]
    series = [curve_frame(root, f"{folder}/{name}/equity.csv", cash, title)
              for name, title in (("strategy", "策略"), ("baseline", "同池等权基准"))]
    frame = pd.concat(series, ignore_index=True)
    colors = alt.Color("series:N", title=None, scale=alt.Scale(domain=["策略", "同池等权基准"], range=["#117d78", "#8a98af"]))
    for field, title, fmt, height in (("nav", "账户净值", ".3f", 290), ("drawdown", "账户回撤", ".1%", 200)):
        st.altair_chart(alt.Chart(frame).mark_line(strokeWidth=2).encode(
            x=alt.X("date:T", title=None), y=alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False), axis=alt.Axis(format=fmt)),
            color=colors, tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("series:N", title="账户"), alt.Tooltip(f"{field}:Q", title=title, format=fmt)],
        ).properties(height=height).interactive(bind_y=False), width="stretch")
    st.caption("真实资金路径，已计佣金、滑点及卖出税；回撤包含最初投入资金作为高水位。")


def details(campaign, trading_runs=None):
    root = campaign["directory"]
    attempts = {row["id"]: row for row in campaign["attempts"]}
    lifecycle = {row["factor_id"]: row.get("lifecycle") for row in campaign["catalog"]}
    chosen = st.selectbox("选择候选", list(attempts), key="attempt_id", format_func=lambda key:
        f"#{key} · 第{attempts[key]['round']}轮 · " + str((attempts[key]["spec"] if isinstance(attempts[key]["spec"], dict) else {}).get("name", "无效定义"))
        + " · " + label(lifecycle.get(attempts[key]["factor_id"]) or attempts[key]["status"]))
    attempt = attempts[chosen]
    spec = attempt["spec"] if isinstance(attempt["spec"], dict) else {}
    st.write(spec.get("hypothesis", "未提供有效研究假设"))
    st.caption(f"开发状态：{label(attempt['status'])}　·　父因子：{spec.get('parent_id') or '初始候选'}")
    with st.expander("因子表达式与完整ID"):
        st.json({"factor_id": attempt["factor_id"], **spec})
    if attempt["error"]:
        st.error(attempt["error"])
    st.info((attempt["feedback"] or {}).get("reason", "该候选尚无完整开发诊断。"))
    if not attempt["artifact"]:
        return
    artifact = contained(root, attempt["artifact"])
    windows = {path.name: str(path.relative_to(root)) for path in sorted(artifact.glob("fold_*"))
               if (path / "evaluation.json").is_file() and (path / "account.json").is_file()}
    if campaign["state"] == "reviewed" and attempt["factor_id"]:
        path = contained(root, f"review/{attempt['factor_id'][:16]}")
        if (path / "account.json").is_file():
            windows["历史审核"] = str(path.relative_to(root))
    if not windows:
        st.caption("该候选没有完整窗口产物；无效、重复或异常候选仍保留在台账中。")
        return
    window = st.selectbox("验证窗口", list(windows), key="fold", format_func=lambda value: "开发窗口 " + value[5:] if value.startswith("fold_") else value)
    folder = windows[window]
    evaluation = read_json(root, folder + "/evaluation.json")
    account = read_json(root, folder + "/account.json")
    periods = list(evaluation["periods"])
    default = periods.index("test") if window == "历史审核" and "test" in periods else periods.index("validation")
    period = st.selectbox("统计区间", periods, index=default, format_func=lambda value: PERIODS[value], key=f"period_{window}")
    stats = evaluation["periods"][period]
    cells = st.columns(4)
    cells[0].metric("RankIC", f"{stats['ic']['mean']:.4f}" if stats["ic"]["mean"] is not None else "—")
    cells[1].metric("平均净标签超额", percent(stats["long_excess_net"]["mean"]))
    cells[2].metric("账户累计净收益", percent(account["strategy"]["total_return"]))
    cells[3].metric("相对可执行基准超额", percent(account["excess_total_return"]))
    st.caption("前两项对应所选统计区间；后两项对应当前窗口完整账户。开发窗口不会展示审核期指标。")
    if trading_runs is not None and st.button("在策略回测中打开此结果", key="open_in_backtest",
            disabled=attempt["status"] not in {"development_passed", "rejected"}):
        from research_ui.integration import open_in_backtest
        run = open_in_backtest(campaign, chosen, window, trading_runs)
        st.session_state["active_run"] = run
        st.session_state.pop("download_archive", None)
        st.session_state["platform_section_next"] = "策略回测"
        st.rerun()
    account_charts(root, folder, account)
    gates = pd.DataFrame(evaluation["gates"])
    gates = gates.loc[gates.period.eq(period)].copy()
    gates["metric"] = gates.metric.map(lambda value: METRICS.get(value, value))
    gates["actual"] = gates.actual.map(lambda value: "—" if value is None or pd.isna(value) else f"{value:.6g}" if isinstance(value, (int, float)) else str(value))
    gates["threshold"] = gates.threshold.map(str)
    gates["passed"] = gates.passed.map({True: "通过", False: "未通过"})
    left, right = st.columns(2)
    with left:
        st.subheader("统计准入")
        st.dataframe(gates[["metric", "actual", "threshold", "passed"]].rename(columns={"metric": "指标", "actual": "实际值", "threshold": "门槛", "passed": "结论"}), hide_index=True, width="stretch")
    with right:
        st.subheader("账户准入")
        table = pd.DataFrame([{"条件": METRICS.get(key, key), "结论": "通过" if passed else "未通过"} for key, passed in account["gates"].items()])
        st.dataframe(table, hide_index=True, width="stretch")
    if st.checkbox("查看成交与未成交明细", key="show_trades"):
        for name, title in (("trades", "成交记录"), ("rejected_orders", "未成交原因")):
            st.write(title)
            path = contained(root, f"{folder}/strategy/{name}.csv")
            try:
                st.dataframe(pd.read_csv(path, nrows=500), hide_index=True, width="stretch")
            except pd.errors.EmptyDataError:
                st.caption("没有记录。")
        st.caption("每张表最多预览500行；完整明细保存在本次实验目录。")


def render(runs_dir=None, data_dir=None, status_dir=None, trading_runs=None):
    runs = Path(runs_dir) if runs_dir is not None else RUNS
    st.markdown("""<style>
      .stApp { background:#f5f7f3; color:#203934; }
      [data-testid="stMetric"] {background:white; border:1px solid #e1e8df; border-radius:12px; padding:16px;}
      [data-testid="stMetricValue"] {color:#176c59;}
      .block-container {padding-top:1.7rem; max-width:1480px;}
      h1 {letter-spacing:-.035em; color:#163e33;}
      h2,h3 {color:#244d40;}
      [data-testid="stSidebar"] {background:#eaf0e6;}
      [data-baseweb="tab-list"] {gap:22px; border-bottom:1px solid #d9e2d5;}
      [data-testid="stForm"] {background:#fff; padding:14px; border-radius:12px;}
    </style>""", unsafe_allow_html=True)
    st.caption(TAGLINE)
    st.title(DISPLAY_NAME)
    st.caption("因子演化 · 从一个因子，开始下一轮研究")
    st.caption("Alpha101 种子 → 变异与组合 → 统计与账户验证 → 反馈 → 冻结候选")
    new_study(runs, data_dir, status_dir)
    task_monitor(runs)
    mining_examples(runs)
    choices = list_campaigns(runs)
    if not choices:
        st.info("还没有可查看的研究批次。先在终端运行一次 demo 或 discover，再刷新页面。")
        st.code("lesson04/.venv/bin/python -m quant_research demo --output research_runs/my_demo", language="bash")
        return
    with st.sidebar:
        st.caption("研究批次")
        names = {row["name"]: row for row in choices}
        requested = st.session_state.pop("requested_campaign", None)
        if requested in names:
            st.session_state["campaign"] = requested
        name = st.selectbox("选择研究批次", list(names), key="campaign", format_func=lambda key:
                            ("合成对照 · " if names[key]["synthetic"] else "真实行情 · ") + key)
        st.button("刷新已保存结果", key="refresh")
        st.caption("切换、回放和查看结果只读。仅点击“开始演化研究”才创建新开发任务。")
    try:
        campaign = load_campaign(names[name]["path"])
        latest_round = max((row["round"] for row in campaign["attempts"]), default=0)
        if latest_round > 1:
            selected_round = st.slider("演化回放 · 查看截至哪一轮", min_value=1, max_value=latest_round,
                                       value=latest_round, step=1, key=f"replay_{name}")
        else:
            selected_round = latest_round
        campaign = replay(campaign, selected_round)
        frame = attempt_frame(campaign)
        manifest = campaign["manifest"]
        st.subheader(name)
        st.caption(f"{label(campaign['state'])}　·　创建于 {manifest.get('created_utc', '未知')}　·　{'合成数据' if manifest.get('synthetic') else '历史行情'}")
        if manifest.get("synthetic"):
            st.warning("合成数据仅用于验证流程，收益和审核通过结果不代表真实市场有效性。")
        elif campaign["state"] == "sealed":
            st.info("当前展示开发期记录。没有通过完整准入的候选时，系统保留失败结果，不打开历史审核。")
        cells = st.columns(4)
        cells[0].metric("候选尝试", len(frame))
        cells[1].metric("研究轮次", int(frame["round"].max()) if len(frame) else 0)
        cells[2].metric("开发通过", int(frame.raw_status.eq("development_passed").sum()))
        lifecycle = "synthetic_control_passed" if manifest.get("synthetic") else "historical_review_passed"
        cells[3].metric("合成对照通过" if manifest.get("synthetic") else "历史审核通过", sum(row.get("lifecycle") == lifecycle for row in campaign["catalog"]))
        tabs = st.tabs(["演化驾驶舱", "候选与回测", "Alpha101 因子库", "血缘与事件", "协议与证据"])
        with tabs[0]:
            if len(frame):
                evolution_panel(campaign, selected_round)
                with st.expander("候选筛选与失败分布"):
                    overview(campaign, frame)
            else:
                st.info("尚未登记候选。")
        with tabs[1]:
            if len(frame):
                details(campaign, trading_runs)
        with tabs[2]:
            alpha_catalog()
        with tabs[3]:
            st.subheader("因子演化血缘")
            st.graphviz_chart(lineage_dot(campaign), width="stretch")
            st.subheader("研究事件")
            events = pd.DataFrame([{"时间": row["time"], "事件": row["kind"], "摘要": json.dumps(row["payload"], ensure_ascii=False)[:300]} for row in campaign["events"]])
            st.dataframe(events, hide_index=True, width="stretch", height=320)
        with tabs[4]:
            st.write("开发与审核时间、预算、评价阈值和交易约束在运行前固定。")
            st.json(manifest.get("protocol", {}))
            with st.expander("数据及源码指纹"):
                st.json({key: value for key, value in manifest.items() if key.endswith("sha256")})
            path = contained(campaign["directory"], "REPORT.md")
            if path.exists() and campaign["state"] != "replay":
                st.download_button("下载研究报告", data=path.read_bytes(), file_name=f"{name}_REPORT.md", mime="text/markdown", key="download_report")
            st.caption("旧批次按原协议保留。计算版本升级后，只创建新研究，不用新代码重开旧审核。")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        st.error(f"当前批次读取未完成：{exc}")
        st.caption("研究可能仍在写入结果。可点击刷新或切换其他批次。")
