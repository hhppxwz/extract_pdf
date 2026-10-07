"""批次状态必须区分实际运行、等待重试和等待启动。"""
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch

from batch_processor import _refresh_batch, get_batch_status, run_batch
from main import _print_batch_summary


class BatchStatusTests(unittest.TestCase):
    def setUp(self):
        self.batch = {"batch_id": "batch_test", "status": "running", "total_count": 5,
                      "started_at": datetime(2026, 9, 27, 17, 51), "finished_at": None}

    def snapshot(self, items):
        with patch("batch_processor.get_batch", return_value=self.batch), patch(
            "batch_processor.get_batch_items", return_value=items
        ):
            return get_batch_status("batch_test")

    def test_finished_command_with_retry_wait_is_not_running(self):
        items = [{"status": "succeeded"}] * 3 + [{"status": "skipped"},
                 {"status": "retry_wait", "error_kind": "transient", "last_error": "文件最近仍有单文件任务在处理"}]
        status = self.snapshot(items)
        self.assertEqual(status["batch"]["status"], "retry_wait")
        self.assertEqual(status["batch"]["succeeded_count"], 3)
        self.assertEqual(status["batch"]["retryable_failed_count"], 1)
        self.assertEqual(self.batch["status"], "running")

    def test_status_tracks_actual_items(self):
        cases = [
            ([{"status": "running"}, {"status": "retry_wait"}], "running"),
            ([{"status": "pending"}, {"status": "retry_wait"}], "pending"),
            ([{"status": "succeeded"}, {"status": "skipped"}], "succeeded"),
            ([{"status": "succeeded"}, {"status": "failed", "error_kind": "permanent"}], "partial_failed"),
            ([{"status": "failed", "error_kind": "permanent"}], "failed"),
            ([], "pending"),
        ]
        for items, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.snapshot(items)["batch"]["status"], expected)

    def test_old_transient_error_on_success_is_not_counted_as_retry(self):
        status = self.snapshot([{"status": "succeeded", "error_kind": "transient"}])
        self.assertEqual(status["batch"]["retryable_failed_count"], 0)

    def test_refresh_persists_waiting_state(self):
        saved = {}
        def update_batch(batch_id, values):
            saved.update(values)
            self.batch.update(values)
        with patch("batch_processor.get_batch", return_value=self.batch), patch(
            "batch_processor.get_batch_items", return_value=[{"status": "retry_wait", "error_kind": "transient"}]
        ), patch("batch_processor.update_batch", side_effect=update_batch):
            _refresh_batch("batch_test")
        self.assertEqual(saved["status"], "retry_wait")
        self.assertIsNone(saved["finished_at"])

    def test_summary_explains_which_file_is_waiting(self):
        status = {"batch": self.batch, "items": [{"file_name": "评选办法.doc", "status": "retry_wait",
                  "last_error": "文件最近仍有单文件任务在处理"}]}
        output = io.StringIO()
        with redirect_stdout(output):
            _print_batch_summary(status)
        self.assertIn("评选办法.doc", output.getvalue())
        self.assertIn("文件最近仍有单文件任务在处理", output.getvalue())
        self.assertIn("--resume-batch batch_test", output.getvalue())

    def test_runner_returns_retry_wait_when_file_is_deferred(self):
        items = [{"item_id": "item_wait", "file_name": "评选办法.doc", "status": "pending"}]
        def process_item(item, *args):
            item.update(status="retry_wait", error_kind="transient", last_error="文件被其他任务占用")
        def update_batch(batch_id, values):
            self.batch.update(values)
        with patch("batch_processor.get_batch", return_value=self.batch), patch(
            "batch_processor.get_batch_items", return_value=items
        ), patch("batch_processor.update_batch", side_effect=update_batch), patch(
            "batch_processor._process_item", side_effect=process_item
        ):
            status = run_batch("batch_test")
        self.assertEqual(status["batch"]["status"], "retry_wait")
        self.assertEqual(self.batch["status"], "retry_wait")
        self.assertEqual(status["batch"]["retryable_failed_count"], 1)
        self.assertIsNone(status["batch"]["finished_at"])

    def test_refresh_preserves_completed_timestamp(self):
        finished = datetime(2026, 9, 27, 18, 0)
        self.batch.update(status="succeeded", finished_at=finished)
        def update_batch(batch_id, values):
            self.batch.update(values)
        with patch("batch_processor.get_batch", return_value=self.batch), patch(
            "batch_processor.get_batch_items", return_value=[{"status": "succeeded"}]
        ), patch("batch_processor.update_batch", side_effect=update_batch):
            _refresh_batch("batch_test")
        self.assertEqual(self.batch["finished_at"], finished)
