"""验证成交时序、资金守恒、复权和缺失数据等经济语义。"""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from backtest import BacktestConfig, BacktestEngine, EqualWeightStrategy, MarketData


def market(open_prices, close_prices=None, factors=None, symbols=None):
    opens = np.array(open_prices, dtype=float)
    if opens.ndim == 1:
        opens = opens[:, None]
    dates = pd.bdate_range("2024-01-02", periods=len(opens))
    columns = symbols or [f"S{i}" for i in range(opens.shape[1])]

    def frame(values):
        return pd.DataFrame(np.asarray(values, dtype=float).reshape(opens.shape), index=dates, columns=columns)

    return MarketData({
        "open": frame(opens),
        "close": frame(opens if close_prices is None else close_prices),
        "cumadj": frame(np.ones_like(opens) if factors is None else factors),
    })


class Script:
    def __init__(self, targets):
        self.targets = iter(targets)
        self.contexts = []

    def on_close(self, context):
        self.contexts.append(context)
        target = next(self.targets, None)
        return None if target is None else pd.Series(target, dtype=float)


def run(data, targets, **config):
    options = dict(initial_cash=1000.0, commission=0.0, sell_tax=0.0, slippage=0.0, lot_size=0,
                   apply_price_limits=False, defer_suspended_orders=False)
    options.update(config)
    return BacktestEngine(data, BacktestConfig(**options)).run(Script(targets))


class AccountingTests(unittest.TestCase):
    def test_close_signal_executes_at_next_open(self):
        data = market([10, 20, 30], [10, 25, 35])
        result = run(data, [{"S0": 1}])
        self.assertEqual(len(result.trades), 1)
        trade = result.trades.iloc[0]
        self.assertEqual(trade["date"], data.dates[1])
        self.assertEqual(trade["signal_date"], data.dates[0])
        self.assertEqual(trade["quantity"], 50)
        self.assertEqual(trade["fill_price"], 20)
        np.testing.assert_allclose(result.equity["equity"], [1000, 1250, 1750])

    def test_fees_slippage_tax_and_no_cash_overdraft(self):
        result = run(market([10, 10, 10]), [{"S0": 1}, {}], commission=0.01, sell_tax=0.02, slippage=0.01)
        quantity = 1000 / (10.1 * 1.01)
        expected_final = quantity * 9.9 * (1 - 0.01 - 0.02)
        self.assertAlmostEqual(result.trades.iloc[0]["quantity"], quantity)
        self.assertAlmostEqual(result.equity.iloc[-1]["equity"], expected_final)
        self.assertAlmostEqual(result.metrics["total_fees"], result.trades["commission"].sum() + result.trades["sell_tax"].sum())
        self.assertTrue((result.equity["cash"] >= 0).all())
        self.assertTrue((result.trades["cash_after"] >= -1e-8).all())
        self.assertGreater(result.metrics["total_slippage_cost"], 0)

    def test_adjustment_prevents_false_split_loss(self):
        result = run(market([100, 100, 50], factors=[1, 1, 2]), [{"S0": 1}])
        np.testing.assert_allclose(result.equity["equity"], [1000, 1000, 1000])
        np.testing.assert_allclose(result.positions["shares"], [10, 20])
        self.assertEqual(len(result.trades), 1)

    def test_missing_price_retains_position_and_does_not_retry_order(self):
        data = market([10, 10, np.nan, 12])
        result = run(data, [{"S0": 1}, {}, None])
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(len(result.rejected_orders), 1)
        self.assertEqual(result.rejected_orders.iloc[0]["reason"], "invalid_open")
        self.assertEqual(result.equity.iloc[2]["stale_positions"], 1)
        np.testing.assert_allclose(result.equity["equity"], [1000, 1000, 1000, 1200])

    def test_open_marks_new_purchase_when_close_missing(self):
        result = run(market([10, 12], [10, np.nan]), [{"S0": 1}])
        self.assertAlmostEqual(result.equity.iloc[-1]["equity"], 1000)
        self.assertEqual(result.positions.iloc[-1]["mark_price"], 12)
        self.assertTrue(result.positions.iloc[-1]["stale"])

    def test_missing_factor_blocks_buy(self):
        result = run(market([10, 10], factors=[1, np.nan]), [{"S0": 1}])
        self.assertTrue(result.trades.empty)
        self.assertEqual(result.rejected_orders.iloc[0]["reason"], "missing_adjustment_factor")

    def test_factor_gap_restores_adjustment_once(self):
        result = run(market([10, 10, 5, 5], factors=[1, 1, np.nan, 2]), [{"S0": 1}])
        np.testing.assert_allclose(result.equity["equity"], [1000] * 4)
        np.testing.assert_allclose(result.positions["shares"], [100, 100, 200])
        self.assertEqual(result.metrics["stale_position_days"], 1)

    def test_missing_price_on_adjustment_day_preserves_adjusted_value(self):
        result = run(market([10, 10, np.nan, 5], factors=[1, 1, 2, 2]), [{"S0": 1}])
        np.testing.assert_allclose(result.equity["equity"], [1000] * 4)
        self.assertEqual(result.positions.iloc[1]["mark_price"], 5)

    def test_lot_rounding_leaves_residual_cash(self):
        result = run(market([[3, 7], [3, 7]]), [{"S0": 0.6, "S1": 0.4}], lot_size=100)
        self.assertEqual(result.trades.iloc[0]["quantity"], 200)
        self.assertEqual(len(result.trades), 1)
        self.assertAlmostEqual(result.equity.iloc[-1]["cash"], 400)

    def test_sell_releases_cash_before_buy(self):
        result = run(market([[10, 20]] * 3), [{"S0": 1}, {"S1": 1}])
        self.assertEqual(result.trades["side"].tolist(), ["buy", "sell", "buy"])
        self.assertEqual(result.trades.iloc[-1]["quantity"], 50)
        np.testing.assert_allclose(result.equity["equity"], [1000] * 3)

    def test_blocked_sell_does_not_fund_other_purchase(self):
        data = market([[10, 10], [10, 10], [np.nan, 10]], [[10, 10]] * 3)
        result = run(data, [{"S0": 1}, {"S1": 1}])
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(len(result.rejected_orders), 1)
        self.assertEqual(result.positions.iloc[-1]["symbol"], "S0")

    def test_empty_target_liquidates_odd_shares(self):
        result = run(market([3, 3, 2], factors=[1, 1, 1.5]), [{"S0": 1}, {}], lot_size=100)
        self.assertEqual(result.trades.iloc[-1]["quantity"], 450)
        self.assertEqual(result.equity.iloc[-1]["market_value"], 0)
        self.assertEqual(result.equity.iloc[-1]["equity"], 1000)

    def test_initial_loss_counts_in_drawdown(self):
        result = run(market([10, 10]), [{"S0": 1}], commission=0.01)
        self.assertAlmostEqual(result.metrics["max_drawdown"], result.metrics["total_return"])
        self.assertLess(result.metrics["max_drawdown"], 0)

    def test_cash_only_and_undefined_sharpe(self):
        result = run(market([10, 10]), [])
        self.assertEqual(result.metrics["total_return"], 0)
        self.assertEqual(result.metrics["max_drawdown"], 0)
        self.assertIsNone(result.metrics["sharpe_zero_rf"])
        self.assertEqual(result.metrics["final_cash"], 1000)
        self.assertEqual(result.metrics["average_exposure"], 0)


