"""
元数据与溯源服务
记录每个 PDF 的处理状态和每个内容块的存储去向。
"""
import hashlib
from datetime import datetime
from typing import Optional

from models import ProcessingStatus, ContentBlock
from storage_adapter import storage

# 元数据表名常量
TABLE_FILES = "pdf_files"
TABLE_BLOCK_STORAGE = "pdf_block_storage"


def _ensure_meta_tables() -> None:
    """确保元数据表存在"""
    storage.relational.create_table(TABLE_FILES, [
        ("file_id", "VARCHAR(64) PRIMARY KEY"),
        ("file_name", "TEXT"),
        ("file_hash", "VARCHAR(64)"),
        ("file_size", "BIGINT"),
        ("page_count", "INTEGER"),
        ("status", "VARCHAR(20)"),
        ("minio_key", "TEXT"),
        ("error_message", "TEXT"),
        ("created_at", "TIMESTAMP DEFAULT NOW()"),
        ("updated_at", "TIMESTAMP DEFAULT NOW()"),
    ])

    storage.relational.create_table(TABLE_BLOCK_STORAGE, [
        ("id", "SERIAL PRIMARY KEY"),
        ("file_id", "VARCHAR(64)"),
        ("block_id", "VARCHAR(64)"),
        ("block_type", "VARCHAR(20)"),
        ("page_num", "INTEGER"),
        ("storage_target", "TEXT"),
        ("storage_location", "TEXT"),
        ("created_at", "TIMESTAMP DEFAULT NOW()"),
    ])


def _generate_file_id(file_name: str, file_data: bytes = b"") -> str:
    """基于文件名 + 内容哈希生成稳定的 file_id"""
    seed = f"{file_name}:{hashlib.md5(file_data).hexdigest()[:16]}"
    return f"pdf_{hashlib.md5(seed.encode()).hexdigest()[:16]}"


def record_file_start(
    file_name: str, file_data: bytes, page_count: int = 0
) -> str:
    """
    记录文件开始处理。幂等：同哈希已处理过的文件直接返回已有 file_id。
    返回 file_id。
    """
    _ensure_meta_tables()

    file_hash = hashlib.sha256(file_data).hexdigest()
    file_id = _generate_file_id(file_name, file_data)

    # 检查是否已存在且已完成（幂等跳过）
    existing = storage.relational.query(
        TABLE_FILES, where=f"file_hash='{file_hash}' AND status='done'", limit=1
    )
    if existing:
        return existing[0]["file_id"]

    # 插入新记录
    try:
        storage.relational.insert_rows(TABLE_FILES, [
            "file_id", "file_name", "file_hash", "file_size",
            "page_count", "status", "minio_key", "error_message",
        ], [[
            file_id, file_name, file_hash, len(file_data),
            page_count, ProcessingStatus.PROCESSING.value,
            f"pdf/{file_id}.pdf", "",
        ]])
    except Exception:
        pass  # 并发冲突时忽略

    return file_id


def record_file_done(file_id: str) -> None:
    """标记文件处理完成"""
    _update_file_status(file_id, ProcessingStatus.DONE)


def record_file_failed(file_id: str, error: str) -> None:
    """标记文件处理失败"""
    _update_file_status(file_id, ProcessingStatus.FAILED, error)


def _update_file_status(
    file_id: str, status: ProcessingStatus, error: str = ""
) -> None:
    """更新文件状态（mock 模式操作内存表）"""
    if hasattr(storage.relational, "_tables"):
        for row in storage.relational._tables.get(TABLE_FILES, []):
            if row.get("file_id") == file_id:
                row["status"] = status.value
                row["error_message"] = error
                row["updated_at"] = datetime.now().isoformat()


def record_block_storage(
    file_id: str,
    block: ContentBlock,
    storage_target: str,
    storage_location: str,
) -> None:
    """
    记录一个内容块的存储去向。
    storage_target: pg_relational / pg_vector / pg_jsonb / minio
    storage_location: 表名 / bucket key / 索引名
    """
    _ensure_meta_tables()
    try:
        storage.relational.insert_rows(TABLE_BLOCK_STORAGE, [
            "file_id", "block_id", "block_type", "page_num",
            "storage_target", "storage_location",
        ], [[
            file_id, block.block_id, block.type.value, block.page_num,
            storage_target, storage_location,
        ]])
    except Exception:
        pass  # 单条溯源记录失败不阻塞整体流程


def get_file_status(file_id: str) -> Optional[dict]:
    """查询文件处理状态"""
    results = storage.relational.query(
        TABLE_FILES, where=f"file_id='{file_id}'", limit=1
    )
    return results[0] if results else None


def get_file_blocks(file_id: str) -> list[dict]:
    """查询某文件所有块的存储溯源"""
    return storage.relational.query(
        TABLE_BLOCK_STORAGE, where=f"file_id='{file_id}'", limit=1000
    )
