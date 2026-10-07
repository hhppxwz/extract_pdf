"""验证同名制度版本匹配不能造成新制度自我废止。"""
import unittest
from contextlib import nullcontext
from unittest.mock import patch
from types import SimpleNamespace

from policy.abolition import build_abolition_relations, resolve_abolition_target
from policy import storage


class AbolitionVersionTests(unittest.TestCase):
    def test_source_is_excluded_from_title_and_number_matches(self):
        for number in ("", "武大党字〔2021〕98号"):
            with patch("policy.abolition.find_policy_documents_by_doc_number", return_value=[{"policy_id":"new"}]), patch(
                "policy.abolition.find_policy_documents_by_normalized_title", return_value=[{"policy_id":"new"}]
            ):
                self.assertIsNone(resolve_abolition_target("细则", number, source_policy_id="new"))

    def test_quoted_publication_year_selects_old_version_without_effective_date(self):
        documents = [{"policy_id":"new", "doc_number":"武大党字〔2021〕98号"},
                     {"policy_id":"old", "doc_number":"武大党字〔2017〕10号"}]
        with patch("policy.abolition.find_policy_documents_by_normalized_title", return_value=documents):
            relation = build_abolition_relations({"policy_id":"new", "clause_id":"c",
                "raw_text":"2017年印发的《细则》废止。"})[0]
        self.assertEqual(relation.target_policy_id,"old")
        self.assertIsNone(relation.effective_date)

    def test_mismatching_year_is_unresolved_even_if_title_unique(self):
        with patch("policy.abolition.find_policy_documents_by_normalized_title", return_value=[
            {"policy_id":"other", "doc_number":"武大党字〔2021〕98号"}
        ]):
            relation = build_abolition_relations({"policy_id":"new", "raw_text":"2017年印发的《细则》废止。"})[0]
        self.assertIsNone(relation.target_policy_id)

    def test_multiple_versions_same_year_remain_unresolved(self):
        with patch("policy.abolition.find_policy_documents_by_normalized_title", return_value=[
            {"policy_id":"a","doc_number":"字〔2017〕1号"}, {"policy_id":"b","doc_number":"字〔2017〕2号"}
        ]):
            self.assertIsNone(resolve_abolition_target("细则","",source_policy_id="new",target_issue_year=2017))

    def test_publication_year_falls_back_to_issue_date_but_not_effective_date(self):
        for field, expected in (("issue_date","old"),("effective_date",None)):
            with patch("policy.abolition.find_policy_documents_by_normalized_title",return_value=[
                {"policy_id":"old",field:"2017-01-01"}
            ]):
                self.assertEqual(resolve_abolition_target("细则","",target_issue_year=2017),expected)

    def test_approval_rejects_self_abolition_before_any_write(self):
        relation = {"review_status":"pending", "source_policy_id":"new", "target_policy_id":"new"}
        fake = SimpleNamespace(relational=SimpleNamespace(transaction=nullcontext))
        with patch.object(storage,"storage",fake), patch.object(storage,"lock_policy_document_relation",return_value=relation):
            with self.assertRaisesRegex(ValueError,"自身"):
                storage.review_policy_document_relation("r","approved","reviewer")

    def test_explicit_doc_number_does_not_fall_back_to_conflicting_title(self):
        with patch("policy.abolition.find_policy_documents_by_doc_number",return_value=[]), patch(
            "policy.abolition.find_policy_documents_by_normalized_title",return_value=[{"policy_id":"wrong"}]
        ):
            self.assertIsNone(resolve_abolition_target("细则","字〔2017〕1号"))

    def test_approval_rejects_publication_year_conflict(self):
        relation = {"review_status":"pending", "source_policy_id":"new", "target_policy_id":"wrong",
                    "target_title":"细则", "evidence_text":"2017年印发的《细则》废止。"}
        fake = SimpleNamespace(relational=SimpleNamespace(transaction=nullcontext))
        with patch.object(storage,"storage",fake), patch.object(storage,"lock_policy_document_relation",return_value=relation), patch.object(
            storage,"lock_policy_document",return_value={"policy_id":"wrong","doc_number":"字〔2021〕1号"}
        ):
            with self.assertRaisesRegex(ValueError,"年份"):
                storage.review_policy_document_relation("r","approved","reviewer")

    def test_self_abolition_is_rejected_when_saving_candidate(self):
        from models import PolicyDocumentRelation
        relation = PolicyDocumentRelation(relation_id="r",source_policy_id="new",target_policy_id="new",
                                          target_title="细则",evidence_clause_id="c",evidence_text="《细则》废止。")
        with patch.object(storage,"ensure_policy_tables",side_effect=AssertionError("不应写库")):
            with self.assertRaisesRegex(ValueError,"自身"):
                storage.upsert_policy_document_relation(relation)
