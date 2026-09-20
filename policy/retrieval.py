"""跨制度条款级检索的纯规则、索引数据和结果组装。"""
from __future__ import annotations

import re
from typing import Any


_SEARCHABLE_LEVELS = frozenset({"article", "paragraph", "item"})
_QUERY_PUNCTUATION = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
POLICY_CLAUSE_INDEX_NAME = "policy_clause_search"


class PolicyClauseIndexNotReadyError(RuntimeError):
    """尚无可检索条款索引时抛出，供 API 返回可操作提示。"""


class PolicyClauseRetrievalServiceError(RuntimeError):
    """真实嵌入或存储服务不可用时抛出。"""


def is_searchable_policy_clause(clause: dict[str, Any]) -> bool:
    """仅让能够被引用的实质条款进入跨制度检索索引。"""
    return (
        str(clause.get("level") or "") in _SEARCHABLE_LEVELS
        and bool(str(clause.get("raw_text") or "").strip())
    )


def build_clause_index_text(clause: dict[str, Any]) -> str:
    """组合章节、条款号和原文，给向量模型保留制度上下文。"""
    chapter_path = clause.get("chapter_path") or []
    if not isinstance(chapter_path, list):
        chapter_path = []
    parts = [str(item).strip() for item in chapter_path if str(item).strip()]
    parts.extend(
        str(clause.get(field) or "").strip()
        for field in ("article_no", "paragraph_no", "item_no")
        if str(clause.get(field) or "").strip()
    )
    raw_text = str(clause.get("raw_text") or "").strip()
    if raw_text:
        parts.append(raw_text)
    return " ".join(parts)


def build_clause_metadata(
    clause: dict[str, Any],
    document: dict[str, Any],
) -> dict[str, Any]:
    """把条款的可回查字段完整保存到向量 metadata。"""
    chapter_path = clause.get("chapter_path") or []
    return {
        "clause_id": str(clause.get("clause_id") or ""),
        "policy_id": str(clause.get("policy_id") or document.get("policy_id") or ""),
        "file_id": str(document.get("file_id") or ""),
        "file_name": str(document.get("file_name") or ""),
        "title": str(document.get("title") or document.get("file_name") or ""),
        "level": str(clause.get("level") or ""),
        "article_no": str(clause.get("article_no") or ""),
        "paragraph_no": str(clause.get("paragraph_no") or ""),
        "item_no": str(clause.get("item_no") or ""),
        "chapter_path": list(chapter_path) if isinstance(chapter_path, list) else [],
        "page_start": int(clause.get("page_start") or 0),
        "page_end": int(clause.get("page_end") or clause.get("page_start") or 0),
        "raw_text": str(clause.get("raw_text") or ""),
        "search_text": build_clause_index_text(clause),
        "structure_version": str(clause.get("structure_version") or ""),
    }


def _normalize_query(text: str) -> str:
    return _QUERY_PUNCTUATION.sub("", str(text or "")).lower()


def _query_terms(query: str) -> set[str]:
    """生成长度 2 至 4 的连续查询片段，兼容中文无空格问题。"""
    normalized = _normalize_query(query)
    terms: set[str] = set()
    for length in range(2, min(4, len(normalized)) + 1):
        terms.update(
            normalized[start:start + length]
            for start in range(0, len(normalized) - length + 1)
        )
    return terms


def _keyword_score(query: str, search_text: str) -> float:
    """为原文连续术语命中提供小幅、可解释的排序加分。"""
    terms = _query_terms(query)
    normalized_text = _normalize_query(search_text)
    if not terms or not normalized_text:
        return 0.0
    matched_weight = sum(len(term) for term in terms if term in normalized_text)
    total_weight = sum(len(term) for term in terms)
    return round(min(0.15, 0.15 * matched_weight / max(total_weight, 1)), 6)


def rerank_clause_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """以真实向量相似度为主、原文术语命中为辅进行去重排序。"""
    if not 1 <= top_k <= 100:
        raise ValueError("top_k 必须在 1 到 100 之间")

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        metadata = dict(candidate.get("metadata") or {})
        similarity = float(candidate.get("similarity") or 0.0)
        keyword_score = _keyword_score(query, str(metadata.get("search_text") or ""))
        ranked.append({
            **candidate,
            "metadata": metadata,
            "similarity": round(similarity, 6),
            "keyword_score": keyword_score,
            "score": round(similarity + keyword_score, 6),
        })

    ranked.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item["similarity"]),
            str(item["metadata"].get("clause_id") or ""),
        )
    )
    unique: list[dict[str, Any]] = []
    seen_clause_ids: set[str] = set()
    for candidate in ranked:
        clause_id = str(candidate["metadata"].get("clause_id") or "")
        dedupe_key = clause_id or str(candidate.get("id") or "")
        if dedupe_key in seen_clause_ids:
            continue
        seen_clause_ids.add(dedupe_key)
        unique.append(candidate)
        if len(unique) == top_k:
            break
    return unique


