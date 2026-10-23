"""STRATEGY.md 规定方向的门禁、停牌顺延与历史印花税。"""

import unittest

import numpy as np
import pandas as pd

from backtest import BacktestConfig, BacktestEngine
from backtest.market_rules import limit_rates, opening_limits, stamp_tax
from test_backtest import Script, market, run


class MarketRuleTests(unittest.TestCase):
    def test_prefix_rates_and_boundary(self):
        symbols = ["000001.SZ", "300001.SZ", "301001.SZ", "688001.SH", "600001.SH"]
        np.testing.assert_allclose(limit_rates(symbols), [.1, .2, .2, .2, .1])
        down, up, valid = opening_limits([9, 8, 12, 12, 11], [10] * 5, symbols)
        self.assertEqual(down.tolist(), [True, True, False, False, False])
        self.assertEqual(up.tolist(), [False, False, True, True, True])
        self.assertTrue(valid.all())

    def test_limit_down_buy_skips_without_delay(self):
        data = market([10, 9, 9.5], symbols=["000001.SZ"])
        result = run(data, [{"000001.SZ": 1}], apply_price_limits=True)
        self.assertTrue(result.trades.empty)
        self.assertEqual(result.rejected_orders["reason"].tolist(), ["limit_down_buy"])
        self.assertEqual(result.metrics["limit_rejected_ratio"], 1)
        self.assertTrue((result.equity["cash"] == 1000).all())

    def test_limit_up_sell_skips_without_delay(self):
        data = market([10, 10, 11, 10.5], symbols=["600000.SH"])
        result = run(data, [{"600000.SH": 1}, {}], apply_price_limits=True)
        self.assertEqual(result.trades["side"].tolist(), ["buy"])
        self.assertEqual(result.rejected_orders["reason"].tolist(), ["limit_up_sell"])
        self.assertEqual(result.equity.iloc[-1]["positions"], 1)

    def test_spec_direction_is_not_reversed(self):
        data = market([10, 11, 9.9], symbols=["000001.SZ"])
        result = run(data, [{"000001.SZ": 1}, {}], apply_price_limits=True)
        self.assertEqual(result.trades["side"].tolist(), ["buy", "sell"])
        self.assertTrue(result.rejected_orders.empty)

    def test_blocked_budget_is_not_redistributed(self):
        data = market([[10, 10], [9, 10]], symbols=["000001.SZ", "600000.SH"])
        result = run(data, [{"000001.SZ": .5, "600000.SH": .5}], apply_price_limits=True)
        self.assertEqual(result.trades["symbol"].tolist(), ["600000.SH"])
        self.assertEqual(result.trades.iloc[0]["quantity"], 50)
        self.assertEqual(result.equity.iloc[-1]["cash"], 500)

    def test_raw_price_is_used_for_limit_and_fill(self):
        data = market([10, 10], factors=[100, 120], symbols=["000001.SZ"])
        result = run(data, [{"000001.SZ": 1}], apply_price_limits=True)
        self.assertEqual(result.trades.iloc[0]["fill_price"], 10)
        self.assertTrue(result.rejected_orders.empty)

    def test_future_high_low_cannot_change_open_execution(self):
        data = market([10, 10], symbols=["000001.SZ"])
        first = run(data, [{"000001.SZ": 1}], apply_price_limits=True)
        data.frames["high"] = data.frames["close"] * 5
        data.frames["low"] = data.frames["close"] / 5
        second = run(data, [{"000001.SZ": 1}], apply_price_limits=True)
        pd.testing.assert_frame_equal(first.trades, second.trades)

    def test_stamp_tax_switches_on_exact_date(self):
        self.assertEqual(stamp_tax("2023-08-27"), .001)
        self.assertEqual(stamp_tax("2023-08-28"), .0005)
        self.assertIsNone(BacktestConfig().sell_tax)

    def test_tax_uses_actual_sell_date_and_only_sell(self):
        data = market([10] * 5, symbols=["000001.SZ"])
        dates = pd.DatetimeIndex(["2023-08-23", "2023-08-24", "2023-08-25", "2023-08-28", "2023-08-29"])
        for frame in data.frames.values():
            frame.index = dates
        data.dates = dates
        result = run(data, [{"000001.SZ": 1}, {}, {"000001.SZ": 1}, {}], sell_tax=None, apply_price_limits=True)
        self.assertEqual(result.trades["sell_tax_rate"].tolist(), [0, .001, 0, .0005])
        sells = result.trades.loc[result.trades["side"] == "sell"]
        np.testing.assert_allclose(sells["sell_tax"], sells["notional"] * [.001, .0005])

    def test_suspension_defers_and_preserves_signal_date(self):
        data = market([10, 10, np.nan, 10], [10] * 4, symbols=["000001.SZ"])
        result = run(data, [{"000001.SZ": 1}, {}], apply_price_limits=True, defer_suspended_orders=True)
        self.assertEqual(result.trades["side"].tolist(), ["buy", "sell"])
        self.assertTrue(result.trades.iloc[-1]["deferred"])
        self.assertEqual(result.trades.iloc[-1]["signal_date"], data.dates[1])
        self.assertEqual(result.metrics["deferred_fills"], 1)
        self.assertEqual(result.metrics["suspended_unique_orders"], 1)
        self.assertEqual(result.metrics["suspended_unique_ratio"], .5)

    def test_new_signal_cancels_old_suspended_target(self):
        data = market([10, np.nan, 10], [10] * 3, symbols=["000001.SZ"])
        result = run(data, [{"000001.SZ": 1}, {}], apply_price_limits=True, defer_suspended_orders=True)
        self.assertTrue(result.trades.empty)

    def test_suspension_retry_at_limit_is_cancelled(self):
        data = market([10, np.nan, 9, 9.1], [10, 10, 9, 9.1], symbols=["000001.SZ"])
        result = run(data, [{"000001.SZ": 1}], apply_price_limits=True, defer_suspended_orders=True)
        self.assertTrue(result.trades.empty)
        self.assertEqual(result.rejected_orders["reason"].tolist(), ["invalid_open", "limit_down_buy"])
        self.assertEqual(result.rejected_orders["deferred"].tolist(), [True, False])

    def test_daily_target_cannot_sell_shares_bought_same_day(self):
        data = market([[10, 10]] * 4, symbols=["000001.SZ", "600000.SH"])
        result = run(data, [{"000001.SZ": 1}, {"600000.SH": 1}, {}], apply_price_limits=True)
        for _, trades in result.trades.groupby(["date", "symbol"]):
            self.assertLessEqual(trades["side"].nunique(), 1)
        bought = result.trades.loc[result.trades["side"] == "buy"].set_index("symbol")["date"]
        for trade in result.trades.loc[result.trades["side"] == "sell"].itertuples():
            self.assertGreater(trade.date, bought[trade.symbol])


if __name__ == "__main__":
    unittest.main()
