"""制度文档废止关系的抽取、匹配与审核写入测试。"""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch


# 存储模块导入时需要完整配置；本测试只替换关系存储，不连接真实服务。
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

from policy import storage as policy_storage
from policy.abolition import (
    build_abolition_relations,
    extract_batch_abolition_relations,
    extract_abolition_candidates,
    normalize_doc_number,
    resolve_abolition_target,
    run_interactive_abolition_review,
)
from models import PolicyDocumentRelation, PolicyDocumentRelationType, PolicyReviewStatus
from storage_adapter import PgStorageAdapter


class _FakeConnection:
    """模拟 PostgreSQL 连接，验证事务适配器的提交和自动提交恢复。"""

    def __init__(self) -> None:
        self.autocommit = True
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class PolicyAbolitionExtractionTests(unittest.TestCase):
    """验证废止条款仅提取明确引用且可追溯的候选。"""

    def test_extracts_date_title_number_and_evidence(self) -> None:
        """防止废止候选遗漏生效日期、文号或完整条款证据。"""
        clause = {
            "clause_id": "clause_104", "policy_id": "policy_new",
            "page_start": 18, "page_end": 18,
            "raw_text": "第一百零四条 本办法自2016年1月1日起施行，《武汉大学财务管理办法》（武大[2000]25号）即废止。",
        }
        candidate = extract_abolition_candidates(clause)[0]
        self.assertEqual(candidate["effective_date"], "2016-01-01")
        self.assertEqual(candidate["target_title"], "武汉大学财务管理办法")
        self.assertEqual(candidate["target_doc_number"], "武大[2000]25号")
        self.assertIn("即废止", candidate["evidence_text"])
        self.assertEqual(candidate["page_start"], 18)

    def test_missing_date_keeps_candidate(self) -> None:
        """防止没有生效日期时丢弃明确的废止声明。"""
        candidate = extract_abolition_candidates({
            "clause_id": "c1", "policy_id": "new",
            "raw_text": "《旧办法》（武大〔2000〕25号）同时废止。",
        })[0]
        self.assertIsNone(candidate["effective_date"])

    def test_missing_date_candidate_has_lower_confidence(self) -> None:
        """防止日期不明候选与有明确日期的候选使用相同置信度。"""
        with patch("policy.abolition.resolve_abolition_target", return_value=None):
            dated = build_abolition_relations({
                "clause_id": "dated", "policy_id": "new",
                "raw_text": "本办法自2016年1月1日起施行，《旧办法》即废止。",
            })[0]
            undated = build_abolition_relations({
                "clause_id": "undated", "policy_id": "new",
                "raw_text": "《旧办法》即废止。",
            })[0]
        self.assertLess(undated.confidence, dated.confidence)

    def test_doc_number_match_wins_over_title_candidate(self) -> None:
        """防止文号唯一命中后仍因标题候选覆盖目标。"""
        with patch("policy.abolition.find_policy_documents_by_doc_number", return_value=[{"policy_id": "old_2000"}]), \
             patch("policy.abolition.find_policy_documents_by_normalized_title") as by_title:
            self.assertEqual(resolve_abolition_target("武汉大学财务管理办法", "武大[2000]25号"), "old_2000")
        by_title.assert_not_called()

    def test_multiple_title_candidates_do_not_bind_target(self) -> None:
        """防止同名制度有多个候选时错误绑定任一目标。"""
        with patch("policy.abolition.find_policy_documents_by_doc_number", return_value=[]), \
             patch("policy.abolition.find_policy_documents_by_normalized_title", return_value=[
                 {"policy_id": "old_a"}, {"policy_id": "old_b"},
             ]):
            self.assertIsNone(resolve_abolition_target("武汉大学财务管理办法", ""))

    def test_candidate_requires_abolition_trigger_within_24_characters(self) -> None:
        """防止远距离的废止词误绑定到不相关制度名称。"""
        clause = {"clause_id": "c2", "policy_id": "new", "raw_text": "《旧办法》" + "补充说明" * 7 + "废止。"}
        self.assertEqual(extract_abolition_candidates(clause), [])

    def test_extracts_target_after_explicit_abolition_trigger(self) -> None:
        """防止“废止《目标》”语序遗漏明确的被废止制度。"""
        clause = {
            "clause_id": "c4", "policy_id": "new",
            "raw_text": "本办法施行后，废止《旧办法》（武大[2000]25号）。",
        }
        candidate = extract_abolition_candidates(clause)[0]
        self.assertEqual(candidate["target_title"], "旧办法")
        self.assertEqual(candidate["target_doc_number"], "武大[2000]25号")
        self.assertEqual(candidate["evidence_text"], clause["raw_text"])

    def test_extracts_target_before_adjacent_bare_abolition_trigger(self) -> None:
        """防止紧邻“《目标》废止”这一明确语序被遗漏。"""
        clause = {
            "clause_id": "c6", "policy_id": "new",
            "raw_text": "《旧办法》废止。",
        }
        candidate = extract_abolition_candidates(clause)[0]
        self.assertEqual(candidate["target_title"], "旧办法")
        self.assertEqual(candidate["evidence_text"], clause["raw_text"])

    def test_extracts_half_width_parenthesized_doc_number(self) -> None:
        """防止半角括号中的文号未随引用制度一并提取。"""
        clause = {
            "clause_id": "c7", "policy_id": "new",
            "raw_text": "《旧办法》(武大[2000]25号)一并废止。",
        }
        candidate = extract_abolition_candidates(clause)[0]
        self.assertEqual(candidate["target_doc_number"], "武大[2000]25号")

    def test_does_not_treat_general_reference_as_abolished_target(self) -> None:
        """防止“根据《文件》规定，本办法废止”误废止依据文件。"""
        clause = {
            "clause_id": "c5", "policy_id": "new",
            "raw_text": "根据《档案管理办法》规定，本办法废止。",
        }
        self.assertEqual(extract_abolition_candidates(clause), [])

    def test_rejects_cross_sentence_reference_after_abolition(self) -> None:
        """防止废止本办法时把下一句的依据制度误作目标。"""
        clause = {
            "clause_id": "c8", "policy_id": "new",
            "raw_text": "本办法废止。依据《档案管理办法》执行。",
        }
        self.assertEqual(extract_abolition_candidates(clause), [])

    def test_rejects_negated_abolition(self) -> None:
        """防止否定性废止表述被反向抽取为废止关系。"""
        clause = {
            "clause_id": "c9", "policy_id": "new",
            "raw_text": "不予废止《旧办法》。",
        }
        self.assertEqual(extract_abolition_candidates(clause), [])

    def test_rejects_common_negated_abolition_prefixes(self) -> None:
        """防止常见否定前缀在同句中仍被识别为废止关系。"""
        for raw_text in (
            "没有废止《旧办法》。",
            "未予废止《旧办法》。",
            "不再废止《旧办法》。",
            "并非废止《旧办法》。",
            "禁止废止《旧办法》。",
            "不应废止《旧办法》。",
        ):
            with self.subTest(raw_text=raw_text):
                self.assertEqual(extract_abolition_candidates({
                    "clause_id": "c9_prefix", "policy_id": "new", "raw_text": raw_text,
                }), [])

    def test_rejects_partial_clause_abolition(self) -> None:
        """防止制度局部条款废止被扩大为整份制度废止。"""
        clause = {
            "clause_id": "c10", "policy_id": "new",
            "raw_text": "废止《旧办法》第三条。",
        }
        self.assertEqual(extract_abolition_candidates(clause), [])

    def test_normalizes_bracket_forms_for_document_number_matching(self) -> None:
        """防止全半角括号差异导致同一文号无法匹配。"""
        self.assertEqual(normalize_doc_number(" 武大〔2000〕25号 "), "武大[2000]25号")
        self.assertEqual(normalize_doc_number("武大(2000)25号"), "武大[2000]25号")

    def test_builds_pending_abolition_relation_without_changing_document_status(self) -> None:
        """防止构建候选时伪造审核结论或修改制度状态。"""
        clause = {"clause_id": "c3", "policy_id": "new", "raw_text": "《旧办法》（武大[2000]25号）即废止。"}
        with patch("policy.abolition.resolve_abolition_target", return_value="old"):
            relation = build_abolition_relations(clause)[0]
        self.assertEqual(relation.target_policy_id, "old")
        self.assertEqual(relation.relation_type, PolicyDocumentRelationType.ABOLISHES)
        self.assertEqual(relation.review_status, PolicyReviewStatus.PENDING)


