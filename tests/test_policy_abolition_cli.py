"""制度废止关系的批次编排与命令行测试。"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from models import ProcessingResult, ProcessingStatus


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
    prompt_abolition_relation_insertion,
    reconcile_unresolved_abolition_relations,
    scan_policy_abolition_relations,
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

    def test_scan_without_candidate_does_not_request_confirmation(self) -> None:
        """防止没有识别到废止关系时仍打断用户。"""
        with (
            patch("policy.abolition.get_policy_clauses", return_value=[]),
            patch("policy.abolition.upsert_policy_document_relation") as save,
        ):
            result = scan_policy_abolition_relations(
                "policy_new", lambda _relation: self.fail("不应请求确认")
            )

        self.assertEqual(result, {"candidates": 0, "inserted": 0, "skipped": 0})
        save.assert_not_called()

    def test_scan_only_inserts_confirmed_candidate(self) -> None:
        """防止被用户跳过的候选仍然写入数据库。"""
        clauses = [
            {"clause_id": "c1", "policy_id": "policy_new", "raw_text": "《旧甲办法》废止。"},
            {"clause_id": "c2", "policy_id": "policy_new", "raw_text": "《旧乙办法》废止。"},
        ]
        with (
            patch("policy.abolition.get_policy_clauses", return_value=clauses),
            patch("policy.abolition.resolve_abolition_target", return_value=None),
            patch("policy.abolition.upsert_policy_document_relation") as save,
        ):
            result = scan_policy_abolition_relations(
                "policy_new", lambda relation: "insert" if relation.target_title == "旧甲办法" else "skip"
            )

        self.assertEqual(result, {"candidates": 2, "inserted": 1, "skipped": 1})
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.args[0].target_title, "旧甲办法")

    def test_scan_quit_skips_all_remaining_candidates(self) -> None:
        """防止用户结束询问后继续弹出候选或写库。"""
        clauses = [
            {"clause_id": "c1", "policy_id": "policy_new", "raw_text": "《旧甲办法》废止。"},
            {"clause_id": "c2", "policy_id": "policy_new", "raw_text": "《旧乙办法》废止。"},
        ]
        confirmations: list[str] = []

        def confirm(relation):
            confirmations.append(relation.target_title)
            return "quit"

        with (
            patch("policy.abolition.get_policy_clauses", return_value=clauses),
            patch("policy.abolition.resolve_abolition_target", return_value=None),
            patch("policy.abolition.upsert_policy_document_relation") as save,
        ):
            result = scan_policy_abolition_relations("policy_new", confirm)

        self.assertEqual(confirmations, ["旧甲办法"])
        self.assertEqual(result, {"candidates": 2, "inserted": 0, "skipped": 2})
        save.assert_not_called()

    def test_reconcile_approves_historical_relation_targeting_new_document(self) -> None:
        """防止目标制度后入库时历史废止关系一直保持未解析。"""
        relations = [{
            "relation_id": "relation_a_b", "source_policy_id": "policy_a",
            "target_policy_id": None, "target_title": "文件B", "target_doc_number": "B号",
            "effective_date": "2020-01-01", "review_status": "pending",
        }]
        with (
            patch("policy.abolition.list_policy_document_relations", return_value=relations),
            patch("policy.abolition.resolve_abolition_target", return_value="policy_b"),
            patch("policy.abolition.review_policy_document_relation") as review,
        ):
            result = reconcile_unresolved_abolition_relations(
                "policy_b", lambda _relation: "approve"
            )

        self.assertEqual(result, {"matched": 1, "approved": 1, "skipped": 0})
        review.assert_called_once_with(
            "relation_a_b",
            "approved",
            "pdf_import_interactive",
            review_note="目标制度后入库时经用户确认",
            target_policy_id="policy_b",
            effective_date="2020-01-01",
        )

    def test_reconcile_does_not_prompt_for_relation_targeting_another_document(self) -> None:
        """防止新制度与历史候选不匹配时误将其标记为失效。"""
        relations = [{
            "relation_id": "relation_a_c", "target_policy_id": None,
            "target_title": "文件C", "target_doc_number": "C号", "review_status": "pending",
        }]
        with (
            patch("policy.abolition.list_policy_document_relations", return_value=relations),
            patch("policy.abolition.resolve_abolition_target", return_value="policy_c"),
            patch("policy.abolition.review_policy_document_relation") as review,
        ):
            result = reconcile_unresolved_abolition_relations(
                "policy_b", lambda _relation: self.fail("不应请求确认")
            )

        self.assertEqual(result, {"matched": 0, "approved": 0, "skipped": 0})
        review.assert_not_called()

    def test_prompt_retries_invalid_input_and_eof_quits_future_prompts(self) -> None:
        """防止非法输入被误接受，并确保输入流结束时不阻塞后续处理。"""
        relation = type("Relation", (), {
            "source_policy_id": "policy_new", "target_title": "旧办法",
            "target_doc_number": "文号1", "target_policy_id": None,
            "effective_date": None, "page_start": 3, "page_end": 3,
            "evidence_text": "《旧办法》废止。",
        })()
        with (
            patch("policy.abolition.get_policy_document", return_value={"title": "新办法"}),
            patch("builtins.input", side_effect=["x", EOFError]),
            patch("builtins.print") as output,
        ):
            prompt = prompt_abolition_relation_insertion()
            self.assertEqual(prompt(relation), "quit")
            self.assertEqual(prompt(relation), "quit")

        self.assertEqual(sum("请输入 y、n 或 q" in str(call) for call in output.call_args_list), 1)

    def test_single_file_command_enables_abolition_confirmation(self) -> None:
        """防止单文件解析遗漏废止关系确认回调。"""
        result = ProcessingResult(file_id="pdf_1", status=ProcessingStatus.DONE)
        fake_document = type("Document", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *_args: None,
            "__len__": lambda self: 1,
        })()
        with (
            patch("fitz.open", return_value=fake_document),
            patch("pipeline.process_pdf", return_value=result) as process,
        ):
            from main import run_process
            run_process("policy.pdf")

        self.assertIsNotNone(process.call_args.kwargs["abolition_confirmation"])
        self.assertIsNotNone(process.call_args.kwargs["abolition_approval_confirmation"])

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
