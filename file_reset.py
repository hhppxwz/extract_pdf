"""按文件重置可自动再生的数据库产物。"""
from __future__ import annotations

import re
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from storage_adapter import storage


_FILE_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_DATA_TABLE_PATTERN = re.compile(r"^pdf_[A-Za-z0-9_]+_tbl_[A-Za-z0-9_]+$")
_IN_RESET_TRANSACTION = ContextVar("in_reset_transaction", default=False)
_POLICY_TASK_TABLES = (
    ("policy_process_items", "policy_process_runs"),
    ("policy_extraction_items", "policy_extraction_runs"),
    ("policy_clause_classification_items", "policy_clause_classification_runs"),
    ("policy_knowledge_items_v2", "policy_knowledge_runs_v2"),
    ("policy_clause_index_items", "policy_clause_index_runs"),
)


@contextmanager
def _reset_transaction():
    """目录与单文件共用一个事务，任何清理失败均回滚。"""
    if _IN_RESET_TRANSACTION.get():
        yield
        return
    with storage.relational.transaction():
        token = _IN_RESET_TRANSACTION.set(True)
        try:
            yield
        finally:
            _IN_RESET_TRANSACTION.reset(token)


def is_valid_file_id(file_id: str) -> bool:
    """校验将用于内部动态表名的文件标识。"""
    return bool(_FILE_ID_PATTERN.fullmatch(str(file_id or "")))


def _inside_folder(file_path: Any, folder: Path) -> bool:
    """仅接受规范化后确实落在目标目录下的路径。"""
    try:
        return Path(str(file_path)).resolve().is_relative_to(folder)
    except (OSError, TypeError, ValueError):
        return False


def _hash_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expired_processing_time(value: Any) -> bool:
    """有可核验时间且超过批处理保护期时，才允许显式释放旧占用。"""
    from batch_processor import HEARTBEAT_TIMEOUT_SECONDS

    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return datetime.now(moment.tzinfo) - moment > timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return False


def _processing_groups(files: list[dict], items: list[dict], selected: set[str]) -> tuple[set[str], set[str]]:
    """区分所有占用和可核验已过期的占用；任何近期信号都会阻止释放。"""
    busy: set[str] = set()
    active: set[str] = set()
    for item in items:
        affected = {str(item.get(key) or "") for key in ("file_id", "reuse_file_id")} & selected
        if str(item.get("status") or "").lower() in {"running", "processing"}:
            busy.update(affected)
            if not _expired_processing_time(item.get("heartbeat_at") or item.get("started_at")):
                active.update(affected)
    for record in files:
        file_id = str(record.get("file_id") or "")
        if file_id in selected and str(record.get("status") or "").lower() in {"running", "processing"}:
            busy.add(file_id)
            if not _expired_processing_time(record.get("last_attempt_at")):
                active.add(file_id)
    return busy, busy - active


