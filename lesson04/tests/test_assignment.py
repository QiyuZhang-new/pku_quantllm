"""新作业的首尾时点、ST、正常方向涨跌停、容量和因子因果性。"""

import unittest

import numpy as np
import pandas as pd

from backtest import BacktestConfig, BacktestEngine, MarketData, StrategyContext
from backtest.factor_strategy import FactorSelectionStrategy
from backtest.market_rules import standard_limit_rates
from test_backtest import Script, market, run


class AssignmentExecutionTests(unittest.TestCase):
    def test_weight_normalization_accepts_pandas_readonly_array(self):
        data = market([[10, 10]]*2, symbols=["600000.SH", "000001.SZ"])
        result = run(data, [{"600000.SH": .5, "000001.SZ": .5000000000000002}])
        self.assertEqual(len(result.trades), 2)
        self.assertGreaterEqual(result.equity.iloc[-1].cash, 0)

    def test_st_rule_changes_on_actual_effective_date(self):
        symbols = ["600000.SH", "000001.SZ", "300001.SZ", "688001.SH", "689009.SH"]
        np.testing.assert_allclose(standard_limit_rates(symbols, "2026-07-03", [1]*5), [.05, .05, .2, .2, .2])
        np.testing.assert_allclose(standard_limit_rates(symbols, "2026-07-06", [1]*5), [.1, .1, .2, .2, .2])

    def test_standard_limit_up_blocks_buy(self):
        data = market([10, 11, 11], symbols=["600000.SH"])
        result = run(data, [{"600000.SH": 1}], apply_price_limits=True, execution_policy="standard")
        self.assertTrue(result.trades.empty)
        self.assertEqual(result.rejected_orders.reason.tolist(), ["limit_up_buy"])

    def test_standard_limit_down_blocks_sell_and_keeps_position(self):
        data = market([10, 10, 9, 9], symbols=["600000.SH"])
        result = run(data, [{"600000.SH": 1}, {}], apply_price_limits=True, execution_policy="standard")
        self.assertEqual(result.trades.side.tolist(), ["buy"])
        self.assertEqual(result.rejected_orders.reason.tolist(), ["limit_down_sell"])
        self.assertEqual(result.equity.iloc[-1].positions, 1)

    def test_prestart_signal_and_last_day_liquidation_include_all_costs(self):
        data = market([10, 10, 12, 12], symbols=["600000.SH"])
        result = run(data, [{"600000.SH": 1}], start=str(data.dates[1].date()),
                     prestart_signal=True, liquidate_on_last_day=True, commission=.0001, sell_tax=.0005)
        self.assertEqual(result.trades.date.tolist(), [data.dates[1], data.dates[-1]])
        self.assertEqual(result.trades.signal_date.tolist(), [data.dates[0], data.dates[-2]])
        self.assertEqual(result.equity.iloc[-1].positions, 0)
        self.assertAlmostEqual(result.metrics["final_equity"], 1000 / 1.0001 * 1.2 * .9994)
        self.assertAlmostEqual(result.trades.sell_tax.sum(), result.trades.iloc[-1].notional * .0005)

    def test_st_or_unknown_at_execution_cannot_be_bought(self):
        for flag in [1, np.nan]:
            data = market([10, 10], symbols=["600000.SH"])
            data.frames["is_st"] = pd.DataFrame([0, flag], index=data.dates, columns=data.symbols)
            result = run(data, [{"600000.SH": 1}], execution_policy="standard")
            self.assertTrue(result.trades.empty)
            self.assertEqual(result.rejected_orders.reason.tolist(), ["st_or_unknown_status"])

    def test_existing_stock_exits_when_st_takes_effect_before_open(self):
        data = market([10]*4, symbols=["600000.SH"])
        data.frames["is_st"] = pd.DataFrame([0, 0, 1, 1], index=data.dates, columns=data.symbols)
        result = run(data, [{"600000.SH": 1}], execution_policy="standard", exit_st_on_open=True)
        self.assertEqual(result.trades.side.tolist(), ["buy", "sell"])
        self.assertEqual(result.trades.iloc[-1].date, data.dates[2])
        self.assertEqual(result.equity.st_positions.sum(), 0)

    def test_star_minimum_200_then_single_share_increments(self):
        data = market([10, 10], symbols=["688001.SH"])
        too_small = run(data, [{"688001.SH": 1}], initial_cash=1990, lot_size=100, board_lots=True)
        self.assertTrue(too_small.trades.empty)
        enough = run(data, [{"688001.SH": 1}], initial_cash=2050, lot_size=100, board_lots=True)
        self.assertEqual(enough.trades.iloc[0].quantity, 205)

    def test_standard_302_is_chinext_twenty_percent(self):
        self.assertEqual(standard_limit_rates(["302132.SZ"], "2025-03-01")[0], .2)

    def test_capacity_uses_only_prior_amount_not_execution_day_amount(self):
        data = market([10]*5, symbols=["600000.SH"])
        data.frames["amt"] = pd.DataFrame([1000, 1000, 1e9, 1e9, 1e9], index=data.dates, columns=data.symbols)
        result = run(data, [None, {"600000.SH": 1}], volume_lookback=2, max_participation=.1)
        self.assertEqual(result.trades.iloc[0].quantity, 10)
        data.frames["amt"].iloc[2:] = 1
        other = run(data, [None, {"600000.SH": 1}], volume_lookback=2, max_participation=.1)
        pd.testing.assert_frame_equal(result.trades, other.trades)

    def test_failed_last_day_sell_is_visible(self):
        data = market([10, 10, np.nan], [10]*3, symbols=["600000.SH"])
        result = run(data, [{"600000.SH": 1}], liquidate_on_last_day=True)
        self.assertEqual(result.equity.iloc[-1].positions, 1)
        self.assertEqual(result.trades.side.tolist(), ["buy"])

    def test_official_raw_reference_handles_ex_rights(self):
        data = market([10, 10, 5], factors=[1, 1, 2], symbols=["600000.SH"])
        data.frames["preclose"] = pd.DataFrame([10, 10, 5], index=data.dates, columns=data.symbols)
        result = run(data, [{"600000.SH": 1}, {}], apply_price_limits=True, execution_policy="standard")
        self.assertEqual(result.trades.side.tolist(), ["buy", "sell"])
        self.assertAlmostEqual(result.metrics["final_equity"], 1000)


