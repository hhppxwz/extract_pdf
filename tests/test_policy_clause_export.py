"""制度 PDF 批处理后的条款重组 JSON 导出测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import run_process_dir
from policy.clause_export import export_batch_clause_structure


class PolicyClauseExportTests(unittest.TestCase):
    """验证批次完成后可交付按条款组织的结构化 JSON。"""

    def test_export_batch_clause_structure_preserves_clause_hierarchy_and_citation(self) -> None:
        """防止导出的 JSON 丢失条款层级、页码或原文，无法用作后续核查输入。"""
        documents = [{
            "policy_id": "policy_1",
            "file_id": "file_1",
            "file_name": "报销办法.pdf",
            "title": "报销办法",
            "document_no": "校财字〔2026〕1号",
            "structure_version": "policy-structure-v1",
        }]
        clauses = [{
            "clause_id": "c_article",
            "policy_id": "policy_1",
            "parent_clause_id": "",
            "level": "article",
            "chapter_path": ["第一章 总则"],
            "article_no": "第一条",
            "paragraph_no": "",
            "item_no": "",
            "raw_text": "第一条 本办法适用于报销事项。",
            "page_start": 1,
            "page_end": 1,
            "sequence_no": 1,
        }]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "条款重组结果.json"
            path = export_batch_clause_structure(
                "batch_1",
                output_path,
                document_loader=lambda batch_id: documents,
                clause_loader=lambda policy_id: clauses,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["batch_id"], "batch_1")
        self.assertEqual(payload["documents"][0]["document_no"], "校财字〔2026〕1号")
        self.assertEqual(payload["documents"][0]["clauses"][0], {
            "clause_id": "c_article",
            "parent_clause_id": "",
            "level": "article",
            "chapter_path": ["第一章 总则"],
            "article_no": "第一条",
            "paragraph_no": "",
            "item_no": "",
            "raw_text": "第一条 本办法适用于报销事项。",
            "page_start": 1,
            "page_end": 1,
            "sequence_no": 1,
        })

    def test_process_dir_exports_clause_structure_json_after_batch_completion(self) -> None:
        """防止批处理只把条款写入数据库，却不向用户交付可查看的 JSON。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            output_path = source_dir / "batch_1_条款重组结果.json"
            status = {"batch": {"batch_id": "batch_1", "total_count": 1}}
            with (
                patch("batch_processor.create_batch_job", return_value="batch_1"),
                patch("batch_processor.run_batch", return_value=status),
                patch("policy.clause_export.export_batch_clause_structure", return_value=output_path) as export_structure,
            ):
                run_process_dir(str(source_dir))

        export_structure.assert_called_once_with("batch_1", output_path)


if __name__ == "__main__":
    unittest.main()