def build_folder_reset_plan(
    folder: str | Path, file_records: list[dict[str, Any]], batch_items: list[dict[str, Any]]
) -> dict[str, Any]:
    """从批次来源路径及当前文件哈希推导目录内已导入文件。"""
    root = Path(folder).resolve()
    if not root.is_dir() or root == Path(root.anchor) or root in (Path.cwd().resolve(), Path.home().resolve()):
        raise ValueError("重置目标必须是存在的具体子目录，不能是磁盘根目录、项目根目录或用户目录")
    file_records = [record for record in file_records if not str(record.get("file_name") or "").startswith("~$")]
    by_hash = {
        str(record.get("file_hash")): str(record.get("file_id"))
        for record in file_records if record.get("file_hash") and is_valid_file_id(record.get("file_id"))
    }
    valid_ids = {str(record.get("file_id")) for record in file_records if is_valid_file_id(record.get("file_id"))}
    selected: set[str] = set()
    source_count = 0
    for source in root.rglob("*"):
        if source.is_file() and not source.name.startswith("~$") and source.suffix.lower() in {".pdf", ".doc", ".docx"}:
            source_count += 1
            matched = by_hash.get(_hash_file(source))
            if matched:
                selected.add(matched)
    for item in batch_items:
        if str(item.get("file_name") or "").startswith("~$") or Path(str(item.get("file_path") or "")).name.startswith("~$"):
            continue
        if _inside_folder(item.get("file_path"), root):
            for key in ("file_id", "reuse_file_id"):
                file_id = str(item.get(key) or "")
                if file_id in valid_ids:
                    selected.add(file_id)
    shared: set[str] = set()
    for item in batch_items:
        item_ids = {str(item.get(key) or "") for key in ("file_id", "reuse_file_id")}
        affected = item_ids & selected
        if not affected:
            continue
        if not _inside_folder(item.get("file_path"), root):
            shared.update(affected)
    running, stale = _processing_groups(file_records, batch_items, selected)
    return {
        "folder": str(root),
        "source_file_count": source_count,
        "file_ids": sorted(selected),
        "shared_file_ids": sorted(shared),
        "running_file_ids": sorted(running),
        "stale_file_ids": sorted(stale),
    }


def load_folder_reset_plan(folder: str | Path) -> dict[str, Any]:
    """读取当前数据库记录；结果过多时拒绝生成不完整的重置清单。"""
    limit = 1_000_000
    files = storage.relational.query("pdf_files", limit=limit)
    items = _ignore_missing_table(lambda: storage.relational.query("pdf_batch_items", limit=limit)) or []
    if len(files) >= limit or len(items) >= limit:
        raise ValueError("数据库记录超过安全读取上限，不能保证目录重置清单完整")
    plan = build_folder_reset_plan(folder, files, items)
    documents = _ignore_missing_table(lambda: storage.relational.query("policy_documents", limit=limit)) or []
    relations = _ignore_missing_table(lambda: storage.relational.query("policy_document_relations", limit=limit)) or []
    if len(documents) >= limit or len(relations) >= limit:
        raise ValueError("制度记录超过安全读取上限，不能保证清理影响清单完整")
    policy_ids = {str(doc["policy_id"]) for doc in documents if doc.get("file_id") in plan["file_ids"]}
    affected = [relation for relation in relations if relation.get("source_policy_id") in policy_ids]
    plan["abolition_relation_count"] = len(affected)
    plan["affected_policy_ids"] = sorted({str(relation["target_policy_id"]) for relation in affected
        if relation.get("target_policy_id") and relation.get("review_status") == "approved"})
    return plan


def reset_folder_database_artifacts(
    folder: str | Path, *, confirm: bool = False, allow_shared: bool = False, release_stale: bool = False,
    purge: bool = False, force: bool = False,
) -> dict[str, Any]:
    """默认仅预览；彻底清理忽略占用状态，共享文件仍需显式允许。"""
    # 彻底清理本身即代表强制重置，不再要求调用方另外指定强制参数。
    force = force or purge
    plan = load_folder_reset_plan(folder)
    plan["purge"] = purge
    plan["force"] = force
    if not confirm:
        return plan
    if plan["shared_file_ids"] and not allow_shared:
        raise ValueError("目标含目录外批次共用的文件 ID，已停止重置：" + ", ".join(plan["shared_file_ids"])
                         + "；接受共享结果同步失效时可追加 --allow-shared-reset")
    blocked = set(plan["running_file_ids"])
    if release_stale:
        blocked -= set(plan.get("stale_file_ids", []))
    if blocked and not force:
        hint = "；核实旧进程已退出后，可追加 --release-stale-reset 释放已过期占用" if plan.get("stale_file_ids") else ""
        raise ValueError("目标含正在处理或遗留占用的文件 ID，已停止重置：" + ", ".join(sorted(blocked)) + hint)
    plan["reset_file_ids"] = []
    plan["released_stale_file_ids"] = []
    plan["released_stale_policy_run_ids"] = []
    with _reset_transaction():
        for file_id in plan["file_ids"]:
            if release_stale and not force:
                policies = _ignore_missing_table(lambda: storage.relational.query_for_update(
                    "policy_documents", '"file_id" = %s', 100000, (file_id,))) or []
                for policy in policies:
                    plan["released_stale_policy_run_ids"].extend(
                        _assert_no_active_policy_items(str(policy["policy_id"]), release_stale=True))
            if release_stale and not force and _release_stale_processing(file_id):
                plan["released_stale_file_ids"].append(file_id)
            if force:
                reset_file_database_artifacts(file_id, purge=purge, force=True)
            elif purge:
                reset_file_database_artifacts(file_id, purge=True)
            else:
                reset_file_database_artifacts(file_id)
            plan["reset_file_ids"].append(file_id)
    from policy.retrieval import invalidate_policy_clause_bm25_index
    invalidate_policy_clause_bm25_index()
    return plan


