"""制度废止关系的批次编排与命令行测试。"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch


# 存储模块导入时需要完整配置；本测试全部替换数据库访问。
for _key, _value in {
    "CLOUDMINERU_API_KEY": "test",
    "PG_HOST": "test",
    "PG_PORT": "5432",
    "PG_USER": "test",
    "PG_PASSWORD": "test",
    "PG_DATABASE": "test",
    "MINIO_ENDPOINT": "test",
    "MINIO_ACCESS_KEY": "test",
    "MINIO_SECRET_KEY": "test",
    "MINIO_BUCKET_PDF": "test",
    "MINIO_BUCKET_IMAGES": "test",
}.items():
    os.environ.setdefault(_key, _value)

from policy.abolition import (
    extract_batch_abolition_relations,
    get_batch_abolition_relation_status,
    run_interactive_abolition_review,
)


class PolicyAbolitionCliTests(unittest.TestCase):
    """验证批次编排和废止审核命令的最小行为。"""

    def test_batch_extraction_keeps_unresolved_reference(self) -> None:
        """未能唯一匹配旧制度时仍必须保存待审核候选。"""
        clauses = [{
            "clause_id": "c1", "policy_id": "new", "raw_text": "《旧办法》即废止。",
            "page_start": 3, "page_end": 3,
        }]
        with (
            patch("policy.abolition.get_policy_documents_for_batch", return_value=[{"policy_id": "new"}]),
            patch("policy.abolition.get_policy_clauses", return_value=clauses),
            patch("policy.abolition.resolve_abolition_target", return_value=None),
            patch("policy.abolition.upsert_policy_document_relation") as save,
        ):
            result = extract_batch_abolition_relations("batch_1")
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["unresolved"], 1)
        self.assertIsNone(save.call_args.args[0].target_policy_id)

    def test_status_counts_review_and_resolution_states(self) -> None:
        """审核状态和目标解析状态必须分别准确汇总。"""
        rows = [
            {"review_status": "pending", "target_policy_id": "old_a"},
            {"review_status": "approved", "target_policy_id": ""},
            {"review_status": "rejected", "target_policy_id": "old_b"},
        ]
        with patch("policy.abolition.list_policy_document_relations", return_value=rows):
            self.assertEqual(get_batch_abolition_relation_status("batch_1"), {
                "total": 3, "pending": 1, "approved": 1, "rejected": 1,
                "resolved": 2, "unresolved": 1,
            })

    def test_full_help_lists_abolition_commands(self) -> None:
        """完整帮助必须暴露废止关系操作入口。"""
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--help-all"],
            capture_output=True,
            check=True,
        )
        self.assertIn(b"--extract-policy-abolition-relations", completed.stdout)

    def test_review_command_requires_nonempty_abolition_reviewer(self) -> None:
        """审核模式没有审核人时必须在连接数据库前失败。"""
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--review-policy-abolition-relations", "batch_1"],
            capture_output=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--abolition-reviewer", completed.stderr.decode(errors="replace"))

    def test_review_limit_must_be_positive(self) -> None:
        """零审核上限不能静默跳过候选。"""
        completed = subprocess.run(
            [
                sys.executable, "-B", "main.py", "--review-policy-abolition-relations", "batch_1",
                "--abolition-reviewer", "张三", "--abolition-review-limit", "0",
            ],
            capture_output=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--abolition-review-limit", completed.stderr.decode(errors="replace"))

    def test_explicit_review_limit_without_review_command_is_rejected(self) -> None:
        """防止显式传入默认值 20 时绕过仅审核命令可用的限制。"""
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--abolition-review-limit", "20"],
            capture_output=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--abolition-review-limit", completed.stderr.decode(errors="replace"))

    def test_target_action_submits_preserved_date_in_same_review(self) -> None:
        """防止指定目标后批准时丢失已识别日期。"""
        relation = {
            "relation_id": "relation_1", "target_policy_id": None,
            "effective_date": "2016-01-01", "review_status": "pending",
        }
        with (
            patch("policy.abolition.list_policy_document_relations", return_value=[relation]),
            patch("policy.abolition.get_policy_document", return_value={}),
            patch("builtins.input", side_effect=["t", "policy_old", ""]),
            patch("policy.abolition.review_policy_document_relation") as review,
        ):
            result = run_interactive_abolition_review("batch_1", "reviewer_1")

        self.assertEqual(result["approved"], 1)
        self.assertEqual(review.call_args.kwargs["target_policy_id"], "policy_old")
        self.assertEqual(review.call_args.kwargs["effective_date"], "2016-01-01")

    def test_date_action_submits_entered_target_in_same_review(self) -> None:
        """防止指定日期后批准未解析关系时未同时提交目标制度。"""
        relation = {
            "relation_id": "relation_1", "target_policy_id": None,
            "effective_date": None, "review_status": "pending",
        }
        with (
            patch("policy.abolition.list_policy_document_relations", return_value=[relation]),
            patch("policy.abolition.get_policy_document", return_value={}),
            patch("builtins.input", side_effect=["d", "2016-01-01", "policy_old"]),
            patch("policy.abolition.review_policy_document_relation") as review,
        ):
            result = run_interactive_abolition_review("batch_1", "reviewer_1")

        self.assertEqual(result["approved"], 1)
        self.assertEqual(review.call_args.kwargs["target_policy_id"], "policy_old")
        self.assertEqual(review.call_args.kwargs["effective_date"], "2016-01-01")


if __name__ == "__main__":
    unittest.main()
