"""制度条款义务、许可、禁止分类测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys


class PolicyClauseRuleClassificationTests(unittest.TestCase):
    def test_explicit_obligation_keeps_continuous_evidence(self) -> None:
        from policy.classification import classify_clause_by_rules

        result = classify_clause_by_rules("申请人应当提交完整的报销材料。")

        self.assertEqual(result.labels, ["obligation"])
        self.assertEqual(result.evidence, {"obligation": "应当"})
        self.assertEqual(result.source, "rule")

    def test_explicit_permission_is_recognized(self) -> None:
        from policy.classification import classify_clause_by_rules

        result = classify_clause_by_rules("学生可以申请延期办理。")

        self.assertEqual(result.labels, ["permission"])
        self.assertEqual(result.evidence, {"permission": "可以"})

    def test_explicit_prohibition_is_recognized(self) -> None:
        from policy.classification import classify_clause_by_rules

        result = classify_clause_by_rules("任何单位不得虚报费用。")

        self.assertEqual(result.labels, ["prohibition"])
        self.assertEqual(result.evidence, {"prohibition": "不得"})

    def test_mixed_clause_keeps_all_normative_labels(self) -> None:
        from policy.classification import classify_clause_by_rules

        result = classify_clause_by_rules("申请人不得拆分报销，并应当如实填写用途。")

        self.assertEqual(result.labels, ["obligation", "prohibition"])
        self.assertEqual(
            result.evidence,
            {"obligation": "应当", "prohibition": "不得"},
        )

    def test_clause_without_explicit_signal_requires_model(self) -> None:
        from policy.classification import classify_clause_by_rules

        result = classify_clause_by_rules("学校财务部门负责相关事项。")

        self.assertEqual(result.labels, [])
        self.assertTrue(result.needs_model)

    def test_empty_clause_is_other_without_model(self) -> None:
        from policy.classification import classify_clause_by_rules

        result = classify_clause_by_rules("   ")

        self.assertEqual(result.labels, ["other"])
        self.assertFalse(result.needs_model)


class PolicyClauseClassificationValidationTests(unittest.TestCase):
    def test_other_cannot_be_combined_with_normative_label(self) -> None:
        from policy.classification import validate_classification

        with self.assertRaisesRegex(ValueError, "other"):
            validate_classification(
                "本条为一般说明。",
                ["other", "obligation"],
                {"other": "本条为一般说明。", "obligation": "一般说明"},
            )

    def test_model_payload_supports_multiple_labels(self) -> None:
        from policy.classification import parse_model_classification

        result = parse_model_classification(
            "申请人不得隐瞒事实，并需承担相应责任。",
            {
                "labels": ["prohibition", "obligation"],
                "evidence": {"prohibition": "不得", "obligation": "需承担"},
                "reason": "同时包含禁止和义务。",
                "confidence": 0.88,
            },
        )

        self.assertEqual(result.labels, ["obligation", "prohibition"])
        self.assertEqual(result.source, "llm")

    def test_model_other_uses_original_clause_as_evidence(self) -> None:
        from policy.classification import parse_model_classification

        result = parse_model_classification(
            "本办法由财务部门负责解释。",
            {"labels": ["other"], "evidence": {}, "reason": "解释条款", "confidence": 0.8},
        )

        self.assertEqual(result.evidence, {"other": "本办法由财务部门负责解释。"})

    def test_model_unknown_label_is_rejected_instead_of_ignored(self) -> None:
        from policy.classification import parse_model_classification

        with self.assertRaisesRegex(ValueError, "非法"):
            parse_model_classification(
                "学生可以申请延期。",
                {
                    "labels": ["permission", "advice"],
                    "evidence": {"permission": "可以", "advice": "申请延期"},
                    "confidence": 0.8,
                },
            )


class PolicyClauseClassificationRunnerTests(unittest.TestCase):
    def test_rule_result_does_not_call_model(self) -> None:
        from policy.classification_runner import classify_policy_clause

        with patch("policy.classification_runner.call_classification_llm") as call_model:
            result = classify_policy_clause({"raw_text": "申请人必须提交材料。"})

        self.assertEqual(result.labels, ["obligation"])
        call_model.assert_not_called()

    def test_uncertain_rule_result_uses_model(self) -> None:
        from policy.classification import ClauseClassification
        from policy.classification_runner import classify_policy_clause

        model_result = ClauseClassification(
            labels=["other"],
            evidence={"other": "学校财务部门负责相关事项。"},
            reason="职责说明。",
            confidence=0.8,
            source="llm",
        )
        with patch(
            "policy.classification_runner.call_classification_llm",
            return_value=model_result,
        ) as call_model:
            result = classify_policy_clause({"raw_text": "学校财务部门负责相关事项。"})

        self.assertEqual(result, model_result)
        call_model.assert_called_once()

    def test_only_nonempty_article_paragraph_and_item_are_selected(self) -> None:
        from policy.classification_runner import select_classifiable_clauses

        clauses = [
            {"clause_id": "chapter", "level": "chapter", "raw_text": "第一章 总则"},
            {"clause_id": "article", "level": "article", "raw_text": "第一条 应当办理。"},
            {"clause_id": "paragraph", "level": "paragraph", "raw_text": "可以申请。"},
            {"clause_id": "item", "level": "item", "raw_text": "不得隐瞒。"},
            {"clause_id": "empty", "level": "article", "raw_text": "  "},
        ]

        selected = select_classifiable_clauses(clauses)

        self.assertEqual([item["clause_id"] for item in selected], ["article", "paragraph", "item"])

    def test_model_failure_marks_item_failed_for_resume(self) -> None:
        from policy.classification_runner import run_policy_clause_classification

        updates: list[tuple[str, dict]] = []
        with (
            patch("policy.storage.get_policy_classification_run", return_value={"run_id": "run_1"}),
            patch("policy.storage.get_policy_classification_items", return_value=[{
                "item_id": "item_1", "clause_id": "clause_1", "status": "pending",
            }]),
            patch("policy.storage.get_policy_clause", return_value={
                "clause_id": "clause_1", "raw_text": "财务部门负责审核。",
            }),
            patch("policy.storage.update_policy_classification_run"),
            patch("policy.storage.update_policy_classification_item", side_effect=lambda item_id, values: updates.append((item_id, values))),
            patch("policy.storage.upsert_policy_classification_result") as save_result,
            patch("policy.storage.refresh_policy_classification_run", return_value={"run_id": "run_1", "status": "partial_failed"}),
            patch("policy.classification_runner.call_classification_llm", side_effect=RuntimeError("模型不可用")),
        ):
            result = run_policy_clause_classification("run_1")

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(updates[-1][1]["status"], "failed")
        self.assertIn("模型不可用", updates[-1][1]["last_error"])
        save_result.assert_not_called()


class PolicyClassificationReviewTests(unittest.TestCase):
    def test_chinese_labels_round_trip(self) -> None:
        from policy.classification_review import format_labels, parse_reviewed_labels

        text = format_labels(["obligation", "prohibition"])

        self.assertEqual(text, "义务、禁止")
        self.assertEqual(parse_reviewed_labels(text), ["obligation", "prohibition"])

    def test_import_rejects_other_combined_with_normative_label(self) -> None:
        from policy.classification_review import parse_reviewed_labels

        with self.assertRaisesRegex(ValueError, "其他"):
            parse_reviewed_labels("其他、许可")

    def test_quality_metrics_include_exact_match_precision_and_recall(self) -> None:
        from policy.classification_review import calculate_classification_metrics

        metrics = calculate_classification_metrics([
            ({"obligation"}, {"obligation"}),
            ({"permission"}, {"permission", "prohibition"}),
        ])

        self.assertEqual(metrics["reviewed_count"], 2)
        self.assertEqual(metrics["exact_match_rate"], 0.5)
        self.assertEqual(metrics["labels"]["permission"]["precision"], 1.0)
        self.assertEqual(metrics["labels"]["prohibition"]["recall"], 0.0)

    def test_reviewed_labels_take_priority_over_system_labels(self) -> None:
        from policy.classification_review import effective_labels

        self.assertEqual(
            effective_labels({
                "labels": ["permission"],
                "review_status": "approved",
                "reviewed_labels": ["prohibition"],
            }),
            ["prohibition"],
        )

    def test_manual_review_uses_full_clause_as_evidence(self) -> None:
        import json
        from policy.storage import review_policy_classification_result

        with (
            patch("policy.storage.ensure_policy_tables"),
            patch("policy.storage.storage.relational.query", return_value=[{"result_id": "result_1"}]),
            patch("policy.storage.get_policy_clause", return_value={"raw_text": "申请人不得隐瞒事实。"}),
            patch("policy.storage.storage.relational.update_rows") as update_rows,
        ):
            review_policy_classification_result(
                "run_1", "clause_1", ["prohibition"], "张三", "已核对",
            )

        values = update_rows.call_args.args[1]
        self.assertEqual(
            json.loads(values["reviewed_evidence"]),
            {"prohibition": "申请人不得隐瞒事实。"},
        )


class PolicyClassificationCliTests(unittest.TestCase):
    def test_full_help_lists_classification_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, "main.py", "--help-all"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn(b"--classify-policy-clauses", completed.stdout)
        self.assertIn(b"--import-policy-classification-review", completed.stdout)

    def test_import_review_requires_reviewer(self) -> None:
        completed = subprocess.run(
            [sys.executable, "main.py", "--import-policy-classification-review", "review.csv"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"--classification-reviewer", completed.stderr)

    def test_each_label_requires_evidence_from_source_text(self) -> None:
        from policy.classification import validate_classification

        with self.assertRaisesRegex(ValueError, "原文"):
            validate_classification(
                "学生可以申请延期。",
                ["permission"],
                {"permission": "允许延期"},
            )


if __name__ == "__main__":
    unittest.main()
