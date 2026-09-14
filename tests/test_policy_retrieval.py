"""跨制度条款级检索的真实数据转换行为测试。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from policy.retrieval import (
    POLICY_CLAUSE_INDEX_NAME,
    build_clause_index_text,
    build_clause_metadata,
    build_policy_search_response,
    is_searchable_policy_clause,
    rerank_clause_candidates,
    search_indexed_policy_clauses,
    sync_policy_clause_index,
)


class PolicyRetrievalRuleTests(unittest.TestCase):
    """验证条款而非标题或文本块成为检索结果。"""

    def test_heading_clause_is_not_searchable_but_article_is_searchable(self) -> None:
        """防止章节标题占据结果，掩盖真正可引用的办事条款。"""
        self.assertFalse(is_searchable_policy_clause({
            "level": "chapter",
            "raw_text": "第三章 差旅费报销",
        }))
        self.assertTrue(is_searchable_policy_clause({
            "level": "article",
            "raw_text": "出差人员报销差旅费应提供发票。",
        }))

    def test_metadata_preserves_original_citation_fields(self) -> None:
        """防止入索引后丢失制度名、条款号、页码或原文证据。"""
        clause = {
            "clause_id": "clause_1",
            "policy_id": "policy_1",
            "level": "item",
            "chapter_path": ["第三章 差旅管理"],
            "article_no": "第十二条",
            "paragraph_no": "第一款",
            "item_no": "（一）",
            "page_start": 8,
            "page_end": 8,
            "raw_text": "报销差旅费应提交发票。",
            "search_text": "第三章 差旅管理 第十二条 报销差旅费应提交发票。",
            "structure_version": "policy-structure-v1",
        }
        document = {
            "file_id": "file_1",
            "file_name": "差旅费管理办法.pdf",
            "title": "差旅费管理办法",
        }

        metadata = build_clause_metadata(clause, document)

        self.assertEqual(metadata["clause_id"], "clause_1")
        self.assertEqual(metadata["title"], "差旅费管理办法")
        self.assertEqual(metadata["item_no"], "（一）")
        self.assertEqual(metadata["page_start"], 8)
        self.assertEqual(metadata["raw_text"], "报销差旅费应提交发票。")
        self.assertIn("第三章 差旅管理", build_clause_index_text(clause))

    def test_exact_policy_terms_raise_relevant_clause_and_deduplicate_clause_id(self) -> None:
        """防止语义近似但无材料要求的条款压过精确命中的报销条款，或重复返回同一条。"""
        candidates = [
            {
                "similarity": 0.91,
                "metadata": {
                    "clause_id": "c_general",
                    "search_text": "出差人员应厉行节约。",
                    "raw_text": "出差人员应厉行节约。",
                },
            },
            {
                "similarity": 0.88,
                "metadata": {
                    "clause_id": "c_material",
                    "search_text": "差旅费报销应提交发票和审批单。",
                    "raw_text": "差旅费报销应提交发票和审批单。",
                },
            },
            {
                "similarity": 0.87,
                "metadata": {
                    "clause_id": "c_material",
                    "search_text": "差旅费报销应提交发票和审批单。",
                    "raw_text": "差旅费报销应提交发票和审批单。",
                },
            },
        ]

        ranked = rerank_clause_candidates("差旅费报销发票", candidates, top_k=3)

        self.assertEqual([item["metadata"]["clause_id"] for item in ranked], ["c_material", "c_general"])
        self.assertGreater(ranked[0]["keyword_score"], 0.0)

    def test_search_response_returns_citable_clause_not_generated_answer(self) -> None:
        """防止 API 只给摘要或相似文本，缺少用户核验所需的出处。"""
        response = build_policy_search_response("差旅报销材料", [{
            "similarity": 0.9,
            "keyword_score": 0.03,
            "score": 0.93,
            "metadata": {
                "clause_id": "c1",
                "policy_id": "policy_1",
                "file_id": "file_1",
                "title": "差旅费管理办法",
                "file_name": "差旅费管理办法.pdf",
                "article_no": "第十二条",
                "paragraph_no": "",
                "item_no": "",
                "chapter_path": ["第三章 差旅管理"],
                "page_start": 8,
                "page_end": 8,
                "raw_text": "出差人员报销差旅费应提交发票和审批单。",
            },
        }])

        result = response["results"][0]
        self.assertEqual(response["query"], "差旅报销材料")
        self.assertEqual(result["policy"]["title"], "差旅费管理办法")
        self.assertEqual(result["clause"]["clause_no"], "第十二条")
        self.assertEqual(result["clause"]["page_start"], 8)
        self.assertEqual(result["clause"]["raw_text"], "出差人员报销差旅费应提交发票和审批单。")

    def test_nested_clause_keeps_full_citation_number(self) -> None:
        """防止结果只显示“（一）”而无法定位到具体条款层级。"""
        response = build_policy_search_response("差旅报销材料", [{
            "similarity": 0.9,
            "metadata": {
                "clause_id": "c2",
                "article_no": "第十二条",
                "paragraph_no": "第一款",
                "item_no": "（一）",
                "raw_text": "应提交发票。",
            },
        }])

        self.assertEqual(
            response["results"][0]["clause"]["clause_no"],
            "第十二条 第一款 （一）",
        )

    def test_cli_full_help_exposes_policy_clause_index_commands(self) -> None:
        """防止已实现的重建能力没有出现在完整帮助中。"""
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--help-all"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--rebuild-policy-clause-index", completed.stdout)
        self.assertIn("--resume-policy-clause-index", completed.stdout)
        self.assertIn("--policy-clause-index-status", completed.stdout)


class PolicyPackageLayoutTests(unittest.TestCase):
    """验证制度领域模块从统一包导入，防止目录重构留下分散入口。"""

    def test_policy_modules_are_available_from_one_package(self) -> None:
        """防止迁移后只移动了文件却遗漏任一制度处理能力。"""
        from policy import (
            extraction,
            pipeline,
            process,
            process_runner,
            quality,
            retrieval,
            reviewer,
            storage,
        )

        self.assertTrue(callable(pipeline.structure_policy_document))
        self.assertTrue(callable(retrieval.search_indexed_policy_clauses))
        self.assertTrue(callable(storage.get_policy_clauses))
        self.assertTrue(callable(process.classify_process_clause))
        self.assertTrue(callable(process_runner.run_policy_process_classification))
        self.assertTrue(callable(extraction.run_policy_extraction))
        self.assertTrue(callable(reviewer.run_interactive_policy_review))
        self.assertTrue(callable(quality.write_quality_report))


@unittest.skipUnless(
    os.getenv("POLICY_RETRIEVAL_TEST_DATABASE"),
    "未配置 POLICY_RETRIEVAL_TEST_DATABASE，跳过真实 PostgreSQL/pgvector 集成测试",
)
class PolicyRetrievalIntegrationTests(unittest.TestCase):
    """只在隔离测试库中验证真实嵌入、真实向量和真实条款。"""

    def setUp(self) -> None:
        """只允许显式指定、名称明确的测试数据库承载写入验证。"""
        database = str(os.environ["POLICY_RETRIEVAL_TEST_DATABASE"]).strip()
        if not (database == "test" or database.startswith("test_") or database.endswith("_test")):
            self.skipTest("测试数据库名称必须为 test、test_* 或 *_test，避免误写生产库")
        if "config" in sys.modules:
            from config import app_config

            if app_config.postgres.database != database:
                self.skipTest("当前进程已连接非测试数据库，拒绝写入集成测试数据")
        else:
            os.environ["PG_DATABASE"] = database

        from metadata_service import ensure_file_record
        from policy.storage import replace_policy_clauses, upsert_policy_document
        from storage_adapter import storage

        self._ensure_file_record = ensure_file_record
        self._replace_policy_clauses = replace_policy_clauses
        self._upsert_policy_document = upsert_policy_document
        self._storage = storage
        self._policy_ids: list[str] = []
        self._file_ids: list[str] = []

    def tearDown(self) -> None:
        """仅清除本测试创建的精确制度与向量，不动其他测试数据。"""
        for policy_id in self._policy_ids:
            try:
                self._storage.vector.delete_vectors_by_metadata(
                    POLICY_CLAUSE_INDEX_NAME, "policy_id", policy_id
                )
            except Exception:
                pass
            self._storage.relational.execute(
                'DELETE FROM "policy_clauses" WHERE policy_id = %s', (policy_id,)
            )
            self._storage.relational.execute(
                'DELETE FROM "policy_documents" WHERE policy_id = %s', (policy_id,)
            )
        for file_id in self._file_ids:
            self._storage.relational.execute(
                'DELETE FROM "pdf_files" WHERE file_id = %s', (file_id,)
            )

    def _create_policy(self, title: str, clause_id: str, raw_text: str) -> str:
        """在隔离库创建带真实条款的制度，用于端到端索引验证。"""
        from models import PolicyClause, PolicyStructureStatus

        token = uuid.uuid4().hex
        record = self._ensure_file_record(
            f"{title}_{token}.pdf", token.encode("utf-8"), page_count=2
        )
        file_id = str(record["file_id"])
        self._file_ids.append(file_id)
        policy_id = self._upsert_policy_document(
            file_id=file_id,
            file_name=f"{title}.pdf",
            metadata={"title": title},
            parse_quality=1.0,
            structure_version="policy-retrieval-test-v1",
            structure_status=PolicyStructureStatus.RUNNING,
        )
        self._policy_ids.append(policy_id)
        self._replace_policy_clauses(policy_id, [PolicyClause(
            clause_id=clause_id,
            policy_id=policy_id,
            level="article",
            chapter_path=["第三章 办理要求"],
            article_no="第十二条",
            raw_text=raw_text,
            search_text=raw_text,
            page_start=2,
            page_end=2,
            content_hash=token,
            sequence_no=1,
            structure_version="policy-retrieval-test-v1",
        )], "policy-retrieval-test-v1")
        return policy_id

    def test_real_index_replaces_one_policy_without_deleting_another(self) -> None:
        """防止真实批量重建误删其他制度向量，且查询必须返回原始条款。"""
        trip_policy_id = self._create_policy(
            "差旅费管理办法", "trip_clause", "出差人员报销差旅费应提交发票和审批单。"
        )
        procurement_policy_id = self._create_policy(
            "采购管理办法", "purchase_clause", "采购申请经部门负责人审批后报采购中心办理。"
        )

        self.assertEqual(sync_policy_clause_index(trip_policy_id), 1)
        self.assertEqual(sync_policy_clause_index(procurement_policy_id), 1)
        self.assertEqual(sync_policy_clause_index(trip_policy_id), 1)

        procurement_vectors = self._storage.relational.query(
            POLICY_CLAUSE_INDEX_NAME,
            'metadata ->> %s = %s',
            10,
            ("policy_id", procurement_policy_id),
        )
        results = search_indexed_policy_clauses("差旅报销需要哪些材料", top_k=3)

        self.assertEqual(len(procurement_vectors), 1)
        self.assertEqual(results[0]["metadata"]["clause_id"], "trip_clause")
        self.assertEqual(results[0]["metadata"]["raw_text"], "出差人员报销差旅费应提交发票和审批单。")

    def test_policy_search_api_returns_original_clause_citation(self) -> None:
        """防止新接口退化成摘要、单文件文本块或无页码的结果。"""
        policy_id = self._create_policy(
            "差旅费管理办法", "trip_api_clause", "出差人员报销差旅费应提交发票和审批单。"
        )
        sync_policy_clause_index(policy_id)

        from api import policy_search

        response = asyncio.run(policy_search(q="差旅报销材料", top_k=3))
        payload = json.loads(response.body)
        result = payload["results"][0]

        self.assertEqual(result["policy"]["title"], "差旅费管理办法")
        self.assertEqual(result["clause"]["clause_no"], "第十二条")
        self.assertEqual(result["clause"]["page_start"], 2)
        self.assertEqual(result["clause"]["raw_text"], "出差人员报销差旅费应提交发票和审批单。")


if __name__ == "__main__":
    unittest.main()
