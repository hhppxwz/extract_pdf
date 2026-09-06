"""
元数据、批任务与溯源服务。

文件级记录保留现有接口；批任务使用独立表保存可恢复执行所需的状态。
"""
import hashlib
from datetime import datetime
from typing import Any, Optional

from models import (
    BatchItemStatus,
    ContentBlock,
    ProcessingStatus,
    ProcessingVersion,
)
from storage_adapter import storage


# 元数据表名常量
TABLE_FILES = "pdf_files"
TABLE_BLOCK_STORAGE = "pdf_block_storage"
TABLE_BATCHES = "pdf_batches"
TABLE_BATCH_ITEMS = "pdf_batch_items"
_META_READY = False


def _ensure_meta_tables() -> None:
    """确保文件、批次和溯源表存在，并补齐旧表字段。"""
    global _META_READY
    if _META_READY:
        return
    storage.relational.create_table(TABLE_FILES, [
        ("file_id", "VARCHAR(64) PRIMARY KEY"),
        ("file_name", "TEXT"),
        ("file_hash", "VARCHAR(64)"),
        ("file_size", "BIGINT"),
        ("page_count", "INTEGER"),
        ("status", "VARCHAR(20)"),
        ("minio_key", "TEXT"),
        ("error_message", "TEXT"),
        ("parser_version", "TEXT"),
        ("embedding_model", "TEXT"),
        ("llm_model", "TEXT"),
        ("last_attempt_at", "TIMESTAMP"),
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

    # CREATE TABLE IF NOT EXISTS 不会给历史表增加新列，因此使用幂等 ALTER。
    for column, data_type in (
        ("parser_version", "TEXT"),
        ("embedding_model", "TEXT"),
        ("llm_model", "TEXT"),
        ("last_attempt_at", "TIMESTAMP"),
    ):
        storage.relational.execute(
            f'ALTER TABLE "{TABLE_FILES}" ADD COLUMN IF NOT EXISTS "{column}" {data_type}'
        )

    # 历史数据若存在重复哈希，唯一索引会失败；不阻塞现有服务启动。
    try:
        storage.relational.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{TABLE_FILES}_file_hash_uq" '
            f'ON "{TABLE_FILES}" (file_hash) WHERE file_hash IS NOT NULL AND file_hash <> \'\''
        )
    except Exception:
        pass

    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_BATCHES}" (
            batch_id VARCHAR(64) PRIMARY KEY,
            source_dir TEXT NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            total_count INTEGER NOT NULL DEFAULT 0,
            succeeded_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            retryable_failed_count INTEGER NOT NULL DEFAULT 0,
            permanent_failed_count INTEGER NOT NULL DEFAULT 0,
            parser_version TEXT NOT NULL DEFAULT '',
            embedding_model TEXT NOT NULL DEFAULT '',
            llm_model TEXT NOT NULL DEFAULT '',
            pipeline_version TEXT NOT NULL DEFAULT '',
            max_attempts INTEGER NOT NULL DEFAULT 3,
            created_at TIMESTAMP DEFAULT NOW(),
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_BATCH_ITEMS}" (
            item_id VARCHAR(64) PRIMARY KEY,
            batch_id VARCHAR(64) NOT NULL REFERENCES "{TABLE_BATCHES}"(batch_id) ON DELETE CASCADE,
            file_id VARCHAR(64),
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_hash VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            error_kind VARCHAR(32),
            next_retry_at TIMESTAMP,
            started_at TIMESTAMP,
            heartbeat_at TIMESTAMP,
            finished_at TIMESTAMP,
            reuse_file_id VARCHAR(64),
            duplicate_of_item_id VARCHAR(64),
            UNIQUE(batch_id, file_path)
        )'''
    )
    storage.relational.execute(
        f'ALTER TABLE "{TABLE_BATCH_ITEMS}" ADD COLUMN IF NOT EXISTS '
        f'"duplicate_of_item_id" VARCHAR(64)'
    )
    storage.relational.execute(
        f'CREATE INDEX IF NOT EXISTS "{TABLE_BATCH_ITEMS}_batch_status_idx" '
        f'ON "{TABLE_BATCH_ITEMS}" (batch_id, status)'
    )
    storage.relational.execute(
        f'CREATE INDEX IF NOT EXISTS "{TABLE_BATCH_ITEMS}_hash_idx" '
        f'ON "{TABLE_BATCH_ITEMS}" (file_hash)'
    )
    _META_READY = True


def _generate_file_id(file_name: str, file_data: bytes = b"") -> str:
    """基于文件名和内容生成兼容旧数据的 file_id。"""
    seed = f"{file_name}:{hashlib.md5(file_data).hexdigest()[:16]}"
    return f"pdf_{hashlib.md5(seed.encode()).hexdigest()[:16]}"


def calculate_file_hash(file_data: bytes) -> str:
    """计算文件 SHA256。"""
    return hashlib.sha256(file_data).hexdigest()


def get_file_by_hash(file_hash: str) -> Optional[dict[str, Any]]:
    """按 SHA256 查询文件记录。"""
    _ensure_meta_tables()
    rows = storage.relational.query(
        TABLE_FILES,
        where='"file_hash" = %s',
        limit=1,
        params=(file_hash,),
    )
    return rows[0] if rows else None


def ensure_file_record(
    file_name: str,
    file_data: bytes,
    page_count: int = 0,
    versions: Optional[ProcessingVersion] = None,
) -> dict[str, Any]:
    """按 SHA256 幂等创建文件记录，并返回最终记录。"""
    _ensure_meta_tables()
    file_hash = calculate_file_hash(file_data)
    existing = get_file_by_hash(file_hash)
    if existing:
        return existing

    file_id = _generate_file_id(file_name, file_data)
    version_values = versions.model_dump() if versions else {}
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_FILES}"
            (file_id, file_name, file_hash, file_size, page_count, status,
             minio_key, error_message, parser_version, embedding_model,
             llm_model, last_attempt_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING''',
        (
            file_id,
            file_name,
            file_hash,
            len(file_data),
            page_count,
            ProcessingStatus.PENDING.value,
            f"pdf/{file_id}.pdf",
            "",
            version_values.get("parser_version", ""),
            version_values.get("embedding_model", ""),
            version_values.get("llm_model", ""),
            None,
        ),
    )
    created = get_file_by_hash(file_hash)
    if not created:
        raise RuntimeError(f"无法创建文件记录: {file_name}")
    return created


