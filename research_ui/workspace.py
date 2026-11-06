"""研究操作、代际证据及Alpha101目录的页面组件。"""

import json

import altair as alt
import pandas as pd
import streamlit as st

from quant_research.alpha101 import DEFINITIONS, SOURCE, catalog
from quant_research.formula_search import METHODS
from . import jobs
from .data import contained, read_json
from .evolution import development_evidence, expression_text, round_history


def pct(value):
    return "—" if value is None or pd.isna(value) else f"{value:+.2%}"


def mining_examples(root):
    """提供原系统内的实例入口；打开和下载均不启动新计算。"""
    if not root.is_dir():
        return
    examples = []
    for path in sorted(root.glob("*_comparison/comparison.json"), reverse=True):
        try:
            value = read_json(root, str(path.relative_to(root)))
            arms = {row["method"]: row for row in value["arms"]}
            if {"adaptive_gp", "random_gp"} <= set(arms):
                examples.append((path.parent, arms))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not examples:
        return
    directory, arms = examples[0]
    with st.expander("真实行情 · 自动挖掘实例", expanded=True):
        st.write(f"已完成：{directory.name.removesuffix('_comparison')}")
        st.caption("相同初始因子、评价预算与账户约束，比较反馈驱动的公式进化和随机公式搜索。打开实例可逐代回放、查看实际公式与失败依据。")
        left, right = st.columns(2)
        for column, method, title in ((left, "adaptive_gp", "查看反馈进化实例"), (right, "random_gp", "查看同预算随机对照")):
            name = arms[method]["campaign"]
            if contained(root, name).parent != root.resolve():
                continue
            if column.button(title, key=f"example_{method}"):
                st.session_state["requested_campaign"] = name
        report = contained(root, str((directory / "REPORT.md").relative_to(root)))
        if report.exists():
            st.download_button("下载两组最终对照报告", data=report.read_bytes(),
                               file_name=directory.name + "_REPORT.md", mime="text/markdown", key="example_report")


def new_study(root, data_directory=None, status_directory=None):
    with st.expander("新建演化研究", expanded=not bool(st.session_state.get("campaign"))):
        st.caption("从已有因子出发，自动组合与变异公式，实测后继续搜索。公式进化会用开发反馈选择父代并更新变异算子；当前不调用在线LLM。")
        with st.form("new_study", border=False):
            left, right = st.columns([2, 3])
            with left:
                mode = st.selectbox("数据", ["historical", "synthetic"], format_func=lambda value:
                                    "真实行情" if value == "historical" else "合成流程对照", key="new_mode")
                scope = st.selectbox("行情范围", ["120", "full"], format_func=lambda value:
                                     "固定120只 · 工程验证" if value == "120" else "课程完整面板 · 5451列", key="new_scope")
                cash = st.selectbox("初始资金", [1_000_000, 10_000_000, 1_000_000_000],
                                    format_func=lambda value: {1_000_000: "100万元", 10_000_000: "1000万元", 1_000_000_000: "10亿元"}[value])
            with right:
                method = st.selectbox("自动挖掘方式", ["adaptive_gp", "random_gp", "rules"],
                                      format_func=lambda value: METHODS[value], key="new_search_method")
                seed_names = {"return5": "5日价格变化", "return20": "20日价格变化", "volatility20": "20日波动率",
                              "market_deviation20": "20日市场偏离波动"}
                seed_names.update({f"alpha{number}": f"Alpha#{number} · {value['theme']}" for number, value in DEFINITIONS.items()})
                seeds = st.multiselect("起始因子", list(seed_names), default=["alpha33", "alpha101", "return5", "volatility20"],
                                       format_func=lambda value: seed_names[value], key="new_seeds")
                a, b = st.columns(2)
                rounds = a.number_input("研究轮数", min_value=1, max_value=8, value=3, step=1, key="new_rounds")
                batch = b.number_input("每轮候选预算", min_value=1, max_value=8, value=4, step=1, key="new_batch")
                seed = st.number_input("搜索随机种子", min_value=0, max_value=2**32 - 1, value=20261106, step=1)
                st.caption("股票范围、资金、费用、时间窗口及搜索预算均写入新批次；已有研究不会覆盖。")
            st.caption("完整面板可能使用GB级内存。开始后只运行开发搜索，不自动打开历史审核。停止请求在轮次边界生效。")
            submitted = st.form_submit_button("开始演化研究", type="primary", key="start_study")
        if submitted:
            try:
                value = jobs.launch(root, {"mode": mode, "rounds": int(rounds), "batch_size": int(batch),
                                          "scope": scope, "seeds": seeds, "initial_cash": cash,
                                          "search_method": method, "random_seed": int(seed)},
                                    data_directory=data_directory, status_directory=status_directory)
                st.session_state["launched_job"] = value["id"]
                st.success("任务已提交。进度会在下方更新，研究批次创建后可从侧栏打开。")
            except (OSError, ValueError) as exc:
                st.error(f"未启动研究：{exc}")


