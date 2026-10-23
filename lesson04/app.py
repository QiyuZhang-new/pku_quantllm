"""启动：streamlit run app.py。"""

from dataclasses import asdict
from datetime import date
import logging
import os
from pathlib import Path

import altair as alt
import pandas as pd
import pyarrow as pa
import streamlit as st

from backtest import BacktestConfig
from backtest.dashboard import (
    STRATEGIES, archive_experiment, data_stamp, execute_experiment,
    inspect_data, load_experiment, saved_experiments,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("QUANTLLM_DATA_DIR", ROOT / "feathers")).resolve()
RUNS_DIR = Path(os.environ.get("QUANTLLM_RUNS_DIR", ROOT / "runs")).resolve()
PERIODS = {"每月": "monthly", "每周": "weekly", "每日": "daily"}
LABELS = {
    "date": "日期", "signal_date": "信号日", "symbol": "证券代码", "side": "方向",
    "quantity": "成交份额", "open_price": "开盘价", "fill_price": "成交价",
    "notional": "成交金额", "commission": "佣金", "sell_tax": "卖出税",
    "slippage_cost": "滑点成本", "cash_after": "成交后现金", "shares": "持仓份额",
    "mark_price": "估值价格", "market_value": "持仓市值", "weight": "权重",
    "stale": "缺少有效收盘价", "target_weight": "目标权重", "reason": "未成交原因",
    "equity": "账户权益", "cash": "现金", "return": "日收益率", "turnover": "双边换手率",
    "fees": "费用", "positions": "持仓数量", "stale_positions": "缺价持仓数",
    "sell_tax_rate": "卖出印花税率", "deferred": "因停牌顺延",
    "attempted_orders": "订单尝试数", "limit_rejected_orders": "涨跌停阻止数",
    "suspended_orders": "停牌受阻尝试数", "deferred_fills": "顺延成交数",
}


@st.cache_data(show_spinner=False, max_entries=2)
def catalog(directory, stamp):
    return inspect_data(directory)


def activate(run):
    st.session_state["active_run"] = run
    st.session_state.pop("download_archive", None)


def sidebar(info):
    with st.sidebar:
        st.markdown("### 实验配置")
        mode = st.radio("工作区", ["新建回测", "查看历史"], horizontal=True, key="workspace")
        if mode == "查看历史":
            experiments = saved_experiments(RUNS_DIR)
            if not experiments:
                st.info("还没有保存的实验。运行一次回测后会自动出现在这里。")
                return
            selected = st.selectbox("已保存实验", experiments, format_func=lambda p: p.name, key="saved_run")
            if st.button("载入实验", type="primary", width="stretch", key="load_run"):
                try:
                    activate(load_experiment(selected))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    st.error(f"读取实验失败：{exc}")
            st.caption("可打开通过命令行或网页生成的历史结果。")
            return

        if info is None:
            st.info("行情不可用时，可以切换至「查看历史」浏览已保存的实验。")
            return
        strategy = st.selectbox("策略", list(STRATEGIES), key="strategy")
        st.caption(STRATEGIES[strategy].description)
        universe = st.radio("股票池", ["自选证券", "全部证券"], horizontal=True, key="universe")
        historical_tax = st.checkbox("按成交日使用历史印花税", value=True, key="historical_tax")
        if universe == "全部证券":
            st.caption("全市场回测可能使用 1–2 GB 内存，长期持仓记录会增加开销。")
        dates, symbols = info["dates"], info["symbols"]
        first, last = dates[0].date(), dates[-1].date()
        if first <= date(2024, 3, 31) and last >= date(2024, 1, 1):
            default_dates = (max(first, date(2024, 1, 1)), min(last, date(2024, 3, 31)))
        else:
            default_dates = (dates[max(0, len(dates) - 60)].date(), last)
        defaults = [s for s in ("000001.SZ", "000002.SZ", "600000.SH") if s in symbols] or list(symbols[:3])

        with st.form("backtest_form", border=False):
            chosen = None
            if universe == "自选证券":
                chosen = st.multiselect("证券代码", symbols, default=defaults, key="symbols", placeholder="搜索证券代码")
            interval = st.date_input("回测区间", value=default_dates, min_value=first, max_value=last, key="interval")
            rebalance = st.selectbox("调仓频率", list(PERIODS), key="rebalance")
            initial_cash = st.number_input("初始资金（元）", min_value=1.0, value=1_000_000.0, step=100_000.0, format="%.0f", key="initial_cash")
            lot_mode = st.selectbox("份额约束", ["每手 100 份", "允许零碎份额"], key="lot_mode")
            commission = st.number_input("双向佣金（基点）", min_value=0.0, max_value=1000.0, value=3.0, step=0.5, key="commission_bps")
            slippage = st.number_input("单边滑点（基点）", min_value=0.0, max_value=1000.0, value=5.0, step=0.5, key="slippage_bps")
            sell_tax = st.number_input("固定卖出税（基点）", min_value=0.0, max_value=1000.0, value=0.0, step=0.5, key="sell_tax_bps", disabled=historical_tax)
            st.caption("历史印花税：2023-08-28 前卖出 10 基点，此后 5 基点。")
            st.caption("1 基点 = 0.01%。费用按实际成交金额计算。")
            submitted = st.form_submit_button("运行回测", type="primary", width="stretch", key="run_backtest")

        if submitted:
            if len(interval) != 2 or interval[0] > interval[1]:
                st.error("请选择完整且有序的起止日期。")
                return
            if chosen is not None and not chosen:
                st.error("请至少选择一只证券。")
                return
            if not ((dates >= pd.Timestamp(interval[0])) & (dates <= pd.Timestamp(interval[1]))).any():
                st.error("所选区间没有交易日，请调整日期。")
                return
            config = BacktestConfig(
                initial_cash=initial_cash, commission=commission / 10000,
                slippage=slippage / 10000, sell_tax=None if historical_tax else sell_tax / 10000,
                lot_size=100 if lot_mode == "每手 100 份" else 0,
                start=interval[0].isoformat(), end=interval[1].isoformat(),
            )
            try:
                with st.spinner("正在读取行情、运行回测并保存结果…"):
                    activate(execute_experiment(DATA_DIR, chosen, config, strategy, PERIODS[rebalance], RUNS_DIR))
            except Exception as exc:
                logging.getLogger(__name__).exception("网页回测失败")
                st.error(f"回测未完成：{exc}")
                if "active_run" in st.session_state:
                    st.caption("下方仍显示上一次成功的实验结果。")


