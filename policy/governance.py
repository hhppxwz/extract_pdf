"""制度文档级时间元数据回填与制度族候选编排。"""
from __future__ import annotations

import uuid
from itertools import combinations
from typing import Any

from models import ReviewItem
from policy.storage import (
    get_policy_clauses, get_policy_documents_for_batch, insert_review_item,
    list_policy_document_relations, list_policy_family_candidates,
    upsert_policy_family_candidate, storage, TABLE_POLICY_REVIEWS,
)
from policy.temporal import extract_effective_date, normalize_family_title


def apply_effective_date_governance(
    policy_id: str, texts: list[str], issue_date: str | None,
    existing_effective_date: str | None = None,
) -> dict[str, str | None]:
    """识别并保存单份制度的生效日期，已有日期视为人工或既有结果而保留。"""
    if existing_effective_date:
        return {"effective_date": str(existing_effective_date), "status": "preserved"}
    result = extract_effective_date(texts, issue_date)
    if result["status"] == "identified":
        storage.relational.update_rows(
            "policy_documents", {"effective_date": result["effective_date"]},
            '"policy_id" = %s', (policy_id,),
        )
    elif result["status"] in {"conflict", "needs_review"}:
        insert_review_item(ReviewItem(
            review_id=f"review_{uuid.uuid4().hex}", policy_id=policy_id,
            issue_type=f"effective_date_{result['status']}",
            description="制度生效日期需要人工确认",
        ))
    return result


def backfill_policy_governance(batch_id: str) -> dict[str, int]:
    """不重解析 PDF，回填生效日期并生成待审核归族候选。"""
    documents = get_policy_documents_for_batch(batch_id)
    if not documents:
        raise ValueError(f"批次没有已结构化的制度文档: {batch_id}")
    summary = {"documents": len(documents), "dated": 0, "date_unknown": 0, "date_conflicts": 0,
               "family_assigned": 0, "family_pending": 0, "family_overlaps": 0}
    by_title: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        policy_id = str(document["policy_id"])
        if document.get("effective_date"):
            summary["dated"] += 1
        else:
            clauses = get_policy_clauses(policy_id)
            result = apply_effective_date_governance(
                policy_id,
                [str(document.get("title") or "")] + [
                    str(item.get("raw_text") or "") for item in clauses
                ],
                str(document.get("issue_date") or "") or None,
            )
            if result["status"] == "identified":
                document["effective_date"] = result["effective_date"]
                summary["dated"] += 1
            else:
                summary["date_unknown"] += 1
                summary["date_conflicts"] += result["status"] == "conflict"
        title = normalize_family_title(str(document.get("title") or ""))
        if title:
            by_title.setdefault(title, []).append(document)
        summary["family_assigned"] += bool(document.get("family_id"))
    for title, matches in by_title.items():
        for source, target in combinations(matches, 2):
            upsert_policy_family_candidate(
                str(source["policy_id"]), str(target["policy_id"]), "normalized_title", title
            )
    for relation in list_policy_document_relations(batch_id=batch_id):
        if relation.get("review_status") == "approved" and relation.get("target_policy_id"):
            upsert_policy_family_candidate(
                str(relation["source_policy_id"]), str(relation["target_policy_id"]),
                "approved_abolition",
                normalize_family_title(str(relation.get("target_title") or "")),
                relation_id=str(relation["relation_id"]),
            )
    summary["family_pending"] = sum(
        item.get("review_status") == "pending" for item in list_policy_family_candidates(batch_id)
    )
    by_family: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        if document.get("family_id"):
            by_family.setdefault(str(document["family_id"]), []).append(document)
    for versions in by_family.values():
        for left, right in combinations(versions, 2):
            left_start, right_start = left.get("effective_date"), right.get("effective_date")
            if not left_start or not right_start:
                continue
            left_end, right_end = left.get("expiry_date"), right.get("expiry_date")
            if (not left_end or str(right_start) < str(left_end)) and (not right_end or str(left_start) < str(right_end)):
                summary["family_overlaps"] += 1
    return summary


def get_policy_governance_status(batch_id: str) -> dict[str, int]:
    """只读汇总制度时间元数据和归族状态。"""
    documents = get_policy_documents_for_batch(batch_id)
    candidates = list_policy_family_candidates(batch_id)
    policy_ids = {str(item["policy_id"]) for item in documents}
    reviews = storage.relational.query(TABLE_POLICY_REVIEWS, "", 100000, ()) if policy_ids else []
    by_family: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        if document.get("family_id"):
            by_family.setdefault(str(document["family_id"]), []).append(document)
    overlaps = 0
    for versions in by_family.values():
        for left, right in combinations(versions, 2):
            left_start, right_start = left.get("effective_date"), right.get("effective_date")
            if not left_start or not right_start:
                continue
            left_end, right_end = left.get("expiry_date"), right.get("expiry_date")
            if (not left_end or str(right_start) < str(left_end)) and (not right_end or str(left_start) < str(right_end)):
                overlaps += 1
    return {
        "documents": len(documents),
        "dated": sum(bool(item.get("effective_date")) for item in documents),
        "date_unknown": sum(not bool(item.get("effective_date")) for item in documents),
        "date_conflicts": sum(
            item.get("policy_id") in policy_ids
            and item.get("issue_type") == "effective_date_conflict"
            and item.get("status", "pending") == "pending"
            for item in reviews
        ),
        "family_assigned": sum(bool(item.get("family_id")) for item in documents),
        "family_pending": sum(item.get("review_status") == "pending" for item in candidates),
        "family_overlaps": overlaps,
    }


def run_interactive_family_review(batch_id: str, reviewer: str, limit: int = 20) -> dict[str, int]:
    """在终端审核制度族候选。"""
    from policy.storage import get_policy_documents_by_ids, review_policy_family_candidate

    if not reviewer.strip():
        raise ValueError("审核人不能为空")
    pending = [item for item in list_policy_family_candidates(batch_id) if item.get("review_status") == "pending"][:limit]
    summary = {"approved": 0, "rejected": 0, "skipped": 0}
    for candidate in pending:
        documents = get_policy_documents_by_ids([
            str(candidate["source_policy_id"]), str(candidate["target_policy_id"])
        ])
        print("\n" + "=" * 72)
        print(f"来源制度: {documents.get(str(candidate['source_policy_id']), {}).get('title', '')}")
        print(f"目标制度: {documents.get(str(candidate['target_policy_id']), {}).get('title', '')}")
        print(f"候选原因: {candidate.get('reason', '')}")
        while True:
            action = input("选择 [a]确认归族 [r]拒绝 [f]指定制度族 [n]新建制度族 [s]跳过 [q]结束: ").strip().lower()
            if action == "q":
                return summary
            if action == "s":
                summary["skipped"] += 1
                break
            if action not in {"a", "r", "f", "n"}:
                print("请输入 a、r、f、n、s 或 q。")
                continue
            family_id = None
            canonical_title = str(candidate.get("normalized_title") or "")
            if action == "f":
                family_id = input("制度族 ID: ").strip()
                if not family_id:
                    print("制度族 ID 不能为空。")
                    continue
            if action == "n":
                canonical_title = input(f"制度族规范标题 [{canonical_title}]: ").strip() or canonical_title
            decision = "rejected" if action == "r" else "approved"
            review_policy_family_candidate(
                str(candidate["candidate_id"]), decision, reviewer,
                family_id=family_id, canonical_title=canonical_title,
            )
            summary[decision] += 1
            break
    return summary