def _clause_no(metadata: dict[str, Any]) -> str:
    """拼接条、款、项，保证嵌套条款也能被完整回查。"""
    return " ".join(
        str(metadata.get(field) or "").strip()
        for field in ("article_no", "paragraph_no", "item_no")
        if str(metadata.get(field) or "").strip()
    )


def build_policy_search_response(
    query: str,
    candidates: list[dict[str, Any]],
    as_of: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """把候选条款转成 API 的可引用结果，不生成或改写任何原文。"""
    results: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates, start=1):
        metadata = dict(candidate.get("metadata") or {})
        document = dict(candidate.get("policy_document") or {})
        results.append({
            "rank": rank,
            "score": float(candidate.get("score", candidate.get("similarity", 0.0)) or 0.0),
            "semantic_score": float(candidate.get("similarity") or 0.0),
            "keyword_score": float(candidate.get("keyword_score") or 0.0),
            "policy": {
                "policy_id": str(metadata.get("policy_id") or ""),
                "file_id": str(metadata.get("file_id") or ""),
                "title": str(metadata.get("title") or ""),
                "file_name": str(metadata.get("file_name") or ""),
                "family_id": str(document.get("family_id") or ""),
                "effective_date": str(document.get("effective_date") or ""),
                "expiry_date": str(document.get("expiry_date") or ""),
                "validity_status": str(document.get("validity_status") or ""),
                "temporal_status": str(candidate.get("temporal_status") or "unfiltered"),
            },
            "clause": {
                "clause_id": str(metadata.get("clause_id") or ""),
                "clause_no": _clause_no(metadata),
                "chapter_path": list(metadata.get("chapter_path") or []),
                "page_start": int(metadata.get("page_start") or 0),
                "page_end": int(metadata.get("page_end") or metadata.get("page_start") or 0),
                "raw_text": str(metadata.get("raw_text") or ""),
            },
        })
    return {
        "query": str(query), "as_of": as_of,
        "temporal_filter_applied": as_of is not None,
        "warnings": list(warnings or []),
        "result_count": len(results), "results": results,
    }


def _encode_policy_clause_texts(texts: list[str]) -> list[list[float]]:
    """使用项目配置的真实嵌入模型生成条款向量。"""
    if not texts:
        return []
    from text_pipeline import _get_embedding_model

    model = _get_embedding_model()
    encoded = model.encode(texts, normalize_embeddings=True)
    return [list(map(float, vector.tolist())) for vector in encoded]


def sync_policy_clause_index(policy_id: str) -> int:
    """用当前活动条款替换一份制度在全局索引中的旧向量。"""
    if not str(policy_id or "").strip():
        raise ValueError("policy_id 不能为空")

    from policy.storage import get_policy_clauses, get_policy_document
    from storage_adapter import storage

    document = get_policy_document(policy_id=policy_id)
    if not document:
        raise ValueError(f"制度不存在: {policy_id}")
    clauses = [
        clause for clause in get_policy_clauses(policy_id)
        if is_searchable_policy_clause(clause)
    ]
    texts = [build_clause_index_text(clause) for clause in clauses]
    metadata = [build_clause_metadata(clause, document) for clause in clauses]

    # 先完成真实嵌入，避免模型故障时提前删除可用的旧索引。
    vectors = _encode_policy_clause_texts(texts)
    if vectors:
        storage.vector.create_index(POLICY_CLAUSE_INDEX_NAME, len(vectors[0]))
    else:
        # 没有可检索条款时，删除该制度原有向量，避免陈旧条款继续命中。
        try:
            storage.vector.delete_vectors_by_metadata(
                POLICY_CLAUSE_INDEX_NAME, "policy_id", str(policy_id)
            )
        except Exception as exc:
            if not _is_missing_index_error(exc):
                raise
        return 0

    storage.vector.delete_vectors_by_metadata(
        POLICY_CLAUSE_INDEX_NAME, "policy_id", str(policy_id)
    )
    return storage.vector.insert_vectors(POLICY_CLAUSE_INDEX_NAME, vectors, metadata)


def _is_missing_index_error(exc: Exception) -> bool:
    """仅把 PostgreSQL 的缺表错误转换为“索引尚未建立”。"""
    try:
        from psycopg2.errors import UndefinedTable

        if isinstance(exc, UndefinedTable):
            return True
    except ImportError:
        pass
    return "relation" in str(exc).lower() and "does not exist" in str(exc).lower()