@st.fragment(run_every="3s")
def task_monitor(root):
    items = jobs.list_jobs(root)
    if not items:
        return
    job = items[0]
    names = {"queued": "等待启动", "running": "正在研究", "completed": "开发已完成",
             "cancelled": "已停止", "failed": "任务失败", "interrupted": "进程已中断"}
    with st.container(border=True):
        left, right = st.columns([5, 1])
        left.markdown(f"**{names.get(job['status'], job['status'])}** · `{job['campaign']}`")
        left.caption(job.get("message", ""))
        request = job["request"]
        left.caption(f"{request['rounds']}轮 × 每轮{request['batch_size']}个候选；提案器：{METHODS.get(request.get('search_method', 'rules'), '未知')}")
        campaign_path = contained(root, job["campaign"])
        if (campaign_path / "registry.sqlite3").exists() and right.button("打开研究", key="open_job"):
            st.session_state["requested_campaign"] = job["campaign"]
            st.rerun()
        if job["status"] in {"queued", "running"}:
            if right.button("停止研究", key="cancel_job"):
                try:
                    jobs.cancel(root, job["id"])
                    st.info("已请求停止；当前轮完成后保留全部已有结果并结束。")
                except (OSError, ValueError) as exc:
                    st.warning(str(exc))
        log_path = jobs.job_path(root, job["id"], "log")
        if log_path.exists():
            with st.expander("任务日志"):
                with log_path.open("rb") as source:
                    source.seek(0, 2)
                    source.seek(max(0, source.tell() - 8000))
                    st.code(source.read().decode("utf-8", errors="replace"), language="text")


def search_policy_panel(campaign, round_number):
    method = campaign["manifest"].get("search", {}).get("method", "rules")
    if method not in {"adaptive_gp", "random_gp"}:
        st.caption("该批次使用早期规则反馈基线。新建研究可选择公式进化或随机公式对照。")
        return
    path = f"search/round_{round_number:04d}.json"
    if not contained(campaign["directory"], path).exists():
        st.info("本轮尚在计算，完成后显示搜索反馈。")
        return
    snapshot = read_json(campaign["directory"], path)
    st.subheader("自动挖掘 · 实际搜索记录")
    st.caption(METHODS[method] + "；在线LLM调用为0。CRank为当日合格股票内的百分位排名减0.5。")
    incumbent = snapshot["incumbent"]
    if incumbent:
        st.markdown(f"**搜索选定的研究父代：#{incumbent['attempt_id']}**")
        st.code(expression_text(incumbent["spec"]["expression"]), language="text")
        st.caption(f"研究适应度 {incumbent['fitness'][1]:+.5f}；完整开发准入：{'通过' if incumbent['fitness'][0] else '未通过'}。")
    st.caption(snapshot["configuration"]["objective"])
    track = []
    for number in range(1, round_number + 1):
        prior_path = f"search/round_{number:04d}.json"
        if contained(campaign["directory"], prior_path).exists():
            prior = read_json(campaign["directory"], prior_path)
            if prior["incumbent"]:
                track.append({"轮次": number, "研究适应度": prior["incumbent"]["fitness"][1],
                              "候选": prior["incumbent"]["attempt_id"]})
    if track:
        st.altair_chart(alt.Chart(pd.DataFrame(track)).mark_line(point=True).encode(
            x=alt.X("轮次:O"), y=alt.Y("研究适应度:Q", scale=alt.Scale(zero=False)),
            tooltip=["轮次:O", "候选:O", alt.Tooltip("研究适应度:Q", format=".5f")]
        ).properties(height=180), width="stretch")
    with st.expander("反馈如何改变下一轮搜索", expanded=True):
        stats = [{"变异算子": arm, "已评价子代": value["observed"], "优于实际父代": value["improved"],
                  "下次探索得分": snapshot["ucb_scores"][arm]} for arm, value in snapshot["operator_statistics"].items()]
        st.dataframe(pd.DataFrame(stats), hide_index=True, width="stretch")
        st.caption("反馈进化用改进次数和探索得分选择算子；随机臂均匀抽取算子和已测父代，上表不参与其生成选择。")
        if snapshot["observations"]:
            st.dataframe(pd.DataFrame([{"子代": row["attempt_id"], "算子": row["operator"],
                "父适应度": row["parent_fitness"][1], "子适应度": row["child_fitness"][1],
                "优于父代": row["improved_parent"], "全部准入": row["development_passed"]}
                for row in snapshot["observations"]]), hide_index=True, width="stretch")
        st.caption(f"本轮结束生成下一轮 {len(snapshot['next_proposals'])} 个候选；父代、交叉来源及改动位置均已保存。")