def chart(frame, field, title, color, *, percent=False, area=False, height=250):
    base = alt.Chart(frame)
    base = base.mark_area(color=color, opacity=0.35, line=True) if area else base.mark_line(color=color, strokeWidth=2.5, point=len(frame) == 1)
    result = base.encode(
        x=alt.X("date:T", title=None),
        y=alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=False), axis=alt.Axis(format=".0%" if percent else ".2f")),
        tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"), alt.Tooltip(f"{field}:Q", title=title, format=".2%" if percent else ".4f")],
    ).properties(height=height).interactive(bind_y=False)
    st.altair_chart(result, width="stretch")


def overview(result):
    equity = result.equity
    frame = equity.reset_index().copy()
    frame["nav"] = equity["equity"].to_numpy() / result.config.initial_cash
    frame["drawdown"] = frame["nav"] / frame["nav"].cummax().clip(lower=1) - 1
    frame["exposure"] = frame["market_value"] / frame["equity"]
    st.markdown("#### 账户净值")
    st.caption("起始资金归一化为 1；曲线已计入交易费用与滑点。拖动或滚轮可缩放时间轴。")
    chart(frame, "nav", "净值", "#087f8c", height=310)
    st.markdown("#### 回撤")
    chart(frame, "drawdown", "回撤", "#d27859", percent=True, area=True, height=180)
    left, right = st.columns(2)
    with left:
        st.markdown("#### 持仓占比")
        chart(frame, "exposure", "仓位", "#5876a3", percent=True, height=220)
    with right:
        st.markdown("#### 月度收益")
        monthly = (1 + equity["return"]).groupby(equity.index.to_period("M")).prod() - 1
        monthly_frame = pd.DataFrame({"month": monthly.index.astype(str), "return": monthly.to_numpy()})
        monthly_chart = alt.Chart(monthly_frame).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("month:O", title=None, axis=alt.Axis(labelAngle=-35)),
            y=alt.Y("return:Q", title="月收益", axis=alt.Axis(format=".0%")),
            color=alt.condition(alt.datum["return"] >= 0, alt.value("#087f8c"), alt.value("#d27859")),
            tooltip=[alt.Tooltip("month:O", title="月份"), alt.Tooltip("return:Q", title="收益率", format=".2%")],
        ).properties(height=220)
        st.altair_chart(monthly_chart, width="stretch")
        st.caption("首尾月份按实际回测区间计算，可能不足整月。")
    with st.expander("逐日账户数据"):
        table(equity.reset_index(), "equity.csv")


