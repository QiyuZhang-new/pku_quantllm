"""跨模块因果性、冻结审核、账户会计与失败保留的回归测试。"""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from lesson04.backtest import StrategyContext, MarketData
from lesson05.factor_eval import Market
from lesson05.factor_eval.data import universe
from quant_research.cli import demo_protocol
from quant_research.contracts import Fold, Protocol
from quant_research.evaluation import FactorStrategy, account_validation, development_batch
from quant_research.factors import FactorSpec, check_causality, compute_factor, propose
from quant_research.inputs import demo_inputs, load_inputs
from quant_research.pipeline import discover, review, select_finalists
from quant_research.storage import Registry


def quiet(message):
    pass


class ContractTests(unittest.TestCase):
    def test_calendar_boundaries_and_roundtrip(self):
        protocol = demo_protocol()
        self.assertEqual(Protocol.from_dict(protocol.to_dict()).to_dict(), protocol.to_dict())
        with self.assertRaises(ValueError):
            replace(protocol, holdout_end=protocol.development_end)
        with self.assertRaises(ValueError):
            Fold("2022-01-01T01:00:00", "2023-01-01")
        with self.assertRaises(ValueError):
            replace(protocol, folds=(protocol.folds[1], protocol.folds[0]))

    def test_budget_and_config_cannot_hide_boundaries(self):
        protocol = demo_protocol()
        for kwargs in ({"max_candidates": 65}, {"max_rounds": True},
                       {"evaluation": {"train_end": "2020-01-01"}},
                       {"evaluation": {"one_way_bps": -1}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(protocol, **kwargs)

    def test_expression_identity_and_commutative_dedup(self):
        a, b = {"op": "return", "window": 5}, {"op": "volatility", "window": 20}
        first = FactorSpec("first", {"op": "blend", "args": [a, b]}, "假设")
        second = FactorSpec("rename", {"op": "blend", "args": [b, a]}, "另一描述", "parent")
        self.assertEqual(first.factor_id, second.factor_id)
        self.assertNotEqual(first.factor_id, FactorSpec("a", a, "假设").factor_id)

    def test_arbitrary_code_and_negative_lags_are_rejected(self):
        for expression in ({"op": "eval", "code": "open('/tmp/x','w')"},
                           {"op": "return", "window": -1},
                           {"op": "return", "window": 5, "shift": -1}):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                FactorSpec("unsafe", expression, "假设")

    def test_feedback_changes_search_direction(self):
        parent = FactorSpec("ret20", {"op": "return", "window": 20}, "假设")
        coverage = list(propose(parent, {"failed_metrics": ["factor_coverage"]}))
        economic = list(propose(parent, {"failed_metrics": ["long_excess_net"]}))
        self.assertEqual(coverage[0].expression, {"op": "return", "window": 10})
        self.assertEqual(economic[0].expression["op"], "smooth")
        self.assertTrue(all(child.parent_id == parent.factor_id for child in coverage + economic))


class MarketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name) / "data"
        demo_inputs(cls.directory)
        cls.protocol = demo_protocol()
        cls.market, _ = load_inputs(cls.directory, cls.protocol.development_end)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_factor_arithmetic_and_causality(self):
        spec = FactorSpec("ret20", {"op": "return", "window": 20}, "假设")
        actual = check_causality(spec, self.market)
        close = self.market.frames["close"]
        np.testing.assert_allclose(actual.iloc[100], close.iloc[100] / close.iloc[80] - 1)

    def test_leaking_factor_fails_prefix_check(self):
        spec = FactorSpec("ret20", {"op": "return", "window": 20}, "假设")
        with patch("quant_research.factors.compute_factor", side_effect=lambda spec, market: market.frames["close"].shift(-1)):
            with self.assertRaises(AssertionError):
                check_causality(spec, self.market)

    def test_missing_status_not_invented(self):
        frames = {key: frame.copy() for key, frame in self.market.frames.items()}
        frames["is_st"].iloc[-1, 0] = np.nan
        # loader真实路径也须将未知状态从eligible排除。
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name, frame in frames.items():
                frame.rename_axis("date").reset_index().to_feather(directory / f"{name}.feather")
            actual, _ = load_inputs(directory, self.protocol.development_end)
            self.assertEqual(actual.frames["eligible"].iloc[-1, 0], 0)

    def test_development_rejects_full_holdout_input(self):
        market, _ = load_inputs(self.directory, self.protocol.holdout_end)
        with self.assertRaisesRegex(ValueError, "截断"):
            development_batch(market, {}, self.protocol.config(), 1)

    def test_development_has_no_test_metrics_and_counts_invalid_family(self):
        cfg = self.protocol.config()
        spec = FactorSpec("ret20", {"op": "return", "window": 20}, "假设")
        matrix = compute_factor(spec, self.market)
        results, _ = development_batch(self.market, {"ret": matrix}, cfg, 4)
        summary = results["ret"].summary
        self.assertEqual(set(summary["periods"]), {"train", "validation"})
        self.assertTrue(all(gate["period"] != "test" for gate in summary["gates"]))
        raw_p = summary["periods"]["validation"]["ic"]["p_value"]
        self.assertAlmostEqual(summary["periods"]["validation"]["ic"]["fdr_q"], min(1, 4 * raw_p))

    def test_adapter_matches_signal_day_quantile_and_ignores_future(self):
        cfg = self.protocol.config()
        spec = FactorSpec("ret20", {"op": "return", "window": 20}, "假设")
        matrix = compute_factor(spec, self.market)
        pool = universe(self.market, cfg.min_history, cfg.min_adv, cfg.top_n)
        row = 500
        context = StrategyContext(MarketData(self.market.frames), row, 1e6, 1e6,
                                  pd.Series(0., index=matrix.columns), pd.Series(0., index=matrix.columns))
        strategy = FactorStrategy(matrix, pool, cfg, self.market.dates[-1])
        weights = strategy.on_close(context)
        expected = matrix.iloc[row].where(pool.iloc[row]).rank(pct=True, method="average")
        selected = expected.index[np.ceil(expected * cfg.groups).eq(cfg.groups)]
        self.assertEqual(set(weights.index), set(selected))
        self.assertAlmostEqual(weights.sum(), 1.)
        changed = matrix.copy()
        changed.iloc[row + 1:] *= -100
        pd.testing.assert_series_equal(weights, FactorStrategy(changed, pool, cfg, self.market.dates[-1]).on_close(context))

    def test_account_uses_next_open_standard_limits_and_actual_costs(self):
        cfg = self.protocol.config()
        matrix = compute_factor(FactorSpec("ret20", {"op": "return", "window": 20}, "假设"), self.market)
        pool = universe(self.market, cfg.min_history, cfg.min_adv, cfg.top_n)
        summary, account, baseline = account_validation(self.market, matrix, pool, 1, cfg, self.protocol.execution)
        self.assertEqual(summary["execution_config"]["execution_policy"], "standard")
        self.assertTrue((account.trades.date > account.trades.signal_date).all())
        self.assertGreater(account.metrics["total_fees"], 0)
        self.assertGreater(account.metrics["total_slippage_cost"], 0)
        first_date = account.trades.date.min()
        buys = account.trades.loc[account.trades.date.eq(first_date) & account.trades.side.eq("buy")]
        signal_date = buys.signal_date.iloc[0]
        ranks = matrix.loc[signal_date].where(pool.loc[signal_date]).rank(pct=True, method="average")
        self.assertEqual(set(buys.symbol), set(ranks.index[np.ceil(ranks * cfg.groups).eq(cfg.groups)]))
        np.testing.assert_allclose(buys.fill_price, buys.open_price * (1 + self.protocol.execution.slippage))
        np.testing.assert_allclose(buys.commission, buys.notional * self.protocol.execution.commission)
        self.assertAlmostEqual(summary["excess_total_return"], account.metrics["total_return"] - baseline.metrics["total_return"])


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name) / "base"
        full = demo_protocol()
        cls.protocol = replace(full, folds=(full.folds[0],), holdout_end=full.development_end,
                               max_rounds=2, batch_size=1, max_candidates=2, max_finalists=1)
        cls.seed = FactorSpec("ret20", {"op": "return", "window": 20}, "趋势正对照").to_dict()
        cls.result = discover(cls.protocol, cls.base / "inputs", cls.base,
                              raw_seeds=[cls.seed], synthetic=True, progress=quiet)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.copy = Path(self.directory.name) / "campaign"
        shutil.copytree(self.base, self.copy)

    def test_feedback_loop_preserves_lineage_and_budget(self):
        attempts = self.result["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]["round"], 2)
        self.assertEqual(attempts[1]["spec"]["parent_id"], attempts[0]["factor_id"])
        self.assertEqual(self.result["state"], "sealed")
        self.assertFalse((self.base / "review").exists())
        self.assertNotIn("test", json.loads((self.base / "trials/0001/fold_01/evaluation.json").read_text())["periods"])

    def test_positive_control_can_be_reviewed_exactly_once(self):
        result = review(self.copy, progress=quiet)
        self.assertTrue(result["results"][0]["passed"])
        self.assertEqual(result["results"][0]["lifecycle"], "synthetic_control_passed")
        self.assertFalse(result["automatic_trading_activation"])
        with self.assertRaisesRegex(ValueError, "不可重复"):
            review(self.copy, progress=quiet)

    def test_closed_campaign_cannot_add_candidates(self):
        registry = Registry(self.copy)
        with self.assertRaisesRegex(ValueError, "封存"):
            registry.reserve(3, self.seed)

    def test_catalog_separates_development_and_review_lifecycle(self):
        before = Registry(self.copy).catalog()
        self.assertEqual(sum(item["lifecycle"] == "frozen_for_review" for item in before), 1)
        review(self.copy, progress=quiet)
        after = Registry(self.copy).catalog()
        self.assertEqual(sum(item["lifecycle"] == "synthetic_control_passed" for item in after), 1)
        self.assertFalse(any(item["lifecycle"] in {"active", "validated_alpha"} for item in after))

    def test_highly_correlated_factors_do_not_fill_finalist_budget(self):
        matrix = pd.read_parquet(self.base / "trials/0001/development_factor.parquet")
        specs = [FactorSpec("a", {"op": "return", "window": 20}, "假设"),
                 FactorSpec("b", {"op": "return", "window": 40}, "假设")]
        completed = [{"attempt_id": i + 1, "spec": spec, "feedback": {"passed": True, "development_score": .01},
                      "direction": 1, "artifact": "unused"} for i, spec in enumerate(specs)]
        # 完全相同的排序，即便公式ID不同也只能进入一个候选。
        selection = select_finalists(completed, {spec.factor_id: matrix for spec in specs},
                                     matrix.notna(), replace(self.protocol, max_finalists=2))
        self.assertEqual(len(selection["finalists"]), 1)
        self.assertEqual(selection["decisions"][1]["redundant_to"], specs[0].factor_id)

    def test_overwrite_is_refused(self):
        with self.assertRaises(FileExistsError):
            discover(self.protocol, self.base / "inputs", self.base, progress=quiet)

    def test_tampered_candidate_consumes_review_and_fails(self):
        path = self.copy / "trials/0001/factor.json"
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "产物已变化"):
            review(self.copy, progress=quiet)
        self.assertEqual(Registry(self.copy).metadata("state"), "review_error")
        with self.assertRaisesRegex(ValueError, "不可重复"):
            review(self.copy, progress=quiet)

    def test_changed_input_cannot_use_old_evidence(self):
        path = self.base / "inputs/amt.feather"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "快照已变化"):
                review(self.copy, progress=quiet)
        finally:
            path.write_bytes(original)

    def test_source_change_is_detected_before_holdout_loading(self):
        with patch("quant_research.pipeline.source_hashes", return_value={}):
            with patch("quant_research.pipeline.load_inputs") as loader:
                with self.assertRaisesRegex(ValueError, "源码已变化"):
                    review(self.copy, progress=quiet)
                loader.assert_not_called()

    def test_runtime_change_is_detected_before_holdout_loading(self):
        with patch("quant_research.pipeline.platform.python_version", return_value="0.0.0"):
            with patch("quant_research.pipeline.load_inputs") as loader:
                with self.assertRaisesRegex(ValueError, "环境版本已变化"):
                    review(self.copy, progress=quiet)
                loader.assert_not_called()

    def test_batch_failure_closes_and_preserves_pending_attempts(self):
        target = Path(self.directory.name) / "broken"
        with patch("quant_research.pipeline.development_batch", side_effect=ValueError("injected failure")):
            with self.assertRaisesRegex(ValueError, "injected failure"):
                discover(self.protocol, self.base / "inputs", target, raw_seeds=[self.seed], progress=quiet)
        self.assertEqual(Registry(target).metadata("state"), "development_error")
        self.assertEqual(Registry(target).attempts()[0]["status"], "error")
        self.assertTrue((target / "campaign_error.json").exists())

    def test_invalid_duplicate_and_failed_candidates_are_retained(self):
        target = Path(self.directory.name) / "mixed"
        protocol = replace(self.protocol, max_rounds=1, batch_size=4, max_candidates=4)
        clone = {**self.seed, "name": "renamed"}
        bad = {"name": "bad", "expression": {"op": "eval", "code": "1"}, "hypothesis": "坏定义"}
        result = discover(protocol, self.base / "inputs", target,
                          raw_seeds=[bad, self.seed, clone, ["invalid"]], progress=quiet)
        self.assertEqual([row["status"] for row in result["attempts"]],
                         ["invalid", "development_passed", "duplicate", "invalid"])
        self.assertEqual(result["attempt_count"], 4)
        self.assertTrue(json.loads((target / "manifest.json").read_text())["synthetic"])

    def test_all_rejected_does_not_open_holdout(self):
        target = Path(self.directory.name) / "rejected"
        protocol = replace(self.protocol, max_rounds=1, max_candidates=1,
                           evaluation={**self.protocol.evaluation, "min_days": 10000})
        result = discover(protocol, self.base / "inputs", target, raw_seeds=[self.seed], progress=quiet)
        self.assertEqual(result["attempts"][0]["status"], "rejected")
        with patch("quant_research.pipeline.load_inputs") as loader:
            with self.assertRaisesRegex(ValueError, "没有通过"):
                review(target, progress=quiet)
            loader.assert_not_called()

    def test_holdout_changes_do_not_change_development_feedback(self):
        other_data = Path(self.directory.name) / "other_inputs"
        shutil.copytree(self.base / "inputs", other_data)
        for name in ("open", "close", "amt", "cumadj"):
            path = other_data / f"{name}.feather"
            frame = pd.read_feather(path).set_index("date")
            frame.loc[frame.index > pd.Timestamp(self.protocol.development_end)] *= 3
            frame.reset_index().to_feather(path)
        target = Path(self.directory.name) / "other_campaign"
        protocol = replace(self.protocol, max_rounds=1, max_candidates=1)
        result = discover(protocol, other_data, target, raw_seeds=[self.seed], progress=quiet)
        self.assertEqual(result["attempts"][0]["feedback"], self.result["attempts"][0]["feedback"])


if __name__ == "__main__":
    unittest.main()
