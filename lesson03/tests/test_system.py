"""交易规则、账户守恒、FIFO、集合竞价及数据回放顺序的行为测试。"""
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
import pyarrow as pa
import pyarrow.parquet as pq

from sse_sim.rules import Instrument, Quote, Rules, Reject, cents, parse_time, feed_time, phase_at, Phase, cancellation_allowed
from sse_sim.matching import MatchingEngine
from sse_sim.exchange import Exchange
from sse_sim.broker import PaperBroker, Account, Position, Fees
from sse_sim.replay import HistoricalBook, merged_events, REQUIRED

T = parse_time("09:30:00")


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.main = Instrument(600036, 1000)
        self.star = Instrument(688981, 1000, "STAR")
        self.rules = Rules()

    def valid(self, instrument=None, side="BUY", price=1000, qty=100, now=T, **kwargs):
        return self.rules.validate(instrument or self.main, side, price, qty, now, **kwargs)

    def test_float32_rounding_only_allowed_in_data_adapter(self):
        self.assertEqual(cents(9.270000457763672, feed=True), 927)
        with self.assertRaises(Reject): cents(9.270000457763672)
        for value in [0, -1, "nan", "inf", "9.271"]:
            with self.assertRaises(Reject): cents(value)

    def test_time_encoding(self):
        self.assertEqual(feed_time(93000120), parse_time("09:30:00.120"))
        with self.assertRaises(ValueError): feed_time(96000000)

    def test_daily_limits_round_half_up(self):
        self.assertEqual(Instrument(600036, 1005).limits, (905, 1106))
        self.assertEqual(self.star.limits, (800, 1200))

    def test_buy_main_lot(self):
        for quantity in [1, 99, 101, 1000001, 100.0, True]:
            with self.assertRaises(Reject): self.valid(qty=quantity)
        self.valid(qty=1000000)

    def test_star_lot_increment_and_permission(self):
        self.valid(self.star, qty=201, star_permission=True)
        with self.assertRaises(Reject): self.valid(self.star, qty=199, star_permission=True)
        with self.assertRaises(Reject): self.valid(self.star, qty=100001, star_permission=True)
        with self.assertRaises(Reject): self.valid(self.star, qty=200)

    def test_sell_odd_balance(self):
        self.valid(side="SELL", qty=50, sellable=150)
        self.valid(side="SELL", qty=150, sellable=250)
        with self.assertRaises(Reject): self.valid(side="SELL", qty=30, sellable=150)
        self.valid(self.star, side="SELL", qty=199, sellable=199)
        with self.assertRaises(Reject): self.valid(self.star, side="SELL", qty=99, sellable=199)

    def test_t1_and_frozen_available_stock(self):
        with self.assertRaises(Reject): self.valid(side="SELL", qty=100, sellable=0)
        with self.assertRaises(Reject): self.valid(side="SELL", qty=200, sellable=100)

    def test_continuous_price_cage_main_ten_ticks_vs_star(self):
        low = Instrument(600036, 100)
        self.valid(low, price=110)  # 2%=0.02元，主板可取十跳0.10元。
        with self.assertRaises(Reject): self.valid(Instrument(688981, 100, "STAR"), price=110, qty=200, star_permission=True)
        self.valid(price=1020)
        with self.assertRaises(Reject): self.valid(price=1021)
        self.valid(side="SELL", price=980, sellable=100)
        with self.assertRaises(Reject): self.valid(side="SELL", price=979, sellable=100)

    def test_reference_price_fallback(self):
        self.valid(price=1030, quote=Quote([(1000, 100)], [(1010, 100)]))
        with self.assertRaises(Reject): self.valid(price=1031, quote=Quote([(1000, 100)], [(1010, 100)]))
        self.valid(price=1020, quote=Quote([(1000, 100)], []))

    def test_auction_no_price_cage_but_daily_limit(self):
        self.valid(price=1100, now=parse_time("09:16:00"))
        with self.assertRaises(Reject): self.valid(price=1101, now=parse_time("09:16:00"))

    def test_no_orders_during_break_post_or_after_close(self):
        for clock in ["09:14:59", "09:25:00", "11:30:00.001", "12:00:00", "15:00:00", "15:10:00"]:
            with self.assertRaises(Reject): self.valid(now=parse_time(clock))

    def test_close_and_no_cancel_boundaries(self):
        self.assertEqual(phase_at(parse_time("14:57:00")), Phase.CLOSE_CALL)
        self.assertTrue(cancellation_allowed(parse_time("09:19:59.999")))
        self.assertFalse(cancellation_allowed(parse_time("09:20:00")))
        self.assertFalse(cancellation_allowed(parse_time("14:57:00")))

    def test_unsupported_instrument_and_market_order_fail_closed(self):
        with self.assertRaises(Reject): self.valid(Instrument(600036, 1000, ordinary=False))
        with self.assertRaises(Reject): self.valid(Instrument(600036, 1000, suspended=True))
        with self.assertRaises(Reject): self.valid(order_type="MARKET")


