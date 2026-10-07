"""跨制度条款级检索的纯规则、索引数据和结果组装。"""
from __future__ import annotations

import re
import threading
from typing import Any

import jieba
from rank_bm25 import BM25Okapi


_bm25_documents: list[dict[str, Any]] = []
_bm25_index: BM25Okapi | None = None
_bm25_index_ready = False
_bm25_lock = threading.RLock()
_BM25_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def _tokenize_bm25(text: str) -> list[str]:
    """使用稳定的中文分词结果构建 BM25 词元，过滤标点和空白。"""
    return [
        token.lower()
        for token in jieba.lcut(str(text or ""))
        if _BM25_TOKEN_RE.search(token)
    ]


def rebuild_policy_clause_bm25_index() -> int:
    """重建当前活动条款的进程内 BM25 索引，并返回条款数量。"""
    global _bm25_documents, _bm25_index, _bm25_index_ready

    documents = load_policy_clauses_for_bm25()
    usable_documents: list[dict[str, Any]] = []
    tokenized_corpus: list[list[str]] = []
    for document in documents:
        tokens = _tokenize_bm25(
            str(document.get("search_text") or document.get("raw_text") or "")
        )
        if not tokens:
            continue
        usable_documents.append(document)
        tokenized_corpus.append(tokens)

    new_index = BM25Okapi(tokenized_corpus) if tokenized_corpus else None
    from policy.semantic_reranking import invalidate_semantic_rerank_cache
    invalidate_semantic_rerank_cache()
    with _bm25_lock:
        _bm25_documents = usable_documents
        _bm25_index = new_index
        _bm25_index_ready = True
    return len(usable_documents)


def invalidate_policy_clause_bm25_index() -> None:
    """使进程内 BM25 索引失效，下一次查询时按最新条款重新加载。"""
    global _bm25_documents, _bm25_index, _bm25_index_ready
    from policy.semantic_reranking import invalidate_semantic_rerank_cache
    invalidate_semantic_rerank_cache()
    with _bm25_lock:
        _bm25_documents = []
        _bm25_index = None
        _bm25_index_ready = False


def _get_bm25_index_snapshot() -> tuple[BM25Okapi | None, list[dict[str, Any]]]:
    """按需建立索引，并返回不会被后续刷新替换的查询快照。"""
    with _bm25_lock:
        if not _bm25_index_ready:
            rebuild_policy_clause_bm25_index()
        return _bm25_index, list(_bm25_documents)


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
        "parent_clause_id": clause.get("parent_clause_id"),
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


