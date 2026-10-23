"""保留第一轮失败，运行有限、明确标注探索性质的后续研究。"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from backtest import BacktestEngine
from backtest.market_deviation_strategy import MarketDeviationStrategy
from .run import BASE, DATA, benchmark_series, configuration, digest, load_data


def run_case(data, output, window=20, slippage=.0005, equal_weight=False, train=False):
    start,end = ("2023-01-03","2024-12-31") if train else ("2025-01-02","2026-09-30")
    rows = np.flatnonzero((data.dates>=pd.Timestamp(start)) & (data.dates<=pd.Timestamp(end)))
    # 只传已知交易日期，不向策略传未来行情。
    schedule = {data.dates[row-1]:len(rows)-offset for offset,row in enumerate(rows) if offset>=len(rows)-5}
    notices = pd.read_csv(DATA/"corporate_notices.csv",parse_dates=["announced_date"])
    model = MarketDeviationStrategy(window=window,equal_weight=equal_weight,
        notices=notices.set_index("symbol").announced_date.to_dict(),liquidation_signals=schedule)
    config = configuration(start,end,slippage,record_positions=not equal_weight)
    result = BacktestEngine(data,config).run(model)
    result.metrics.update({"end_positions":int(result.equity.iloc[-1].positions),
        "first_day_buys":int((result.trades.date.eq(result.equity.index[0]) & result.trades.side.eq("buy")).sum()),
        "st_position_days":int(result.equity.st_positions.sum()),
        "unknown_status_position_days":int(result.equity.unknown_status_positions.sum()),
        "fees_and_slippage_initial_capital_ratio":(result.metrics["total_fees"]+result.metrics["total_slippage_cost"])/1e9,
        "annualized_two_way_turnover":float(result.equity.turnover.mean()*252)})
    result.save(output)
    pd.DataFrame(model.selections).to_csv(output/"selections.csv",index=False)
    pd.DataFrame(model.controls).to_csv(output/"controls.csv",index=False)
    parameters={"method":"market_deviation" if not equal_weight else "equal_weight_deviation_pool",
        "k":200,"holding":20,"window":window,"min_listed":120,"min_adv":1e8,
        "liquidation_days":5,"notice_file":"corporate_notices.csv","market_beta_fixed":1}
    (output/"strategy.json").write_text(json.dumps(parameters,ensure_ascii=False,indent=2))
    print(output.name,json.dumps({k:result.metrics[k] for k in ["total_return","sharpe_zero_rf","max_drawdown","end_positions"]}),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=BASE/"research/assignment/followup_results")
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic()
    data,coverage,hashes=load_data()
    coverage.to_csv(out/"status_coverage.csv",index_label="date")
    pd.DataFrame(hashes).to_csv(out/"status_file_hashes.csv",index=False)
    records=[];results={}
    cases=[("train_deviation20",20,.0005,False,True),
        ("selected",20,.0005,False,False),("slip_0",20,0,False,False),
        ("slip_15bp",20,.0015,False,False),("window60",60,.0005,False,False),
        ("equal_weight_liquid",20,.0005,True,False)]
    for label,window,slip,eq,train in cases:
        result=run_case(data,out/label,window,slip,eq,train)
        records.append({"experiment":label,**result.metrics});results[label]=result
    pd.DataFrame(records).to_csv(out/"experiments.csv",index=False)
    comp=benchmark_series(results["selected"].equity.index)
    for label in ["selected","window60","equal_weight_liquid"]:
        comp["strategy" if label=="selected" else label]=results[label].equity.equity/1e9
    comp.to_csv(out/"comparison.csv",index_label="date")
    comparisons=[]
    for name in ["strategy","csi300","sse","window60","equal_weight_liquid"]:
        nav=comp[name];r=nav.pct_change(fill_method=None);r.iloc[0]=nav.iloc[0]-1
        comparisons.append({"name":name,"total_return":float(nav.iloc[-1]-1),
            "annualized_return":float(nav.iloc[-1]**(252/len(nav))-1),
            "max_drawdown":float((nav/nav.cummax().clip(lower=1)-1).min()),
            "sharpe_zero_rf":float(r.mean()/r.std(ddof=1)*np.sqrt(252))})
    pd.DataFrame(comparisons).to_csv(out/"comparison_metrics.csv",index=False)
    source=["backtest/engine.py","backtest/data.py","backtest/result.py","backtest/market_rules.py",
        "backtest/market_deviation_strategy.py","research/assignment/run.py","research/assignment/run_followup.py",
        "research/assignment/FOLLOWUP_PROTOCOL.md","tests/test_market_deviation.py"]
    manifest={"created_at_utc":datetime.now(timezone.utc).isoformat(),"source_code":{f:digest(BASE/f)for f in source},
        "raw_inputs":{f:digest(BASE/"feathers"/(f+".feather"))for f in ["open","close","cumadj","amt"]},
        "external_inputs":{f:digest(DATA/f)for f in ["corporate_notices.csv","csi300.csv","sse.csv","data_crosschecks.json"]},
        "config":asdict(results["selected"].config),"default_window":20,
        "exploratory_after_failed_round1":True,"prior_results":"research/assignment/results",
        "runtime_seconds":time.monotonic()-started}
    (out/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"]="SimHei";plt.rcParams["axes.unicode_minus"]=False
    fig,axes=plt.subplots(2,1,figsize=(12,8),sharex=True)
    for name,label in [("strategy","20日市场偏离波动选股"),("csi300","沪深300价格指数"),("sse","上证综指"),("equal_weight_liquid","同池等权账户")]:
        nav=comp[name];axes[0].plot(nav.index,nav,label=label);axes[1].plot(nav.index,nav/nav.cummax().clip(lower=1)-1)
    axes[0].set_title("10亿元、历史非ST全市场选股：2025-01-02至2026-09-30")
    axes[0].set_ylabel("净值");axes[1].set_ylabel("回撤");axes[0].legend()
    for ax in axes:ax.grid(alpha=.3)
    fig.tight_layout();fig.savefig(out/"comparison.png",dpi=160);plt.close(fig)
    print("completed",time.monotonic()-started,flush=True)


if __name__=="__main__":main()
