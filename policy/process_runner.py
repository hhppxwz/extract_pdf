"""制度流程条款分流运行器。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from batch_processor import current_processing_versions
from policy.domain_catalog import ProcessDomainCatalog, load_process_domain_catalog
from policy.process import build_process_label
from policy.storage import (
    create_policy_process_run,
    get_policy_clause,
    get_policy_process_items,
    get_policy_process_run,
    refresh_policy_process_run,
    update_policy_process_item,
    update_policy_process_run,
    upsert_policy_process_label,
)


def _should_process(item: dict[str, Any], resume: bool) -> bool:
    """只处理待运行任务；恢复时允许重试失败任务。"""
    status = str(item.get("status") or "pending")
    return status == "pending" or (resume and status == "failed")


def run_policy_process_classification(run_id: str, resume: bool = False) -> dict[str, Any]:
    """运行或恢复流程分流，并为每条条款保存独立判定记录。"""
    run = get_policy_process_run(run_id)
    if not run:
        raise ValueError(f"流程判定运行不存在: {run_id}")
    stored_domains = run.get("selected_domains")
    if stored_domains is not None and not isinstance(stored_domains, list):
        raise ValueError("流程判定运行的 selected_domains 必须是数组。")
    update_policy_process_run(run_id, {
        "status": "running",
        "started_at": run.get("started_at") or datetime.now(),
        "finished_at": None,
    })
    for item in get_policy_process_items(run_id):
        if not _should_process(item, resume):
            continue
        item_id = str(item["item_id"])
        update_policy_process_item(item_id, {
            "status": "running",
            "started_at": datetime.now(),
            "last_error": "",
        })
        try:
            clause = get_policy_clause(str(item["clause_id"]))
            if not clause:
                raise ValueError(f"条款不存在: {item['clause_id']}")
            label = build_process_label(clause, selected_domains=stored_domains)
            label["run_id"] = run_id
            upsert_policy_process_label(label)
            update_policy_process_item(item_id, {
                "status": "succeeded",
                "finished_at": datetime.now(),
            })
        except Exception as exc:
            update_policy_process_item(item_id, {
                "status": "failed",
                "last_error": str(exc)[:2000],
                "finished_at": datetime.now(),
            })
    return refresh_policy_process_run(run_id)


def create_and_run_policy_process_classification(
    batch_id: str,
    selected_domains: Sequence[str] | None = None,
    catalog: ProcessDomainCatalog | None = None,
) -> dict[str, Any]:
    """按当前版本快照创建并执行一个新的流程分流运行。"""
    active_catalog = catalog or load_process_domain_catalog()
    selected = active_catalog.select_domains(selected_domains)
    run_id = create_policy_process_run(
        batch_id,
        current_processing_versions(),
        selected_domains=[domain.code for domain in selected],
        domain_catalog_version=active_catalog.version,
    )
    return run_policy_process_classification(run_id)


def get_policy_process_status(run_id: str) -> dict[str, Any]:
    """查看流程判定运行及其条款任务状态。"""
    run = get_policy_process_run(run_id)
    if not run:
        raise ValueError(f"流程判定运行不存在: {run_id}")
    return {"run": run, "items": get_policy_process_items(run_id)}
