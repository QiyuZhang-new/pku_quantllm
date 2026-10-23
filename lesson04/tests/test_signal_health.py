"""信号体检的时点、收益端点、成本、现金权重与无未来函数。"""

from dataclasses import replace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.signal_health import SignalConfig, analyze, build_signals, forward_panel, hac_mean, wilder_rsi


def frames(prices=None, size=14):
    if prices is None:
        prices = np.tile([10., 11., 12.], (size, 1))
    close = pd.DataFrame(prices, index=pd.bdate_range("2024-01-02", periods=len(prices)), columns=["000001.SZ", "000002.SZ", "600000.SH"])
    return {"open": close.copy(), "close": close, "amt": close * 0 + [300., 200., 100.], "cumadj": close * 0 + 1.0}


def config(**kwargs):
    options = dict(N=3, K=1, L=1, MA=3, MIN_LISTED=2, horizons=(1, 3, 5))
    options.update(kwargs)
    return SignalConfig(**options)


class SignalHealthTests(unittest.TestCase):
    def test_forward_returns_start_at_next_open(self):
        observed = forward_panel(np.array([[10.], [20.], [30.], [40.]]), 1, 1)
        np.testing.assert_allclose(observed[:2, 0], [.5, 1 / 3])
        self.assertTrue(np.isnan(observed[2:]).all())

    def test_rsi_seed_and_gap_reset(self):
        prices = np.array([[10, 10, 10], [9, 11, 10], [8, 12, 10], [np.nan, 13, 10], [7, 14, 10], [6, 15, 10], [5, 16, 10]], dtype=float)
        rsi = wilder_rsi(prices)
        self.assertTrue(np.isnan(rsi[:2]).all())
        np.testing.assert_allclose(rsi[2], [0, 100, 50])
        self.assertTrue(np.isnan(rsi[3:6, 0]).all())
        self.assertEqual(rsi[6, 0], 0)

    def test_no_future_data_in_pool_or_ranking_or_regime(self):
        data = frames(np.array([[10 - i * .1, 11 + i * .1, 12.] for i in range(14)]))
        first_pool, first_signals, first_market = build_signals(data, config())
        modified = {name: frame.copy() for name, frame in data.items()}
        for frame in modified.values():
            frame.iloc[8:] *= 100
        second_pool, second_signals, second_market = build_signals(modified, config())
        pd.testing.assert_frame_equal(first_pool.loc[first_pool["row"] < 8], second_pool.loc[second_pool["row"] < 8])
        pd.testing.assert_frame_equal(first_signals.loc[first_signals["row"] < 8], second_signals.loc[second_signals["row"] < 8])
        pd.testing.assert_frame_equal(first_market.iloc[:8], second_market.iloc[:8])

    def test_no_fill_across_missing_price(self):
        data = frames()
        data["close"].iloc[3, 0] = np.nan
        pool, selected, _ = build_signals(data, config())
        missing = pool.loc[(pool["row"] == 4) & (pool["symbol"] == "000001.SZ")]
        self.assertTrue(np.isnan(missing.iloc[0]["return_L"]))
        self.assertFalse(((selected["row"] == 4) & (selected["symbol"] == "000001.SZ")).any())

    def test_positive_amount_and_minimum_age(self):
        data = frames()
        data["amt"].iloc[3, 0] = 0
        data["amt"].iloc[4, 1] = np.nan
        pool, _, _ = build_signals(data, config(MIN_LISTED=3))
        self.assertGreaterEqual(pool["row"].min(), 2)
        self.assertFalse(((pool["row"] == 3) & (pool["col"] == 0)).any())
        self.assertFalse(((pool["row"] == 4) & (pool["col"] == 1)).any())

    def test_regime_has_full_specified_warmup(self):
        data = frames(np.array([[10 + i, 11 + i, 12 + i] for i in range(14)], dtype=float))
        _, _, market = build_signals(data, config(MA=5))
        self.assertFalse(market["regime_on"].iloc[:5].any())
        self.assertTrue(market["regime_on"].iloc[5:].all())

    def test_flat_returns_fail_cost_gate_without_engine(self):
        with patch("backtest.BacktestEngine.run", side_effect=AssertionError("体检不能调用引擎")):
            result = analyze(frames(), config())
        self.assertFalse(result["gate"]["passed"])
        self.assertAlmostEqual(result["gate"]["primary_net_excess"], -.0035)
        self.assertIn("默认成本", result["gate"]["failures"][0])

    def test_limit_skip_is_cash_without_budget_redistribution(self):
        data = frames()
        data["close"].iloc[1] = [9., 9.5, 12.]
        data["open"].iloc[2] = [8.1, 9.5, 12.]
        data["open"].iloc[3] = [9., 10.45, 12.]
        result = analyze(data, config(N=2, K=2))
        daily = result["daily"].loc[(result["daily"]["row"] == 1) & (result["daily"]["signal"] == "reversal") & (result["daily"]["variant"] == "buyable_cash") & (result["daily"]["horizon"] == 1)].iloc[0]
        self.assertAlmostEqual(daily["gross_return"], .05)
        self.assertAlmostEqual(daily["fill_ratio"], .5)
        self.assertAlmostEqual(daily["estimated_cost"], .00175)
        observation = result["observations"].loc[(result["observations"]["row"] == 1) & (result["observations"]["symbol"] == "000001.SZ")]
        self.assertTrue((observation["entry_reason"] == "limit_down").all())
        self.assertTrue((observation["return_buyable_1"] == 0).all())

    def test_unknown_exit_is_not_zero(self):
        data = frames()
        data["open"].iloc[3, 0] = np.nan
        result = analyze(data, config())
        daily = result["daily"].loc[(result["daily"]["row"] == 1) & (result["daily"]["signal"] == "reversal") & (result["daily"]["variant"] == "buyable_cash") & (result["daily"]["horizon"] == 1)].iloc[0]
        self.assertTrue(np.isnan(daily["gross_return"]))
        self.assertFalse(daily["right_censored"])

    def test_factor_level_does_not_change_returns(self):
        data = frames()
        baseline = analyze(data, config())
        data["cumadj"] *= 100
        scaled = analyze(data, config())
        pd.testing.assert_frame_equal(baseline["daily"], scaled["daily"])

    def test_sample_end_is_explicitly_censored(self):
        result = analyze(frames(), config())
        last = result["observations"].loc[result["observations"]["row"] == 13]
        self.assertTrue((last["entry_reason"] == "right_censored").all())
        self.assertTrue(last["return_buyable_5"].isna().all())
        row = result["summary"].loc[(result["summary"]["signal"] == "reversal") & (result["summary"]["variant"] == "buyable_cash") & (result["summary"]["horizon"] == 5)].iloc[0]
        self.assertEqual(row["right_censored_dates"], 6)

    def test_newey_west_accounts_for_overlapping_persistent_values(self):
        values = np.repeat([-.01, .01], 40)
        _, se = hac_mean(values, 20)
        self.assertGreater(se, np.std(values, ddof=1) / np.sqrt(len(values)))

    def test_paired_comparison_uses_identical_signal_dates(self):
        data = frames(np.array([[10 - i * .1, 11 + i * .1, 12.] for i in range(14)]))
        result = analyze(data, config())
        daily = result["daily"].loc[(result["daily"]["variant"] == "buyable_cash") & (result["daily"]["horizon"] == 1)]
        paired = daily.pivot(index="row", columns="signal", values="gross_excess").dropna()
        actual = result["paired"].loc[(result["paired"]["variant"] == "buyable_cash") & (result["paired"]["horizon"] == 1) & (result["paired"]["measure"] == "gross_excess")].iloc[0]
        self.assertEqual(actual["n_paired_dates"], len(paired))
        self.assertAlmostEqual(actual["rsi_minus_reversal"], (paired["rsi2"] - paired["reversal"]).mean())


if __name__ == "__main__":
    unittest.main()
