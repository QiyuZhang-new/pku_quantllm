"""研究界面的只读边界、会计展示及交互验证。"""

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from quant_research.factors import FactorSpec
from quant_research.formula_search import FormulaSearch
from quant_research.storage import Registry, write_json
from research_ui.data import contained, curve_frame, lineage_dot, list_campaigns, load_campaign


APP = Path(__file__).resolve().parents[2] / "research_app.py"


def fixture(root, name, synthetic=False, search_method=None):
    directory = root / name
    manifest = {"synthetic": synthetic, "created_utc": "2026-10-30T00:00:00Z", "protocol": {"max_rounds": 2}}
    if search_method:
        manifest["search"] = FormulaSearch(search_method, 7).configuration()
    registry = Registry(directory)
    registry.create(manifest)
    write_json(directory / "manifest.json", manifest)
    parent = FactorSpec("return20", {"op": "return", "window": 20}, "初始假设")
    child = FactorSpec("return20_smooth", {"op": "smooth", "window": 3, "arg": parent.expression}, "后继假设", parent.factor_id)
    for index, spec in enumerate((parent, child), 1):
        attempt_id = registry.reserve(index, spec.to_dict())
        registry.identify(attempt_id, spec)
        artifact = f"trials/{attempt_id:04d}"
        registry.finish(attempt_id, "rejected", {"reason": "净超额未通过", "development_score": -.001,
                                              "failed_metrics": ["positive_executable_excess"]}, artifact)
        folder = directory / artifact / "fold_01"
        stats = {"ic": {"mean": .03}, "long_excess_net": {"mean": -.001}}
        write_json(folder / "evaluation.json", {"periods": {"train": stats, "validation": stats},
            "gates": [{"period": "validation", "metric": "rank_ic", "actual": .03, "threshold": .02, "passed": True},
                      {"period": "validation", "metric": "long_excess_net", "actual": -.001, "threshold": ">0", "passed": False}]})
        write_json(folder / "account.json", {"execution_config": {"initial_cash": 100},
            "strategy": {"total_return": .05}, "excess_total_return": -.05,
            "gates": {"has_trades": True, "positive_executable_excess": False}})
        for account, values in (("strategy", [99, 110, 105]), ("baseline", [100, 110, 110])):
            path = folder / account
            path.mkdir()
            pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=3), "equity": values}).to_csv(path / "equity.csv", index=False)
            pd.DataFrame({"date": ["2024-01-02"], "symbol": ["000001.SZ"]}).to_csv(path / "trades.csv", index=False)
            (path / "rejected_orders.csv").write_text("\n")
    registry.transition("running", "sealed")
    if search_method:
        search = FormulaSearch(search_method, 7)
        completed = [{"attempt_id": index, "spec": spec, "feedback": {"passed": False, "reason": "未准入"},
                      "folds": [{"account": {"excess_total_return": -.1 / index, "strategy": {"total_return": .01}}}]}
                     for index, spec in enumerate((parent, child), 1)]
        search.origins[child.factor_id] = {"operator": "smooth", "parent_id": parent.factor_id}
        for number in (1, 2):
            visible = completed[:number]
            observations = search.observe(visible)
            write_json(directory / f"search/round_{number:04d}.json", search.snapshot(visible, number, observations, []))
    (directory / "REPORT.md").write_text("# 测试研究报告\n")
    return directory


class ResearchUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.real = fixture(self.root, "real_verified")
        self.demo = fixture(self.root, "demo_verified", synthetic=True)
        environment = patch.dict(os.environ, {"QUANTLLM_RESEARCH_RUNS_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=20).run()
        self.assertFalse(app.exception, [error.message for error in app.exception])
        self.assertFalse(app.error, [error.value for error in app.error])
        return app

    def snapshot(self):
        return {str(path.relative_to(self.root)): sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_initial_view_prefers_real_and_preserves_all_evidence(self):
        original = self.snapshot()
        app = self.app()
        self.assertEqual(app.selectbox("campaign").value, "real_verified")
        self.assertEqual([metric.value for metric in app.metric[:4]], ["2", "2", "0", "0"])
        self.assertGreaterEqual(len(app.get("vega_lite_chart")), 4)
        self.assertEqual(original, self.snapshot())

    def test_switch_campaign_candidate_and_preview_trades(self):
        original = self.snapshot()
        app = self.app()
        app.selectbox("campaign").select("demo_verified").run()
        self.assertTrue(app.warning)
        app.selectbox("attempt_id").select(2).run()
        app.checkbox("show_trades").check().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertTrue(any("symbol" in table.value.columns for table in app.dataframe))
        self.assertEqual(original, self.snapshot())

    def test_development_has_no_holdout_selector(self):
        app = self.app()
        self.assertEqual(app.selectbox("period_fold_01").options, ["训练期", "开发验证期"])
        self.assertNotIn("历史审核", app.selectbox("fold").options)

    def test_filtering_does_not_change_full_experiment_metrics(self):
        app = self.app()
        values = [metric.value for metric in app.metric]
        app.multiselect("status_filter").set_value(["开发拒绝"]).run()
        self.assertEqual(values, [metric.value for metric in app.metric])

    def test_replay_slider_hides_future_candidates(self):
        app = self.app()
        app.slider("replay_real_verified").set_value(1).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].value, "1")
        self.assertEqual(app.selectbox("attempt_id").options[0].split(" · ")[0], "#1")
        self.assertEqual(len(app.selectbox("attempt_id").options), 1)

    def test_new_research_requires_explicit_submission(self):
        with patch("research_ui.jobs.launch", return_value={"id": "a" * 32}) as launcher:
            app = self.app()
            launcher.assert_not_called()
            app.button("start_study").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(launcher.call_count, 1)
            self.assertEqual(launcher.call_args.args[1]["seeds"], ["alpha33", "alpha101", "return5", "volatility20"])
            self.assertEqual(launcher.call_args.args[1]["search_method"], "adaptive_gp")

    def test_formula_example_navigation_and_feedback_replay_preserve_evidence(self):
        fixture(self.root, "formula_saved", search_method="adaptive_gp")
        write_json(self.root / "formula_comparison/comparison.json", {"arms": [
            {"method": "adaptive_gp", "campaign": "formula_saved"},
            {"method": "random_gp", "campaign": "real_verified"}]})
        (self.root / "formula_comparison/REPORT.md").write_text("# 实例对照\n")
        original = self.snapshot()
        app = self.app()
        app.button("example_adaptive_gp").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox("campaign").value, "formula_saved")
        stats = next(table.value for table in app.dataframe if "已评价子代" in table.value.columns)
        self.assertEqual(stats.loc[stats["变异算子"].eq("smooth"), "优于实际父代"].iloc[0], 1)
        app.slider("replay_formula_saved").set_value(1).run()
        self.assertFalse(app.exception)
        early = next(table.value for table in app.dataframe if "已评价子代" in table.value.columns)
        self.assertEqual(early["已评价子代"].sum(), 0)
        self.assertEqual(original, self.snapshot())

    def test_missing_database_is_not_created(self):
        missing = self.root / "missing"
        with self.assertRaises(Exception):
            load_campaign(missing)
        self.assertFalse(missing.exists())

    def test_paths_and_symlinks_cannot_escape_campaign(self):
        with self.assertRaises(ValueError):
            contained(self.real, "../demo_verified/manifest.json")
        (self.real / "escape").symlink_to(self.demo)
        with self.assertRaises(ValueError):
            contained(self.real, "escape/manifest.json")

    def test_nav_and_drawdown_include_initial_cash(self):
        curve = curve_frame(self.real, "trials/0001/fold_01/strategy/equity.csv", 100, "策略")
        self.assertAlmostEqual(curve.nav.iloc[0], .99)
        self.assertAlmostEqual(curve.drawdown.iloc[0], -.01)
        self.assertAlmostEqual(curve.drawdown.iloc[-1], 105 / 110 - 1)

    def test_lineage_contains_actual_parent_child_edge(self):
        campaign = load_campaign(self.real)
        parent = campaign["attempts"][0]["factor_id"]
        child = campaign["attempts"][1]["factor_id"]
        self.assertIn(f"{json.dumps(parent)} -> {json.dumps(child)}", lineage_dot(campaign))

    def test_empty_runs_page_does_not_write_files(self):
        empty = self.root / "empty"
        with patch.dict(os.environ, {"QUANTLLM_RESEARCH_RUNS_DIR": str(empty)}):
            app = AppTest.from_file(str(APP)).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.info)
        self.assertFalse(empty.exists())


if __name__ == "__main__":
    unittest.main()
