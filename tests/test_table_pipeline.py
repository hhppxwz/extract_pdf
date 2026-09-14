"""表格处理管线的行为测试。"""
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


import table_pipeline
from config import app_config
from models import BlockType, ContentBlock, TableCategory


MERGED_FUNDING_TABLE = """
<table>
  <tr>
    <td rowspan="2">项目</td>
    <td colspan="2">合计经费</td>
    <td colspan="2">中央专项经费</td>
  </tr>
  <tr>
    <td>预算经费</td><td>实际支出</td>
    <td>预算经费</td><td>实际支出</td>
  </tr>
  <tr><td>Rcpy</td><td>100</td><td>50</td><td>60</td><td>20</td></tr>
  <tr><td>szdw</td><td>250</td><td>200</td><td>190</td><td>30</td></tr>
</table>
"""

MIXED_TAG_MERGED_FUNDING_TABLE = """
<table>
  <tr>
    <th rowspan="2">项目</th>
    <th colspan="2">合计经费</th>
    <th colspan="2">中央专项经费</th>
  </tr>
  <tr>
    <td>预算经费</td><td>实际支出</td>
    <td>预算经费</td><td>实际支出</td>
  </tr>
  <tr><td>Rcpy</td><td>100</td><td>50</td><td>60</td><td>20</td></tr>
</table>
"""


THOUSANDS_SEPARATOR_TABLE = """
<table>
  <tr><th>系统</th><th>样本数量</th><th>准确率</th></tr>
  <tr><td>PARROT-Diverse</td><td>28,003</td><td>38.53</td></tr>
  <tr><td>PARROT-Small</td><td>1，200</td><td>12.50</td></tr>
</table>
"""


