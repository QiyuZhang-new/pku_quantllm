"""防止时间泄漏、未来选股、虚假净值与评价误判的行为测试。"""

from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from factor_eval import Config, Market, build_factors, evaluate_batch, forward_returns
from factor_eval.core import _gt, group_diagnostics, split_dates
from factor_eval.data import demo_market, read_matrix, universe
from factor_eval.stats import benjamini_hochberg, hac_mean, row_correlation


def simple_market(days=90, symbols=20):
    dates = pd.bdate_range("2020-01-01", periods=days)
    columns = [f"S{i:03d}" for i in range(symbols)]
    prices = np.exp(np.arange(days)[:, None] * np.linspace(-.003, .004, symbols)) * 20
    wrap = lambda v: pd.DataFrame(v, index=dates, columns=columns)
    return Market({"open": wrap(prices), "close": wrap(prices.copy()),
                   "cumadj": wrap(np.ones_like(prices)), "amt": wrap(np.ones_like(prices) * 1e9)})


class TimeAlignmentTests(unittest.TestCase):
    def test_five_day_open_label_and_tail(self):
        m = simple_market()
        y = forward_returns(m)
        expected = m.frames["open"].iloc[6] / m.frames["open"].iloc[1] - 1
        np.testing.assert_allclose(y.iloc[0], expected)
        self.assertTrue(y.tail(6).isna().all().all())
        self.assertTrue(y.iloc[-7].notna().all())

    def test_adjustment_split_does_not_create_return(self):
        m = simple_market(40)
        m.frames["open"].iloc[5:] /= 2
        m.frames["cumadj"].iloc[5:] *= 2
        np.testing.assert_allclose(forward_returns(m).iloc[0], forward_returns(simple_market(40)).iloc[0])

    def test_missing_entry_not_forward_filled(self):
        m = simple_market()
        m.frames["open"].iloc[1, 0] = np.nan
        self.assertTrue(pd.isna(forward_returns(m).iloc[0, 0]))

    def test_factor_and_universe_cannot_see_future(self):
        m = simple_market()
        cfg = dict(min_history=5, min_adv=0, top_n=10)
        original, pool = build_factors(m), universe(m, **cfg)
        for field in ("open", "close", "amt", "cumadj"):
            m.frames[field].iloc[50:] *= 17
        changed = build_factors(m)
        for name in original:
            pd.testing.assert_frame_equal(original[name].iloc[:50], changed[name].iloc[:50])
        pd.testing.assert_frame_equal(pool.iloc[:50], universe(m, **cfg).iloc[:50])

    def test_split_purges_cross_boundary_label(self):
        m = simple_market(100)
        cfg = Config(train_end=str(m.dates[39].date()), valid_end=str(m.dates[69].date()))
        split, metadata = split_dates(m.dates, cfg)
        self.assertEqual(split.iloc[33], "train")
        self.assertTrue(split.iloc[34:40].eq("purged").all())
        self.assertEqual(split.iloc[40], "validation")
        self.assertEqual(split.iloc[63], "validation")
        self.assertTrue(split.iloc[64:70].eq("purged").all())
        self.assertEqual(split.iloc[70], "test")
        self.assertEqual(metadata["purged_rows"], 18)

    def test_direction_frozen_when_future_reversed(self):
        m, factors = demo_market(seed=4, days=140, symbols=30)
        cfg = Config(min_pairs=20, min_history=1, min_adv=0, top_n=0, min_days=3)
        first = evaluate_batch(m, {"signal": factors["synthetic_signal"]}, cfg)[0]
        changed = factors["synthetic_signal"].copy()
        changed.iloc[70:] *= -1
        second = evaluate_batch(m, {"signal": changed}, cfg)[0]
        self.assertEqual(first.summary["direction"], second.summary["direction"])
        self.assertEqual(first.summary["direction_train_ic"], second.summary["direction_train_ic"])
        self.assertAlmostEqual(first.summary["periods"]["test"]["ic"]["mean"],
                               -second.summary["periods"]["test"]["ic"]["mean"])


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(min_pairs=10)
        self.score = pd.DataFrame([np.arange(10, dtype=float)] * 15)
        self.labels = self.score / 100

    def test_missing_outcome_does_not_reselect_members(self):
        baseline = group_diagnostics(self.score, self.labels, self.cfg)
        changed = self.labels.copy()
        changed.iloc[0, 9] = np.nan
        actual = group_diagnostics(self.score, changed, self.cfg)
        self.assertEqual(actual.loc[0, "q5_count"], baseline.loc[0, "q5_count"])
        self.assertEqual(actual.loc[0, "q5_count"], 2)
        self.assertTrue(pd.isna(actual.loc[0, "q5"]))
        self.assertTrue(pd.isna(actual.loc[0, "benchmark"]))
        self.assertEqual(actual.loc[0, "q1"], baseline.loc[0, "q1"])

    def test_round_trip_costs_and_stress(self):
        result = group_diagnostics(self.score, self.labels, self.cfg)
        cost = self.cfg.one_way_bps / 10000
        np.testing.assert_allclose(result.long_excess_gross - result.long_excess_net, 2 * cost)
        np.testing.assert_allclose(result.long_short_gross - result.long_short_net, 4 * cost)
        np.testing.assert_allclose(result.long_short_gross - result.long_short_stress, 8 * cost)

    def test_constant_factor_cannot_create_fake_groups(self):
        result = group_diagnostics(self.score * 0 + 1, self.labels, self.cfg)
        self.assertTrue(result.q5.isna().all())
        self.assertTrue(result.long_short_net.isna().all())

    def test_ties_are_not_split_by_symbol_order(self):
        tied = pd.DataFrame([[0, 0, 1, 1, 2, 2, 3, 3, 4, 4]] * 15)
        original = group_diagnostics(tied, self.labels, self.cfg)
        shuffled = group_diagnostics(tied.iloc[:, ::-1], self.labels.iloc[:, ::-1], self.cfg)
        pd.testing.assert_frame_equal(original, shuffled)