def search_policy_clauses_bm25(
    query: str, top_k: int = 50,
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("检索问题不能为空")
    if not 1 <= top_k <= 100:
        raise ValueError("BM25 top_k 必须在 1 到 100 之间")

    query_tokens = _tokenize_bm25(query)
    if not query_tokens:
        return []

    index, documents = _get_bm25_index_snapshot()
    if index is None or not documents:
        return []

    scores = index.get_scores(query_tokens)
    top_indices = sorted(
        range(len(scores)),
        key=lambda index: (float(scores[index]), -index),
        reverse=True,
    )[:top_k]

    results: list[dict[str, Any]] = []

    for index in top_indices:
        score = float(scores[index])
        document_tokens = set(
            _tokenize_bm25(
                str(documents[index].get("search_text") or "")
            )
        )

        # BM25 在单文档或高频词场景下可能得分为 0，但只要确实命中词元仍应保留。
        if not document_tokens.intersection(query_tokens):
            continue

        doc = documents[index]
        metadata = dict(doc.get("metadata") or {})
        metadata.setdefault("clause_id", str(doc.get("clause_id") or ""))
        metadata.setdefault("policy_id", str(doc.get("policy_id") or ""))
        metadata.setdefault("raw_text", str(doc.get("raw_text") or ""))
        metadata.setdefault("search_text", str(doc.get("search_text") or ""))

        results.append({
            "id": doc["clause_id"],
            "bm25_score": score,
            "metadata": metadata,
        })

    return results

def rerank_clause_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int,
    use_fusion_score: bool = False,
) -> list[dict[str, Any]]:
    """按传统规则或 RRF 融合分数排序，并去除重复条款。"""
    if not 1 <= top_k <= 100:
        raise ValueError("top_k 必须在 1 到 100 之间")

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        metadata = dict(candidate.get("metadata") or {})
        similarity = float(candidate.get("similarity") or 0.0)
        keyword_score = _keyword_score(query, str(metadata.get("search_text") or ""))
        fusion_score = float(candidate.get("rrf_score") or 0.0)
        ranked.append({
            **candidate,
            "metadata": metadata,
            "similarity": round(similarity, 6),
            "keyword_score": keyword_score,
            "score": round(
                fusion_score if use_fusion_score else similarity + keyword_score,
                8 if use_fusion_score else 6,
            ),
        })

    ranked.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item.get("rrf_score") or 0.0),
            -float(item["similarity"]),
            -float(item["keyword_score"]),
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
            "rerank_score": candidate.get("semantic_score"),
            "rerank_mode": str(candidate.get("rerank_mode") or "fusion"),
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
                "parent_clause_id": metadata.get("parent_clause_id"),
                "related_clauses": list(metadata.get("related_clauses") or []),
            },
        })
    return {
        "query": str(query), "as_of": as_of,
        "temporal_filter_applied": as_of is not None,
        "warnings": list(dict.fromkeys([*(warnings or []), *[
            str(item["rerank_warning"]) for item in candidates if item.get("rerank_warning")
        ]])),
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
        invalidate_policy_clause_bm25_index()
        return 0

    storage.vector.delete_vectors_by_metadata(
        POLICY_CLAUSE_INDEX_NAME, "policy_id", str(policy_id)
    )
    count = storage.vector.insert_vectors(POLICY_CLAUSE_INDEX_NAME, vectors, metadata)
    invalidate_policy_clause_bm25_index()
    return count


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


def load_policy_clauses_for_bm25() -> list[dict[str, Any]]:
    """读取当前结构版本的可引用条款，并组装 BM25 所需的完整 metadata。"""
    from policy.storage import (
        TABLE_POLICY_CLAUSES,
        TABLE_POLICY_DOCUMENTS,
        ensure_policy_tables,
    )
    from storage_adapter import storage

    ensure_policy_tables()
    clause_rows = storage.relational.query(
        TABLE_POLICY_CLAUSES,
        '"is_active" = TRUE',
        100000,
        (),
    )
    document_rows = storage.relational.query(
        TABLE_POLICY_DOCUMENTS,
        "",
        100000,
        (),
    )
    documents_by_id = {
        str(row.get("policy_id") or ""): row
        for row in document_rows
        if str(row.get("policy_id") or "").strip()
    }

    documents: list[dict[str, Any]] = []
    for clause in clause_rows:
        if not is_searchable_policy_clause(clause):
            continue
        policy_id = str(clause.get("policy_id") or "")
        policy_document = documents_by_id.get(policy_id)
        if not policy_document:
            continue
        if str(clause.get("structure_version") or "") != str(
            policy_document.get("structure_version") or ""
        ):
            continue

        chapter_path = clause.get("chapter_path") or []
        if not isinstance(chapter_path, list):
            chapter_path = []
        search_text = str(clause.get("search_text") or "").strip()
        if not search_text:
            search_text = build_clause_index_text(clause)
        metadata = {
            "clause_id": str(clause.get("clause_id") or ""),
            "policy_id": policy_id,
            "file_id": str(policy_document.get("file_id") or ""),
            "file_name": str(policy_document.get("file_name") or ""),
            "title": str(
                policy_document.get("title")
                or policy_document.get("file_name")
                or ""
            ),
            "level": str(clause.get("level") or ""),
            "article_no": str(clause.get("article_no") or ""),
            "paragraph_no": str(clause.get("paragraph_no") or ""),
            "item_no": str(clause.get("item_no") or ""),
            "chapter_path": list(chapter_path),
            "page_start": int(clause.get("page_start") or 0),
            "page_end": int(
                clause.get("page_end") or clause.get("page_start") or 0
            ),
            "raw_text": str(clause.get("raw_text") or ""),
            "search_text": search_text,
            "structure_version": str(clause.get("structure_version") or ""),
        }
        documents.append({
            "clause_id": metadata["clause_id"],
            "search_text": search_text,
            "raw_text": metadata["raw_text"],
            "sequence_no": int(clause.get("sequence_no") or 0),
            "metadata": metadata,
        })

    documents.sort(
        key=lambda item: (
            str(item["metadata"].get("policy_id") or ""),
            int(item.get("sequence_no") or 0),
            str(item["clause_id"]),
        )
    )
    return documents

def rrf_fusion(
    vector_candidates: list[dict[str, Any]],
    bm25_candidates: list[dict[str, Any]],
    top_k: int = 100,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """
    使用 RRF（Reciprocal Rank Fusion）融合向量检索和 BM25 检索结果。

    RRF(d) = 1 / (rrf_k + vector_rank)
           + 1 / (rrf_k + bm25_rank)

    同一个 clause_id 会自动合并。
    """

    fused: dict[str, dict[str, Any]] = {}

    # 1. 融合向量检索结果
    for rank, candidate in enumerate(vector_candidates, start=1):
        metadata = dict(candidate.get("metadata") or {})

        clause_id = str(
            metadata.get("clause_id")
            or candidate.get("id")
            or ""
        )

        if not clause_id:
            continue

        if clause_id not in fused:
            fused[clause_id] = {
                **candidate,
                "metadata": metadata,
                "rrf_score": 0.0,
                "vector_rank": None,
                "bm25_rank": None,
                "similarity": candidate.get("similarity"),
                "bm25_score": None,
            }

        fused[clause_id]["vector_rank"] = rank
        fused[clause_id]["similarity"] = candidate.get("similarity")

        fused[clause_id]["rrf_score"] += 1.0 / (rrf_k + rank)

    # 2. 融合 BM25 检索结果
    for rank, candidate in enumerate(bm25_candidates, start=1):
        metadata = dict(candidate.get("metadata") or {})

        clause_id = str(
            metadata.get("clause_id")
            or candidate.get("id")
            or ""
        )

        if not clause_id:
            continue

        if clause_id not in fused:
            fused[clause_id] = {
                **candidate,
                "metadata": metadata,
                "rrf_score": 0.0,
                "vector_rank": None,
                "bm25_rank": None,
                "similarity": None,
                "bm25_score": candidate.get("bm25_score"),
            }
        else:
            # BM25 结果里可能带有更完整 metadata，可以补进去
            fused[clause_id]["metadata"].update(metadata)

        fused[clause_id]["bm25_rank"] = rank
        fused[clause_id]["bm25_score"] = candidate.get("bm25_score")

        fused[clause_id]["rrf_score"] += 1.0 / (rrf_k + rank)

    # 3. 按 RRF 分数排序
    results = list(fused.values())

    results.sort(
        key=lambda item: (
            -float(item["rrf_score"]),
            str((item.get("metadata") or {}).get("clause_id") or ""),
        )
    )

    # 4. 保留一定小数位
    for item in results:
        item["rrf_score"] = round(float(item["rrf_score"]), 8)

    return results[:top_k]

def attach_clause_hierarchy_context(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为命中条款补充同一活动版本的祖先与后代原文，不拼接改写条款。"""
    from policy.storage import get_policy_clauses
    loaded = {}
    results = []
    for candidate in candidates:
        metadata = dict(candidate.get('metadata') or {})
        policy_id = str(metadata.get('policy_id') or '')
        if policy_id not in loaded:
            loaded[policy_id] = get_policy_clauses(policy_id) if policy_id else []
        rows = loaded[policy_id]
        by_id = {str(row['clause_id']): row for row in rows}
        hit = by_id.get(str(metadata.get('clause_id') or ''))
        related = []
        if hit:
            metadata['parent_clause_id'] = hit.get('parent_clause_id')
            ancestors = set()
            parent = hit.get('parent_clause_id')
            while parent in by_id and parent not in ancestors:
                ancestors.add(parent)
                parent = by_id[parent].get('parent_clause_id')
            descendants = {str(hit['clause_id'])}
            # 每次只沿真实父子边扩展，防止混入同级职责或跨版本条款。
            for _ in range(len(rows)):
                added = {str(row['clause_id']) for row in rows
                         if row.get('parent_clause_id') in descendants} - descendants
                if not added:
                    break
                descendants.update(added)
            for row in rows:
                cid = str(row['clause_id'])
                if cid in ancestors or (cid in descendants and cid != str(hit['clause_id'])):
                    related.append({key: row.get(key) for key in (
                        'clause_id', 'parent_clause_id', 'level', 'article_no', 'paragraph_no',
                        'item_no', 'raw_text', 'page_start', 'page_end')})
            # 明确告知截断，限制问答上下文长度。
            metadata['hierarchy_context_truncated'] = len(related) > 40
            related = related[:40]
        metadata['related_clauses'] = related
        results.append({**candidate, 'metadata': metadata})
    return results


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
        candidate_top_k = min(100, max(50, top_k * 5))

        vectors = _encode_policy_clause_texts([query])

        #根据向量相似度检索
        vector_candidates = storage.vector.search(
            POLICY_CLAUSE_INDEX_NAME,
            vectors[0],
            top_k=candidate_top_k,
        )
        #根据关键词检索
        bm25_candidates = search_policy_clauses_bm25(
            query,
            top_k=candidate_top_k,
        )

        candidates = rrf_fusion(
            vector_candidates,
            bm25_candidates,
            top_k=candidate_top_k,
        )

    except Exception as exc:
        raise PolicyClauseRetrievalServiceError(
            f"条款检索服务暂不可用: {exc}"
        ) from exc
    ranked = rerank_clause_candidates(
        query,
        candidates,
        100,
        use_fusion_score=True,
    )
    from config import app_config
    if app_config.answer_llm.rerank_enabled:
        from policy.semantic_reranking import semantic_rerank_candidates
        ranked = semantic_rerank_candidates(query, ranked, cache_context=as_of or "")
    if as_of is None:
        return attach_clause_hierarchy_context(ranked[:top_k])
    from policy.storage import get_policy_documents_by_ids
    from policy.temporal import rank_temporal_candidates

    policy_ids = [str((item.get("metadata") or {}).get("policy_id") or "") for item in ranked]
    temporal, warnings = rank_temporal_candidates(
        ranked, get_policy_documents_by_ids(policy_ids), as_of, top_k
    )
    warnings = list(dict.fromkeys([*warnings, *[
        str(item["rerank_warning"]) for item in ranked if item.get("rerank_warning")
    ]]))
    for item in temporal:
        item["temporal_warnings"] = warnings
    return attach_clause_hierarchy_context(temporal)
