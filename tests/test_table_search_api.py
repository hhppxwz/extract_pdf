"""表格目录搜索 API 的行为测试。"""
from __future__ import annotations

import os
import json
import unittest
from unittest.mock import patch

import asyncio
import httpx

import table_catalog


class _TableDataRelationalStorage:
    """模拟目录和关系表读取，验证接口返回真实结构化表格数据。"""

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
        if table_name == "pdf_file_1_tbl_table_1":
            rows = [
                {"_row_id": 1, "_page_num": 0, "_row_idx": 0, "项目": "Rcpy", "合计经费_预算经费": 100},
                {"_row_id": 2, "_page_num": 0, "_row_idx": 1, "项目": "szdw", "合计经费_预算经费": 250},
            ]
            if '"项目" ILIKE %s' in where:
                keyword = str((params or ())[0]).strip("%").casefold()
                rows = [row for row in rows if keyword in str(row["项目"]).casefold()]
            if '"合计经费_预算经费" >= %s' in where:
                minimum = int((params or ())[0])
                rows = [row for row in rows if int(row["合计经费_预算经费"]) >= minimum]
            if '"合计经费_预算经费" <= %s' in where:
                maximum = int((params or ())[0])
                rows = [row for row in rows if int(row["合计经费_预算经费"]) <= maximum]
            if '"项目" = %s' in where:
                rows = [row for row in rows if row["项目"] == (params or ())[0]]
            return rows[:limit]
        raise AssertionError(f"不应查询的表：{table_name}")


class _TableDataStorage:
    def __init__(self) -> None:
        self.relational = _TableDataRelationalStorage()


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


class TableSearchApiTests(unittest.TestCase):
    """验证按表格或原始 PDF 名称检索后可直接展示行数据。"""

    def test_search_by_original_file_name_returns_table_data(self) -> None:
        """防止搜索接口只返回表格位置，前端仍需额外请求才能展示内容。"""
        from api import app

        catalog_result = [{
            "file_id": "file_1",
            "file_name": "2024年度经费汇总.pdf",
            "block_id": "table_1",
            "page_num": 0,
            "table_code": "X010102",
            "table_title": "经费数额",
            "storage_target": "pg_relational",
            "storage_location": "pdf_file_1_tbl_table_1",
            "columns": [
                {"key": "项目", "label": "项目", "data_type": "TEXT"},
                {"key": "合计经费_预算经费", "label": "合计经费 / 预算经费", "data_type": "INTEGER"},
            ],
            "rows": [{"项目": "Rcpy", "合计经费_预算经费": 100}],
        }]

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(
                    "/tables/search",
                    params={"q": "2024年度经费", "row_limit": 25},
                )

        with patch("api.search_extracted_tables", return_value=catalog_result, create=True) as search_tables:
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["query"], "2024年度经费")
        self.assertEqual(payload["results"], catalog_result)
        search_tables.assert_called_once_with(
            query="2024年度经费",
            file_id=None,
            limit=10,
            row_limit=25,
        )

    def test_table_data_endpoint_returns_paginated_table_without_text_results(self) -> None:
        """防止表格详情仍混入文本搜索结果，导致前端无法直接渲染表格。"""
        from api import app

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(
                    "/tables/file_1/table_1/data",
                    params={"limit": 1, "offset": 0},
                )

        with patch.object(table_catalog, "storage", _TableDataStorage()):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["table"]["table_title"], "经费数额")
        self.assertEqual(payload["columns"][1]["label"], "合计经费 / 预算经费")
        self.assertEqual(payload["rows"], [{"项目": "Rcpy", "合计经费_预算经费": 100}])
        self.assertEqual(payload["page"], {"limit": 1, "offset": 0, "returned": 1, "has_more": True})
        self.assertNotIn("text_blocks", payload)

    def test_table_data_endpoint_filters_rows_by_catalogued_column(self) -> None:
        """防止筛选参数被忽略，返回不符合条件的表格行。"""
        from api import app

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(
                    "/tables/file_1/table_1/data",
                    params={
                        "limit": 1,
                        "filters": json.dumps([
                            {"column": "项目", "operator": "contains", "value": "zd"},
                        ], ensure_ascii=False),
                    },
                )

        with patch.object(table_catalog, "storage", _TableDataStorage()):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["rows"], [{"项目": "szdw", "合计经费_预算经费": 250}])
        self.assertFalse(payload["page"]["has_more"])

    def test_table_data_endpoint_filters_numeric_column_by_lower_bound(self) -> None:
        """防止数值筛选退化为文本匹配，导致预算范围查询结果错误。"""
        from api import app

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(
                    "/tables/file_1/table_1/data",
                    params={
                        "filters": json.dumps([
                            {"column": "合计经费_预算经费", "operator": "gte", "value": 200},
                        ], ensure_ascii=False),
                    },
                )

        with patch.object(table_catalog, "storage", _TableDataStorage()):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{"项目": "szdw", "合计经费_预算经费": 250}])

    def test_table_data_endpoint_filters_rows_by_exact_value(self) -> None:
        """防止精确查询被当成模糊查询，返回额外的表格行。"""
        from api import app

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(
                    "/tables/file_1/table_1/data",
                    params={
                        "filters": json.dumps([
                            {"column": "项目", "operator": "equals", "value": "Rcpy"},
                        ], ensure_ascii=False),
                    },
                )

        with patch.object(table_catalog, "storage", _TableDataStorage()):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{"项目": "Rcpy", "合计经费_预算经费": 100}])

    def test_table_data_endpoint_filters_numeric_column_by_upper_bound(self) -> None:
        """防止数值上限筛选缺失，无法查询指定预算区间。"""
        from api import app

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(
                    "/tables/file_1/table_1/data",
                    params={
                        "filters": json.dumps([
                            {"column": "合计经费_预算经费", "operator": "lte", "value": 150},
                        ], ensure_ascii=False),
                    },
                )

        with patch.object(table_catalog, "storage", _TableDataStorage()):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{"项目": "Rcpy", "合计经费_预算经费": 100}])


if __name__ == "__main__":
    unittest.main()