class _RecordingRelationalStorage:
    """记录关系库写入，避免测试连接真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.dropped: list[str] = []
        self.created: list[tuple[str, list[tuple[str, str]]]] = []
        self.inserted: list[tuple[str, list[str], list[list[object]]]] = []

    def drop_table(self, table_name: str) -> None:
        self.dropped.append(table_name)

    def create_table(self, table_name: str, columns: list[tuple[str, str]]) -> None:
        self.created.append((table_name, columns))

    def insert_rows(self, table_name: str, columns: list[str], rows: list[list[object]]) -> int:
        self.inserted.append((table_name, columns, rows))
        return len(rows)

    def query(self, table_name: str, *args: object, **kwargs: object) -> list[dict[str, object]]:
        return []


class _RecordingStorage:
    def __init__(self) -> None:
        self.relational = _RecordingRelationalStorage()


class MergedHeaderTableTests(unittest.TestCase):
    """验证合并单元格表头不会在入库时丢失层级。"""

    def test_parse_expands_merged_headers_into_complete_paths(self) -> None:
        """防止 rowspan/colspan 使二级表头被误当作第一条数据。"""
        structure = table_pipeline.parse_table_html(MERGED_FUNDING_TABLE)

        self.assertIsNotNone(structure)
        assert structure is not None
        self.assertEqual(
            structure.headers,
            [
                "项目",
                "合计经费 / 预算经费",
                "合计经费 / 实际支出",
                "中央专项经费 / 预算经费",
                "中央专项经费 / 实际支出",
            ],
        )
        self.assertEqual(
            structure.rows,
            [
                ["Rcpy", "100", "50", "60", "20"],
                ["szdw", "250", "200", "190", "30"],
            ],
        )

    def test_store_uses_complete_merged_header_paths_as_columns(self) -> None:
        """防止经费金额写入匿名列，导致合计与专项经费无法区分。"""
        structure = table_pipeline.parse_table_html(MERGED_FUNDING_TABLE)
        self.assertIsNotNone(structure)
        assert structure is not None
        recording_storage = _RecordingStorage()

        with patch.object(table_pipeline, "storage", recording_storage):
            table_name = table_pipeline.store_data_table(
                file_id="file_1",
                block_id="table_1",
                structure=structure,
                page_num=0,
            )

        self.assertEqual(table_name, "pdf_file_1_tbl_table_1")
        self.assertEqual(
            recording_storage.relational.created,
            [
                (
                    "pdf_file_1_tbl_table_1",
                    [
                        ("_row_id", "SERIAL PRIMARY KEY"),
                        ("_page_num", "INTEGER"),
                        ("_row_idx", "INTEGER"),
                        ("项目", "TEXT"),
                        ("合计经费_预算经费", "INTEGER"),
                        ("合计经费_实际支出", "INTEGER"),
                        ("中央专项经费_预算经费", "INTEGER"),
                        ("中央专项经费_实际支出", "INTEGER"),
                    ],
                )
            ],
        )
        self.assertEqual(
            recording_storage.relational.inserted,
            [
                (
                    "pdf_file_1_tbl_table_1",
                    [
                        "_page_num",
                        "_row_idx",
                        "项目",
                        "合计经费_预算经费",
                        "合计经费_实际支出",
                        "中央专项经费_预算经费",
                        "中央专项经费_实际支出",
                    ],
                    [
                        [0, 0, "Rcpy", 100, 50, 60, 20],
                        [0, 1, "szdw", 250, 200, 190, 30],
                    ],
                )
            ],
        )

    def test_store_converts_thousands_separated_numeric_cells_before_insert(self) -> None:
        """防止推断为数值列后仍把 28,003 原样写入 PostgreSQL 而导致整表失败。"""
        structure = table_pipeline.parse_table_html(THOUSANDS_SEPARATOR_TABLE)

        self.assertIsNotNone(structure)
        assert structure is not None
        recording_storage = _RecordingStorage()

        with patch.object(table_pipeline, "storage", recording_storage):
            table_pipeline.store_data_table(
                file_id="file_1",
                block_id="numeric_table",
                structure=structure,
                page_num=3,
            )

        self.assertEqual(
            recording_storage.relational.inserted,
            [
                (
                    "pdf_file_1_tbl_numeric_table",
                    ["_page_num", "_row_idx", "系统", "样本数量", "准确率"],
                    [
                        [3, 0, "PARROT-Diverse", 28003, 38.53],
                        [3, 1, "PARROT-Small", 1200, 12.5],
                    ],
                )
            ],
        )

    def test_parse_keeps_td_subheaders_after_th_parent_headers(self) -> None:
        """防止首层是 th、次层是 td 时把次级表头误写为数据。"""
        structure = table_pipeline.parse_table_html(MIXED_TAG_MERGED_FUNDING_TABLE)

        self.assertIsNotNone(structure)
        assert structure is not None
        self.assertEqual(
            structure.headers,
            [
                "项目",
                "合计经费 / 预算经费",
                "合计经费 / 实际支出",
                "中央专项经费 / 预算经费",
                "中央专项经费 / 实际支出",
            ],
        )
        self.assertEqual(structure.rows, [["Rcpy", "100", "50", "60", "20"]])

    def test_process_records_nearby_heading_as_table_identity(self) -> None:
        """防止带编号的相邻标题未随表格入库，导致后续无法按表名检索。"""
        heading = ContentBlock(
            block_id="heading_1",
            type=BlockType.TEXT,
            page_num=0,
            content="X010102 经费数额",
            raw={"text_level": 2},
        )
        table = ContentBlock(
            block_id="table_1",
            type=BlockType.TABLE,
            page_num=0,
            content=MERGED_FUNDING_TABLE,
            table_html=MERGED_FUNDING_TABLE,
            table_category=TableCategory.DATA_TABLE,
            raw={"table_caption": []},
        )
        recording_storage = _RecordingStorage()

        with (
            patch.object(table_pipeline, "storage", recording_storage),
            patch.object(table_pipeline, "record_table_catalog", create=True) as record_catalog,
        ):
            stored, forms, uncertain = table_pipeline.process_table_blocks(
                [heading, table], "file_1"
            )

        self.assertEqual((stored, forms, uncertain), (1, 0, 0))
        self.assertEqual(record_catalog.call_count, 1)
        kwargs = record_catalog.call_args.kwargs
        self.assertEqual(kwargs["file_id"], "file_1")
        self.assertEqual(kwargs["block"], table)
        self.assertEqual(kwargs["table_code"], "X010102")
        self.assertEqual(kwargs["table_title"], "经费数额")
        self.assertEqual(kwargs["storage_location"], "pdf_file_1_tbl_table_1")

    def test_normalized_columns_keep_original_header_mapping(self) -> None:
        """防止中文合并表头转为 col_N 后失去展示名称和追溯关系。"""
        structure = table_pipeline.parse_table_html(MERGED_FUNDING_TABLE)
        self.assertIsNotNone(structure)
        assert structure is not None
        recording_storage = _RecordingStorage()
        original_style = app_config.column_naming_style
        app_config.column_naming_style = "normalized"
        try:
            with patch.object(table_pipeline, "storage", recording_storage):
                table_pipeline.store_data_table("file_1", "table_1", structure, page_num=0)
        finally:
            app_config.column_naming_style = original_style

        created_table_names = [table_name for table_name, _ in recording_storage.relational.created]
        self.assertIn("metadata_table", created_table_names)

    def test_process_uses_following_heading_when_no_prior_heading_exists(self) -> None:
        """防止解析器把相邻标题排在表格后时目录丢失表名。"""
        table = ContentBlock(
            block_id="table_1",
            type=BlockType.TABLE,
            page_num=0,
            content=MERGED_FUNDING_TABLE,
            table_html=MERGED_FUNDING_TABLE,
            table_category=TableCategory.DATA_TABLE,
            raw={"table_caption": []},
        )
        heading = ContentBlock(
            block_id="heading_1",
            type=BlockType.TEXT,
            page_num=0,
            content="X010102 经费数额",
            raw={"text_level": 2},
        )
        recording_storage = _RecordingStorage()

        with (
            patch.object(table_pipeline, "storage", recording_storage),
            patch.object(table_pipeline, "record_table_catalog", create=True) as record_catalog,
        ):
            table_pipeline.process_table_blocks([table, heading], "file_1")

        kwargs = record_catalog.call_args.kwargs
        self.assertEqual(kwargs["table_code"], "X010102")
        self.assertEqual(kwargs["table_title"], "经费数额")


if __name__ == "__main__":
    unittest.main()
