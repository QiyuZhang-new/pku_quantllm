"""明确提交才启动、参数冻结、无shell执行、互斥和停止检查。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_ui import jobs
from research_ui.worker import run


def request():
    return {"mode": "synthetic", "rounds": 1, "batch_size": 1, "scope": "120", "seeds": ["return20"], "initial_cash": 1_000_000}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_validation_rejects_unknown_inputs_and_oversized_budget(self):
        for change in ({"shell": "bad"}, {"rounds": 99}, {"seeds": ["alpha2"]}, {"seeds": []}, {"initial_cash": -1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                jobs.validate_request({**request(), **change})
        jobs.validate_request({**request(), "seeds": ["alpha101", "alpha33"]})
        jobs.validate_request({**request(), "search_method": "adaptive_gp", "random_seed": 11})
        for change in ({"search_method": "arbitrary_python"}, {"random_seed": -1}, {"random_seed": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                jobs.validate_request({**request(), **change})

    def test_listing_missing_jobs_does_not_create_directory(self):
        self.assertEqual(jobs.list_jobs(self.root), [])
        self.assertFalse((self.root / ".ui_jobs").exists())

    def test_reused_pid_does_not_count_as_this_research_worker(self):
        self.assertTrue(jobs.is_alive(os.getpid()))
        self.assertFalse(jobs.is_alive(os.getpid(), "b" * 32))

    def test_launch_freezes_request_and_uses_argument_array(self):
        with patch("research_ui.jobs.subprocess.Popen", return_value=Mock(pid=77)) as popen:
            value = jobs.launch(self.root, request())
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][1:4], ["-u", "-m", "research_ui.worker"])
        self.assertNotIn("shell", kwargs)
        saved = json.loads(jobs.job_path(self.root, value["id"], "request.json").read_text())
        self.assertEqual(saved["request"], request())
        self.assertFalse((self.root / value["campaign"]).exists())

    def test_shared_market_configuration_is_frozen_in_job(self):
        with patch("research_ui.jobs.subprocess.Popen", return_value=Mock(pid=77)):
            value = jobs.launch(self.root, request(), data_directory=self.root / "custom_data", status_directory=None)
        saved = json.loads(jobs.job_path(self.root, value["id"], "request.json").read_text())
        self.assertEqual(saved["sources"], {"data": str(self.root / "custom_data"), "status": None})

    def test_worker_uses_shared_market_configuration(self):
        (self.root / ".ui_jobs").mkdir()
        identifier = "c" * 32
        path = jobs.job_path(self.root, identifier, "request.json")
        jobs.atomic_json(path, {"id": identifier, "created_utc": "2026-11-06", "campaign": "shared_data",
            "runs_root": str(self.root), "request": {**request(), "mode": "historical", "scope": "full"},
            "sources": {"data": str(self.root / "custom_data"), "status": None}})
        with patch("research_ui.worker.discover", return_value={"attempt_count": 1}) as discover:
            self.assertEqual(run(path), 0)
        self.assertEqual(discover.call_args.args[1], self.root / "custom_data")
        self.assertIsNone(discover.call_args.args[5])
        self.assertEqual(discover.call_args.kwargs["search_method"], "rules")

    def test_active_job_blocks_duplicate_launch_and_cancel_only_marks_request(self):
        with patch("research_ui.jobs.subprocess.Popen", return_value=Mock(pid=77)):
            value = jobs.launch(self.root, request())
        with patch("research_ui.jobs.is_alive", return_value=True):
            with self.assertRaisesRegex(ValueError, "已有研究"):
                jobs.launch(self.root, request())
        jobs.cancel(self.root, value["id"])
        self.assertTrue(jobs.job_path(self.root, value["id"], "cancel").exists())

    def test_process_lock_prevents_concurrent_workers(self):
        (self.root / ".ui_jobs").mkdir()
        lock = jobs.task_lock(self.root)
        try:
            with self.assertRaises(ValueError):
                jobs.task_lock(self.root)
        finally:
            lock.close()
        jobs.task_lock(self.root).close()

    def test_worker_runs_development_without_opening_review(self):
        directory = self.root / ".ui_jobs"
        directory.mkdir()
        identifier = "a" * 32
        path = jobs.job_path(self.root, identifier, "request.json")
        jobs.atomic_json(path, {"id": identifier, "created_utc": "2026-10-30", "campaign": "test_worker",
                               "runs_root": str(self.root), "request": request()})
        self.assertEqual(run(path), 0)
        state = json.loads(jobs.job_path(self.root, identifier, "state.json").read_text())
        self.assertEqual(state["status"], "completed")
        self.assertFalse((self.root / "test_worker/review").exists())
        self.assertTrue((self.root / "test_worker/seal.json").exists())


if __name__ == "__main__":
    unittest.main()