class FactorTests(unittest.TestCase):
    def data(self):
        n = 170
        x = np.arange(n)
        prices = np.column_stack([10*np.exp(.0002*x + .001*np.sin(x)),
                                  10*np.exp(.001*x + .02*np.sin(x))])
        data = market(prices, symbols=["600000.SH", "000001.SZ"])
        data.frames["amt"] = pd.DataFrame(1e9, index=data.dates, columns=data.symbols)
        data.frames["is_st"] = pd.DataFrame(0.0, index=data.dates, columns=data.symbols)
        return data

    def context(self, data, row):
        z = pd.Series(0., index=data.symbols)
        return StrategyContext(data, row, 1e9, 1e9, z, z)

    def test_actual_factor_future_rows_cannot_change_selection(self):
        data = self.data()
        first = FactorSelectionStrategy(k=1).on_close(self.context(data, 140))
        self.assertEqual(first.index.tolist(), ["600000.SH"])
        for name in ["close", "amt", "is_st"]:
            data.frames[name].iloc[141:] = 123456
        second = FactorSelectionStrategy(k=1).on_close(self.context(data, 140))
        pd.testing.assert_series_equal(first, second)

    def test_current_st_and_unknown_are_excluded(self):
        for flag in [1, np.nan]:
            data = self.data()
            data.frames["is_st"].iloc[140, 0] = flag
            target = FactorSelectionStrategy(k=1).on_close(self.context(data, 140))
            self.assertEqual(target.index.tolist(), ["000001.SZ"])

    def test_missing_return_does_not_become_zero_or_forward_filled(self):
        data = self.data()
        data.frames["close"].iloc[130, 0] = np.nan
        target = FactorSelectionStrategy(k=1).on_close(self.context(data, 140))
        self.assertEqual(target.index.tolist(), ["000001.SZ"])

    def test_schedule_is_fixed_before_future_returns(self):
        data = self.data()
        strategy = FactorSelectionStrategy(k=1, holding=20)
        dates = []
        for row in range(130, 151):
            if strategy.on_close(self.context(data, row)) is not None:
                dates.append(row)
        self.assertEqual(dates, [130, 150])


if __name__ == "__main__":
    unittest.main()