class PolicyAbolitionLookupTests(unittest.TestCase):
    """验证目标制度查询仅读取并执行规范化后的精确比较。"""

    def test_doc_number_lookup_matches_equivalent_bracket_form_without_writing(self) -> None:
        """防止文号查询忽略等价括号或发生意外写入。"""
        queries: list[tuple[str, str, int, tuple[object, ...]]] = []
        with (
            patch.object(policy_storage, "ensure_policy_tables") as ensure_tables,
            patch.object(
                policy_storage.storage.relational,
                "query",
                side_effect=lambda table, where, limit, params: queries.append(
                    (table, where, limit, params)
                ) or [{"policy_id": "old", "doc_number": "武大〔2000〕25号"}],
            ),
        ):
            result = policy_storage.find_policy_documents_by_doc_number("武大(2000)25号")
        self.assertEqual(result, [{"policy_id": "old", "doc_number": "武大〔2000〕25号"}])
        self.assertEqual(queries, [(policy_storage.TABLE_POLICY_DOCUMENTS, "", 100000, ())])
        ensure_tables.assert_not_called()

    def test_title_lookup_requires_normalized_exact_match(self) -> None:
        """防止标题查询退化为包含关系等模糊匹配。"""
        with (
            patch.object(policy_storage, "ensure_policy_tables") as ensure_tables,
            patch.object(policy_storage.storage.relational, "query", return_value=[
                {"policy_id": "exact", "title": "武汉大学 财务管理办法"},
                {"policy_id": "similar", "title": "武汉大学财务管理办法实施细则"},
            ]),
        ):
            result = policy_storage.find_policy_documents_by_normalized_title("武汉大学财务管理办法")
        self.assertEqual(result, [{"policy_id": "exact", "title": "武汉大学 财务管理办法"}])
        ensure_tables.assert_not_called()


