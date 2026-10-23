"""验证实验复用、参数传递、页面交互和历史结果加载。"""

from dataclasses import asdict
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pandas as pd
from streamlit.testing.v1 import AppTest

from backtest import BacktestConfig, BacktestEngine, EqualWeightStrategy, MarketData
from backtest.dashboard import (
    archive_experiment, data_stamp, execute_experiment, inspect_data,
    load_experiment, saved_experiments,
)


APP = Path(__file__).resolve().parents[1] / "app.py"


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "feathers"
        self.runs_dir = self.root / "runs"
        self.data_dir.mkdir()
        dates = pd.bdate_range("2024-01-02", periods=5)
        self.symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
        for name in ("open", "close", "cumadj"):
            frame = pd.DataFrame({"date": dates})
            for index, symbol in enumerate(self.symbols):
                frame[symbol] = 1.0 if name == "cumadj" else pd.Series([10, 10, 11, 12, 13], dtype=float) + index
            frame.to_feather(self.data_dir / f"{name}.feather")

    def experiment(self):
        return execute_experiment(
            self.data_dir, self.symbols, BacktestConfig(initial_cash=100_000),
            "等权示例", "monthly", self.runs_dir,
        )

    def application(self):
        environment = patch.dict(os.environ, {
            "QUANTLLM_DATA_DIR": str(self.data_dir),
            "QUANTLLM_RUNS_DIR": str(self.runs_dir),
        })
        environment.start()
        self.addCleanup(environment.stop)
        return AppTest.from_file(str(APP), default_timeout=20).run()

    def assert_no_page_exception(self, app):
        self.assertFalse(app.exception, [error.message for error in app.exception])


class ExperimentTests(DashboardFixture):
    def test_metadata_does_not_require_loading_all_fields(self):
        info = inspect_data(self.data_dir)
        self.assertEqual(info["symbols"], tuple(self.symbols))
        self.assertEqual(len(info["dates"]), 5)
        stamp = data_stamp(self.data_dir)
        self.assertEqual({item[0] for item in stamp}, {"open", "close", "cumadj"})

    def test_web_result_matches_engine_and_preserves_parameters(self):
        run = self.experiment()
        expected = BacktestEngine(MarketData.from_feathers(self.data_dir), run.result.config).run(EqualWeightStrategy("monthly"))
        pd.testing.assert_frame_equal(run.result.equity, expected.equity)
        pd.testing.assert_frame_equal(run.result.trades, expected.trades)
        metadata = json.loads((run.directory / "experiment.json").read_text())
        self.assertEqual(metadata["config"], asdict(run.result.config))
        self.assertEqual(metadata["symbols"], self.symbols)
        self.assertEqual(len(metadata["data_files"]), 3)

    def test_history_round_trip_and_zip(self):
        run = self.experiment()
        loaded = load_experiment(run.directory)
        pd.testing.assert_frame_equal(loaded.result.equity, run.result.equity, check_freq=False)
        self.assertAlmostEqual(loaded.result.metrics["total_return"], run.result.metrics["total_return"])
        self.assertEqual(saved_experiments(self.runs_dir), [run.directory])
        with ZipFile(BytesIO(archive_experiment(run.directory))) as archive:
            self.assertEqual(set(archive.namelist()), {
                "equity.csv", "trades.csv", "positions.csv", "rejected_orders.csv",
                "config.json", "metrics.json", "experiment.json", "equity.png",
            })
            self.assertEqual(json.loads(archive.read("experiment.json"))["symbols"], self.symbols)

    def test_empty_universe_rejected_without_output(self):
        with self.assertRaises(ValueError):
            execute_experiment(self.data_dir, [], BacktestConfig(), "等权示例", "monthly", self.runs_dir)
        self.assertFalse(self.runs_dir.exists())

    def test_partial_output_not_listed_as_history(self):
        partial = self.runs_dir / "partial"
        partial.mkdir(parents=True)
        (partial / "equity.csv").write_text("date,equity\n")
        self.assertEqual(saved_experiments(self.runs_dir), [])


