"""同一入口往返、原账户导入、去重及来源保留。"""

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from lesson04.backtest import BacktestConfig, BacktestEngine, EqualWeightStrategy, MarketData
from lesson04.backtest.dashboard import saved_experiments
from quant_research.factors import FactorSpec
from quant_research.storage import Registry, write_json
from research_ui.data import load_campaign
from research_ui.integration import open_in_backtest


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        dates = pd.bdate_range("2024-01-02", periods=8)
        frame = pd.DataFrame({"000001.SZ": [10, 11, 12, 11, 13, 12, 13, 14], "600000.SH": [20] * 8}, index=pd.Index(dates, name="date"))
        frames = {"open": frame, "close": frame, "cumadj": frame * 0 + 1}
        for name, value in frames.items():
            value.reset_index().to_feather(self.data / f"{name}.feather")
        self.result = BacktestEngine(MarketData(frames), BacktestConfig(initial_cash=1e6)).run(EqualWeightStrategy("daily"))
        self.runs = self.root / "research"
        self.source = self.runs / "candidate"
        registry = Registry(self.source)
        manifest = {"synthetic": False, "created_utc": "2026-11-06T00:00:00Z", "protocol": {}}
        registry.create(manifest)
        write_json(self.source / "manifest.json", manifest)
        spec = FactorSpec("alpha33", {"op": "alpha101", "number": 33}, "测试研究候选")
        identifier = registry.reserve(1, spec.to_dict())
        registry.identify(identifier, spec)
        registry.finish(identifier, "rejected", {"reason": "未通过", "development_score": -.01}, "trials/0001")
        folder = self.source / "trials/0001/fold_01"
        write_json(folder / "account.json", {"passed": False, "strategy": self.result.metrics,
            "execution_config": asdict(self.result.config), "gates": {"positive_total_return": False}, "excess_total_return": -.01})
        stats = {"ic": {"mean": .03, "ir": .1}, "long_excess_net": {"mean": -.01}}
        write_json(folder / "evaluation.json", {"periods": {"train": stats, "validation": stats}, "gates": [
            {"period": "validation", "metric": "long_excess_net", "actual": -.01, "threshold": ">0", "passed": False}]})
        path = folder / "strategy"
        path.mkdir()
        for name in ("equity", "trades", "positions", "rejected_orders"):
            getattr(self.result, name).to_csv(path / f"{name}.csv", index=name == "equity")
        registry.transition("running", "sealed")
        self.campaign = load_campaign(self.source)
        self.output = self.root / "trading_runs"

    def test_account_import_is_idempotent_and_source_preserved(self):
        original = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.source.rglob("*") if path.is_file()}
        first = open_in_backtest(self.campaign, 1, "fold_01", self.output)
        second = open_in_backtest(self.campaign, 1, "fold_01", self.output)
        self.assertEqual(first.directory, second.directory)
        self.assertEqual(saved_experiments(self.output), [first.directory])
        pd.testing.assert_frame_equal(first.result.equity, self.result.equity, check_freq=False)
        self.assertTrue(first.experiment["research_source"]["research_only"])
        self.assertEqual(first.experiment["research_source"]["decision"], "开发拒绝")
        self.assertEqual(original, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.source.rglob("*") if path.is_file()})

    def test_holdout_and_path_escape_are_rejected(self):
        for window in ("历史审核", "../../unexpected"):
            with self.subTest(window=window), self.assertRaises(ValueError):
                open_in_backtest(self.campaign, 1, window, self.output)

    def test_same_app_opens_research_account_in_original_backtest_and_returns(self):
        env = {"QUANTLLM_RESEARCH_RUNS_DIR": str(self.runs), "QUANTLLM_DATA_DIR": str(self.data), "QUANTLLM_RUNS_DIR": str(self.output)}
        with patch.dict(os.environ, env):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "research_app.py"), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio("platform_section").value, "因子演化")
            app.button("open_in_backtest").click().run()
            self.assertFalse(app.exception, [error.message for error in app.exception])
            self.assertFalse(app.error, [error.value for error in app.error])
            self.assertEqual(app.radio("platform_section").value, "策略回测")
            self.assertEqual(len(app.metric), 8)
            app.radio("platform_section").set_value("因子演化").run()
            self.assertFalse(app.exception)
            self.assertTrue(app.selectbox("campaign"))


if __name__ == "__main__":
    unittest.main()
