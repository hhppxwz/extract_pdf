"""批量 PDF 处理器：批次记录、断点续跑、重试和 SHA256 去重。"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import fitz

from config import app_config
from metadata_service import (
    add_batch_item,
    calculate_file_hash,
    create_batch,
    ensure_file_record,
    get_batch,
    get_batch_items,
    get_file_by_hash,
    get_processing_owners,
    release_stale_file_processing,
    update_batch,
    update_batch_item,
    record_file_attempt,
)
from models import BatchItemStatus, BatchStatus, ErrorKind, ProcessingResult, ProcessingVersion
from pipeline import process_pdf


DEFAULT_MAX_ATTEMPTS = 3
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("BATCH_HEARTBEAT_TIMEOUT_SECONDS", "1800"))
RETRY_BACKOFF_SECONDS = (2, 4, 8)


def current_processing_versions() -> ProcessingVersion:
    """读取本批次固定使用的解析器和模型版本。"""
    parser_version = os.getenv(
        "PARSER_VERSION",
        f"{app_config.parser.parser_backend}-{app_config.cloudmineru.mode}",
    )
    return ProcessingVersion(
        parser_version=parser_version,
        embedding_model=app_config.embedding.model_name,
        llm_model=app_config.llm.model,
        pipeline_version=os.getenv("PIPELINE_VERSION", "1.0.0"),
    )


def _sha256_file(file_path: Path) -> str:
    """分块计算文件 SHA256，避免一次性占用大量内存。"""
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _list_pdf_files(source_dir: Path) -> list[Path]:
    """递归收集 PDF，按路径排序以保证批次顺序稳定。"""
    return sorted(
        (path.resolve() for path in source_dir.rglob("*")
         if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: str(path).lower(),
    )


def create_batch_job(
    source_dir: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """扫描目录并创建批次及文件任务，返回 batch_id。"""
    directory = Path(source_dir).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"批处理目录不存在: {directory}")

    files = _list_pdf_files(directory)
    if not files:
        raise ValueError(f"目录中没有 PDF 文件: {directory}")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于等于 1")

    batch_id = f"batch_{uuid.uuid4().hex[:16]}"
    versions = current_processing_versions()
    create_batch(batch_id, str(directory), len(files), versions, max_attempts)

    seen_hashes: dict[str, str] = {}
    for file_path in files:
        file_hash = _sha256_file(file_path)
        existing = get_file_by_hash(file_hash)
        item_id = f"item_{uuid.uuid4().hex[:20]}"
        duplicate_of_item_id = seen_hashes.get(file_hash)
        is_done = existing and existing.get("status") in {
            "done", "succeeded"
        }
        if not duplicate_of_item_id:
            seen_hashes[file_hash] = item_id
        add_batch_item(
            batch_id=batch_id,
            item_id=item_id,
            file_path=str(file_path),
            file_name=file_path.name,
            file_hash=file_hash,
            status=BatchItemStatus.SKIPPED if is_done or duplicate_of_item_id else BatchItemStatus.PENDING,
            file_id=existing.get("file_id") if is_done else None,
            reuse_file_id=existing.get("file_id") if is_done else None,
            duplicate_of_item_id=duplicate_of_item_id,
        )
    return batch_id


def _parse_datetime(value: Any) -> Optional[datetime]:
    """兼容 PostgreSQL datetime 和 ISO 字符串。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _is_stale(item: dict[str, Any]) -> bool:
    """判断 running 任务是否已经失去心跳。"""
    heartbeat = _parse_datetime(item.get("heartbeat_at"))
    if heartbeat is None:
        return True
    return datetime.now() - heartbeat > timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)


def _is_file_attempt_stale(record: dict[str, Any]) -> bool:
    """判断没有批任务心跳时，文件级 processing 是否已经过期。"""
    last_attempt = _parse_datetime(record.get("last_attempt_at"))
    if last_attempt is None:
        return True
    return datetime.now() - last_attempt > timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)


def _release_stale_owner(owner: dict[str, Any]) -> None:
    """释放心跳过期的旧批任务，让后续任务可以接管。"""
    now = datetime.now()
    update_batch_item(
        owner["item_id"],
        {
            "status": BatchItemStatus.RETRY_WAIT.value,
            "last_error": "原处理进程心跳超时，任务已释放，等待其他批次接管",
            "error_kind": ErrorKind.TRANSIENT.value,
            "next_retry_at": now,
            "heartbeat_at": now,
        },
    )


def _is_retry_due(item: dict[str, Any]) -> bool:
    """判断 retry_wait 任务是否到达下次重试时间。"""
    next_retry = _parse_datetime(item.get("next_retry_at"))
    return next_retry is None or next_retry <= datetime.now()


