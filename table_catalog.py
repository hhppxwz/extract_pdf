"""已提取表格的目录存储与安全检索。"""
from __future__ import annotations

import json
import re
from typing import Any

from models import ContentBlock
from storage_adapter import storage


TABLE_TABLE_CATALOG = "pdf_table_catalog"
_DATA_TABLE_NAME_PATTERN = re.compile(r"^pdf_[A-Za-z0-9_]+_tbl_[A-Za-z0-9_]+$")


def _ensure_table_catalog() -> None:
    """创建表格目录及搜索索引，兼容重复调用。"""
    storage.relational.create_table(TABLE_TABLE_CATALOG, [
        ("id", "SERIAL PRIMARY KEY"),
        ("file_id", "VARCHAR(64) NOT NULL"),
        ("file_name", "TEXT NOT NULL DEFAULT ''"),
        ("block_id", "VARCHAR(64) NOT NULL"),
        ("page_num", "INTEGER NOT NULL"),
        ("table_code", "TEXT NOT NULL DEFAULT ''"),
        ("table_title", "TEXT NOT NULL DEFAULT ''"),
        ("table_category", "VARCHAR(32) NOT NULL"),
        ("storage_target", "VARCHAR(32) NOT NULL"),
        ("storage_location", "TEXT NOT NULL"),
        ("columns_json", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
        ("row_count", "INTEGER NOT NULL DEFAULT 0"),
        ("created_at", "TIMESTAMP DEFAULT NOW()"),
        ("updated_at", "TIMESTAMP DEFAULT NOW()"),
    ])
    storage.relational.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS "{TABLE_TABLE_CATALOG}_file_block_uq" '
        f'ON "{TABLE_TABLE_CATALOG}" (file_id, block_id)'
    )
    storage.relational.execute(
        f'CREATE INDEX IF NOT EXISTS "{TABLE_TABLE_CATALOG}_search_idx" '
        f'ON "{TABLE_TABLE_CATALOG}" (file_id, table_code, table_title)'
    )
    # 文件主记录是文件名的唯一来源，修正历史上传留下的 tmp*.pdf 目录值。
    storage.relational.execute(
        f'''UPDATE "{TABLE_TABLE_CATALOG}" AS catalog
            SET file_name = files.file_name,
                updated_at = NOW()
            FROM "pdf_files" AS files
            WHERE catalog.file_id = files.file_id
              AND catalog.file_name IS DISTINCT FROM files.file_name'''
    )


