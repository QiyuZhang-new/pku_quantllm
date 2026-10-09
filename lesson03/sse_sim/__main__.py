"""命令行演示与数据回放入口。"""
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import time
import pyarrow.parquet as pq
from .rules import Rules, Phase, Quote, cents, parse_time, format_time, phase_at
from .matching import MatchingEngine
from .exchange import Exchange
from .broker import Account, PaperBroker, Position
from .replay import instruments_from_snapshots, merged_events, quote_from_snapshot, HistoricalBook
from .report import write_report
from .plan import OrderPlan


ROOT = Path(__file__).resolve().parents[1]


def matching_showcase(instrument):
    p, lot = instrument.previous_close, instrument.lot
    continuous = Exchange(instrument)
    continuous.submit("卖方先到", "SELL", p+1, lot, parse_time("09:30:00"), sellable=lot)
    continuous.submit("卖方后到", "SELL", p+1, lot, parse_time("09:30:00"), sellable=lot)
    trades = continuous.submit("买方吃单", "BUY", p+2, 2*lot, parse_time("09:30:00"), star_permission=True)
    auction = Exchange(instrument)
    auction.submit("集合买单", "BUY", p+2, lot, parse_time("09:16:00"), star_permission=True)
    auction.submit("集合卖单", "SELL", p-2, lot, parse_time("09:17:00"), sellable=lot)
    auction.advance(parse_time("09:25:00"))
    call = auction.trades
    rules = Rules()
    checks = {}
    for title, now, side, price, quantity in [
        ("午间拒单", parse_time("12:00:00"), "BUY", p, lot),
        ("涨停外拒单", parse_time("09:16:00"), "BUY", instrument.limits[1]+1, lot),
        ("正常限价申报", parse_time("09:30:00"), "BUY", p, lot),
    ]:
        try:
            rules.validate(instrument, side, price, quantity, now, Quote([], []), star_permission=True)
            checks[title] = "通过"
        except ValueError as exc:
            checks[title] = str(exc)
    return {"label": "以数据前收盘价为基准的合成申报，不是历史成交重撮合",
            "continuous": [asdict(x) for x in trades], "opening_call": [asdict(x) for x in call],
            "rules": checks}


class Demonstration:
    def __init__(self):
        self.started = set()
        self.t1_checked = set()

    def on_snapshot(self, broker, symbol, quote):
        if phase_at(quote.time) != Phase.CONTINUOUS or not quote.bids or not quote.asks:
            return
        instrument = broker.instruments[symbol]
        lot = instrument.lot
        if symbol not in self.started:
            self.started.add(symbol)
            now, ask, bid = quote.time, quote.asks[0][0], quote.bids[0][0]
            broker.log("DEMO", now, symbol=symbol, purpose="有效买卖、无效数量、涨跌停、冻结及撤单")
            broker.submit(symbol, "BUY", ask, lot-1, now)
            broker.submit(symbol, "BUY", instrument.limits[1]+1, lot, now)
            broker.submit(symbol, "BUY", min(ask+2, instrument.limits[1]), lot, now)
            position = broker.account.position(symbol)
            if position.sellable >= lot:
                broker.submit(symbol, "SELL", max(bid-2, instrument.limits[0]), lot, now)
            passive = broker.submit(symbol, "BUY", max(instrument.limits[0], bid-5), lot, now)
            if passive:
                broker.cancel(passive, now)
        position = broker.account.position(symbol)
        if position.today and symbol not in self.t1_checked:
            self.t1_checked.add(symbol)
            broker.log("DEMO", quote.time, symbol=symbol, purpose="尝试卖出昨日库存加今日买入；应触发T+1拒单")
            broker.submit(symbol, "SELL", quote.bids[0][0], position.yesterday+position.today, quote.time)