def _is_retryable_error(result: ProcessingResult) -> bool:
    """从现有管线错误摘要中识别临时错误。"""
    text = " ".join(result.errors).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "连接",
        "connection",
        "temporarily",
        "rate limit",
        "too many requests",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "operationalerror",
        "server error",
    )
    return any(marker in text for marker in transient_markers)


def _mark_item_running(item: dict[str, Any], file_id: str) -> int:
    """领取任务并递增尝试次数。"""
    attempt_count = int(item.get("attempt_count") or 0) + 1
    now = datetime.now()
    update_batch_item(
        item["item_id"],
        {
            "file_id": file_id,
            "status": BatchItemStatus.RUNNING.value,
            "attempt_count": attempt_count,
            "last_error": "",
            "error_kind": None,
            "next_retry_at": None,
            "started_at": item.get("started_at") or now,
            "heartbeat_at": now,
            "finished_at": None,
        },
    )
    return attempt_count


def _mark_retry(item_id: str, error: str, attempt_count: int) -> None:
    """保存临时错误和下次重试时间。"""
    delay = RETRY_BACKOFF_SECONDS[min(attempt_count - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    update_batch_item(
        item_id,
        {
            "status": BatchItemStatus.RETRY_WAIT.value,
            "last_error": error[:4000],
            "error_kind": ErrorKind.TRANSIENT.value,
            "next_retry_at": datetime.now() + timedelta(seconds=delay),
            "heartbeat_at": datetime.now(),
        },
    )


def _mark_failed(item_id: str, error: str, kind: ErrorKind) -> None:
    """保存不可恢复或达到最大次数的错误。"""
    update_batch_item(
        item_id,
        {
            "status": BatchItemStatus.FAILED.value,
            "last_error": error[:4000],
            "error_kind": kind.value,
            "finished_at": datetime.now(),
            "heartbeat_at": datetime.now(),
        },
    )


def _process_item(item: dict[str, Any], versions: ProcessingVersion, max_attempts: int) -> None:
    """处理一个文件任务，临时错误自动重试并持久化每次状态。"""
    file_path = Path(item["file_path"])
    if not file_path.is_file():
        _mark_failed(item["item_id"], f"本地文件不存在: {file_path}", ErrorKind.PERMANENT)
        return

    try:
        with fitz.open(str(file_path)) as document:
            page_count = len(document)
        file_data = file_path.read_bytes()
    except Exception as exc:
        _mark_failed(item["item_id"], f"PDF 文件无法读取: {exc}", ErrorKind.PERMANENT)
        return

    # 二次核对，避免批次创建后源文件被替换。
    actual_hash = calculate_file_hash(file_data)
    if actual_hash != item["file_hash"]:
        _mark_failed(item["item_id"], "文件内容在批次创建后发生变化", ErrorKind.PERMANENT)
        return

    record = ensure_file_record(item["file_name"], file_data, page_count, versions)
    existing_status = record.get("status")
    if existing_status in {"done", "succeeded"}:
        update_batch_item(
            item["item_id"],
            {
                "file_id": record["file_id"],
                "reuse_file_id": record["file_id"],
                "status": BatchItemStatus.SKIPPED.value,
                "finished_at": datetime.now(),
                "heartbeat_at": datetime.now(),
            },
        )
        return
    if existing_status in {"processing", "running"} and item.get("file_id") != record.get("file_id"):
        # 不能只看 pdf_files.status。旧进程中断后可能留下 processing，
        # 需要结合真正占用该文件的批任务 heartbeat 判断是否仍有活跃任务。
        owners = get_processing_owners(record["file_id"])
        active_owner = False
        for owner in owners:
            if _is_stale(owner):
                _release_stale_owner(owner)
            else:
                active_owner = True

        if active_owner:
            _mark_retry(
                item["item_id"],
                "同一 SHA256 的文件正在其他活跃任务中处理",
                int(item.get("attempt_count") or 0) + 1,
            )
            return

        # 也兼容单文件命令留下的 processing 状态：没有批任务占用者时，
        # 文件级最近尝试时间未过期就等待，过期则允许当前批次接管。
        if not owners and not _is_file_attempt_stale(record):
            _mark_retry(
                item["item_id"],
                "文件最近仍有单文件任务在处理",
                int(item.get("attempt_count") or 0) + 1,
            )
            return

        if _is_file_attempt_stale(record):
            # 旧单文件进程可能只留下 pdf_files.processing，恢复时一并释放。
            release_stale_file_processing(
                record["file_id"],
                datetime.now() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS),
            )

    latest_item = item
    for _ in range(max_attempts):
        attempt_count = _mark_item_running(latest_item, record["file_id"])
        record_file_attempt(record["file_id"], versions)
        result = process_pdf(str(file_path), page_count)
        if result.status.value in {"done", "succeeded"}:
            update_batch_item(
                item["item_id"],
                {
                    "file_id": result.file_id or record["file_id"],
                    "status": BatchItemStatus.SUCCEEDED.value,
                    "last_error": "",
                    "error_kind": None,
                    "finished_at": datetime.now(),
                    "heartbeat_at": datetime.now(),
                },
            )
            return

        error = "; ".join(result.errors) or "文件处理失败"
        if _is_retryable_error(result) and attempt_count < max_attempts:
            _mark_retry(item["item_id"], error, attempt_count)
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt_count - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
            latest_item = dict(item)
            latest_item["attempt_count"] = attempt_count
            latest_item["file_id"] = record["file_id"]
            continue

        is_transient = _is_retryable_error(result)
        kind = (
            ErrorKind.TRANSIENT
            if is_transient and attempt_count < max_attempts
            else ErrorKind.PERMANENT
        )
        _mark_failed(item["item_id"], error, kind)
        return


def _eligible(
    item: dict[str, Any],
    resume: bool,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """判断任务是否应在本次运行中领取。"""
    status = item.get("status")
    if status in {BatchItemStatus.SUCCEEDED.value, BatchItemStatus.SKIPPED.value}:
        return False
    if status == BatchItemStatus.PENDING.value:
        return True
    if status == BatchItemStatus.RETRY_WAIT.value:
        return _is_retry_due(item)
    if status == BatchItemStatus.RUNNING.value:
        return _is_stale(item)
    if status == BatchItemStatus.FAILED.value:
        return resume and item.get("error_kind") == ErrorKind.TRANSIENT.value \
            and int(item.get("attempt_count") or 0) < max_attempts
    return False


def _refresh_batch(batch_id: str) -> dict[str, Any]:
    """重新统计批次状态并持久化。"""
    batch = get_batch(batch_id)
    items = get_batch_items(batch_id)
    counts = {
        "succeeded_count": sum(i.get("status") == BatchItemStatus.SUCCEEDED.value for i in items),
        "skipped_count": sum(i.get("status") == BatchItemStatus.SKIPPED.value for i in items),
        "retryable_failed_count": sum(
            i.get("status") == BatchItemStatus.RETRY_WAIT.value
            or i.get("error_kind") == ErrorKind.TRANSIENT.value
            for i in items
        ),
        "permanent_failed_count": sum(
            i.get("status") == BatchItemStatus.FAILED.value
            and i.get("error_kind") == ErrorKind.PERMANENT.value
            for i in items
        ),
    }
    terminal = {
        BatchItemStatus.SUCCEEDED.value,
        BatchItemStatus.SKIPPED.value,
        BatchItemStatus.FAILED.value,
    }
    all_terminal = bool(items) and all(i.get("status") in terminal for i in items)
    has_failures = counts["permanent_failed_count"] > 0 or any(
        i.get("status") == BatchItemStatus.FAILED.value for i in items
    )
    if all_terminal:
        has_success = counts["succeeded_count"] + counts["skipped_count"] > 0
        if not has_failures:
            status = BatchStatus.SUCCEEDED.value
        elif has_success:
            status = BatchStatus.PARTIAL_FAILED.value
        else:
            status = BatchStatus.FAILED.value
        finished_at = datetime.now()
    else:
        status = BatchStatus.RUNNING.value
        finished_at = None

    update_batch(
        batch_id,
        {
            "status": status,
            **counts,
            "started_at": (batch or {}).get("started_at") or datetime.now(),
            "finished_at": finished_at,
        },
    )
    return get_batch_status(batch_id)


def run_batch(batch_id: str, resume: bool = False) -> dict[str, Any]:
    """运行新批次或恢复已有批次。"""
    batch = get_batch(batch_id)
    if not batch:
        raise ValueError(f"批次不存在: {batch_id}")

    update_batch(
        batch_id,
        {
            "status": BatchStatus.RUNNING.value,
            "started_at": batch.get("started_at") or datetime.now(),
            "finished_at": None,
        },
    )
    versions = ProcessingVersion(
        parser_version=batch.get("parser_version", ""),
        embedding_model=batch.get("embedding_model", ""),
        llm_model=batch.get("llm_model", ""),
        pipeline_version=batch.get("pipeline_version", ""),
    )
    max_attempts = int(batch.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)

    for item in get_batch_items(batch_id):
        if item.get("status") == BatchItemStatus.RUNNING.value and _is_stale(item):
            update_batch_item(item["item_id"], {"status": BatchItemStatus.PENDING.value})
        if _eligible(item, resume, max_attempts):
            _process_item(item, versions, max_attempts)
            _refresh_batch(batch_id)

    return _refresh_batch(batch_id)


def get_batch_status(batch_id: str) -> dict[str, Any]:
    """返回批次及其当前文件统计。"""
    batch = get_batch(batch_id)
    if not batch:
        raise ValueError(f"批次不存在: {batch_id}")
    items = get_batch_items(batch_id)
    return {"batch": batch, "items": items}
