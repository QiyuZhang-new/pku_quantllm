"""固定一轮 16 因子；仅用 train/valid 选择，test 一次性评估。"""
import hashlib, json, pathlib, datetime, gzip
import numpy as np
import pandas as pd
ROOT=pathlib.Path(__file__).resolve().parent

def features(d):
    c,o,h,l,v=[d[x] for x in ['close','open','high','low','volume']]
    prev=c.shift(1); gap=o/prev-1
    return pd.DataFrame({
      'gap':gap,'gap_neg':-gap,'gap_abs_signed':gap*gap.abs(),
      'gap_vol20':gap/c.pct_change(fill_method=None).rolling(20).std().shift(1),
      'gap_range20':gap/((h-l)/c).rolling(20).mean().shift(1),
      'gap_mom5':gap*(prev/c.shift(6)-1),'gap_mom20':gap*(prev/c.shift(21)-1),
      'gap_volume':gap*v/v.rolling(20).mean().shift(1),
      'gap_intraday':gap*(c/o-1),'gap_closepos':gap*((c-l)/(h-l)-.5),
      'gap_mean3':gap.rolling(3).mean(),'gap_mean5':gap.rolling(5).mean(),
      'gap_mean10':gap.rolling(10).mean(),'gap_surprise':gap-gap.rolling(20).mean().shift(1),
      'gap_trend':gap*(c/c.rolling(20).mean().shift(1)-1),
      'gap_breakout':gap*(c/h.rolling(20).max().shift(1)-1)
    },index=d.index).replace([np.inf,-np.inf],np.nan)

def block_ci(x):
    x=np.asarray(x); n=len(x); rng=np.random.default_rng(42)
    starts=rng.integers(0,n,size=(1000,(n+9)//10))
    ix=(starts[:,:,None]+np.arange(10))%n
    means=x[ix.reshape(1000,-1)[:,:n]].mean(axis=1)
    return np.quantile(means,[.025,.975]).tolist()

def metrics(x):
    x=np.asarray(x); wealth=np.cumprod(1+x)
    peaks=np.maximum.accumulate(np.r_[1.,wealth])[1:]
    return {'days':len(x),'total_return':float(wealth[-1]-1),'annual_return':float(wealth[-1]**(252/len(x))-1),'max_drawdown':float(np.min(wealth/peaks-1)),'daily_mean_ci95':block_ci(x)}

def main():
    protocol=json.loads((ROOT/'protocol.json').read_text())
    frozen=json.loads((ROOT.parent/'evidence/protocol_frozen.json').read_text())
    assert hashlib.sha256((ROOT/'protocol.json').read_bytes()).hexdigest()==frozen['sha256']
    if not (ROOT/'data/ohlcv.csv').exists():
        (ROOT/'data/ohlcv.csv').write_bytes(gzip.decompress((ROOT/'data/ohlcv.csv.gz').read_bytes()))
    manifest=json.loads((ROOT/'data/manifest.json').read_text())
    assert hashlib.sha256((ROOT/'data/ohlcv.csv').read_bytes()).hexdigest()==manifest['sha256']
    out=ROOT/'results'
    if (out/'summary.json').exists(): raise SystemExit('结果已存在；停止规则禁止重复打开 test。')
    # 在任何结果计算前同时记录协议、数据指纹。
    out.mkdir(exist_ok=True)
    (out/'freeze.json').write_text(json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'protocol_sha256':frozen['sha256'],'data_sha256':manifest['sha256'],'universe':protocol['universe']},indent=2))
    data=pd.read_csv(ROOT/'data/ohlcv.csv',parse_dates=['date'])
    assert sorted(data.symbol.unique())==sorted(protocol['universe'])
    parts=[]
    for symbol,d in data.groupby('symbol'):
        d=d.sort_values('date').set_index('date'); f=features(d)
        f['label']=d.close.shift(-1)/d.open.shift(-1)-1
        f['trade_date']=pd.Series(d.index,index=d.index).shift(-1)
        f['tradable']=(d.volume.shift(-1)>0)&(d.open.shift(-1)>0)&(d.close.shift(-1)>0)&(d.close*d.volume*.01>=100000/3)
        f['symbol']=symbol; parts.append(f.reset_index())
    frame=pd.concat(parts).dropna(subset=list(protocol['formulas'])+['label','trade_date'])
    columns=list(protocol['formulas']); scores=[]; ics={}
    for name in columns:
        vals={}
        for split in ['train','valid']:
            a,b=protocol[split]; z=frame[(frame.date>=a)&(frame.trade_date<=b)]
            ic=z.groupby('date')[[name,'label']].apply(lambda g:g[name].rank().corr(g.label.rank()))
            vals[split+'_ic']=float(ic.mean()); ics[(name,split)]=ic
        scores.append({'factor':name,**vals})
    leaderboard=pd.DataFrame(scores)
    candidates=leaderboard[leaderboard.train_ic>0].sort_values(['valid_ic','factor'],ascending=[False,True])
    if candidates.empty: raise RuntimeError('无正向训练 IC，协议要求停止。')
    chosen=candidates.iloc[0].factor
    (out/'selection.json').write_text(json.dumps({'factor':chosen,'selection_time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'test_not_opened':True},indent=2))
    # 候选冻结后才对 test 执行一次评估；全部因子 test IC 仅用于披露，不重新筛选。
    a,b=protocol['test']; test=frame[(frame.date>=a)&(frame.trade_date<=b)]
    leaderboard['test_ic']=[float(test.groupby('date')[[n,'label']].apply(lambda g:g[n].rank().corr(g.label.rank())).mean()) for n in columns]
    leaderboard.to_csv(out/'all_candidates.csv',index=False)
    portfolios=[]
    for date,g in test.groupby('date'):
        g=g[g.tradable].sort_values([chosen,'symbol'],ascending=[False,True]).head(3)
        # 每只占固定 1/3，缺失标的留现金；每日开平无隔夜持仓。
        gross=float(g.label.sum()/3); exposure=len(g)/3
        portfolios.append({'signal_date':date,'trade_date':g.trade_date.iloc[0] if len(g) else date,'symbols':','.join(g.symbol),'gross':gross,'net':gross-.0012*exposure,'stress_net':gross-.0042*exposure,'turnover':2*exposure})
    pnl=pd.DataFrame(portfolios); pnl.to_csv(out/'daily_pnl.csv',index=False)
    summary={'selected':chosen,'data_rows':len(data),'test_start':str(pnl.trade_date.min()),'test_end':str(pnl.trade_date.max()),'average_turnover':float(pnl.turnover.mean()),'gross':metrics(pnl.gross),'net':metrics(pnl.net),'stress_net':metrics(pnl.stress_net),'selected_test_ic':float(leaderboard.set_index('factor').loc[chosen,'test_ic']),'limitations':['历史复权数据存在修订风险','12只存续ETF，存在存续及股票池选择偏差','日线无法保证开盘成交；滑点仅情景假设','不是A股：不适用印花税/涨跌停/T+1；不做融券','未执行未来样本；一次打开历史test仍非前瞻验证','使用pandas独立实现，未声称运行vnpy.alpha框架']}
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    print(json.dumps(summary,indent=2,ensure_ascii=False))
if __name__=='__main__': main()