def evolution_panel(campaign, round_number):
    search_policy_panel(campaign, round_number)
    evidence, snapshots = development_evidence(campaign, round_number)
    if evidence.empty:
        st.info("这一轮还没有候选记录。")
        return
    st.caption("以下均来自已保存的开发窗口。累计最优为回看诊断，不是新的晋升规则；准入结论沿用原协议。")
    history = round_history(evidence)
    best = evidence.dropna(subset=["label_excess"]).sort_values("label_excess", ascending=False)
    approved = evidence.loc[evidence.all_passed]
    if len(best):
        row = best.iloc[0]
        with st.container(border=True):
            st.caption("当前回放范围内的开发诊断最优")
            st.markdown(f"**#{int(row.id)} · {row['name']}**")
            st.code(row.expression, language="text")
            cells = st.columns(4)
            cells[0].metric("开发RankIC", f"{row.rank_ic:.4f}" if pd.notna(row.rank_ic) else "—")
            cells[1].metric("RankICIR · 不年化", f"{row.icir:.4f}" if pd.notna(row.icir) else "—")
            cells[2].metric("账户净超额", pct(row.excess))
            cells[3].metric("完整开发准入", "通过" if row.all_passed else "未通过")
    if not len(approved):
        st.info("尚无候选通过全部开发门槛。指标有改善的候选仍保留用于研究，不计作有效因子入库。")
    chart_data = history.melt(id_vars=["round"], value_vars=["best_diagnostic", "best_approved"],
                              var_name="series", value_name="value")
    chart_data["series"] = chart_data.series.map({"best_diagnostic": "累计诊断最优", "best_approved": "全部准入通过候选最优"})
    st.altair_chart(alt.Chart(chart_data).mark_line(interpolate="step-after", point=True).encode(
        x=alt.X("round:O", title="已完成研究轮次"), y=alt.Y("value:Q", title="平均净标签超额", axis=alt.Axis(format=".2%")),
        color=alt.Color("series:N", title=None), tooltip=[alt.Tooltip("round:O", title="轮次"),
            alt.Tooltip("series:N", title="范围"), alt.Tooltip("value:Q", title="净标签超额", format=".3%")],
    ).properties(height=250), width="stretch")
    st.caption("缺少通过者时，对应曲线留空，不填0；平台期表示没有更高的累计诊断值。")
    view = evidence[["id", "round", "expression", "operation", "rank_ic", "icir", "net_return", "excess", "drawdown", "coverage", "status", "ic_delta", "excess_delta"]].copy()
    for field in ("net_return", "excess", "drawdown", "coverage", "excess_delta"):
        view[field] = view[field].map(pct)
    view = view.rename(columns={"id": "候选", "round": "轮次", "expression": "表达式", "operation": "修改操作",
        "rank_ic": "RankIC", "icir": "RankICIR", "net_return": "账户收益", "excess": "账户超额",
        "drawdown": "最大回撤", "coverage": "因子覆盖", "status": "开发结论",
        "ic_delta": "较父因子ΔRankIC", "excess_delta": "较父因子Δ账户超额"})
    st.dataframe(view, hide_index=True, width="stretch")
    st.download_button("导出当前代际对比 CSV", data=view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{campaign['directory'].name}_round_{round_number}.csv", mime="text/csv", key="export_evolution")
    st.subheader("父子改进证据")
    children = evidence.loc[evidence.parent_attempt.notna()]
    if children.empty:
        st.caption("当前回放范围内还没有已记录父因子的子代。")
    else:
        ids = children.id.tolist()
        selected = st.selectbox("选择父子对比", ids, key="compare_child", format_func=lambda value: f"#{value} · {evidence.loc[evidence.id.eq(value), 'name'].iloc[0]}")
        child = evidence.loc[evidence.id.eq(selected)].iloc[0]
        parent = evidence.loc[evidence.id.eq(child.parent_attempt)].iloc[0]
        a, b = st.columns(2)
        with a:
            st.caption(f"父因子 #{int(parent.id)}")
            st.code(parent.expression, language="text")
        with b:
            st.caption(f"子因子 #{int(child.id)} · {child.operation}")
            st.code(child.expression, language="text")
        st.write(child.hypothesis)
        st.write(f"ΔRankIC：{child.ic_delta:+.4f}" if pd.notna(child.ic_delta) else "ΔRankIC：暂无可比证据")
        st.write(f"Δ账户净超额：{pct(child.excess_delta)}；完整准入：{'通过' if child.all_passed else '未通过'}。")
        current = next(row for row in campaign["attempts"] if row["id"] == selected)
        st.caption((current["feedback"] or {}).get("reason", "尚无完整反馈"))


def alpha_catalog():
    st.subheader("Alpha101 种子库")
    st.write(f"已核验并接入 {len(DEFINITIONS)} / 101 个公式，可作为新研究的起始因子。其余编号不会参与搜索。")
    st.link_button("原论文 · 101 Formulaic Alphas", SOURCE)
    records = catalog()
    show_all = st.checkbox("同时显示尚未实现的编号", key="all_alphas")
    table = pd.DataFrame([{"编号": f"Alpha#{row['number']}", "研究假设": row["theme"],
                           "公式": row["formula"] or "—", "输入字段": ", ".join(row["fields"]) or "待核验",
                           "接入状态": row["status"]} for row in records if row["implemented"] or show_all])
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption("使用累计复权OHLC；横截面排名限制在当日历史状态合格且有值的证券。最终流动性与历史长度门槛另由评价器统一施加。")
    st.caption("成交额及成交笔数不充当volume。需要VWAP、真实成交量、行业或市值的公式尚未接入；delay-0公式也不直接套用次日开盘。")
