"""无行情时推进、计划引用和时间倒退的回归测试。"""
import unittest
from sse_sim.broker import Account, PaperBroker
from sse_sim.plan import OrderPlan
from sse_sim.rules import Instrument, Quote, Reject, parse_time

T = parse_time("09:30:00")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.broker = PaperBroker({600036: Instrument(600036, 1000)}, Account(cash=1000000))
        self.broker.on_quote(600036, Quote([(999, 100)], [(1000, 100)], 1000, T))

    def test_submit_and_cancel_advance_clock(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T+200)
        self.assertEqual(self.broker.last_time, T+200)
        self.assertIsNone(self.broker.submit(600036, "BUY", 1000, 100, T+100))
        self.assertFalse(self.broker.cancel(oid, T+100))
        self.assertTrue(self.broker.cancel(oid, T+300))
        with self.assertRaises(Reject):
            self.broker.advance(T+299)

    def test_no_snapshot_close_expires_at_boundary(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T)
        self.broker.advance(parse_time("15:10:00"))
        self.assertEqual(self.broker.orders[oid].status, "EXPIRED")
        self.assertEqual(self.broker.account.frozen_cash, 0)
        self.assertEqual(self.broker.events[-1]["time_ms"], parse_time("15:00:00"))
        self.broker.advance(parse_time("15:20:00"))
        self.assertEqual(sum(e["event"] == "EXPIRED" for e in self.broker.events), 1)

    def test_cannot_expire_early(self):
        oid = self.broker.submit(600036, "BUY", 1000, 100, T)
        with self.assertRaises(Reject):
            self.broker.expire(T)
        self.assertEqual(self.broker.orders[oid].status, "PENDING")

    def test_plan_alias_cancel_after_last_quote(self):
        plan = OrderPlan([
            {"time": "09:30:00.100", "id": "entry", "symbol": 600036,
             "side": "BUY", "price": "10.00", "quantity": 100},
            {"time": "09:30:00.200", "action": "cancel", "ref": "entry"},
            {"time": "09:31:00", "symbol": 600036, "side": "BUY", "price": 10, "quantity": 100},
        ], [600036])
        plan.run_until(self.broker, T+100)
        self.assertEqual(plan.state()["executed"], 1)
        plan.run_until(self.broker, T+500)
        self.assertEqual(plan.state()["pending"], 1)
        self.assertEqual(self.broker.orders[plan.aliases["entry"]].status, "CANCELLED")
        self.assertEqual(self.broker.account.frozen_cash, 0)

    def test_rejected_submit_alias_cannot_cancel_other_order(self):
        plan = OrderPlan([
            {"time": "09:30:00", "id": "bad", "symbol": 600036, "side": "BUY", "price": 10, "quantity": 99},
            {"time": "09:30:00", "action": "cancel", "ref": "bad"},
        ], [600036])
        plan.run_until(self.broker, T)
        self.assertIsNone(plan.aliases["bad"])
        self.assertEqual(self.broker.events[-1]["event"], "CANCEL_REJECTED")

    def test_malformed_plan_rejected_before_execution(self):
        for items in [{}, [{"time": "09:30:00", "action": "unknown"}],
                      [{"time": "09:30:00", "action": "cancel", "ref": "missing"}],
                      [{"time": "09:30:00", "symbol": 1, "side": "BUY", "price": 10, "quantity": 100}]]:
            with self.subTest(items=items), self.assertRaises(ValueError):
                OrderPlan(items, [600036])


if __name__ == "__main__":
    unittest.main()