class ApplicationTests(DashboardFixture):
    def test_initial_page_does_not_run_backtest(self):
        app = self.application()
        self.assert_no_page_exception(app)
        self.assertEqual(len(app.metric), 0)
        self.assertFalse(self.runs_dir.exists())
        self.assertEqual(app.multiselect("symbols").value, self.symbols)

    def test_submit_render_filter_and_prepare_download(self):
        app = self.application()
        app.number_input("commission_bps").set_value(12.0)
        app.number_input("slippage_bps").set_value(7.0)
        app.selectbox("lot_mode").select("允许零碎份额")
        app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        self.assertEqual(len(app.metric), 8)
        active = app.session_state["active_run"]
        self.assertEqual(active.result.config.commission, 0.0012)
        self.assertEqual(active.result.config.slippage, 0.0007)
        self.assertEqual(active.result.config.lot_size, 0)
        self.assertEqual(len(active.result.trades), 3)
        self.assertEqual(len(app.get("vega_lite_chart")), 4)
        app.text_input(f"trade_symbol_{active.directory.name}").set_value("000001.SZ").run()
        self.assert_no_page_exception(app)
        trade_tables = [item.value for item in app.dataframe if "成交份额" in item.value.columns]
        self.assertEqual(len(trade_tables), 1)
        self.assertEqual(trade_tables[0]["证券代码"].tolist(), ["000001.SZ"])
        app.button("prepare_download").click().run()
        self.assert_no_page_exception(app)
        with ZipFile(BytesIO(app.session_state["download_archive"])) as archive:
            self.assertIn("equity.csv", archive.namelist())
        self.assertEqual(len(saved_experiments(self.runs_dir)), 1)

    def test_parameter_edit_does_not_change_completed_experiment(self):
        app = self.application()
        app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        first = app.session_state["active_run"]
        app.number_input("commission_bps").set_value(99.0).run()
        self.assert_no_page_exception(app)
        current = app.session_state["active_run"]
        self.assertEqual(current.directory, first.directory)
        self.assertEqual(current.result.config.commission, 0.0003)
        self.assertEqual(len(saved_experiments(self.runs_dir)), 1)

    def test_empty_universe_displays_validation_error(self):
        app = self.application()
        app.multiselect("symbols").set_value([])
        app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        self.assertTrue(any("至少选择" in error.value for error in app.error))
        self.assertFalse(self.runs_dir.exists())

    def test_historical_tax_default_and_explicit_fixed_override(self):
        app = self.application()
        app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        self.assertIsNone(app.session_state["active_run"].result.config.sell_tax)
        app.checkbox("historical_tax").uncheck().run()
        app.number_input("sell_tax_bps").set_value(10.0)
        app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        self.assertEqual(app.session_state["active_run"].result.config.sell_tax, .001)

    def test_all_symbols_mode_passes_complete_universe(self):
        app = self.application()
        app.radio("universe").set_value("全部证券").run()
        app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        self.assertEqual(app.session_state["active_run"].experiment["symbols"], self.symbols)

    def test_failed_run_keeps_previous_successful_result(self):
        app = self.application()
        app.button("run_backtest").click().run()
        first = app.session_state["active_run"]
        with patch("app.execute_experiment", side_effect=RuntimeError("测试失败")):
            # AppTest 将入口脚本放在独立模块；替换其依赖模块的引入源。
            with patch("backtest.dashboard.execute_experiment", side_effect=RuntimeError("测试失败")):
                app.button("run_backtest").click().run()
        self.assert_no_page_exception(app)
        self.assertEqual(app.session_state["active_run"].directory, first.directory)
        self.assertTrue(any("测试失败" in error.value for error in app.error))

    def test_history_load_does_not_rerun_engine(self):
        saved = self.experiment()
        app = self.application()
        app.radio("workspace").set_value("查看历史").run()
        with patch("backtest.dashboard.BacktestEngine.run", side_effect=AssertionError("不应重新计算")):
            app.button("load_run").click().run()
        self.assert_no_page_exception(app)
        self.assertEqual(app.session_state["active_run"].directory, saved.directory)
        self.assertEqual(len(app.metric), 8)

    def test_missing_data_still_allows_history(self):
        saved = self.experiment()
        app = self.application()
        with patch.dict(os.environ, {"QUANTLLM_DATA_DIR": str(self.root / "missing")}):
            app.radio("workspace").set_value("查看历史").run()
            app.button("load_run").click().run()
        self.assert_no_page_exception(app)
        self.assertEqual(app.session_state["active_run"].directory, saved.directory)
        self.assertEqual(len(app.metric), 8)


if __name__ == "__main__":
    unittest.main()