def _should_process_index_item(item: dict[str, Any], resume: bool) -> bool:
    """首次执行处理待处理项，恢复执行额外重试失败项。"""
    status = str(item.get("status") or "pending")
    return status == "pending" or (resume and status == "failed")


def run_policy_clause_index(run_id: str, resume: bool = False) -> dict[str, Any]:
    """运行或恢复一次批量条款索引，逐制度记录真实写入结果。"""
    from datetime import datetime
    from policy.storage import (
        get_policy_clause_index_items,
        get_policy_clause_index_run,
        refresh_policy_clause_index_run,
        update_policy_clause_index_item,
        update_policy_clause_index_run,
    )

    run = get_policy_clause_index_run(run_id)
    if not run:
        raise ValueError(f"条款索引运行不存在: {run_id}")
    update_policy_clause_index_run(run_id, {
        "status": "running",
        "started_at": run.get("started_at") or datetime.now(),
        "finished_at": None,
    })
    for item in get_policy_clause_index_items(run_id):
        if not _should_process_index_item(item, resume):
            continue
        item_id = str(item["item_id"])
        update_policy_clause_index_item(item_id, {
            "status": "running",
            "last_error": "",
            "started_at": datetime.now(),
            "finished_at": None,
        })
        try:
            vector_count = sync_policy_clause_index(str(item["policy_id"]))
            update_policy_clause_index_item(item_id, {
                "status": "succeeded" if vector_count else "skipped",
                "vector_count": vector_count,
                "finished_at": datetime.now(),
            })
        except Exception as exc:
            update_policy_clause_index_item(item_id, {
                "status": "failed",
                "last_error": str(exc)[:2000],
                "finished_at": datetime.now(),
            })
    return refresh_policy_clause_index_run(run_id)


def create_and_run_policy_clause_index(batch_id: str) -> dict[str, Any]:
    """使用当前版本快照创建并执行一个批次的条款索引。"""
    from batch_processor import current_processing_versions
    from policy.storage import create_policy_clause_index_run

    run_id = create_policy_clause_index_run(batch_id, current_processing_versions())
    return run_policy_clause_index(run_id)


def get_policy_clause_index_status(run_id: str) -> dict[str, Any]:
    """查看索引运行及逐制度处理状态，不执行重建。"""
    from policy.storage import (
        get_policy_clause_index_items,
        get_policy_clause_index_run,
    )

    run = get_policy_clause_index_run(run_id)
    if not run:
        raise ValueError(f"条款索引运行不存在: {run_id}")
    return {"run": run, "items": get_policy_clause_index_items(run_id)}


def search_indexed_policy_clauses(
    query: str, top_k: int = 10, as_of: str | None = None
) -> list[dict[str, Any]]:
    """在全局真实条款索引中召回并重排可引用的制度条款。"""
    query = str(query or "").strip()
    if not query:
        raise ValueError("检索问题不能为空")
    if not 1 <= top_k <= 20:
        raise ValueError("top_k 必须在 1 到 20 之间")

    from storage_adapter import storage

    try:
        if storage.vector.count_vectors(POLICY_CLAUSE_INDEX_NAME) < 1:
            raise PolicyClauseIndexNotReadyError("条款索引为空，请先重建制度条款索引")
    except PolicyClauseIndexNotReadyError:
        raise
    except Exception as exc:
        if _is_missing_index_error(exc):
            raise PolicyClauseIndexNotReadyError(
                "条款索引尚未建立，请先重建制度条款索引"
            ) from exc
        raise PolicyClauseRetrievalServiceError(
            f"无法读取条款索引: {exc}"
        ) from exc

    try:
        vectors = _encode_policy_clause_texts([query])
        candidates = storage.vector.search(
            POLICY_CLAUSE_INDEX_NAME,
            vectors[0],
            top_k=min(100, max(50, top_k * 5)),
        )
    except Exception as exc:
        raise PolicyClauseRetrievalServiceError(
            f"条款检索服务暂不可用: {exc}"
        ) from exc
    ranked = rerank_clause_candidates(query, candidates, 100 if as_of else top_k)
    if as_of is None:
        return ranked
    from policy.storage import get_policy_documents_by_ids
    from policy.temporal import rank_temporal_candidates

    policy_ids = [str((item.get("metadata") or {}).get("policy_id") or "") for item in ranked]
    temporal, warnings = rank_temporal_candidates(
        ranked, get_policy_documents_by_ids(policy_ids), as_of, top_k
    )
    for item in temporal:
        item["temporal_warnings"] = warnings
    return temporal