def _release_stale_processing(file_id: str) -> bool:
    """在清理事务中复核时间和心跳，仅释放确已过期的文件及其旧批任务。"""
    files = storage.relational.query_for_update("pdf_files", '"file_id" = %s', 1, (file_id,))
    condition = '("file_id" = %s OR "reuse_file_id" = %s) AND "status" IN (\'running\', \'processing\')'
    params = (file_id, file_id)
    owners = _ignore_missing_table(lambda: storage.relational.query_for_update(
        "pdf_batch_items", where=condition, limit=1_000_000, params=params)) or []
    if len(owners) >= 1_000_000:
        raise ValueError("占用记录超过安全读取上限，已停止释放")
    busy, stale = _processing_groups(files, owners, {file_id})
    if not busy:
        return False
    if file_id not in stale:
        raise ValueError(f"文件仍有近期处理记录或心跳，或无法确认占用过期，已停止重置：{file_id}")
    policies = _ignore_missing_table(lambda: storage.relational.query_for_update(
        "policy_documents", '"file_id" = %s', 100000, (file_id,))) or []
    for policy in policies:
        _assert_no_active_policy_items(str(policy["policy_id"]))
        if policy.get("structure_status") in {"running", "processing"}:
            storage.relational.update_rows("policy_documents", {"structure_status": "pending"},
                                           '"policy_id" = %s', (policy["policy_id"],))
    if owners:
        storage.relational.update_rows("pdf_batch_items", {
            "status": "pending", "last_error": "目录重置释放已过期的旧占用，后续可续跑",
            "next_retry_at": None, "finished_at": None, "heartbeat_at": datetime.now(),
        }, condition, params)
    if files and files[0].get("status") in {"running", "processing"}:
        storage.relational.update_rows("pdf_files", {
            "status": "pending", "error_message": "目录重置释放已过期的旧占用", "last_attempt_at": None,
        }, '"file_id" = %s', (file_id,))
    return True


def _is_missing_table_error(error: Exception) -> bool:
    """将尚未创建的可选产物表视为没有可清理内容。"""
    pgcode = getattr(error, "pgcode", None)
    if pgcode is not None:
        return pgcode == "42P01"
    text = str(error).lower()
    return "does not exist" in text or "undefinedtable" in text


def _ignore_missing_table(action: Callable[[], Any]) -> Any:
    """执行清理操作，忽略未创建的可选产物表。"""
    # PostgreSQL 缺表错误会使事务失效，必须先回滚保存点才能继续。
    in_transaction = _IN_RESET_TRANSACTION.get()
    if in_transaction:
        storage.relational.execute("SAVEPOINT reset_optional_table")
    try:
        result = action()
    except Exception as error:
        if in_transaction:
            storage.relational.execute("ROLLBACK TO SAVEPOINT reset_optional_table")
        if _is_missing_table_error(error):
            return None
        raise
    else:
        return result
    finally:
        if in_transaction:
            storage.relational.execute("RELEASE SAVEPOINT reset_optional_table")


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
    if records and len(records) >= 10000:
        raise ValueError("数据表目录超过安全读取上限，已停止重置")
    return list(records or [])