class MatchingTests(unittest.TestCase):
    def test_price_first_then_fifo(self):
        book = MatchingEngine()
        book.add("expensive", "SELL", 1002, 100)
        book.add("first", "SELL", 1001, 100)
        book.add("second", "SELL", 1001, 100)
        fills = book.add("buy", "BUY", 1003, 250)
        self.assertEqual([(f.sell_id, f.price, f.quantity) for f in fills], [("first", 1001, 100), ("second", 1001, 100), ("expensive", 1002, 50)])
        self.assertEqual(book.orders["expensive"].remaining, 50)

    def test_sell_executes_at_resting_buy_price(self):
        book = MatchingEngine(); book.add("buyer", "BUY", 1000, 100)
        self.assertEqual(book.add("seller", "SELL", 990, 100)[0].price, 1000)

    def test_partial_fill_and_cancel(self):
        book = MatchingEngine(); book.add("seller", "SELL", 1000, 100)
        book.add("buyer", "BUY", 1000, 200)
        self.assertEqual(book.cancel("buyer"), 100)
        self.assertEqual(book.levels("BUY"), [])

    def test_auction_midpoint_and_one_price(self):
        book = MatchingEngine()
        book.add("buy", "BUY", 1010, 100, continuous=False)
        book.add("sell", "SELL", 990, 100, continuous=False)
        self.assertEqual(book.clearing(), (1000, 100))
        self.assertEqual([(f.price, f.quantity) for f in book.auction()], [(1000, 100)])

    def test_auction_all_better_orders_must_fill(self):
        book = MatchingEngine()
        book.add("buy", "BUY", 1010, 200, continuous=False)
        book.add("sell", "SELL", 990, 100, continuous=False)
        self.assertEqual(book.clearing(), (1010, 100))

    def test_auction_same_price_fifo(self):
        book = MatchingEngine()
        book.add("first", "BUY", 1000, 100, continuous=False)
        book.add("second", "BUY", 1000, 100, continuous=False)
        book.add("sell", "SELL", 1000, 150, continuous=False)
        self.assertEqual([(f.buy_id, f.quantity) for f in book.auction()], [("first", 100), ("second", 50)])

    def test_auction_no_cross(self):
        book = MatchingEngine(); book.add("b", "BUY", 990, 100, continuous=False); book.add("s", "SELL", 1010, 100, continuous=False)
        self.assertEqual(book.auction(), [])

    def test_exchange_open_call_timing_and_cancel_freeze(self):
        exchange = Exchange(Instrument(600036, 1000))
        exchange.submit("b", "BUY", 1010, 100, parse_time("09:16:00"))
        exchange.submit("s", "SELL", 990, 100, parse_time("09:17:00"), sellable=100)
        self.assertEqual(exchange.trades, [])
        with self.assertRaises(Reject): exchange.cancel("b", parse_time("09:20:00"))
        exchange.advance(parse_time("09:25:00"))
        self.assertEqual(exchange.trades[0].price, 1000)
        with self.assertRaises(Reject): exchange.advance(parse_time("09:24:00"))

    def test_exchange_closing_call_and_expiry(self):
        exchange = Exchange(Instrument(600036, 1000))
        exchange.submit("b", "BUY", 1010, 200, parse_time("14:57:00"))
        exchange.submit("s", "SELL", 990, 100, parse_time("14:58:00"), sellable=100)
        self.assertEqual(exchange.trades, [])
        with self.assertRaises(Reject): exchange.cancel("b", parse_time("14:59:00"))
        exchange.advance(parse_time("15:00:00"))
        self.assertEqual(len(exchange.trades), 1)
        self.assertEqual(exchange.expired, ["b"])

    def test_exchange_orders_carry_across_auction_and_lunch(self):
        exchange = Exchange(Instrument(600036, 1000))
        exchange.submit("b", "BUY", 990, 100, parse_time("09:16:00"))
        exchange.advance(parse_time("12:00:00"))
        self.assertEqual(exchange.book.orders["b"].remaining, 100)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = PaperBroker({600036: Instrument(600036, 1000)}, Account(cash=1000000))
        self.broker.on_quote(600036, Quote([(999, 1000)], [(1000, 1000)], 1000, T))

    def quote(self, t, quantity=1000):
        self.broker.on_quote(600036, Quote([(999, 1000)], [(1000, quantity)], 1000, t))

    def test_cash_freeze_prevents_double_spend(self):
        self.broker.account.cash = 100600
        one = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.assertIsNotNone(one)
        self.assertIsNone(self.broker.submit(600036, "BUY", 1000, 100, T))
        self.assertEqual(self.broker.account.frozen_cash, 100501)
        self.broker.cancel(one, T)
        self.assertEqual(self.broker.account.frozen_cash, 0)

    def test_no_fill_on_same_quote_or_before_latency(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T)
        self.quote(T+50)
        self.assertEqual(self.broker.trades, [])
        self.quote(T+100)
        self.assertEqual(self.broker.orders[oid].status, "FILLED")

    def test_newly_bought_stock_is_not_sellable(self):
        self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T+100)
        p = self.broker.account.position(600036)
        self.assertEqual((p.today, p.sellable), (100, 0))
        self.assertIsNone(self.broker.submit(600036, "SELL", 999, 100, T+100))

    def test_sell_freezes_yesterday_stock(self):
        self.broker.account.positions[600036] = Position(yesterday=100)
        one = self.broker.submit(600036, "SELL", 999, 100, T)
        self.assertIsNotNone(one)
        self.assertIsNone(self.broker.submit(600036, "SELL", 999, 100, T))
        self.broker.cancel(one, T)
        self.assertEqual(self.broker.account.position(600036).sellable, 100)

    def test_partial_fills_charge_minimum_only_once(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T+100, 50)
        self.quote(T+200, 50)
        self.assertEqual(self.broker.orders[oid].filled, 100)
        self.assertEqual(sum(t["fee_cents"] for t in self.broker.trades), 501)
        self.assertEqual(self.broker.account.cash, 899499)

    def test_cancel_partial_releases_exact_remainder(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T+100, 50)
        self.assertGreater(self.broker.account.frozen_cash, 0)
        self.assertTrue(self.broker.cancel(oid, T+100))
        self.assertEqual(self.broker.account.frozen_cash, 0)
        self.assertEqual(self.broker.account.position(600036).today, 50)

    def test_snapshot_depth_cannot_be_used_twice(self):
        self.broker.submit(600036, "BUY", 1000, 100, T)
        self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T+100, 100)
        self.assertEqual(sum(t["quantity"] for t in self.broker.trades), 100)
        self.quote(T+100, 100)
        self.assertEqual(sum(t["quantity"] for t in self.broker.trades), 100)

    def test_broker_same_price_time_priority(self):
        first = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T+100, 100)
        self.assertEqual(self.broker.trades[0]["order_id"], first)

    def test_broker_price_priority(self):
        first = self.broker.submit(600036, "BUY", 1000, 100, T)
        better = self.broker.submit(600036, "BUY", 1001, 100, T)
        self.quote(T+100, 100)
        self.assertEqual(self.broker.trades[0]["order_id"], better)
        self.assertEqual(self.broker.orders[first].filled, 0)

    def test_arrival_price_cage_revalidation(self):
        oid = self.broker.submit(600036, "BUY", 1020, 100, T)
        self.broker.on_quote(600036, Quote([(970, 100)], [(971, 100)], 971, T+100))
        self.assertEqual(self.broker.orders[oid].status, "REJECTED")
        self.assertEqual(self.broker.account.frozen_cash, 0)

    def test_missing_stale_and_future_quote_rejected(self):
        self.assertIsNone(self.broker.submit(600036, "BUY", 1000, 100, T+3001))
        self.assertIsNone(self.broker.submit(600036, "BUY", 1000, 100, T-1))

    def test_lunch_no_fill_and_no_cancellation(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(parse_time("12:00:00"))
        self.assertEqual(self.broker.trades, [])
        self.assertFalse(self.broker.cancel(oid, parse_time("12:00:00")))

    def test_expiration_releases_funds_and_stock(self):
        self.broker.submit(600036, "BUY", 1000, 100, T)
        self.broker.expire(parse_time("15:00:00"))
        self.assertEqual(self.broker.account.frozen_cash, 0)

    def test_t1_settlement_requires_verified_next_trading_day(self):
        self.broker.submit(600036, "BUY", 1000, 100, T)
        self.quote(T+100)
        calendar = ["2026-09-23", "2026-09-24"]
        with self.assertRaises(ValueError): self.broker.settlement("2026-09-24", "2026-09-23", trading_calendar=calendar)
        self.quote(parse_time("15:00:00"))
        with self.assertRaises(ValueError): self.broker.settlement("2026-09-25", "2026-09-23", trading_calendar=calendar)
        self.broker.settlement("2026-09-24", "2026-09-23", trading_calendar=calendar)
        self.assertEqual(self.broker.account.position(600036).sellable, 100)
        self.assertEqual(self.broker.quotes, {})

    def test_invalid_external_quote_fail_closed(self):
        with self.assertRaises(ValueError): self.broker.on_quote(600036, Quote([(1001, 100)], [(1000, 100)], 1000, T+1))
        with self.assertRaises(ValueError): self.broker.on_quote(600036, Quote([], [(1200, 100)], 1000, T+1))


class DataTests(unittest.TestCase):
    def row(self, **kw):
        d = {"Channel": 1001, "OrderOriNo": 12, "OrderNumber": 500, "BizIndex": 500,
             "FunctionCode": 66, "OrderKind": 65, "Price": 10., "Volume": 200, "Symbol": 600036, "Time": 91500000}
        return d | kw

    def test_original_id_is_used_and_aggressive_missing_is_expected(self):
        book = HistoricalBook(600036); book.on_order(self.row())
        book.on_trade({"Channel": 1001, "BidOrder": 12, "AskOrder": 33, "BSFlag": 83, "Volume": 50})
        self.assertEqual(book.levels("BUY"), [(1000, 150)])
        self.assertEqual(book.stats["unpublished_aggressor"], 1)
        self.assertEqual(book.stats["unresolved_passive"], 0)
        book.check()

    def test_status_is_not_an_order(self):
        book = HistoricalBook(600036); book.on_order(self.row(OrderKind=83, FunctionCode=73, Price=0., Volume=0))
        self.assertEqual(book.orders, {})

    def test_cancel_reduces_original_order_only(self):
        book = HistoricalBook(600036); book.on_order(self.row())
        book.on_order(self.row(OrderKind=68, Volume=200, OrderNumber=1000))
        self.assertEqual(book.orders, {}); book.check()

    def test_invalid_codes_are_not_guessed(self):
        with self.assertRaises(ValueError): HistoricalBook(600036).on_order(self.row(OrderKind=90))

    def test_channel_ids_are_separate(self):
        book = HistoricalBook(600036); book.on_order(self.row()); book.on_order(self.row(Channel=1002))
        self.assertEqual(len(book.orders), 2); book.check()

    def test_replay_reorders_equal_times_by_business_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for kind in ["ord", "exe", "snp"]:
                if kind == "ord":
                    rows = [self.row(Time=91500000, BizIndex=20)]
                elif kind == "exe":
                    rows = [{"Channel": 1001, "BizIndex": 21, "Symbol": 600036, "Time": 92500000,
                             "BidOrder": 12, "AskOrder": 33, "BSFlag": 83, "Price": 10., "Volume": 50}]
                else:
                    rows = [{**{c: 0 for c in REQUIRED['snp']}, "Symbol": 600036, "Time": 92500000, "PreClose": 10.}]
                schema = pa.schema([(c, pa.int32() if c in {"Symbol", "Time", "Channel", "BizIndex", "OrderOriNo", "BidOrder", "AskOrder", "BSFlag", "OrderKind", "FunctionCode", "Volume"} else pa.float64()) for c in sorted(REQUIRED[kind])])
                pq.write_table(pa.Table.from_pylist(rows, schema=schema), root/f"{kind}_20260923.parquet")
            events = list(merged_events(root, [600036], parse_time("09:30:00")))
            self.assertEqual([e['kind'] for e in events], ['ord', 'exe', 'snp'])
            self.assertEqual(events[0]['time_ms'], parse_time('09:25:00'))
            self.assertEqual(events[0]['original_time_ms'], parse_time('09:15:00'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