def record_file_start(
    file_name: str, file_data: bytes, page_count: int = 0
) -> str:
    """记录文件开始处理，按 SHA256 复用已有 file_id。"""
    record = ensure_file_record(file_name, file_data, page_count)
    return record["file_id"]


def record_file_attempt(
    file_id: str,
    versions: Optional[ProcessingVersion] = None,
) -> None:
    """记录一次文件处理尝试，并保存本次使用的版本。"""
    now = datetime.now()
    values: dict[str, Any] = {
        "status": ProcessingStatus.PROCESSING.value,
        "error_message": "",
        "last_attempt_at": now,
        "updated_at": now,
    }
    if versions:
        values.update({
            "parser_version": versions.parser_version,
            "embedding_model": versions.embedding_model,
            "llm_model": versions.llm_model,
        })
    storage.relational.update_rows(
        TABLE_FILES,
        values,
        '"file_id" = %s',
        (file_id,),
    )


def release_stale_file_processing(
    file_id: str,
    stale_before: datetime,
) -> int:
    """释放超过心跳期限的文件级 processing 状态，不删除文件记录。"""
    return storage.relational.update_rows(
        TABLE_FILES,
        {
            "status": ProcessingStatus.PENDING.value,
            "error_message": "原处理进程心跳超时，文件已释放，等待批任务重新处理",
            "updated_at": datetime.now(),
        },
        '"file_id" = %s AND "status" IN (%s, %s) '
        'AND ("last_attempt_at" IS NULL OR "last_attempt_at" < %s)',
        (
            file_id,
            ProcessingStatus.PROCESSING.value,
            "running",
            stale_before,
        ),
    )


def record_file_done(file_id: str) -> None:
    """标记文件处理完成。"""
    _update_file_status(file_id, ProcessingStatus.DONE)


def record_file_failed(file_id: str, error: str) -> None:
    """标记文件处理失败。"""
    _update_file_status(file_id, ProcessingStatus.FAILED, error)