class InterfaceTests(unittest.TestCase):
    def test_history_is_past_only_and_defensive_copy(self):
        data = market([10, 20, 30, 40])
        strategy = Script([])
        BacktestEngine(data).run(strategy)
        for row, context in enumerate(strategy.contexts):
            history = context.history("close")
            self.assertEqual(history.index[-1], context.date)
            self.assertEqual(len(history), row + 1)
            history.iloc[-1, 0] = -999
            self.assertNotEqual(data.frames["close"].iloc[row, 0], -999)
        self.assertEqual(len(strategy.contexts[-1].history("close", 2)), 2)
        with self.assertRaises(ValueError):
            strategy.contexts[-1].history("close", 0)

    def test_adjusted_history_uses_matching_factors(self):
        data = market([10, 5], factors=[1, 2])
        strategy = Script([])
        BacktestEngine(data).run(strategy)
        np.testing.assert_allclose(strategy.contexts[-1].history("close", adjusted=True)["S0"], [10, 10])

    def test_start_keeps_prior_history_but_does_not_trade_before_start(self):
        data = market([10, 10, 10, 10, 10])
        strategy = Script([{"S0": 1}, {}])
        result = BacktestEngine(data, BacktestConfig(start="2024-01-04", end="2024-01-05")).run(strategy)
        self.assertEqual(len(strategy.contexts[0].history("close")), 3)
        self.assertEqual(result.equity.index[0], data.dates[2])
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["date"], data.dates[3])
        self.assertGreater(result.equity.iloc[-1]["market_value"], 0)

    def test_bad_weights_fail_explicitly(self):
        data = market([10, 10])
        for target in ({"S0": -0.1}, {"S0": 1.1}, {"S0": np.nan}, {"UNKNOWN": 0.5}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                run(data, [target])
        class Duplicate:
            def on_close(self, context):
                return pd.Series([0.5, 0.5], index=["S0", "S0"])
        with self.assertRaises(ValueError):
            BacktestEngine(data).run(Duplicate())

    def test_invalid_data_alignment_rejected(self):
        data = market([10, 10])
        frames = dict(data.frames)
        frames["open"] = frames["open"].iloc[::-1]
        with self.assertRaises(ValueError):
            MarketData(frames)

    def test_invalid_config_and_empty_period(self):
        for config in ({"initial_cash": 0}, {"slippage": 1}, {"commission": -0.1}, {"lot_size": -1}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                BacktestConfig(**config)
        with self.assertRaises(ValueError):
            run(market([10]), [], start="2030-01-01")

    def test_monthly_strategy_uses_observed_period(self):
        data = market([10] * 5)
        result = BacktestEngine(data).run(EqualWeightStrategy("monthly"))
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["date"], data.dates[1])

    def test_future_prices_cannot_change_earlier_trades(self):
        first = run(market([10, 10, 10, 10]), [{"S0": 1}, {}])
        second = run(market([10, 10, 99, 999]), [{"S0": 1}, {}])
        pd.testing.assert_frame_equal(first.trades.iloc[:1], second.trades.iloc[:1])
        pd.testing.assert_frame_equal(first.equity.iloc[:2], second.equity.iloc[:2])

    def test_export_and_no_overwrite(self):
        result = run(market([10, 10]), [{"S0": 1}])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            result.save(output)
            self.assertEqual(json.loads((output / "metrics.json").read_text())["final_equity"], 1000)
            self.assertTrue((output / "equity.png").exists())
            self.assertEqual(len(pd.read_csv(output / "trades.csv")), 1)
            with self.assertRaises(FileExistsError):
                result.save(output)


if __name__ == "__main__":
    unittest.main()