def replay(args):
    data = args.data_dir.resolve()
    symbols = args.symbols
    if args.all_symbols:
        batch = next(pq.ParquetFile(data/"snp_20260923.parquet").iter_batches(batch_size=4096, columns=["Symbol"]))
        symbols = sorted(set(batch.column(0).to_pylist()))
    if len(set(symbols)) != len(symbols):
        raise ValueError("证券列表有重复")
    instruments = instruments_from_snapshots(data, symbols)
    end = parse_time(args.end)
    if end < parse_time("09:30:00"):
        raise ValueError("演示截止时间至少为09:30")
    if args.initial_shares < 0:
        raise ValueError("初始持仓不能为负")
    if args.latency_ms < 0:
        raise ValueError("延迟不能为负")
    initial_cash = cents(args.cash)
    plan = OrderPlan.load(args.plan, symbols) if args.plan else OrderPlan([], symbols)
    output = args.output or ROOT/"runs"/datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True, exist_ok=False)
    account = Account(cash=initial_cash, star_permission=not args.no_star_permission,
                      positions={s: Position(yesterday=args.initial_shares) for s in symbols})
    broker = PaperBroker(instruments, account, latency_ms=args.latency_ms)
    books = {s: HistoricalBook(s) for s in symbols}
    demo = Demonstration() if args.strategy == "demonstration" and not args.plan else None
    events, counts, alignment, sequence_diagnostics = 0, Counter(), Counter(), Counter()
    last_sequence = {}
    frames = []
    initial_nav = account.cash+sum(instruments[s].previous_close*args.initial_shares for s in symbols)
    began = time.monotonic()
    for event in merged_events(data, symbols, end):
        now, row, kind = event["time_ms"], event["data"], event["kind"]
        plan.run_until(broker, now)
        broker.advance(now)
        events += 1
        counts[kind] += 1
        book = books[row["Symbol"]]
        if kind != "snp":
            # 盘后成交序列与竞价逐笔不是同一消息类别，不纳入竞价订单簿。
            if event["original_time_ms"] > parse_time("15:00:00"):
                counts["post_market_ticks"] += 1
                continue
            channel, sequence = row["Channel"], row["BizIndex"]
            if sequence <= last_sequence.get(channel, -1):
                sequence_diagnostics["duplicate_or_reversed"] += 1
                continue
            last_sequence[channel] = sequence
            if kind == "ord":
                book.on_order(row)
            else:
                book.on_trade(row)
        else:
            quote = quote_from_snapshot(row)
            if quote.bids and quote.asks and quote.bids[0][0] > quote.asks[0][0] and phase_at(now) == Phase.CONTINUOUS:
                counts["crossed_continuous_snapshot"] += 1
                broker.log("INVALID_QUOTE", now, symbol=row["Symbol"], reason="连续竞价快照买卖交叉，跳过模拟执行")
                continue
            broker.on_quote(row["Symbol"], quote)
            if demo:
                demo.on_snapshot(broker, row["Symbol"], quote)
            bids, asks = book.levels("BUY"), book.levels("SELL")
            comparable = phase_at(now) == Phase.CONTINUOUS and bool(quote.bids and quote.asks and bids and asks)
            matched = (bids[0][0] == quote.bids[0][0] and asks[0][0] == quote.asks[0][0]) if comparable else None
            if comparable:
                alignment["compared"] += 1
                alignment["matched"] += int(matched)
            if now >= parse_time("09:30:00"):
                position = account.position(row["Symbol"])
                frames.append({"symbol": row["Symbol"], "time_ms": now, "price": quote.last,
                               "bids": quote.bids, "asks": quote.asks, "historical_bids": bids, "historical_asks": asks,
                               "best_price_match": matched, "cash_available": account.available_cash,
                               "sellable": position.sellable, "today": position.today})
        if events % 250000 == 0:
            print(f"已回放 {events:,} 条事件，市场时间 {format_time(now)}", flush=True)
    plan.run_until(broker, end)
    broker.advance(end)
    for book in books.values():
        book.check()
    broker.assert_invariants()
    for trade in broker.trades:
        assert trade["time_ms"] >= broker.orders[trade["order_id"]].eligible
    final_nav = account.cash+sum((broker.quotes.get(s, Quote([], [], instruments[s].previous_close)).last or instruments[s].previous_close)
                               * (p.yesterday+p.today) for s, p in account.positions.items())
    fee_total = sum(t["fee_cents"] for t in broker.trades)
    summary = {"date": "2026-09-23", "symbols": symbols, "end_ms": end,
               "events_processed": events, "event_counts": dict(counts), "simulated_fills": len(broker.trades),
               "order_event_counts": dict(Counter(e["event"] for e in broker.events)),
               "fees_cents": fee_total, "initial_nav_cents": initial_nav, "final_nav_cents": final_nav,
               "marked_pnl_cents": final_nav-initial_nav, "account": broker.state(), "plan": plan.state(),
               "book_diagnostics": {str(s): dict(b.stats) for s, b in books.items()},
               "snapshot_alignment": dict(alignment), "sequence_diagnostics": dict(sequence_diagnostics),
               "matching_showcase": matching_showcase(instruments[symbols[0]]),
               "elapsed_seconds": round(time.monotonic()-began, 3),
               "verification": {"book_quantity_conservation": True, "account_conservation": True,
                                "no_fill_before_eligible_time": True},
               "limit_prices": {str(s): {"board": i.board, "previous_close": i.previous_close, "limits": i.limits} for s, i in instruments.items()},
               "fee_assumptions": {k: str(v) for k, v in asdict(broker.fees).items()},
               "limitations": ["课堂数据的常规限价股假设；缺少IPO/停牌/除权静态标志",
                               "跨通道和同毫秒快照没有完整接收时钟，最优价对照只是诊断",
                               "快照成交是无市场冲击的近似，不证明真实可成交或策略有效",
                               "历史模拟仅连续竞价，独立内核支持开收盘集合竞价",
                               "市价申报、无涨跌幅股、大宗交易及盘后订单执行未实现并拒绝",
                               "当天买入锁定至后续真实交易日；不自动编造翌日行情",
                               "提前结束回放时保留活动委托及冻结资源，15:00才进行当日失效"]}
    for name, content in [("summary.json", summary), ("frames.json", frames), ("orders.json", broker.state()["orders"]), ("trades.json", broker.trades)]:
        (output/name).write_text(json.dumps(content, ensure_ascii=False, indent=2))
    with (output/"events.jsonl").open("w") as f:
        for event in broker.events:
            f.write(json.dumps(event, ensure_ascii=False)+"\n")
    input_manifest = []
    for kind in ["ord", "exe", "snp"]:
        source = data/f"{kind}_20260923.parquet"
        h = hashlib.sha256()
        with source.open("rb") as f:
            for block in iter(lambda: f.read(1024*1024), b""):
                h.update(block)
        input_manifest.append({"file": source.name, "sha256": h.hexdigest(), "rows": pq.ParquetFile(source).metadata.num_rows})
    (output/"inputs.json").write_text(json.dumps(input_manifest, indent=2))
    write_report(output/"report.html", summary, frames, broker.events, broker.trades)
    print(json.dumps({"output": str(output.resolve()), "events": events, "simulated_fills": len(broker.trades),
                      "fees_yuan": fee_total/100, "cash_available_yuan": account.available_cash/100,
                      "verification": summary["verification"], "snapshot_alignment": dict(alignment)}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="上交所规则教学模拟交易系统")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="回放真实数据并演示模拟委托/成交/撤单/T+1")
    demo.add_argument("--data-dir", type=Path, default=ROOT/"sse50_20260923")
    demo.add_argument("--symbols", nargs="+", type=int, default=[601600, 600036, 688981])
    demo.add_argument("--all-symbols", action="store_true")
    demo.add_argument("--end", default="09:35:00")
    demo.add_argument("--cash", default="1000000")
    demo.add_argument("--initial-shares", type=int, default=1000)
    demo.add_argument("--latency-ms", type=int, default=100)
    demo.add_argument("--no-star-permission", action="store_true")
    demo.add_argument("--strategy", choices=["demonstration", "none"], default="demonstration")
    demo.add_argument("--plan", type=Path, help="按时间输入自定义委托/撤单 JSON 计划")
    demo.add_argument("--output", type=Path)
    args = parser.parse_args()
    replay(args)


if __name__ == "__main__":
    main()
