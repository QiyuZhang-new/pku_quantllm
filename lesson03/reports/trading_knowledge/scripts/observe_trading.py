"""分析已保存的回放，并按可见快照做固定时点的成交对照。

从 lesson03 运行：.venv/bin/python scripts/observe_trading.py
这些实验衡量模拟模型的行为，不进行策略选择或实盘收益推断。
"""
import argparse
from bisect import bisect_left
from collections import Counter
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sse_sim.broker import Account, PaperBroker, Position
from sse_sim.__main__ import Demonstration
from sse_sim.rules import Instrument, Phase, Quote, format_time, parse_time, phase_at


def simulate(frames, instruments, latency, mode='demonstration'):
    initial_shares = 1000 if mode == 'demonstration' else 10000
    broker = PaperBroker(instruments, Account(cash=100000000, star_permission=True,
                         positions={s: Position(yesterday=initial_shares) for s in instruments}), latency_ms=latency)
    demo = Demonstration()
    schedule = [parse_time(t) for t in ['09:30:00','10:00:00','10:30:00','11:00:00',
                '13:00:00','13:30:00','14:00:00','14:30:00','14:56:00']]
    observed = set()
    cancel_queue = []
    probes = []
    for f in frames:
        now, symbol = f['time_ms'], f['symbol']
        for oid, due in sorted(cancel_queue, key=lambda x: x[1]):
            if due <= now and broker.orders[oid].status in broker.active_states:
                broker.cancel(oid, due)
        cancel_queue = [(oid,due) for oid,due in cancel_queue if due > now]
        quote = Quote([tuple(x) for x in f['bids']], [tuple(x) for x in f['asks']], f['price'], now)
        broker.on_quote(symbol, quote)
        if mode == 'demonstration':
            demo.on_snapshot(broker, symbol, quote)
            continue
        if phase_at(now) != Phase.CONTINUOUS or not quote.bids or not quote.asks:
            continue
        window = next((t for t in reversed(schedule) if t <= now), None)
        if window is None or now-window > 3000 or (symbol,window) in observed:
            continue
        observed.add((symbol,window))
        buy, sell = (quote.bids[0][0], quote.asks[0][0]) if mode == 'passive' else (quote.asks[0][0], quote.bids[0][0])
        if mode == 'aggressive':
            buy += 2
            sell -= 2
        low, high = instruments[symbol].limits
        for side, price in [('BUY',min(buy,high)), ('SELL',max(sell,low))]:
            oid = broker.submit(symbol, side, price, instruments[symbol].lot, now)
            probes.append({'window':format_time(window), 'symbol':symbol, 'side':side,
                           'order_id':oid, 'price_cents':price, 'submitted_ms':now})
            if oid:
                cancel_queue.append((oid,now+30000))
    broker.advance(parse_time('15:00:00'))
    broker.assert_invariants()
    close = {s: next(f['price'] for f in reversed(frames) if f['symbol']==s and f['price']) for s in instruments}
    baseline = 100000000 + sum(initial_shares*close[s] for s in instruments)
    actual = broker.account.cash + sum((p.yesterday+p.today)*close[s] for s,p in broker.account.positions.items())
    fees = sum(t['fee_cents'] for t in broker.trades)
    position_delta = {s: broker.account.position(s).yesterday + broker.account.position(s).today - initial_shares
                      for s in instruments}
    markouts = []
    symbol_frames = {s: [f for f in frames if f['symbol'] == s] for s in instruments}
    symbol_times = {s: [f['time_ms'] for f in fs] for s, fs in symbol_frames.items()}
    for trade in broker.trades:
        index = bisect_left(symbol_times[trade['symbol']], trade['time_ms'] + 30000)
        fs = symbol_frames[trade['symbol']]
        if index == len(fs):
            continue
        f = fs[index]
        if not f['bids'] or not f['asks'] or f['time_ms'] - trade['time_ms'] > 33000 or phase_at(f['time_ms']) != Phase.CONTINUOUS:
            continue
        future_mid = (f['bids'][0][0] + f['asks'][0][0]) / 2
        signed_ticks = (future_mid - trade['price_cents']) * (1 if trade['side'] == 'BUY' else -1)
        markouts.append({'trade_id': trade['trade_id'], 'quantity': trade['quantity'], 'signed_ticks': signed_ticks})
    filled = [o for o in broker.orders.values() if o.filled]
    delays = [next(t['time_ms'] for t in broker.trades if t['order_id']==o.order_id)-o.submitted for o in filled]
    for p in probes:
        oid=p['order_id']
        order=broker.orders.get(oid)
        p.update(status=order.status if order else 'REJECTED', filled=order.filled if order else 0,
                 quantity=order.quantity if order else instruments[p['symbol']].lot,
                 fills=[t for t in broker.trades if t['order_id']==oid])
    return {'mode':mode,'latency_ms':latency,'submitted':len(broker.orders),
            'rejected':sum(e['event']=='REJECTED' for e in broker.events),
            'orders_with_fill':len(filled),'fully_filled':sum(o.status=='FILLED' for o in broker.orders.values()),
            'statuses':dict(Counter(o.status for o in broker.orders.values())),
            'fills':len(broker.trades),'first_fill_median_delay_ms':statistics.median(delays) if delays else None,
            'fees_cents':fees,'incremental_nav_cents':actual-baseline,
            'incremental_gross_nav_cents':actual-baseline+fees,
            'position_delta_shares':position_delta,
            'markout_30s':{'fills_compared':len(markouts),
                'negative_fills':sum(x['signed_ticks']<0 for x in markouts),
                'quantity_weighted_ticks':sum(x['signed_ticks']*x['quantity'] for x in markouts)/sum(x['quantity'] for x in markouts) if markouts else None},
            'orders':[asdict(o) for o in broker.orders.values()], 'trades':broker.trades,'probes':probes}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,default=Path('runs/runtime_full_day'))
    parser.add_argument('--output',type=Path,default=Path('runs/trade_observations'))
    args=parser.parse_args()
    summary=json.loads((args.run/'summary.json').read_text())
    frames=json.loads((args.run/'frames.json').read_text())
    trades=json.loads((args.run/'trades.json').read_text())
    orders=json.loads((args.run/'orders.json').read_text())
    events=[json.loads(x) for x in (args.run/'events.jsonl').read_text().splitlines()]
    instruments={int(s):Instrument(int(s),v['previous_close'],v['board']) for s,v in summary['limit_prices'].items()}
    assert frames and all(a['time_ms']<=b['time_ms'] for a,b in zip(frames,frames[1:]))
    assert summary['end_ms']==parse_time('15:00:00')
    experiment_runs=[simulate(frames,instruments,t) for t in [0,100,1000,3000,5000,10000]]
    base=next(r for r in experiment_runs if r['latency_ms']==100)
    assert base['trades']==trades, '快照实验必须复现原回放成交'
    assert all(r['trades'] == trades for r in experiment_runs if r['latency_ms'] <= 3000), '低延迟结果相同的发现必须经成交明细验证'
    probe_runs=[simulate(frames,instruments,100,mode) for mode in ['touch','aggressive','passive']]
    baseline=[]
    for symbol in summary['symbols']:
        fs=[f for f in frames if f['symbol']==symbol]
        continuous=[f for f in fs if phase_at(f['time_ms'])==Phase.CONTINUOUS and f['bids'] and f['asks']]
        ts=[t for t in trades if t['symbol']==symbol]
        gross=sum(t['price_cents']*t['quantity']*(1 if t['side']=='SELL' else -1) for t in ts)
        fees=sum(t['fee_cents'] for t in ts)
        original_positions=summary['account']['positions'][str(symbol)]
        holding=original_positions['yesterday']+original_positions['today']
        net_qty=sum(t['quantity']*(1 if t['side']=='BUY' else -1) for t in ts)
        initial_shares=holding-net_qty
        inventory=(fs[-1]['price']-instruments[symbol].previous_close)*initial_shares
        for t in ts:
            o=next(o for o in orders if o['order_id']==t['order_id'])
            ack=next(e for e in events if e['event']=='ACCEPTED' and e['order_id']==o['order_id'])
            t.update(submitted_ms=o['submitted'],accepted_ms=ack['time_ms'],
                     fill_delay_ms=t['time_ms']-o['submitted'])
        gaps=[b['time_ms']-a['time_ms'] for a,b in zip(continuous,continuous[1:]) if 0<b['time_ms']-a['time_ms']<60000]
        baseline.append({'symbol':symbol,'previous_close_cents':instruments[symbol].previous_close,
            'close_cents':fs[-1]['price'],'initial_shares':initial_shares,'net_trade_shares':net_qty,
            'gross_cashflow_cents':gross,'fees_cents':fees,'net_cashflow_cents':gross-fees,
            'initial_inventory_pnl_cents':inventory,'snapshot_median_gap_ms':statistics.median(gaps),
            'trades':ts})
    assert sum(b['initial_inventory_pnl_cents']+b['net_cashflow_cents']+b['net_trade_shares']*b['close_cents'] for b in baseline)==summary['marked_pnl_cents']
    alignment=[]
    for hour in [9,10,11,13,14]:
        fs=[f for f in frames if f['time_ms']//3600000==hour and f['best_price_match'] is not None]
        alignment.append({'hour':hour,'compared':len(fs),'matched':sum(f['best_price_match'] for f in fs)})
    result={'source_run':str(args.run),'source_sha256':{name:hashlib.sha256((args.run/name).read_bytes()).hexdigest() for name in ['summary.json','frames.json','trades.json']},
        'baseline':baseline,'reject_reasons':dict(Counter(e['reason'] for e in events if e['event']=='REJECTED')),
        'alignment_by_hour':alignment,'latency_experiments':experiment_runs,'price_probes':probe_runs,
        'limitations':['仅一个交易日、三只证券；固定时点探针不构成策略推荐',
            '所有实验使用同一快照深度成交模型，未估算真实队列和市场冲击',
            '成交接受时刻受快照采样影响；不同时间快照可能重用历史挂单流动性',
            '费用为当前 Fees 教学配置；实验未调参择优',
            '相对不交易的期末净值包含未平仓头寸按收盘报价计价']}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'observations.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    with (args.output/'probe_orders.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['mode','window','symbol','side','price_cents','status','filled','quantity'])
        writer.writeheader()
        for r in probe_runs:
            for p in r['probes']:
                writer.writerow({'mode':r['mode'],**{k:p[k] for k in writer.fieldnames if k!='mode'}})
    print(json.dumps({'baseline':baseline,'latency':[ {k:v for k,v in r.items() if k not in ['orders','trades','probes']} for r in experiment_runs],
                     'probes':[{k:v for k,v in r.items() if k not in ['orders','trades','probes']} for r in probe_runs],
                     'alignment':alignment,'output':str(args.output)},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
