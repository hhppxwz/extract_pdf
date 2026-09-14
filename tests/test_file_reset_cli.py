"""按文件重置自动产物命令测试。"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from file_reset import reset_file_database_artifacts


class _ResetRelationalStorage:
    """用于验证重置副作用的最小关系存储替身。"""

    def __init__(self) -> None:
        self.dropped_tables: list[str] = []
        self.executed_sql: list[str] = []
        self.updated_rows: list[tuple[str, dict, str, tuple]] = []
        self.block_records_deleted = False

    def query(self, table_name: str, where: str = "", limit: int = 100, params: tuple = ()) -> list[dict]:
        if table_name == "pdf_files":
            return [{"file_id": "pdf_123"}]
        if table_name == "pdf_table_catalog":
            return [
                {"storage_target": "pg_relational", "storage_location": "pdf_pdf_123_tbl_sales"},
                {"storage_target": "pg_jsonb", "storage_location": "form_1"},
            ]
        if table_name == "policy_documents":
            return [{"policy_id": "policy_1"}]
        if table_name == "policy_clauses":
            return [{"clause_id": "c1"}, {"clause_id": "c2"}]
        if table_name == "pdf_block_storage":
            return [] if self.block_records_deleted else [{"id": 1}, {"id": 2}, {"id": 3}]
        return []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed_sql.append(sql)
        if 'DELETE FROM "pdf_block_storage"' in sql:
            self.block_records_deleted = True

    def drop_table(self, table_name: str) -> None:
        self.dropped_tables.append(table_name)

    def update_rows(self, table_name: str, values: dict, where: str, params: tuple = ()) -> int:
        self.updated_rows.append((table_name, values, where, params))
        return 1


class _ResetVectorStorage:
    """用于验证向量清理目标的最小向量存储替身。"""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, str]] = []

    def delete_vectors_by_metadata(self, index_name: str, metadata_key: str, metadata_value: str) -> int:
        self.deleted.append((index_name, metadata_key, metadata_value))
        return 2


class _ResetStorage:
    """组合重置测试需要的两类存储接口。"""

    def __init__(self) -> None:
        self.relational = _ResetRelationalStorage()
        self.vector = _ResetVectorStorage()


class FileResetCliTests(unittest.TestCase):
    """验证文件重置命令可被用户发现和调用。"""

    def test_full_help_lists_reset_file_command(self) -> None:
        """防止重置入口未暴露，用户无法让已完成 PDF 再次进入解析队列。"""
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.update({
            "PYTHONIOENCODING": "utf-8",
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
        })

        completed = subprocess.run(
            [sys.executable, "main.py", "--help-all"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertIn(b"--reset-file", completed.stdout)

    def test_reset_file_rejects_unsafe_file_id(self) -> None:
        """防止文件 ID 被拼入动态表名时形成 SQL 注入入口。"""
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.update({
            "PYTHONIOENCODING": "utf-8",
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
        })

        completed = subprocess.run(
            [sys.executable, "main.py", "--reset-file", "bad/id"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("文件 ID 格式非法", completed.stderr.decode("utf-8", "replace"))

    def test_reset_file_removes_database_artifacts_before_requeueing(self) -> None:
        """防止重跑只改状态却保留旧向量、表格和溯源记录。"""
        fake_storage = _ResetStorage()

        with patch("file_reset.storage", fake_storage):
            summary = reset_file_database_artifacts("pdf_123")

        self.assertEqual(summary, {
            "text_vectors": 2,
            "data_tables": 1,
            "forms": 1,
            "table_catalog_records": 2,
            "block_records": 3,
            "policy_clauses": 2,
        })
        self.assertEqual(fake_storage.relational.dropped_tables, ["pdf_pdf_123_tbl_sales"])
        self.assertEqual(fake_storage.vector.deleted, [
            ("pdf_pdf_123_text", "file_id", "pdf_123"),
            ("policy_clause_vectors", "policy_id", "policy_1"),
        ])
        self.assertIn(
            ("pdf_files", {"status": "pending", "error_message": "", "last_attempt_at": None}, '"file_id" = %s', ("pdf_123",)),
            fake_storage.relational.updated_rows,
        )


if __name__ == "__main__":
    unittest.main()