def table(frame, filename):
    display = frame.copy()
    if "side" in display:
        display["side"] = display["side"].replace({"buy": "买入", "sell": "卖出"})
    if "reason" in display:
        display["reason"] = display["reason"].replace({
            "invalid_open": "开盘价无效", "missing_adjustment_factor": "复权因子缺失",
            "missing_previous_close": "前一日收盘价缺失", "limit_down_buy": "跌停不买",
            "limit_up_sell": "涨停不卖",
        })
    st.dataframe(display.rename(columns=LABELS), hide_index=True, width="stretch", height=360)
    st.download_button("下载当前表格 CSV", frame.to_csv(index=False).encode("utf-8-sig"), file_name=filename, mime="text/csv", key=f"csv_{filename}", on_click="ignore")


def trade_details(run):
    frame = run.result.trades
    if frame.empty:
        st.info("本次回测没有成交。可检查信号、交易区间、资金和整手约束。")
        return
    key = run.directory.name
    left, middle, right = st.columns([2, 1, 2])
    symbol = left.text_input("筛选证券代码", placeholder="如 000001.SZ，留空显示全部", key=f"trade_symbol_{key}")
    side = middle.selectbox("成交方向", ["全部", "买入", "卖出"], key=f"trade_side_{key}")
    first, last = run.result.equity.index[[0, -1]].date
    interval = right.date_input("成交日期", value=(first, last), min_value=first, max_value=last, key=f"trade_dates_{key}")
    if symbol.strip():
        frame = frame.loc[frame["symbol"].str.contains(symbol.strip(), case=False, regex=False)]
    if side != "全部":
        frame = frame.loc[frame["side"] == ("buy" if side == "买入" else "sell")]
    if len(interval) == 2:
        frame = frame.loc[frame["date"].between(pd.Timestamp(interval[0]), pd.Timestamp(interval[1]))]
    st.caption(f"筛选后 {len(frame):,} 笔成交；筛选不改变上方完整实验的绩效指标。")
    table(frame, "trades.csv")


def position_details(run):
    result = run.result
    if result.positions.empty:
        st.info("本次回测没有非零持仓。")
        return
    key = run.directory.name
    day = st.selectbox("持仓日期", result.equity.index, index=len(result.equity) - 1, format_func=lambda d: d.strftime("%Y-%m-%d"), key=f"position_date_{key}")
    frame = result.positions.loc[result.positions["date"] == day].sort_values("weight", ascending=False)
    account = result.equity.loc[day]
    st.caption(f"持仓 {len(frame):,} 只 · 现金 {account['cash']:,.2f} 元 · 市值 {account['market_value']:,.2f} 元")
    table(frame, "positions.csv")


