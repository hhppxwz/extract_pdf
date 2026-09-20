"""制度文档时间治理与版本检索测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from policy.governance import apply_effective_date_governance

from policy.temporal import (
    classify_temporal_status,
    extract_effective_date,
    normalize_family_title,
    rank_temporal_candidates,
)


class PolicyTemporalTests(unittest.TestCase):
    @patch("policy.governance.insert_review_item")
    @patch("policy.governance.storage.relational.update_rows")
    def test_new_document_effective_date_is_saved(self, update_rows, insert_review) -> None:
        result = apply_effective_date_governance(
            "policy_1", ["本办法自2022年10月1日起施行。"], None
        )
        self.assertEqual(result["effective_date"], "2022-10-01")
        update_rows.assert_called_once()
        insert_review.assert_not_called()

    @patch("policy.governance.insert_review_item")
    @patch("policy.governance.storage.relational.update_rows")
    def test_existing_effective_date_is_not_overwritten(self, update_rows, insert_review) -> None:
        result = apply_effective_date_governance(
            "policy_1", ["本办法自2022年10月1日起施行。"], None, "2020-01-01"
        )
        self.assertEqual(result["status"], "preserved")
        update_rows.assert_not_called()
        insert_review.assert_not_called()

    def test_extracts_unique_explicit_effective_date(self) -> None:
        result = extract_effective_date(["本办法自2022年10月1日起施行。"], None)
        self.assertEqual(result, {"effective_date": "2022-10-01", "status": "identified"})

    def test_resolves_issue_date_relative_effective_date(self) -> None:
        result = extract_effective_date(["本办法自印发之日起施行。"], "2022-09-20")
        self.assertEqual(result, {"effective_date": "2022-09-20", "status": "identified"})

    def test_conflicting_dates_are_not_selected(self) -> None:
        result = extract_effective_date([
            "自2020年1月1日起施行。", "自2021年1月1日起执行。"
        ], None)
        self.assertEqual(result, {"effective_date": None, "status": "conflict"})

    def test_normalizes_notice_and_version_markers_for_family_candidate(self) -> None:
        self.assertEqual(
            normalize_family_title("关于印发《武汉大学财务管理办法（修订）》的通知"),
            "武汉大学财务管理办法",
        )

    def test_temporal_interval_is_left_closed_right_open(self) -> None:
        document = {"effective_date": "2016-01-01", "expiry_date": "2022-10-01"}
        self.assertEqual(classify_temporal_status(document, "2016-01-01"), "applicable")
        self.assertEqual(classify_temporal_status(document, "2022-10-01"), "inapplicable")

    def test_unknown_dates_rank_after_applicable_documents(self) -> None:
        candidates = [
            {"metadata": {"policy_id": "unknown"}, "score": 0.99},
            {"metadata": {"policy_id": "known"}, "score": 0.50},
        ]
        documents = {
            "unknown": {"policy_id": "unknown", "effective_date": None, "expiry_date": None},
            "known": {"policy_id": "known", "effective_date": "2020-01-01", "expiry_date": None},
        }
        ranked, warnings = rank_temporal_candidates(candidates, documents, "2021-01-01", 10)
        self.assertEqual([item["metadata"]["policy_id"] for item in ranked], ["known", "unknown"])
        self.assertEqual(ranked[1]["temporal_status"], "unknown")
        self.assertEqual(warnings, [])

    def test_future_and_expired_versions_are_excluded(self) -> None:
        candidates = [
            {"metadata": {"policy_id": "old"}, "score": 0.9},
            {"metadata": {"policy_id": "future"}, "score": 0.8},
            {"metadata": {"policy_id": "current"}, "score": 0.7},
        ]
        documents = {
            "old": {"effective_date": "2010-01-01", "expiry_date": "2020-01-01"},
            "future": {"effective_date": "2030-01-01", "expiry_date": None},
            "current": {"effective_date": "2020-01-01", "expiry_date": None},
        }
        ranked, _ = rank_temporal_candidates(candidates, documents, "2025-01-01", 10)
        self.assertEqual([item["metadata"]["policy_id"] for item in ranked], ["current"])

    def test_same_family_overlap_produces_warning(self) -> None:
        candidates = [
            {"metadata": {"policy_id": "v1"}, "score": 0.9},
            {"metadata": {"policy_id": "v2"}, "score": 0.8},
        ]
        documents = {
            "v1": {"family_id": "family_1", "effective_date": "2020-01-01"},
            "v2": {"family_id": "family_1", "effective_date": "2021-01-01"},
        }
        ranked, warnings = rank_temporal_candidates(candidates, documents, "2022-01-01", 10)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(len(warnings), 1)
        self.assertIn("family_1", warnings[0])


if __name__ == "__main__":
    unittest.main()