class PolicyAbolitionStorageTests(unittest.TestCase):
    """验证人工审核才会改变被废止制度的效力状态。"""

    def test_approval_invalidates_resolved_target_with_effective_date(self) -> None:
        """防止人工批准后遗漏将目标制度标为失效并记录废止日期。"""
        relation = {
            "relation_id": "relation_1",
            "target_policy_id": "policy_old",
            "effective_date": "2016-01-01",
            "review_status": "pending",
        }
        updates: list[tuple[str, dict[str, object], str, tuple[object, ...]]] = []

        @contextmanager
        def transaction():
            yield

        with (
            patch.object(policy_storage, "lock_policy_document_relation", return_value=relation),
            patch.object(policy_storage, "lock_policy_document", return_value={"policy_id": "policy_old"}),
            patch.object(policy_storage, "find_policy_document_relation_date_conflict", return_value=None),
            patch.object(
                policy_storage.storage.relational,
                "transaction",
                wraps=transaction,
                create=True,
            ) as transaction_mock,
            patch.object(
                policy_storage.storage.relational,
                "update_rows",
                side_effect=lambda table, values, where, params: updates.append(
                    (table, values, where, params)
                ) or 1,
            ),
        ):
            policy_storage.review_policy_document_relation(
                "relation_1", "approved", "reviewer_1"
            )

        transaction_mock.assert_called_once_with()
        self.assertIn(
            (
                policy_storage.TABLE_POLICY_DOCUMENTS,
                {"validity_status": "invalid", "expiry_date": "2016-01-01"},
                '"policy_id" = %s',
                ("policy_old",),
            ),
            updates,
        )

    def test_conflicting_approved_date_does_not_update_target_document(self) -> None:
        """防止日期冲突的审核批准覆盖目标制度原有的废止日期。"""
        relation = {
            "relation_id": "relation_1",
            "target_policy_id": "policy_old",
            "effective_date": "2016-01-01",
            "review_status": "pending",
        }

        @contextmanager
        def transaction():
            yield

        with (
            patch.object(policy_storage, "lock_policy_document_relation", return_value=relation),
            patch.object(policy_storage, "lock_policy_document", return_value={"policy_id": "policy_old"}),
            patch.object(
                policy_storage,
                "find_policy_document_relation_date_conflict",
                return_value={"relation_id": "relation_2", "effective_date": "2018-01-01"},
            ),
            patch.object(policy_storage.storage.relational, "transaction", transaction),
            patch.object(policy_storage.storage.relational, "update_rows") as update_rows,
        ):
            with self.assertRaisesRegex(ValueError, "废止日期冲突"):
                policy_storage.review_policy_document_relation(
                    "relation_1", "approved", "reviewer_1"
                )

        update_rows.assert_not_called()

    def test_review_locks_relation_before_target_document(self) -> None:
        """防止审核未锁关系，导致并发审核覆盖同一候选。"""
        relation = {
            "relation_id": "relation_1", "target_policy_id": "policy_old",
            "effective_date": "2016-01-01", "review_status": "pending",
        }
        events: list[str] = []

        @contextmanager
        def transaction():
            yield

        with (
            patch.object(
                policy_storage, "lock_policy_document_relation",
                side_effect=lambda _relation_id: events.append("relation") or relation,
            ),
            patch.object(
                policy_storage, "lock_policy_document",
                side_effect=lambda _policy_id: events.append("document") or {"policy_id": "policy_old"},
            ),
            patch.object(policy_storage, "find_policy_document_relation_date_conflict", return_value=None),
            patch.object(policy_storage.storage.relational, "transaction", transaction),
            patch.object(policy_storage.storage.relational, "update_rows", return_value=1),
        ):
            policy_storage.review_policy_document_relation("relation_1", "approved", "reviewer_1")

        self.assertEqual(events, ["relation", "document"])

    def test_review_rejects_already_reviewed_relation_without_overwriting(self) -> None:
        """防止已审核关系再次审核后覆盖关系结论或目标制度。"""
        relation = {
            "relation_id": "relation_1", "target_policy_id": "policy_old",
            "effective_date": "2016-01-01", "review_status": "approved",
        }

        @contextmanager
        def transaction():
            yield

        with (
            patch.object(policy_storage, "lock_policy_document_relation", return_value=relation) as lock_relation,
            patch.object(policy_storage.storage.relational, "transaction", transaction),
            patch.object(policy_storage.storage.relational, "update_rows") as update_rows,
            patch.object(policy_storage, "lock_policy_document") as lock_document,
        ):
            with self.assertRaisesRegex(ValueError, "已审核"):
                policy_storage.review_policy_document_relation("relation_1", "rejected", "reviewer_2")

        lock_relation.assert_called_once_with("relation_1")
        lock_document.assert_not_called()
        update_rows.assert_not_called()

    def test_upsert_forces_candidate_back_to_pending_without_reviewer(self) -> None:
        """防止抽取端伪造审核结论绕过人工审核入口。"""
        relation = PolicyDocumentRelation(
            relation_id="relation_1",
            source_policy_id="policy_source",
            target_policy_id="policy_old",
            target_title="旧制度",
            target_doc_number="校发〔2010〕1号",
            relation_type=PolicyDocumentRelationType.ABOLISHES,
            effective_date="2016-01-01",
            evidence_clause_id="clause_1",
            evidence_text="本办法废止旧制度。",
            review_status=PolicyReviewStatus.APPROVED,
            reviewer="forged_reviewer",
            review_note="forged_note",
        )
        commands: list[tuple[str, tuple[object, ...]]] = []

        with (
            patch.object(policy_storage, "ensure_policy_tables"),
            patch.object(
                policy_storage.storage.relational,
                "execute",
                side_effect=lambda sql, params: commands.append((sql, params)),
            ),
            patch.object(
                policy_storage.storage.relational,
                "query",
                return_value=[{"relation_id": "relation_1"}],
            ),
        ):
            policy_storage.upsert_policy_document_relation(relation)

        self.assertEqual(commands[0][1][12:], ("pending", "", ""))

    def test_upsert_returns_persisted_relation_id_after_unique_conflict(self) -> None:
        """防止唯一冲突时返回未实际写入的新关系 ID。"""
        relation = PolicyDocumentRelation(
            relation_id="new_relation_id", source_policy_id="policy_source",
            target_policy_id="policy_old", target_title="旧制度", target_doc_number="",
            relation_type=PolicyDocumentRelationType.ABOLISHES,
            evidence_clause_id="clause_1", evidence_text="《旧制度》废止。",
        )
        with (
            patch.object(policy_storage, "ensure_policy_tables"),
            patch.object(policy_storage.storage.relational, "execute"),
            patch.object(
                policy_storage.storage.relational, "query",
                return_value=[{"relation_id": "persisted_relation_id"}],
            ),
        ):
            relation_id = policy_storage.upsert_policy_document_relation(relation)

        self.assertEqual(relation_id, "persisted_relation_id")

    def test_date_conflict_query_limits_target_approved_nonempty_different_dates(self) -> None:
        """防止冲突检查误匹配其他制度、未审核关系、空日期或同日期记录。"""
        queries: list[tuple[str, str, int, tuple[object, ...]]] = []

        with (
            patch.object(policy_storage, "ensure_policy_tables"),
            patch.object(
                policy_storage.storage.relational,
                "query",
                side_effect=lambda table, where, limit, params: queries.append(
                    (table, where, limit, params)
                ) or [],
            ),
        ):
            result = policy_storage.find_policy_document_relation_date_conflict(
                "policy_old", "2016-01-01", "relation_1"
            )

        self.assertIsNone(result)
        table, where, limit, params = queries[0]
        self.assertEqual(table, policy_storage.TABLE_POLICY_DOCUMENT_RELATIONS)
        self.assertEqual(limit, 1)
        self.assertIn('"target_policy_id" = %s', where)
        self.assertIn('"review_status" = %s', where)
        self.assertIn('"effective_date" IS NOT NULL', where)
        self.assertIn('"effective_date" <> %s', where)
        self.assertEqual(params, ("policy_old", "approved", "2016-01-01", "relation_1"))

    def test_postgres_adapter_transaction_commits_and_restores_autocommit(self) -> None:
        """防止新增事务接口提交后遗留关闭的自动提交配置。"""
        connection = _FakeConnection()
        adapter = PgStorageAdapter.__new__(PgStorageAdapter)
        adapter._conn = connection

        with adapter.transaction():
            self.assertFalse(connection.autocommit)

        self.assertTrue(connection.committed)
        self.assertTrue(connection.autocommit)