def record_table_catalog(
    *,
    file_id: str,
    file_name: str,
    block: ContentBlock,
    table_code: str,
    table_title: str,
    table_category: str,
    storage_target: str,
    storage_location: str,
    columns: list[dict[str, str]],
    row_count: int,
) -> None:
    """记录一张已成功入库表格的标题、来源和读取方式。"""
    _ensure_table_catalog()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_TABLE_CATALOG}"
            (file_id, file_name, block_id, page_num, table_code, table_title,
             table_category, storage_target, storage_location, columns_json, row_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (file_id, block_id) DO UPDATE SET
                file_name = EXCLUDED.file_name,
                page_num = EXCLUDED.page_num,
                table_code = EXCLUDED.table_code,
                table_title = EXCLUDED.table_title,
                table_category = EXCLUDED.table_category,
                storage_target = EXCLUDED.storage_target,
                storage_location = EXCLUDED.storage_location,
                columns_json = EXCLUDED.columns_json,
                row_count = EXCLUDED.row_count,
                updated_at = NOW()''',
        (
            file_id,
            file_name,
            block.block_id,
            block.page_num,
            table_code,
            table_title,
            table_category,
            storage_target,
            storage_location,
            json.dumps(columns, ensure_ascii=False),
            row_count,
        ),
    )


def _as_json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def _load_table_rows(entry: dict[str, Any], row_limit: int) -> list[dict[str, Any]]:
    """按目录中受控的存储位置读取可展示的表格数据。"""
    storage_target = str(entry.get("storage_target") or "")
    storage_location = str(entry.get("storage_location") or "")

    if storage_target == "pg_relational":
        if not _DATA_TABLE_NAME_PATTERN.fullmatch(storage_location):
            return []
        return storage.relational.query(storage_location, limit=row_limit)

    if storage_target == "pg_jsonb":
        documents = storage.relational.query(
            "pdf_forms",
            where='"id" = %s',
            limit=1,
            params=(storage_location,),
        )
        if not documents:
            return []
        document = _as_json(documents[0].get("doc"), {})
        fields = document.get("fields", {}) if isinstance(document, dict) else {}
        if not isinstance(fields, dict):
            return []
        return [{"field": str(key), "value": value} for key, value in fields.items()]

    return []


def _quote_identifier(value: str) -> str:
    """转义已校验的数据库列名，避免它参与 SQL 结构注入。"""
    return '"' + value.replace('"', '""') + '"'


def _contains_pattern(value: Any) -> str:
    """将包含查询值转成 PostgreSQL ILIKE 的字面量模式。"""
    normalized = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{normalized}%"


def _build_table_filters(
    filters: list[dict[str, Any]] | None,
    columns: list[dict[str, Any]],
) -> tuple[str, tuple[Any, ...]]:
    """将已登记列的包含条件转为参数化 SQL。"""
    if not filters:
        return "", ()

    allowed_columns = {
        str(column.get("key"))
        for column in columns
        if isinstance(column, dict) and column.get("key")
    }
    column_types = {
        str(column.get("key")): str(column.get("data_type", "")).upper()
        for column in columns
        if isinstance(column, dict) and column.get("key")
    }
    where_parts: list[str] = []
    params: list[Any] = []
    for condition in filters:
        if not isinstance(condition, dict):
            raise ValueError("filters 中的每个条件必须是对象")
        column = condition.get("column")
        operator = condition.get("operator")
        if not isinstance(column, str) or column not in allowed_columns:
            raise ValueError("筛选列不存在于该表格")
        if "value" not in condition:
            raise ValueError("筛选条件缺少 value")
        if operator == "contains":
            where_parts.append(f"{_quote_identifier(column)} ILIKE %s ESCAPE '\\'")
            params.append(_contains_pattern(condition["value"]))
        elif operator == "equals":
            where_parts.append(f"{_quote_identifier(column)} = %s")
            params.append(condition["value"])
        elif operator in {"gte", "lte"}:
            if column_types[column] not in {"INTEGER", "BIGINT", "SMALLINT", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE PRECISION"}:
                raise ValueError(f"{operator} 仅支持数值列")
            value = condition["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{operator} 的 value 必须是数值")
            comparison = ">=" if operator == "gte" else "<="
            where_parts.append(f"{_quote_identifier(column)} {comparison} %s")
            params.append(value)
        else:
            raise ValueError("当前仅支持 contains、equals、gte 和 lte 筛选")
    return " AND ".join(where_parts), tuple(params)


def _filter_rows_in_memory(
    rows: list[dict[str, Any]], filters: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """为 JSONB 表单应用与关系表一致的已校验筛选条件。"""
    if not filters:
        return rows

    matched_rows: list[dict[str, Any]] = []
    for row in rows:
        matched = True
        for condition in filters:
            value = row.get(str(condition["column"]))
            expected = condition["value"]
            operator = condition["operator"]
            if operator == "contains":
                matched = str(expected).casefold() in str(value or "").casefold()
            elif operator == "equals":
                matched = value == expected
            elif operator == "gte":
                matched = value is not None and float(value) >= float(expected)
            else:  # operator 已由 _build_table_filters 校验为 lte
                matched = value is not None and float(value) <= float(expected)
            if not matched:
                break
        if matched:
            matched_rows.append(row)
    return matched_rows


def _catalog_entry(file_id: str, block_id: str) -> dict[str, Any] | None:
    """从表格目录定位指定文件中的一张表。"""
    _ensure_table_catalog()
    entries = storage.relational.query(
        TABLE_TABLE_CATALOG,
        where='"file_id" = %s AND "block_id" = %s',
        limit=1,
        params=(file_id, block_id),
    )
    return entries[0] if entries else None


def _table_summary(entry: dict[str, Any]) -> dict[str, Any]:
    """构造前端展示表格所需的来源信息。"""
    return {
        "file_id": entry.get("file_id", ""),
        "file_name": entry.get("file_name", ""),
        "block_id": entry.get("block_id", ""),
        "page_num": entry.get("page_num", 0),
        "table_code": entry.get("table_code", ""),
        "table_title": entry.get("table_title", ""),
        "table_category": entry.get("table_category", ""),
        "row_count": entry.get("row_count", 0),
    }


def _display_rows(rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留业务列，隐藏关系表的内部行号和页码字段。"""
    keys = [str(column.get("key", "")) for column in columns if column.get("key")]
    return [{key: row.get(key) for key in keys} for row in rows]


def query_catalogued_table_data(
    *,
    file_id: str,
    block_id: str,
    filters: list[dict[str, Any]] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any] | None:
    """读取一张目录表格的分页行数据，响应可直接渲染为二维表格。"""
    entry = _catalog_entry(file_id, block_id)
    if entry is None:
        return None

    columns = _as_json(entry.get("columns_json"), [])
    columns = columns if isinstance(columns, list) else []
    where, params = _build_table_filters(filters, columns)
    storage_target = str(entry.get("storage_target") or "")
    storage_location = str(entry.get("storage_location") or "")
    if storage_target == "pg_relational":
        if not _DATA_TABLE_NAME_PATTERN.fullmatch(storage_location):
            rows_with_extra = []
        else:
            rows_with_extra = storage.relational.query(
                storage_location,
                where=where,
                limit=offset + limit + 1,
                params=params,
            )
    else:
        rows_with_extra = _filter_rows_in_memory(
            _load_table_rows(entry, offset + limit + 1),
            filters,
        )
    visible_rows = _display_rows(rows_with_extra, columns)
    page_rows = visible_rows[offset:offset + limit]
    return {
        "table": _table_summary(entry),
        "columns": columns,
        "rows": page_rows,
        "page": {
            "limit": limit,
            "offset": offset,
            "returned": len(page_rows),
            "has_more": len(visible_rows) > offset + limit,
        },
    }


def search_extracted_tables(
    *, query: str, file_id: str | None = None, limit: int = 10, row_limit: int = 100
) -> list[dict[str, Any]]:
    """按表格编号、表名或原始 PDF 文件名搜索，并返回可展示数据。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("搜索关键词不能为空")

    _ensure_table_catalog()
    keyword = f"%{normalized_query}%"
    where_parts = [
        '("table_code" ILIKE %s OR "table_title" ILIKE %s OR "file_name" ILIKE %s)'
    ]
    params: list[Any] = [keyword, keyword, keyword]
    if file_id:
        where_parts.append('"file_id" = %s')
        params.append(file_id)

    entries = storage.relational.query(
        TABLE_TABLE_CATALOG,
        where=" AND ".join(where_parts),
        limit=limit,
        params=tuple(params),
    )
    results: list[dict[str, Any]] = []
    for entry in entries:
        columns = _as_json(entry.get("columns_json"), [])
        result = {
            "file_id": entry.get("file_id", ""),
            "file_name": entry.get("file_name", ""),
            "block_id": entry.get("block_id", ""),
            "page_num": entry.get("page_num", 0),
            "table_code": entry.get("table_code", ""),
            "table_title": entry.get("table_title", ""),
            "table_category": entry.get("table_category", ""),
            "storage_target": entry.get("storage_target", ""),
            "storage_location": entry.get("storage_location", ""),
            "columns": columns if isinstance(columns, list) else [],
            "row_count": entry.get("row_count", 0),
            "rows": _load_table_rows(entry, row_limit),
        }
        results.append(result)
    return results