def _update_file_status(
    file_id: str, status: ProcessingStatus, error: str = ""
) -> None:
    """更新文件状态，使用参数化条件。"""
    storage.relational.update_rows(
        TABLE_FILES,
        {
            "status": status.value,
            "error_message": error,
            "updated_at": datetime.now(),
        },
        '"file_id" = %s',
        (file_id,),
    )


def record_block_storage(
    file_id: str,
    block: ContentBlock,
    storage_target: str,
    storage_location: str,
) -> None:
    """记录内容块的存储去向。"""
    _ensure_meta_tables()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_BLOCK_STORAGE}"
            (file_id, block_id, block_type, page_num, storage_target, storage_location)
            VALUES (%s, %s, %s, %s, %s, %s)''',
        (
            file_id,
            block.block_id,
            block.type.value,
            block.page_num,
            storage_target,
            storage_location,
        ),
    )


def create_batch(
    batch_id: str,
    source_dir: str,
    total_count: int,
    versions: ProcessingVersion,
    max_attempts: int = 3,
) -> None:
    """创建批次记录。"""
    _ensure_meta_tables()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_BATCHES}"
            (batch_id, source_dir, total_count, parser_version, embedding_model,
             llm_model, pipeline_version, max_attempts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (batch_id) DO NOTHING''',
        (
            batch_id,
            source_dir,
            total_count,
            versions.parser_version,
            versions.embedding_model,
            versions.llm_model,
            versions.pipeline_version,
            max_attempts,
        ),
    )


def add_batch_item(
    batch_id: str,
    item_id: str,
    file_path: str,
    file_name: str,
    file_hash: str,
    status: BatchItemStatus = BatchItemStatus.PENDING,
    file_id: Optional[str] = None,
    reuse_file_id: Optional[str] = None,
    duplicate_of_item_id: Optional[str] = None,
) -> None:
    """向批次加入一个文件任务。"""
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_BATCH_ITEMS}"
            (item_id, batch_id, file_id, file_path, file_name, file_hash, status,
             reuse_file_id, duplicate_of_item_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (batch_id, file_path) DO NOTHING''',
        (
            item_id,
            batch_id,
            file_id,
            file_path,
            file_name,
            file_hash,
            status.value,
            reuse_file_id,
            duplicate_of_item_id,
        ),
    )


def get_batch(batch_id: str) -> Optional[dict[str, Any]]:
    """查询批次记录。"""
    _ensure_meta_tables()
    rows = storage.relational.query(
        TABLE_BATCHES,
        where='"batch_id" = %s',
        limit=1,
        params=(batch_id,),
    )
    return rows[0] if rows else None


def get_batch_items(batch_id: str) -> list[dict[str, Any]]:
    """查询批次中的所有文件任务。"""
    _ensure_meta_tables()
    return storage.relational.query(
        TABLE_BATCH_ITEMS,
        where='"batch_id" = %s',
        limit=10000,
        params=(batch_id,),
    )


def get_processing_owners(file_id: str) -> list[dict[str, Any]]:
    """查询当前仍声明占用某个文件的批任务。"""
    _ensure_meta_tables()
    return storage.relational.query(
        TABLE_BATCH_ITEMS,
        '"file_id" = %s AND "status" = %s',
        100,
        (file_id, BatchItemStatus.RUNNING.value),
    )


def update_batch(batch_id: str, values: dict[str, Any]) -> None:
    """更新批次状态或统计信息。"""
    storage.relational.update_rows(
        TABLE_BATCHES,
        values,
        '"batch_id" = %s',
        (batch_id,),
    )


def update_batch_item(item_id: str, values: dict[str, Any]) -> None:
    """更新单个批任务状态。"""
    storage.relational.update_rows(
        TABLE_BATCH_ITEMS,
        values,
        '"item_id" = %s',
        (item_id,),
    )


def get_file_status(file_id: str) -> Optional[dict]:
    """查询某个 PDF 文件的处理状态。"""
    _ensure_meta_tables()
    results = storage.relational.query(
        TABLE_FILES,
        where='"file_id" = %s',
        limit=1,
        params=(file_id,),
    )
    return results[0] if results else None


def get_file_blocks(file_id: str) -> list[dict]:
    """查询某文件所有块的存储溯源。"""
    return storage.relational.query(
        TABLE_BLOCK_STORAGE,
        where='"file_id" = %s',
        limit=1000,
        params=(file_id,),
    )