def render_result(run):
    result, metrics = run.result, run.result.metrics
    strategy = run.experiment.get("strategy", "历史实验")
    if strategy == "EqualWeightStrategy":
        strategy = "等权示例"
    first, last = result.equity.index[[0, -1]]
    st.markdown(f"### {strategy}")
    st.caption(f"{first:%Y-%m-%d} — {last:%Y-%m-%d} · {len(result.equity):,} 个交易日 · {len(result.trades):,} 笔成交")
    columns = st.columns(4)
    columns[0].metric("累计收益", f"{metrics['total_return']:.2%}")
    columns[1].metric("年化收益", f"{metrics['annualized_return']:.2%}")
    columns[2].metric("最大回撤", f"{metrics['max_drawdown']:.2%}")
    sharpe = metrics["sharpe_zero_rf"]
    columns[3].metric("Sharpe · 无风险利率 0", "—" if sharpe is None else f"{sharpe:.2f}")
    columns = st.columns(4)
    columns[0].metric("期末权益（元）", f"{metrics['final_equity']:,.2f}")
    columns[1].metric("平均仓位", f"{metrics['average_exposure']:.1%}")
    columns[2].metric("交易费用（元）", f"{metrics['total_fees']:,.2f}")
    columns[3].metric("滑点成本（元）", f"{metrics['total_slippage_cost']:,.2f}")
    if "limit_rejected_ratio" in metrics:
        st.caption(f"涨跌停阻止比例 {metrics['limit_rejected_ratio']:.2%}（按订单尝试计） · 停牌顺延目标比例 {metrics.get('suspended_unique_ratio', 0):.2%}（按信号日与证券去重）")

    if metrics["stale_position_days"]:
        st.info(f"本次实验累计有 {metrics['stale_position_days']:,} 个缺价持仓日；可在持仓明细查看估值标记。")
    if metrics["average_exposure"] < 0.5 and result.config.lot_size:
        st.info("本次平均仓位低于 50%。可检查股票池规模、初始资金和整手约束。")
    tabs = st.tabs(["净值与风险", "成交记录", "持仓明细", "未成交目标", "实验参数"])
    with tabs[0]:
        overview(result)
    with tabs[1]:
        trade_details(run)
    with tabs[2]:
        position_details(run)
    with tabs[3]:
        st.caption("记录涨跌停、开盘价、前收或复权因子无效的目标；deferred 标记停牌顺延，整手取整为零不计入。")
        if result.rejected_orders.empty:
            st.info("没有被门禁阻止或顺延的目标。")
        else:
            table(result.rejected_orders, "rejected_orders.csv")
    with tabs[4]:
        st.caption("此处记录实际执行的参数，修改侧栏不会改变已有结果。")
        st.json({"策略": strategy, "账户与交易参数": asdict(result.config), "绩效指标": metrics}, expanded=True)
        with st.expander("数据与股票池记录"):
            st.json(run.experiment, expanded=False)

    st.divider()
    left, right = st.columns([3, 1])
    left.caption(f"实验已保存：{run.directory}")
    with right:
        if st.button("打包完整结果", width="stretch", key="prepare_download"):
            with st.spinner("正在打包…"):
                try:
                    st.session_state["download_archive"] = archive_experiment(run.directory)
                except OSError as exc:
                    st.error(f"打包失败：{exc}")
        if "download_archive" in st.session_state:
            st.download_button("下载实验 ZIP", st.session_state["download_archive"], file_name=f"{run.directory.name}.zip", mime="application/zip", type="primary", width="stretch", key="download_archive_button", on_click="ignore")


def main():
    st.set_page_config(page_title="QuantLLM · 回测工作台", page_icon="📈", layout="wide")
    st.markdown("""<style>
      .block-container {max-width: 1440px; padding-top: 2rem;}
      [data-testid="stMetric"] {background: #fff; border: 1px solid #e4eaf1;
        border-radius: 12px; padding: 16px 18px; margin-bottom: 12px;}
      [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
      h1, h2, h3 {letter-spacing: -.025em;}
      [data-testid="stSidebar"] {border-right: 1px solid #e4eaf1;}
    </style>""", unsafe_allow_html=True)
    st.title("回测工作台")
    st.caption("QuantLLM · 日频策略研究 · 收盘信号，下一交易日开盘成交")
    info = None
    try:
        info = catalog(str(DATA_DIR), data_stamp(DATA_DIR))
    except (OSError, ValueError, KeyError, pa.ArrowException) as exc:
        st.error(f"无法读取本地行情：{exc}")
    if info is not None:
        dates = info["dates"]
        st.caption(f"行情覆盖 {dates[0]:%Y-%m-%d} — {dates[-1]:%Y-%m-%d} · {len(dates):,} 个交易日 · {len(info['symbols']):,} 只证券")
    sidebar(info)
    if "active_run" in st.session_state:
        render_result(st.session_state["active_run"])
    else:
        st.markdown("### 开始一次策略实验")
        st.write("在左侧选择股票池与日期，设置资金和交易成本，然后点击「运行回测」。")
        with st.container(border=True):
            st.markdown("**一个页面，查看完整回测过程**")
            st.write("交互式净值与回撤 · 仓位与月度收益 · 成交与持仓明细 · 参数留档和结果下载")
        experiments = saved_experiments(RUNS_DIR)
        if experiments and st.button("打开最近一次实验", key="open_latest"):
            try:
                activate(load_experiment(experiments[0]))
                st.rerun()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                st.error(f"读取实验失败：{exc}")
    with st.expander("回测口径"):
        st.write("只做多、无杠杆；按累计复权因子模拟总收益持仓。按规格限制跌停买入和涨停卖出：300/301/688 前缀近似 20%，其余 10%；无 ST 标记。停牌顺延，涨跌停跳过不顺延；未模拟盘口排队或成交量限制。")
        st.caption("年化按 252 个交易日计算，短区间年化值仅作同口径统计。缺价时沿用已知价格估值，并记录标记。")

if __name__ == "__main__":
    main()
