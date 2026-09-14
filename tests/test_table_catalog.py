"""表格目录服务的行为测试。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch


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


import table_catalog


class _CatalogRelationalStorage:
    """提供目录和数据表的确定性内存实现，避免测试连接真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.query_calls: list[tuple[str, str, int, tuple[object, ...]]] = []
        self.catalog_rows = [{
            "file_id": "file_1",
            "file_name": "2024年度经费汇总.pdf",
            "block_id": "table_1",
            "page_num": 0,
            "table_code": "X010102",
            "table_title": "经费数额",
            "table_category": "data_table",
            "storage_target": "pg_relational",
            "storage_location": "pdf_file_1_tbl_table_1",
            "columns_json": [
                {"key": "项目", "label": "项目", "data_type": "TEXT"},
                {"key": "合计经费_预算经费", "label": "合计经费 / 预算经费", "data_type": "INTEGER"},
            ],
            "row_count": 2,
        }]

    def create_table(self, *args: object, **kwargs: object) -> None:
        return None

    def execute(self, *args: object, **kwargs: object) -> None:
        return None

    def query(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        bound_params = tuple(params or ())
        self.query_calls.append((table_name, where, limit, bound_params))
        if table_name == table_catalog.TABLE_TABLE_CATALOG:
            keyword = str(bound_params[0]).strip("%").casefold()
            return [
                row for row in self.catalog_rows
                if keyword in str(row["table_code"]).casefold()
                or keyword in str(row["table_title"]).casefold()
                or keyword in str(row["file_name"]).casefold()
            ]
        if table_name == "pdf_file_1_tbl_table_1":
            return [
                {"项目": "Rcpy", "合计经费_预算经费": 100},
                {"项目": "szdw", "合计经费_预算经费": 250},
            ][:limit]
        raise AssertionError(f"不应查询的表：{table_name}")


class _CatalogStorage:
    def __init__(self) -> None:
        self.relational = _CatalogRelationalStorage()


class _FormTableRelationalStorage:
    """模拟 JSONB 表单目录和字段读取。"""

    def create_table(self, *args: object, **kwargs: object) -> None:
        return None

    def execute(self, *args: object, **kwargs: object) -> None:
        return None

    def query(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        if table_name == table_catalog.TABLE_TABLE_CATALOG:
            return [{
                "file_id": "file_1",
                "file_name": "申请表.pdf",
                "block_id": "form_1",
                "page_num": 0,
                "table_code": "",
                "table_title": "申请信息",
                "table_category": "form",
                "storage_target": "pg_jsonb",
                "storage_location": "form_doc_1",
                "columns_json": [
                    {"key": "field", "label": "字段", "data_type": "TEXT"},
                    {"key": "value", "label": "值", "data_type": "TEXT"},
                ],
                "row_count": 2,
            }]
        if table_name == "pdf_forms":
            return [{"doc": {"fields": {"申请单位": "数智部门", "联系人": "张三"}}}]
        raise AssertionError(f"不应查询的表：{table_name}")


class _FormTableStorage:
    def __init__(self) -> None:
        self.relational = _FormTableRelationalStorage()


class _CatalogFileNameSyncRelationalStorage:
    """模拟历史临时文件名由文件主记录回填的目录查询。"""

    def __init__(self) -> None:
        self.catalog_rows = [{
            "file_id": "file_1",
            "file_name": "tmpan2sxyv7.pdf",
            "block_id": "table_1",
            "page_num": 0,
            "table_code": "",
            "table_title": "结果表",
            "table_category": "data_table",
            "storage_target": "pg_relational",
            "storage_location": "pdf_file_1_tbl_table_1",
            "columns_json": [],
            "row_count": 0,
        }]
        self.files = {"file_1": "Cracking SQL Barriers.pdf"}

    def create_table(self, *args: object, **kwargs: object) -> None:
        return None

    def execute(self, sql: str, *args: object, **kwargs: object) -> None:
        if "SET file_name = files.file_name" in sql:
            for row in self.catalog_rows:
                row["file_name"] = self.files[str(row["file_id"])]

    def query(
        self,
        table_name: str,
        where: str = "",
        limit: int = 100,
        params: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        if table_name == table_catalog.TABLE_TABLE_CATALOG:
            keyword = str((params or ())[0]).strip("%").casefold()
            return [
                row for row in self.catalog_rows
                if keyword in str(row["file_name"]).casefold()
            ][:limit]
        if table_name == "pdf_file_1_tbl_table_1":
            return []
        raise AssertionError(f"不应查询的表：{table_name}")


class _CatalogFileNameSyncStorage:
    def __init__(self) -> None:
        self.relational = _CatalogFileNameSyncRelationalStorage()


class TableCatalogTests(unittest.TestCase):
    """验证目录搜索结果包含可展示的表格数据。"""

    def test_search_by_file_name_returns_rows_from_catalogued_table(self) -> None:
        """防止文件名命中后仅返回目录，遗漏关系表中的实际行数据。"""
        storage = _CatalogStorage()

        with patch.object(table_catalog, "storage", storage):
            results = table_catalog.search_extracted_tables(
                query="2024年度",
                limit=10,
                row_limit=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file_name"], "2024年度经费汇总.pdf")
        self.assertEqual(results[0]["table_title"], "经费数额")
        self.assertEqual(results[0]["columns"][1]["label"], "合计经费 / 预算经费")
        self.assertEqual(results[0]["rows"], [{"项目": "Rcpy", "合计经费_预算经费": 100}])
        catalog_query = storage.relational.query_calls[0]
        self.assertIn('"file_name" ILIKE %s', catalog_query[1])
        self.assertEqual(catalog_query[3], ("%2024年度%", "%2024年度%", "%2024年度%"))

    def test_form_table_data_filters_field_rows_before_pagination(self) -> None:
        """防止 JSONB 表单忽略筛选条件，混入不匹配的字段行。"""
        storage = _FormTableStorage()

        with patch.object(table_catalog, "storage", storage):
            result = table_catalog.query_catalogued_table_data(
                file_id="file_1",
                block_id="form_1",
                filters=[{"column": "field", "operator": "equals", "value": "联系人"}],
                limit=1,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["rows"], [{"field": "联系人", "value": "张三"}])
        self.assertFalse(result["page"]["has_more"])

    def test_search_backfills_historical_temporary_file_name_from_file_record(self) -> None:
        """防止历史表格目录仍保存 tmp 文件名而无法按真实 PDF 名搜索。"""
        storage = _CatalogFileNameSyncStorage()

        with patch.object(table_catalog, "storage", storage):
            results = table_catalog.search_extracted_tables(query="Cracking SQL")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file_name"], "Cracking SQL Barriers.pdf")


if __name__ == "__main__":
    unittest.main()
