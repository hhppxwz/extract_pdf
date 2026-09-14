"""制度质检数据导出与汇总的行为测试。"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 存储层在导入时校验真实服务配置；本文件只替换数据库边界，不建立任何连接。
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
from policy.quality import (
    build_graph_review_rows,
    build_clause_review_rows,
    export_clause_review,
    export_graph_review,
    summarize_quality_reviews,
    write_quality_report,
    write_review_csvs,
)


class PolicyQualityTests(unittest.TestCase):
    """验证面向人工审核者的质检数据。"""

    def test_build_clause_review_rows_selects_evenly_and_leaves_review_blank(self) -> None:
        """防止抽样只落在制度开头，或把人工结论误写入导出表。"""
        documents = [
            {
                "policy_id": "policy_1",
                "file_id": "file_1",
                "file_name": "财务报销管理办法.pdf",
                "title": "财务报销管理办法",
            }
        ]
        clauses_by_policy = {
            "policy_1": [
                {
                    "clause_id": "c1",
                    "level": "article",
                    "chapter_path": ["第一章 总则"],
                    "article_no": "第一条",
                    "parent_clause_id": "",
                    "raw_text": "第一条 总则。",
                    "page_start": 1,
                    "page_end": 1,
                    "sequence_no": 1,
                },
                {
                    "clause_id": "c2",
                    "level": "article",
                    "chapter_path": ["第二章 报销"],
                    "article_no": "第二条",
                    "parent_clause_id": "",
                    "raw_text": "第二条 范围。",
                    "page_start": 2,
                    "page_end": 2,
                    "sequence_no": 2,
                },
                {
                    "clause_id": "c3",
                    "level": "article",
                    "chapter_path": ["第三章 附则"],
                    "article_no": "第三条",
                    "parent_clause_id": "",
                    "raw_text": "第三条 附则。",
                    "page_start": 3,
                    "page_end": 3,
                    "sequence_no": 3,
                },
            ]
        }

        rows = build_clause_review_rows(
            documents,
            clauses_by_policy,
            clauses_per_policy=2,
        )

        self.assertEqual([row["条款ID"] for row in rows], ["c1", "c3"])
        self.assertEqual(rows[0]["边界正确"], "")
        self.assertEqual(rows[0]["层级正确"], "")
        self.assertEqual(rows[0]["文字完整"], "")
        self.assertEqual(rows[0]["页码正确"], "")

    def test_write_review_csvs_creates_excel_readable_forms_and_error_catalog(self) -> None:
        """防止导出缺少表头、缺少错误字典，或使用 Excel 无法识别的编码。"""
        clause_rows = [{
            "文档ID": "file_1",
            "文件名": "财务报销管理办法.pdf",
            "制度名称": "财务报销管理办法",
            "条款ID": "c1",
            "页码": "1",
            "系统层级": "article 第一条",
            "父条款ID": "",
            "章节路径": "第一章 总则",
            "系统原文": "第一条 总则。",
            "边界正确": "",
            "层级正确": "",
            "文字完整": "",
            "页码正确": "",
            "错误类型": "",
            "修正说明": "",
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_review_csvs(Path(temp_dir), clause_rows, [])
            with paths["条款抽检"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows[0]["条款ID"], "c1")
            self.assertTrue(paths["错误字典"].exists())

    def test_summarize_quality_reviews_counts_chinese_decisions_errors_and_omissions(self) -> None:
        """防止中文“对/错”和多个错误代码无法被正确汇总。"""
        clause_rows = [
            {
                "边界正确": "对",
                "层级正确": "错",
                "文字完整": "对",
                "页码正确": "对",
                "错误类型": "H01, S01",
            },
            {
                "边界正确": "对",
                "层级正确": "对",
                "文字完整": "对",
                "页码正确": "对",
                "错误类型": "",
            },
            {
                "边界正确": "",
                "层级正确": "",
                "文字完整": "",
                "页码正确": "",
                "错误类型": "",
            },
        ]
        graph_rows = [
            {"正确性": "错", "缺失内容": "遗漏审批单"},
            {"正确性": "是", "缺失内容": ""},
            {"正确性": "", "缺失内容": ""},
        ]

        summary = summarize_quality_reviews(clause_rows, graph_rows)

        self.assertEqual(summary["条款"]["总导出数"], 3)
        self.assertEqual(summary["条款"]["已审核数"], 2)
        self.assertEqual(summary["条款"]["完全正确"], 1)
        self.assertEqual(summary["条款"]["层级错误"], 1)
        self.assertEqual(summary["条款"]["错误类型"]["H01"], 1)
        self.assertEqual(summary["条款"]["错误类型"]["S01"], 1)
        self.assertEqual(summary["图谱"]["错误数"], 1)
        self.assertEqual(summary["图谱"]["正确数"], 1)
        self.assertEqual(summary["图谱"]["遗漏数"], 1)

    def test_build_graph_review_rows_renders_relation_with_evidence(self) -> None:
        """防止关系审核表只显示 ID，导致审核者无法判断原文依据。"""
        document = {
            "file_id": "file_1",
            "file_name": "财务报销管理办法.pdf",
            "title": "财务报销管理办法",
        }
        clause = {
            "clause_id": "c1",
            "raw_text": "报销差旅费应提交发票。",
            "page_start": 12,
            "page_end": 12,
        }
        candidates = [
            {
                "candidate_type": "entity",
                "candidate_id": "e1",
                "entity_type": "matter",
                "name": "报销",
                "evidence_text": "报销",
                "confidence": 0.95,
                "review_status": "pending",
                "clause": clause,
                "document": document,
            },
            {
                "candidate_type": "relation",
                "candidate_id": "r1",
                "relation_type": "requires_material",
                "subject_name": "报销",
                "object_name": "发票",
                "evidence_text": "提交发票",
                "confidence": 0.92,
                "review_status": "pending",
                "clause": clause,
                "document": document,
            },
        ]

        rows = build_graph_review_rows("run_1", candidates)

        self.assertEqual(rows[0]["候选内容"], "matter: 报销")
        self.assertEqual(rows[1]["候选内容"], "报销 --requires_material--> 发票")
        self.assertEqual(rows[1]["原文证据"], "提交发票")
        self.assertEqual(rows[1]["正确性"], "")

    def test_write_quality_report_reads_completed_csvs(self) -> None:
        """防止回填后的审核结果无法形成可阅读、可复用的质量报告。"""
        clause_rows = [{
            "边界正确": "对",
            "层级正确": "错",
            "文字完整": "对",
            "页码正确": "对",
            "错误类型": "H01",
        }]
        graph_rows = [{"正确性": "否", "缺失内容": "遗漏审批单"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            write_review_csvs(Path(temp_dir), clause_rows, graph_rows)
            paths = write_quality_report(Path(temp_dir))

            report_text = paths["Markdown"].read_text(encoding="utf-8")
            report_json = json.loads(paths["JSON"].read_text(encoding="utf-8"))

        self.assertIn("层级错误：1", report_text)
        self.assertIn("H01：1", report_text)
        self.assertEqual(report_json["图谱"]["遗漏数"], 1)

    def test_export_clause_review_uses_batch_loaders_without_writing_database(self) -> None:
        """防止批次导出遗漏制度，或在导出过程中改写原始条款数据。"""
        documents = [{
            "policy_id": "policy_1",
            "file_id": "file_1",
            "file_name": "财务报销管理办法.pdf",
            "title": "财务报销管理办法",
        }]
        clauses = [{
            "clause_id": "c1",
            "level": "article",
            "article_no": "第一条",
            "chapter_path": [],
            "raw_text": "第一条 总则。",
            "page_start": 1,
            "page_end": 1,
            "sequence_no": 1,
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = export_clause_review(
                "batch_1",
                Path(temp_dir),
                clauses_per_policy=10,
                document_loader=lambda batch_id: documents,
                clause_loader=lambda policy_id: clauses,
            )
            with paths["条款抽检"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["条款ID"], "c1")

    def test_export_graph_review_uses_run_loader_and_keeps_review_columns_blank(self) -> None:
        """防止图谱候选导出缺少证据，或覆盖已有人工审核结论。"""
        candidates = [{
            "candidate_type": "entity",
            "candidate_id": "e1",
            "entity_type": "material",
            "name": "发票",
            "evidence_text": "发票",
            "confidence": 0.9,
            "review_status": "pending",
            "clause": {"clause_id": "c1", "raw_text": "应提交发票。", "page_start": 2, "page_end": 2},
            "document": {"file_id": "file_1", "file_name": "报销办法.pdf", "title": "报销办法"},
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = export_graph_review(
                "run_1",
                Path(temp_dir),
                candidate_loader=lambda run_id: candidates,
            )
            with paths["图谱抽检"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(rows[0]["原文证据"], "发票")
        self.assertEqual(rows[0]["正确性"], "")

    def test_get_policy_graph_candidates_resolves_relation_entity_names(self) -> None:
        """防止关系导出只留下实体 ID，导致人工无法识别主体和客体。"""
        entities = [
            {"entity_id": "e1", "clause_id": "c1", "entity_type": "matter", "name": "报销"},
            {"entity_id": "e2", "clause_id": "c1", "entity_type": "material", "name": "发票"},
        ]
        relations = [{
            "relation_id": "r1",
            "clause_id": "c1",
            "relation_type": "requires_material",
            "subject_entity_id": "e1",
            "object_entity_id": "e2",
            "target_text": "",
        }]
        clause = {"clause_id": "c1", "policy_id": "p1", "raw_text": "报销应提交发票。", "sequence_no": 1}
        document = {"policy_id": "p1", "title": "报销办法"}

        def query(table_name: str, *args: object, **kwargs: object) -> list[dict[str, object]]:
            if table_name == policy_storage.TABLE_POLICY_ENTITIES:
                return entities
            if table_name == policy_storage.TABLE_POLICY_RELATIONS:
                return relations
            raise AssertionError(f"意外查询表：{table_name}")

        with (
            patch.object(policy_storage, "ensure_policy_tables"),
            patch.object(policy_storage, "get_policy_extraction_run", return_value={"run_id": "run_1"}),
            patch.object(policy_storage.storage.relational, "query", side_effect=query),
            patch.object(policy_storage, "get_policy_clause", return_value=clause),
            patch.object(policy_storage, "get_policy_document", return_value=document),
        ):
            candidates = policy_storage.get_policy_graph_candidates("run_1")

        relation = next(item for item in candidates if item["candidate_type"] == "relation")
        self.assertEqual(relation["subject_name"], "报销")
        self.assertEqual(relation["object_name"], "发票")
        self.assertEqual(relation["document"]["title"], "报销办法")

    def test_cli_full_help_lists_quality_review_commands(self) -> None:
        """防止质检功能存在于模块中却没有可执行的命令行入口。"""
        project_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--help-all"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )

        self.assertIn(b"--export-policy-clause-review", completed.stdout)
        self.assertIn(b"--export-policy-graph-review", completed.stdout)
        self.assertIn(b"--report-policy-quality", completed.stdout)


if __name__ == "__main__":
    unittest.main()