def reset_file_database_artifacts(file_id: str, *, purge: bool = False, force: bool = False) -> dict[str, int]:
    """事务内清理指定 PDF 的产物，保留入向废止关系并重算下游效力。"""
    normalized_file_id = str(file_id or "").strip()
    if not is_valid_file_id(normalized_file_id):
        raise ValueError("文件 ID 格式非法")
    with _reset_transaction():
        result = _reset_file_database_artifacts(normalized_file_id, purge=purge, force=force)
    from policy.retrieval import invalidate_policy_clause_bm25_index
    invalidate_policy_clause_bm25_index()
    return result


def _reset_file_database_artifacts(file_id: str, *, purge: bool = False, force: bool = False) -> dict[str, int]:
    """清理指定 PDF 的自动数据库产物，并将其重新置为待处理。"""
    normalized_file_id = str(file_id or "").strip()
    if not is_valid_file_id(normalized_file_id):
        raise ValueError("文件 ID 格式非法")

    files = storage.relational.query_for_update(
        "pdf_files",
        where='"file_id" = %s',
        limit=1,
        params=(normalized_file_id,),
    )
    if not files:
        raise ValueError(f"文件不存在: {normalized_file_id}")
    if not force and str(files[0].get("status") or "").lower() in {"running", "processing"}:
        raise ValueError(f"文件正在处理，已停止重置：{normalized_file_id}")
    owners = _ignore_missing_table(lambda: storage.relational.query_for_update(
        "pdf_batch_items", where='("file_id" = %s OR "reuse_file_id" = %s) '
        'AND "status" IN (\'running\', \'processing\')', limit=1_000_000,
        params=(normalized_file_id, normalized_file_id))) or []
    if len(owners) >= 1_000_000:
        raise ValueError("文件批任务超过安全读取上限，已停止重置")
    if owners and not force:
        raise ValueError(f"文件存在正在处理的批任务，已停止重置：{normalized_file_id}")
    if owners and force:
        # 强制清理不依据时间判定占用，将关联旧批任务释放为待处理。
        storage.relational.update_rows("pdf_batch_items", {"status": "pending"},
            '("file_id" = %s OR "reuse_file_id" = %s) AND "status" IN (\'running\', \'processing\')',
            (normalized_file_id, normalized_file_id))
    policy_records = _ignore_missing_table(lambda: storage.relational.query_for_update(
        "policy_documents", where='"file_id" = %s', limit=100000, params=(normalized_file_id,))) or []
    if len(policy_records) >= 100000:
        raise ValueError("同一文件的制度数量超过安全读取上限，已停止重置")
    for policy in policy_records:
        if not force and str(policy.get("structure_status") or "").lower() in {"running", "processing"}:
            raise ValueError(f"制度正在处理，已停止重置：{normalized_file_id}")
        _assert_no_active_policy_items(str(policy.get("policy_id") or ""), force=force)

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
            and location.startswith(f"pdf_{normalized_file_id}_tbl_")
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
    for policy_record in policy_records:
        policy_id = str(policy_record.get("policy_id") or "")
        if policy_id:
            run_snapshots = _snapshot_policy_runs(policy_id) if purge else []
            _ignore_missing_table(lambda: storage.vector.delete_vectors_by_metadata(
                "policy_clause_search", "policy_id", policy_id))
            _ignore_missing_table(
                lambda: storage.vector.delete_vectors_by_metadata(
                    "policy_clause_vectors", "policy_id", policy_id
                )
            )
            _ignore_missing_table(
                lambda: storage.vector.delete_vectors_by_metadata(
                    "policy_assertion_search_v2", "policy_id", policy_id
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
            # 所有直接引用条款的自动产物必须先清理，否则 PostgreSQL 外键会阻止删条款。
            _ignore_missing_table(
                lambda: storage.relational.execute(
                    'DELETE FROM "policy_matter_members_v2" WHERE "matter_id" IN '
                    '(SELECT "matter_id" FROM "policy_matters_v2" WHERE "policy_id" = %s) '
                    'OR "clause_id" IN (SELECT "clause_id" FROM "policy_clauses" WHERE "policy_id" = %s)',
                    (policy_id, policy_id),
                )
            )
            _ignore_missing_table(
                lambda: storage.relational.execute(
                    'DELETE FROM "policy_matters_v2" WHERE "policy_id" = %s',
                    (policy_id,),
                )
            )
            clause_dependents = (
                "policy_process_items",
                "policy_process_labels",
                "policy_extraction_items",
                "policy_entities",
                "policy_relations",
                "policy_manual_annotations",
                "policy_assertion_reviews_v2",
                "policy_assertions_v2",
                "policy_knowledge_items_v2",
                "policy_clause_classification_items",
                "policy_clause_classification_results",
            )
            for table_name in clause_dependents:
                _ignore_missing_table(
                    lambda table_name=table_name: storage.relational.execute(
                        f'DELETE FROM "{table_name}" WHERE "clause_id" IN '
                        '(SELECT "clause_id" FROM "policy_clauses" WHERE "policy_id" = %s)',
                        (policy_id,),
                    )
                )
            _clear_policy_relations(policy_id, include_target=purge)
            if purge:
                _ignore_missing_table(lambda: storage.relational.execute(
                    'DELETE FROM "policy_family_candidates" WHERE "source_policy_id" = %s '
                    'OR "target_policy_id" = %s', (policy_id, policy_id)))
            _ignore_missing_table(lambda: storage.relational.execute(
                'DELETE FROM "policy_clause_index_items" WHERE "policy_id" = %s', (policy_id,)))
            _ignore_missing_table(lambda: storage.relational.execute(
                'DELETE FROM "policy_reviews" WHERE "policy_id" = %s OR "clause_id" IN '
                '(SELECT "clause_id" FROM "policy_clauses" WHERE "policy_id" = %s)', (policy_id, policy_id)))
            _ignore_missing_table(
                lambda: storage.relational.execute(
                    'DELETE FROM "policy_clauses" WHERE "policy_id" = %s',
                    (policy_id,),
                )
            )
            summary["policy_clauses"] += len(clauses)
            metadata = {"structure_status": "pending", "parse_quality": 0.0}
            if purge:
                # 保留稳定标识和源文件定位，清除旧版本推断出的制度元数据。
                cleared = {"title": "", "notice_title": "", "doc_number": "", "doc_number_raw": "", "issuing_department": "", "version": "",
                           "structure_version": "", "issue_date": None, "effective_date": None,
                           "expiry_date": None, "validity_status": "unknown", "family_id": None,
                           "current_extraction_run_id": None}
                metadata.update({key: value for key, value in cleared.items() if key in policy_record})
                _delete_empty_policy_runs(run_snapshots)
            _ignore_missing_table(
                lambda: storage.relational.update_rows(
                    "policy_documents",
                    metadata,
                    '"policy_id" = %s',
                    (policy_id,),
                )
            )

    file_values = {"status": "pending", "error_message": "", "last_attempt_at": None}
    if purge:
        file_values.update({key: "" for key in ("parser_version", "embedding_model", "llm_model") if key in files[0]})
    storage.relational.update_rows(
        "pdf_files",
        file_values,
        '"file_id" = %s',
        (normalized_file_id,),
    )
    return summary


def _policy_task_condition(table: str) -> str:
    return ('"policy_id" = %s' if table == "policy_clause_index_items" else
            '"clause_id" IN (SELECT "clause_id" FROM "policy_clauses" WHERE "policy_id" = %s)')


def _snapshot_policy_runs(policy_id: str) -> list[tuple[str, str, str]]:
    snapshots = []
    for table, run_table in _POLICY_TASK_TABLES:
        rows = _ignore_missing_table(lambda: storage.relational.query_for_update(
            table, _policy_task_condition(table), 1_000_000, (policy_id,))) or []
        if len(rows) >= 1_000_000:
            raise ValueError("制度条款任务超过安全读取上限，已停止重置")
        snapshots.extend((table, run_table, run_id) for run_id in {str(r["run_id"]) for r in rows if r.get("run_id")})
    return snapshots


def _delete_empty_policy_runs(snapshots: list[tuple[str, str, str]]) -> None:
    """仅删除本次涉及且已无任务的运行，保留跨制度共享运行。"""
    for table, run_table, run_id in snapshots:
        remaining = _ignore_missing_table(lambda: storage.relational.query_for_update(
            table, '"run_id" = %s', 1, (run_id,))) or []
        if not remaining:
            _ignore_missing_table(lambda: storage.relational.execute(
                f'DELETE FROM "{run_table}" WHERE "run_id" = %s', (run_id,)))


def _assert_no_active_policy_items(policy_id: str, *, release_stale: bool = False, force: bool = False) -> list[str]:
    """即使 PDF 已完成，也不得删除仍被条款处理任务使用的数据。"""
    released = []
    for table, run_table in _POLICY_TASK_TABLES:
        condition = _policy_task_condition(table)
        # 同时锁住待处理记录，避免检查结束后任务从 pending 切换到 running。
        rows = _ignore_missing_table(lambda table=table, condition=condition: storage.relational.query_for_update(
            table, where=condition, limit=1_000_000, params=(policy_id,))) or []
        if len(rows) >= 1_000_000:
            raise ValueError("制度条款任务超过安全读取上限，已停止重置")
        active_items = [row for row in rows if str(row.get("status") or "").lower() in {"running", "processing"}]
        if active_items and not release_stale and not force:
            raise ValueError(f"制度存在正在处理的条款任务，已停止重置：{policy_id}（{table}）")
        runs = []
        if rows:
            runs = _ignore_missing_table(lambda run_table=run_table, table=table, condition=condition:
                storage.relational.query_for_update(run_table,
                    where=f'"run_id" IN (SELECT "run_id" FROM "{table}" WHERE {condition}) '
                    'AND "status" IN (\'running\', \'processing\')', limit=1_000_000, params=(policy_id,))) or []
            if runs and not release_stale and not force:
                raise ValueError(f"制度存在正在处理的条款运行，已停止重置：{policy_id}（{run_table}）")
        if not (release_stale or force) or not (active_items or runs):
            continue
        for item in active_items:
            if not force and not _expired_processing_time(item.get("heartbeat_at") or item.get("started_at")):
                raise ValueError(f"条款任务仍正在处理或无法核实占用过期，已停止重置：{policy_id}（{table}）")
        # 必须检查整个共享运行，不能因本制度条款较旧而误停其他制度的新任务。
        for run_id in sorted({str(r["run_id"]) for r in [*active_items, *runs] if r.get("run_id")}):
            all_items = _ignore_missing_table(lambda: storage.relational.query_for_update(
                table, '"run_id" = %s', 1_000_000, (run_id,))) or []
            if len(all_items) >= 1_000_000:
                raise ValueError("共享条款任务超过安全读取上限，已停止重置")
            run = next((r for r in runs if str(r.get("run_id")) == run_id), {})
            timestamps = [r.get(key) for r in [run, *all_items] for key in
                          ("heartbeat_at", "started_at", "finished_at", "updated_at") if r.get(key)]
            unknown_active = any(str(r.get("status")) in {"running", "processing"} and
                                 not (r.get("heartbeat_at") or r.get("started_at")) for r in all_items)
            if not force and (unknown_active or not timestamps or not all(_expired_processing_time(t) for t in timestamps)):
                raise ValueError(f"共享条款运行有近期活动或无法核实过期，已停止重置：{run_id}")
            storage.relational.update_rows(table, {"status": "failed"},
                '"run_id" = %s AND "status" IN (\'running\', \'processing\')', (run_id,))
            if run:
                storage.relational.update_rows(run_table, {"status": "failed", "finished_at": datetime.now()},
                    '"run_id" = %s', (run_id,))
            released.append(run_id)
    return sorted(set(released))


def _clear_policy_relations(policy_id: str, *, include_target: bool = False) -> None:
    """清理失去原文依据的关系，谨慎修复其目标制度的效力状态。"""
    where = ('"source_policy_id" = %s OR "evidence_clause_id" IN '
             '(SELECT "clause_id" FROM "policy_clauses" WHERE "policy_id" = %s)')
    params = (policy_id, policy_id)
    if include_target:
        where = f'({where}) OR "target_policy_id" = %s'
        params = (*params, policy_id)
    relations = _ignore_missing_table(lambda: storage.relational.query_for_update(
        "policy_document_relations", where=where, limit=1_000_000, params=params)) or []
    if len(relations) >= 1_000_000:
        raise ValueError("废止关系超过安全读取上限，已停止重置")
    targets = sorted({str(row["target_policy_id"]) for row in relations
                      if row.get("target_policy_id") and row.get("review_status") == "approved"})
    documents = {}
    for target in targets:
        rows = storage.relational.query_for_update("policy_documents", '"policy_id" = %s', 1, (target,))
        if rows:
            documents[target] = rows[0]
    # 归族候选直接引用废止关系，必须先删除；已确认的制度族主记录保留。
    _ignore_missing_table(lambda: storage.relational.execute(
        'DELETE FROM "policy_family_candidates" WHERE "relation_id" IN '
        f'(SELECT "relation_id" FROM "policy_document_relations" WHERE {where})', params))
    _ignore_missing_table(lambda: storage.relational.execute(
        'DELETE FROM "policy_reviews" WHERE "relation_id" IN '
        f'(SELECT "relation_id" FROM "policy_document_relations" WHERE {where})', params))
    _ignore_missing_table(lambda: storage.relational.execute(
        f'DELETE FROM "policy_document_relations" WHERE {where}', params))
    for target, document in documents.items():
        remaining = storage.relational.query("policy_document_relations",
            '"target_policy_id" = %s AND "review_status" = %s', 1_000_000, (target, "approved"))
        if len(remaining) >= 1_000_000:
            raise ValueError("目标制度的废止关系超过安全读取上限，已停止重置")
        dates = sorted({str(row["effective_date"]) for row in remaining if row.get("effective_date")})
        if len(dates) > 1:
            raise ValueError(f"目标制度剩余已批准废止关系存在日期冲突，已停止重置：{target}")
        deleted_dates = {str(row["effective_date"]) for row in relations
                         if row.get("target_policy_id") == target and row.get("review_status") == "approved"
                         and row.get("effective_date")}
        values: dict[str, Any] = {"validity_status": "invalid" if remaining else "current"}
        if dates:
            values["expiry_date"] = dates[0]
        elif str(document.get("expiry_date")) in deleted_dates:
            # 只清除来自被删除关系的日期，不清除不相符的人工失效日期。
            values["expiry_date"] = None
        if not remaining:
            expiry = values.get('expiry_date', document.get('expiry_date'))
            conflicts = _ignore_missing_table(lambda: storage.relational.query('policy_reviews',
                '"policy_id" = %s AND "status" = %s AND "issue_type" IN '
                "('effective_date_conflict', 'validity_conflict', 'version_conflict')", 1, (target, 'pending')))
            values['validity_status'] = 'invalid' if expiry else ('unknown' if conflicts else 'current')
        storage.relational.update_rows("policy_documents", values, '"policy_id" = %s', (target,))
