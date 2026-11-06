"""Alpha101公式独立数值核对、缺字段及时间因果性。"""

import unittest

import numpy as np
import pandas as pd

from lesson05.factor_eval import Market
from quant_research.alpha101 import DEFINITIONS, catalog
from quant_research.factors import FactorSpec, check_causality, compute_factor


def market():
    rng = np.random.default_rng(47)
    dates = pd.bdate_range("2020-01-02", periods=60)
    columns = [f"S{i}" for i in range(6)]
    close = 20 * np.exp(np.cumsum(rng.normal(0, .02, (60, 6)), axis=0))
    opening = close * rng.uniform(.98, 1.02, close.shape)
    frames = {"open": opening, "close": close, "high": np.maximum(close, opening) * 1.01,
              "low": np.minimum(close, opening) * .99, "cumadj": np.ones_like(close),
              "amt": np.full_like(close, 1e9), "eligible": np.ones_like(close)}
    return Market({key: pd.DataFrame(value, index=dates, columns=columns) for key, value in frames.items()})


def calculate(number, data):
    return compute_factor(FactorSpec(f"alpha{number}", {"op": "alpha101", "number": number}, "核对论文定义"), data)


def manual_rank(values):
    return np.array([(sum(v < value for v in values) + (sum(v == value for v in values) + 1) / 2) / len(values)
                     for value in values])


class Alpha101Tests(unittest.TestCase):
    def test_101_formula_and_price_adjustment(self):
        data = market()
        data.frames["cumadj"] *= 2
        values = {field: data.frames[field].iloc[-1].to_numpy() * 2 for field in ("open", "high", "low", "close")}
        expected = (values["close"] - values["open"]) / (values["high"] - values["low"] + .001)
        np.testing.assert_allclose(calculate(101, data).iloc[-1], expected)

    def test_33_cross_section_and_unknown_state(self):
        data = market()
        data.frames["eligible"].iloc[-1, 0] = 0
        frame = calculate(33, data)
        self.assertTrue(pd.isna(frame.iloc[-1, 0]))
        values = data.frames["open"].iloc[-1, 1:].to_numpy() / data.frames["close"].iloc[-1, 1:].to_numpy() - 1
        np.testing.assert_allclose(frame.iloc[-1, 1:], manual_rank(values))

    def test_20_product_of_three_independent_ranks(self):
        data = market()
        opening = data.frames["open"].iloc[-1].to_numpy()
        ranks = [manual_rank(opening - data.frames[field].iloc[-2].to_numpy()) for field in ("high", "close", "low")]
        np.testing.assert_allclose(calculate(20, data).iloc[-1], -ranks[0] * ranks[1] * ranks[2])

    def test_9_and_10_condition_and_complete_history(self):
        data = market()
        change = data.frames["close"].diff().to_numpy()
        expected = [change[-1, col] if min(change[-5:, col]) > 0 or max(change[-5:, col]) < 0 else -change[-1, col]
                    for col in range(6)]
        np.testing.assert_allclose(calculate(9, data).iloc[-1], expected)
        self.assertTrue(calculate(9, data).iloc[:5].isna().all().all())
        expected = [change[-1, col] if min(change[-4:, col]) > 0 or max(change[-4:, col]) < 0 else -change[-1, col]
                    for col in range(6)]
        np.testing.assert_allclose(calculate(10, data).iloc[-1], manual_rank(expected))

    def test_23_mean_high_condition_and_warmup(self):
        data = market()
        high = data.frames["high"].to_numpy()
        expected = [-(high[-1, col] - high[-3, col]) if high[-1, col] > sum(high[-20:, col]) / 20 else 0 for col in range(6)]
        np.testing.assert_allclose(calculate(23, data).iloc[-1], expected)
        self.assertTrue(calculate(23, data).iloc[:19].isna().all().all())

    def test_4_time_rank_of_cross_section(self):
        data = market()
        ranks = np.array([manual_rank(row) for row in data.frames["low"].tail(9).to_numpy()])
        expected = [-manual_rank(ranks[:, col])[-1] for col in range(6)]
        np.testing.assert_allclose(calculate(4, data).iloc[-1], expected)

    def test_18_independent_std_and_correlation(self):
        data = market()
        close, opening = (data.frames[field].to_numpy() for field in ("close", "open"))
        values = [np.std(np.abs(close[-5:, col] - opening[-5:, col]), ddof=1)
                  + close[-1, col] - opening[-1, col] + np.corrcoef(close[-10:, col], opening[-10:, col])[0, 1]
                  for col in range(6)]
        np.testing.assert_allclose(calculate(18, data).iloc[-1], -manual_rank(values))

    def test_all_implemented_factors_are_causal(self):
        data = market()
        for number in DEFINITIONS:
            with self.subTest(number=number):
                check_causality(FactorSpec(f"alpha{number}", {"op": "alpha101", "number": number}, "假设"), data)

    def test_missing_fields_and_unimplemented_alphas_are_rejected(self):
        data = market()
        del data.frames["high"]
        with self.assertRaisesRegex(ValueError, "缺少字段"):
            calculate(101, data)
        for number in (2, 5, 54, 102, True):
            with self.subTest(number=number), self.assertRaises(ValueError):
                FactorSpec("unsupported", {"op": "alpha101", "number": number}, "假设")
        self.assertEqual(len(catalog()), 101)
        self.assertEqual(sum(row["implemented"] for row in catalog()), 8)


if __name__ == "__main__":
    unittest.main()
