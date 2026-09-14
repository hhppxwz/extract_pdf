"""按文件重置可自动再生的数据库产物。"""
from __future__ import annotations

import re
from typing import Any, Callable

from storage_adapter import storage


_FILE_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_DATA_TABLE_PATTERN = re.compile(r"^pdf_[A-Za-z0-9_]+_tbl_[A-Za-z0-9_]+$")


def is_valid_file_id(file_id: str) -> bool:
    """校验将用于内部动态表名的文件标识。"""
    return bool(_FILE_ID_PATTERN.fullmatch(str(file_id or "")))


def _is_missing_table_error(error: Exception) -> bool:
    """将尚未创建的可选产物表视为没有可清理内容。"""
    text = str(error).lower()
    return "does not exist" in text or "undefinedtable" in text


def _ignore_missing_table(action: Callable[[], Any]) -> Any:
    """执行清理操作，忽略未创建的可选产物表。"""
    try:
        return action()
    except Exception as error:
        if _is_missing_table_error(error):
            return None
        raise


def _load_table_artifacts(file_id: str) -> list[dict[str, Any]]:
    """读取文件当前登记的数据表，供删除动态关系表使用。"""
    records = _ignore_missing_table(
        lambda: storage.relational.query(
            "pdf_table_catalog",
            where='"file_id" = %s',
            limit=10000,
            params=(file_id,),
        )
    )
    return list(records or [])


def reset_file_database_artifacts(file_id: str) -> dict[str, int]:
    """清理指定 PDF 的自动数据库产物，并将其重新置为待处理。"""
    normalized_file_id = str(file_id or "").strip()
    if not is_valid_file_id(normalized_file_id):
        raise ValueError("文件 ID 格式非法")

    files = storage.relational.query(
        "pdf_files",
        where='"file_id" = %s',
        limit=1,
        params=(normalized_file_id,),
    )
    if not files:
        raise ValueError(f"文件不存在: {normalized_file_id}")

    summary = {
        "text_vectors": 0,
        "data_tables": 0,
        "forms": 0,
        "table_catalog_records": 0,
        "block_records": 0,
        "policy_clauses": 0,
    }

    # 文本索引按文件独立建表，删除同一 file_id 的旧向量可避免重跑后重复命中。
    text_index_name = f"pdf_{normalized_file_id}_text"
    deleted_vectors = _ignore_missing_table(
        lambda: storage.vector.delete_vectors_by_metadata(
            text_index_name, "file_id", normalized_file_id
        )
    )
    summary["text_vectors"] = int(deleted_vectors or 0)

    table_artifacts = _load_table_artifacts(normalized_file_id)
    for artifact in table_artifacts:
        location = str(artifact.get("storage_location") or "")
        if (
            artifact.get("storage_target") == "pg_relational"
            and _DATA_TABLE_PATTERN.fullmatch(location)
        ):
            storage.relational.drop_table(location)
            summary["data_tables"] += 1

    # 表单存放在统一 JSONB 表，通过 file_id 从文档内容中删除。
    removed_forms = _ignore_missing_table(
        lambda: storage.relational.execute(
            'DELETE FROM "pdf_forms" WHERE doc ->> \'file_id\' = %s',
            (normalized_file_id,),
        )
    )
    summary["forms"] = len([
        artifact for artifact in table_artifacts
        if artifact.get("storage_target") == "pg_jsonb"
    ]) if removed_forms is None else 0

    _ignore_missing_table(
        lambda: storage.relational.execute(
            'DELETE FROM "pdf_table_catalog" WHERE "file_id" = %s',
            (normalized_file_id,),
        )
    )
    summary["table_catalog_records"] = len(table_artifacts)

    block_records = _ignore_missing_table(
        lambda: storage.relational.query(
            "pdf_block_storage",
            where='"file_id" = %s',
            limit=100000,
            params=(normalized_file_id,),
        )
    ) or []
    summary["block_records"] = len(block_records)
    _ignore_missing_table(
        lambda: storage.relational.execute(
            'DELETE FROM "pdf_block_storage" WHERE "file_id" = %s',
            (normalized_file_id,),
        )
    )

    # 制度条款与条款索引可由下一次 PDF 处理完全重建。
    policy_records = _ignore_missing_table(
        lambda: storage.relational.query(
            "policy_documents",
            where='"file_id" = %s',
            limit=1,
            params=(normalized_file_id,),
        )
    ) or []
    if policy_records:
        policy_id = str(policy_records[0].get("policy_id") or "")
        if policy_id:
            _ignore_missing_table(
                lambda: storage.vector.delete_vectors_by_metadata(
                    "policy_clause_vectors", "policy_id", policy_id
                )
            )
            clauses = _ignore_missing_table(
                lambda: storage.relational.query(
                    "policy_clauses",
                    where='"policy_id" = %s',
                    limit=100000,
                    params=(policy_id,),
                )
            ) or []
            _ignore_missing_table(
                lambda: storage.relational.execute(
                    'DELETE FROM "policy_clauses" WHERE "policy_id" = %s',
                    (policy_id,),
                )
            )
            summary["policy_clauses"] = len(clauses)
            _ignore_missing_table(
                lambda: storage.relational.update_rows(
                    "policy_documents",
                    {"structure_status": "pending", "parse_quality": 0.0},
                    '"policy_id" = %s',
                    (policy_id,),
                )
            )

    storage.relational.update_rows(
        "pdf_files",
        {"status": "pending", "error_message": "", "last_attempt_at": None},
        '"file_id" = %s',
        (normalized_file_id,),
    )
    return summary