class StatisticsTests(unittest.TestCase):
    def test_numeric_threshold_tolerates_rounding(self):
        self.assertTrue(_gt(.5999999999999999, .6))
        self.assertFalse(_gt(.5999, .6))

    def test_rank_correlation_pairwise_missing(self):
        x = pd.DataFrame([[1., 3., 2., np.nan]])
        y = pd.DataFrame([[2., 6., 4., 100.]])
        self.assertAlmostEqual(row_correlation(x, y, 3, rank=True).iloc[0], 1.)

    def test_constant_has_no_correlation_or_t(self):
        x = pd.DataFrame([[1., 1., 1.]])
        self.assertTrue(pd.isna(row_correlation(x, x, rank=True).iloc[0]))
        self.assertIsNone(hac_mean(pd.Series([.2] * 20))["hac_t"])

    def test_hac_matches_direct_bartlett_sum(self):
        x = np.array([.3, -.1, .2, .5, -.2, .3, .1])
        u = x - x.mean()
        variance = (u @ u + 2 * ((2 / 3) * (u[1:] @ u[:-1]) +
                                 (1 / 3) * (u[2:] @ u[:-2]))) / len(x)**2
        self.assertAlmostEqual(hac_mean(x, 2)["hac_t"], x.mean() / np.sqrt(variance))

    def test_hac_missing_dates_not_compressed(self):
        x = pd.Series([.2, np.nan, -.3, .4, np.nan, .1])
        self.assertNotEqual(hac_mean(x, 1)["hac_t"], hac_mean(x.dropna(), 1)["hac_t"])

    def test_bh_known_values_and_invalid_candidates(self):
        np.testing.assert_allclose(benjamini_hochberg([.01, .04, .03, None]), [.04, .053333333333, .053333333333, 1])


class ValidationTests(unittest.TestCase):
    def test_misaligned_factor_is_rejected(self):
        m = simple_market()
        cfg = Config(min_pairs=10)
        with self.assertRaisesRegex(ValueError, "严格对齐"):
            evaluate_batch(m, {"wrong": m.frames["close"].iloc[:, ::-1]}, cfg)

    def test_invalid_config_and_infinite_data(self):
        with self.assertRaises(ValueError):
            Config(hac_lags=3)
        with self.assertRaises(ValueError):
            Config(one_way_bps=-1)
        with self.assertRaises(ValueError):
            Config(train_end="2021-01-01")
        with self.assertRaises(ValueError):
            Config(min_pairs=3.2)
        m = simple_market()
        m.frames["close"].iloc[0, 0] = np.inf
        with self.assertRaisesRegex(ValueError, "无穷"):
            Market(m.frames)

    def test_matrix_file_round_trip_and_duplicate_dates(self):
        m = simple_market()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factor.csv"
            expected = m.frames["close"].copy()
            expected.index.name = "date"
            expected.to_csv(path)
            pd.testing.assert_frame_equal(read_matrix(path), expected, check_freq=False)
            duplicated = pd.concat([expected.iloc[:1], expected.iloc[:1]])
            duplicated.to_csv(path)
            with self.assertRaisesRegex(ValueError, "日期"):
                read_matrix(path)

    def test_positive_control_passes_noise_and_constant_do_not(self):
        m, factors = demo_market()
        cfg = Config(min_history=20, min_adv=0, top_n=0)
        results = {r.name: r for r in evaluate_batch(m, factors, cfg)}
        self.assertEqual(results["synthetic_signal"].summary["verdict"], "好因子（当前研究样本通过）")
        self.assertNotEqual(results["synthetic_noise"].summary["verdict"], "好因子（当前研究样本通过）")
        self.assertEqual(results["synthetic_constant"].summary["verdict"], "证据不足")
        self.assertTrue(results["synthetic_signal"].summary["validation_approved"])

    def test_sample_shortage_does_not_mean_bad_factor(self):
        m, factors = demo_market(days=140, symbols=30)
        cfg = Config(min_pairs=20, min_history=1, min_adv=0, top_n=0)
        result = evaluate_batch(m, {"signal": factors["synthetic_signal"]}, cfg)[0]
        self.assertEqual(result.summary["verdict"], "证据不足")


if __name__ == "__main__":
    unittest.main()