class PolicyAbolitionAcceptanceTests(unittest.TestCase):
    """验证抽取、候选保存和人工批准组成的完整废止流程。"""

    def test_pending_candidate_only_invalidates_old_document_after_approval(self) -> None:
        """防止候选保存阶段绕过人工审核，提前使旧制度失效。"""
        old_documents = {
            "policy_old": {
                "policy_id": "policy_old",
                "validity_status": "unknown",
                "expiry_date": None,
            }
        }
        persisted_relations: dict[str, dict[str, object]] = {}
        clauses = [{
            "clause_id": "clause_104",
            "policy_id": "policy_new",
            "page_start": 18,
            "page_end": 18,
            "raw_text": "本办法自2016年1月1日起施行，《武汉大学财务管理办法》（武大[2000]25号）即废止。",
        }]

        def persist_candidate(relation: PolicyDocumentRelation) -> str:
            persisted_relations[relation.relation_id] = relation.model_dump()
            return relation.relation_id

        @contextmanager
        def transaction():
            yield

        def update_in_memory(
            table: str, values: dict[str, object], _where: str, params: tuple[object, ...]
        ) -> int:
            record_id = str(params[0])
            if table == policy_storage.TABLE_POLICY_DOCUMENT_RELATIONS:
                persisted_relations[record_id].update(values)
            elif table == policy_storage.TABLE_POLICY_DOCUMENTS:
                normalized_values = {
                    key: value.isoformat() if hasattr(value, "isoformat") else value
                    for key, value in values.items()
                }
                old_documents[record_id].update(normalized_values)
            else:
                self.fail(f"意外更新表: {table}")
            return 1

        with (
            patch("policy.abolition.get_policy_documents_for_batch", return_value=[{"policy_id": "policy_new"}]),
            patch("policy.abolition.get_policy_clauses", return_value=clauses),
            patch("policy.abolition.resolve_abolition_target", return_value="policy_old"),
            patch("policy.abolition.upsert_policy_document_relation", side_effect=persist_candidate),
        ):
            summary = extract_batch_abolition_relations("batch_1")

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(len(persisted_relations), 1)
        relation_id, persisted_relation = next(iter(persisted_relations.items()))
        self.assertEqual(persisted_relation["review_status"], "pending")
        self.assertEqual(persisted_relation["effective_date"], "2016-01-01")
        self.assertEqual(old_documents["policy_old"]["validity_status"], "unknown")
        self.assertIsNone(old_documents["policy_old"]["expiry_date"])

        with (
            patch.object(policy_storage, "lock_policy_document_relation", side_effect=persisted_relations.get),
            patch.object(policy_storage, "lock_policy_document", side_effect=old_documents.get),
            patch.object(policy_storage, "find_policy_document_relation_date_conflict", return_value=None),
            patch.object(policy_storage.storage.relational, "transaction", transaction),
            patch.object(policy_storage.storage.relational, "update_rows", side_effect=update_in_memory),
        ):
            result = policy_storage.review_policy_document_relation(
                relation_id, "approved", "reviewer_1"
            )

        self.assertEqual(result["review_status"], "approved")
        self.assertEqual(persisted_relations[relation_id]["review_status"], "approved")
        self.assertEqual(old_documents["policy_old"]["validity_status"], "invalid")
        self.assertEqual(old_documents["policy_old"]["expiry_date"], "2016-01-01")


if __name__ == "__main__":
    unittest.main()
