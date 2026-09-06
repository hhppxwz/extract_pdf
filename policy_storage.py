"""制度结构化与实体关系抽取的 PostgreSQL 持久化服务。

本模块只负责表结构和数据库读写，不负责 PDF 解析或大模型调用。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from models import (
    PolicyAnnotationDecision,
    PolicyCandidateKind,
    PolicyClause,
    PolicyDocument,
    PolicyEntity,
    PolicyExtractionItemStatus,
    PolicyExtractionRun,
    PolicyExtractionStatus,
    PolicyRelation,
    PolicyReviewStatus,
    PolicyStructureStatus,
    PolicyValidityStatus,
    PolicyManualAnnotation,
    ProcessingVersion,
    ReviewItem,
)
from storage_adapter import storage


TABLE_POLICY_DOCUMENTS = "policy_documents"
TABLE_POLICY_CLAUSES = "policy_clauses"
TABLE_POLICY_RUNS = "policy_extraction_runs"
TABLE_POLICY_ITEMS = "policy_extraction_items"
TABLE_POLICY_ENTITIES = "policy_entities"
TABLE_POLICY_RELATIONS = "policy_relations"
TABLE_POLICY_REVIEWS = "policy_review_items"
TABLE_POLICY_MANUAL_ANNOTATIONS = "policy_manual_annotations"

_POLICY_TABLES_READY = False


def ensure_policy_tables() -> None:
    """确保制度结构化和抽取相关表存在。"""
    global _POLICY_TABLES_READY
    if _POLICY_TABLES_READY:
        return

    # 先确保现有 pdf_files 和批任务表存在，便于建立外键和按批次查找。
    from metadata_service import _ensure_meta_tables

    _ensure_meta_tables()
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_DOCUMENTS}" (
            policy_id VARCHAR(96) PRIMARY KEY,
            file_id VARCHAR(64) NOT NULL UNIQUE REFERENCES "pdf_files"(file_id),
            file_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            doc_number TEXT NOT NULL DEFAULT '',
            issuing_department TEXT NOT NULL DEFAULT '',
            issue_date DATE,
            effective_date DATE,
            expiry_date DATE,
            validity_status VARCHAR(20) NOT NULL DEFAULT 'unknown'
                CHECK (validity_status IN ('current', 'invalid', 'unknown')),
            version TEXT NOT NULL DEFAULT '',
            original_pdf_url TEXT NOT NULL DEFAULT '',
            parse_quality DOUBLE PRECISION NOT NULL DEFAULT 0,
            structure_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            structure_version TEXT NOT NULL DEFAULT '',
            current_extraction_run_id VARCHAR(96),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_CLAUSES}" (
            clause_id VARCHAR(128) PRIMARY KEY,
            policy_id VARCHAR(96) NOT NULL REFERENCES "{TABLE_POLICY_DOCUMENTS}"(policy_id),
            parent_clause_id VARCHAR(128),
            level VARCHAR(20) NOT NULL DEFAULT 'article',
            chapter_path JSONB NOT NULL DEFAULT '[]'::jsonb,
            article_no TEXT NOT NULL DEFAULT '',
            paragraph_no TEXT NOT NULL DEFAULT '',
            item_no TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            page_start INTEGER NOT NULL DEFAULT 0,
            page_end INTEGER NOT NULL DEFAULT 0,
            bboxes JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            content_hash VARCHAR(64) NOT NULL DEFAULT '',
            sequence_no INTEGER NOT NULL DEFAULT 0,
            structure_version TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )'''
    )
    # 条款ID包含内容哈希，内容变化时要保留旧条款，不能用序号唯一约束阻止新版本写入。
    storage.relational.execute(
        f'ALTER TABLE "{TABLE_POLICY_CLAUSES}" DROP CONSTRAINT IF EXISTS '
        f'"policy_clauses_policy_id_structure_version_sequence_no_key"'
    )
    storage.relational.execute(
        f'ALTER TABLE "{TABLE_POLICY_CLAUSES}" ADD COLUMN IF NOT EXISTS '
        f'"is_active" BOOLEAN NOT NULL DEFAULT TRUE'
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_RUNS}" (
            run_id VARCHAR(96) PRIMARY KEY,
            batch_id VARCHAR(64) NOT NULL REFERENCES "pdf_batches"(batch_id),
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            parser_version TEXT NOT NULL DEFAULT '',
            embedding_model TEXT NOT NULL DEFAULT '',
            llm_model TEXT NOT NULL DEFAULT '',
            pipeline_version TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            rule_version TEXT NOT NULL DEFAULT '',
            schema_version TEXT NOT NULL DEFAULT '',
            total_count INTEGER NOT NULL DEFAULT 0,
            succeeded_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            is_current BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_ITEMS}" (
            item_id VARCHAR(128) PRIMARY KEY,
            run_id VARCHAR(96) NOT NULL REFERENCES "{TABLE_POLICY_RUNS}"(run_id)
                ON DELETE CASCADE,
            clause_id VARCHAR(128) NOT NULL REFERENCES "{TABLE_POLICY_CLAUSES}"(clause_id),
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            next_retry_at TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            UNIQUE(run_id, clause_id)
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_ENTITIES}" (
            entity_id VARCHAR(128) PRIMARY KEY,
            run_id VARCHAR(96) NOT NULL REFERENCES "{TABLE_POLICY_RUNS}"(run_id)
                ON DELETE CASCADE,
            clause_id VARCHAR(128) NOT NULL REFERENCES "{TABLE_POLICY_CLAUSES}"(clause_id),
            entity_type VARCHAR(40) NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            normalized_value JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            evidence_text TEXT NOT NULL DEFAULT '',
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_RELATIONS}" (
            relation_id VARCHAR(128) PRIMARY KEY,
            run_id VARCHAR(96) NOT NULL REFERENCES "{TABLE_POLICY_RUNS}"(run_id)
                ON DELETE CASCADE,
            clause_id VARCHAR(128) NOT NULL REFERENCES "{TABLE_POLICY_CLAUSES}"(clause_id),
            relation_type VARCHAR(40) NOT NULL,
            subject_entity_id VARCHAR(128),
            object_entity_id VARCHAR(128),
            target_policy_id VARCHAR(96),
            target_text TEXT NOT NULL DEFAULT '',
            evidence_text TEXT NOT NULL DEFAULT '',
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_REVIEWS}" (
            review_id VARCHAR(128) PRIMARY KEY,
            run_id VARCHAR(96),
            policy_id VARCHAR(96),
            clause_id VARCHAR(128),
            entity_id VARCHAR(128),
            relation_id VARCHAR(128),
            issue_type VARCHAR(64) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            evidence_text TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            reviewer TEXT NOT NULL DEFAULT '',
            review_note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )'''
    )
    storage.relational.execute(
        f'''CREATE TABLE IF NOT EXISTS "{TABLE_POLICY_MANUAL_ANNOTATIONS}" (
            annotation_id VARCHAR(128) PRIMARY KEY,
            run_id VARCHAR(96) NOT NULL REFERENCES "{TABLE_POLICY_RUNS}"(run_id)
                ON DELETE CASCADE,
            clause_id VARCHAR(128) NOT NULL REFERENCES "{TABLE_POLICY_CLAUSES}"(clause_id),
            candidate_kind VARCHAR(16) NOT NULL
                CHECK (candidate_kind IN ('entity', 'relation')),
            candidate_id VARCHAR(128),
            decision VARCHAR(16) NOT NULL
                CHECK (decision IN ('approved', 'rejected', 'corrected', 'added')),
            label_data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            evidence_start INTEGER,
            evidence_end INTEGER,
            reviewer TEXT NOT NULL DEFAULT '',
            review_note TEXT NOT NULL DEFAULT '',
            guideline_version TEXT NOT NULL DEFAULT 'manual-annotation-v1',
            created_at TIMESTAMP DEFAULT NOW()
        )'''
    )
    for table, index, columns in (
        (TABLE_POLICY_CLAUSES, "policy_clauses_policy_idx", "policy_id, sequence_no"),
        (TABLE_POLICY_ITEMS, "policy_extraction_items_run_idx", "run_id, status"),
        (TABLE_POLICY_ENTITIES, "policy_entities_clause_idx", "run_id, clause_id"),
        (TABLE_POLICY_RELATIONS, "policy_relations_clause_idx", "run_id, clause_id"),
        (TABLE_POLICY_REVIEWS, "policy_reviews_status_idx", "status, issue_type"),
        (TABLE_POLICY_MANUAL_ANNOTATIONS, "policy_manual_annotations_run_idx", "run_id, clause_id"),
    ):
        storage.relational.execute(
            f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}" ({columns})'
        )
    _POLICY_TABLES_READY = True


def _parse_date(value: Any) -> Optional[str]:
    """将元数据日期转换为 PostgreSQL 可接受的 ISO 日期。"""
    if not value:
        return None
    text = str(value).strip().replace("/", "-").replace("年", "-")
    text = text.replace("月", "-").replace("日", "")
    parts = text.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return None


def _stable_policy_id(file_id: str) -> str:
    return f"policy_{hashlib.sha256(file_id.encode('utf-8')).hexdigest()[:24]}"


def upsert_policy_document(
    file_id: str,
    file_name: str,
    metadata: dict[str, Any],
    parse_quality: float,
    structure_version: str,
    structure_status: PolicyStructureStatus = PolicyStructureStatus.RUNNING,
) -> str:
    """创建或更新制度文档记录，并默认保留 unknown 效力状态。"""
    ensure_policy_tables()
    policy_id = _stable_policy_id(file_id)
    title = str(metadata.get("title") or file_name.rsplit(".", 1)[0]).strip()
    original_pdf_url = str(metadata.get("original_pdf_url") or f"pdf/{file_id}.pdf")
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_POLICY_DOCUMENTS}"
            (policy_id, file_id, file_name, title, doc_number, issuing_department,
             issue_date, effective_date, expiry_date, validity_status, version,
             original_pdf_url, parse_quality, structure_status, structure_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_id) DO UPDATE SET
                file_name = EXCLUDED.file_name,
                title = EXCLUDED.title,
                doc_number = EXCLUDED.doc_number,
                issuing_department = EXCLUDED.issuing_department,
                issue_date = EXCLUDED.issue_date,
                effective_date = EXCLUDED.effective_date,
                expiry_date = EXCLUDED.expiry_date,
                version = EXCLUDED.version,
                original_pdf_url = EXCLUDED.original_pdf_url,
                parse_quality = EXCLUDED.parse_quality,
                structure_status = EXCLUDED.structure_status,
                structure_version = EXCLUDED.structure_version,
                updated_at = NOW()''',
        (
            policy_id,
            file_id,
            file_name,
            title,
            str(metadata.get("doc_number") or ""),
            str(metadata.get("issuer") or metadata.get("issuing_department") or ""),
            _parse_date(metadata.get("issue_date")),
            _parse_date(metadata.get("effective_date")),
            _parse_date(metadata.get("expiry_date")),
            PolicyValidityStatus.UNKNOWN.value,
            str(metadata.get("version") or ""),
            original_pdf_url,
            max(0.0, min(float(parse_quality), 1.0)),
            structure_status.value,
            structure_version,
        ),
    )
    return policy_id


def mark_policy_structure_failed(
    policy_id: str, error: str, structure_version: str
) -> None:
    """记录条款结构化失败，并生成审核项。"""
    ensure_policy_tables()
    storage.relational.update_rows(
        TABLE_POLICY_DOCUMENTS,
        {
            "structure_status": PolicyStructureStatus.FAILED.value,
            "structure_version": structure_version,
            "updated_at": datetime.now(),
        },
        '"policy_id" = %s',
        (policy_id,),
    )
    insert_review_item(
        ReviewItem(
            review_id=f"review_{uuid.uuid4().hex}",
            policy_id=policy_id,
            issue_type="clause_structure_error",
            description=error,
            payload={"structure_version": structure_version},
        )
    )


def replace_policy_clauses(
    policy_id: str, clauses: list[PolicyClause], structure_version: str
) -> int:
    """以同一结构版本幂等更新条款，旧条款保留但标记为非当前。"""
    ensure_policy_tables()
    storage.relational.execute(
        f'UPDATE "{TABLE_POLICY_CLAUSES}" SET is_active = FALSE '
        f'WHERE policy_id = %s AND structure_version = %s',
        (policy_id, structure_version),
    )
    for clause in clauses:
        storage.relational.execute(
            f'''INSERT INTO "{TABLE_POLICY_CLAUSES}"
                (clause_id, policy_id, parent_clause_id, level, chapter_path,
                 article_no, paragraph_no, item_no, raw_text, search_text,
                 page_start, page_end, bboxes, confidence, content_hash,
                 sequence_no, structure_version, is_active)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb, %s, %s, %s, %s, TRUE)
                ON CONFLICT (clause_id) DO UPDATE SET
                    parent_clause_id = EXCLUDED.parent_clause_id,
                    level = EXCLUDED.level,
                    chapter_path = EXCLUDED.chapter_path,
                    article_no = EXCLUDED.article_no,
                    paragraph_no = EXCLUDED.paragraph_no,
                    item_no = EXCLUDED.item_no,
                    raw_text = EXCLUDED.raw_text,
                    search_text = EXCLUDED.search_text,
                    page_start = EXCLUDED.page_start,
                    page_end = EXCLUDED.page_end,
                    bboxes = EXCLUDED.bboxes,
                    confidence = EXCLUDED.confidence,
                    content_hash = EXCLUDED.content_hash,
                    sequence_no = EXCLUDED.sequence_no,
                    structure_version = EXCLUDED.structure_version,
                    is_active = TRUE''',
            (
                clause.clause_id,
                clause.policy_id,
                clause.parent_clause_id,
                clause.level,
                json.dumps(clause.chapter_path, ensure_ascii=False),
                clause.article_no,
                clause.paragraph_no,
                clause.item_no,
                clause.raw_text,
                clause.search_text,
                clause.page_start,
                clause.page_end,
                json.dumps(clause.bboxes, ensure_ascii=False),
                clause.confidence,
                clause.content_hash,
                clause.sequence_no,
                clause.structure_version,
            ),
        )
    storage.relational.update_rows(
        TABLE_POLICY_DOCUMENTS,
        {
            "structure_status": PolicyStructureStatus.SUCCEEDED.value,
            "structure_version": structure_version,
            "updated_at": datetime.now(),
        },
        '"policy_id" = %s',
        (policy_id,),
    )
    return len(clauses)


def get_policy_document(policy_id: str = "", file_id: str = "") -> Optional[dict[str, Any]]:
    """按 policy_id 或 file_id 查询制度文档。"""
    ensure_policy_tables()
    if policy_id:
        where, params = '"policy_id" = %s', (policy_id,)
    elif file_id:
        where, params = '"file_id" = %s', (file_id,)
    else:
        return None
    rows = storage.relational.query(TABLE_POLICY_DOCUMENTS, where, 1, params)
    return rows[0] if rows else None


def get_policy_documents_for_batch(batch_id: str) -> list[dict[str, Any]]:
    """获取批次内已完成制度结构化的文档。"""
    ensure_policy_tables()
    from metadata_service import get_batch_items

    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in get_batch_items(batch_id):
        file_id = item.get("file_id")
        if not file_id or file_id in seen:
            continue
        doc = get_policy_document(file_id=file_id)
        if doc and doc.get("structure_status") == PolicyStructureStatus.SUCCEEDED.value:
            documents.append(doc)
            seen.add(file_id)
    return documents


def get_policy_clauses(policy_id: str) -> list[dict[str, Any]]:
    """按顺序获取制度的当前结构版本条款。"""
    ensure_policy_tables()
    document = get_policy_document(policy_id=policy_id)
    if not document:
        return []
    return storage.relational.query(
        TABLE_POLICY_CLAUSES,
        '"policy_id" = %s AND "structure_version" = %s AND "is_active" = TRUE ORDER BY sequence_no',
        100000,
        (policy_id, document.get("structure_version", "")),
    )


def select_policy_clauses_for_extraction(
    documents: list[dict[str, Any]],
    limit: Optional[int] = None,
    clause_loader: Optional[Callable[[str], list[dict[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    """按文件名和条款序号稳定选取实质性条款，可限制本次抽取数量。"""
    if limit is not None and limit < 1:
        raise ValueError("limit 必须大于等于 1")

    loader = clause_loader or get_policy_clauses
    selected: list[dict[str, Any]] = []
    documents = sorted(
        documents,
        key=lambda document: (
            str(document.get("file_name") or ""),
            str(document.get("policy_id") or ""),
        ),
    )
    for document in documents:
        clauses = sorted(
            loader(str(document["policy_id"])),
            key=lambda clause: int(clause.get("sequence_no") or 0),
        )
        for clause in clauses:
            # 纯标题、目录和过短内容不进入实体关系抽取。
            if len(str(clause.get("search_text") or "").strip()) < 20:
                continue
            if clause.get("level") in ("book", "chapter", "section"):
                continue
            selected.append(clause)
            if limit is not None and len(selected) >= limit:
                return selected
    return selected


def create_policy_extraction_run(
    batch_id: str,
    versions: ProcessingVersion,
    prompt_version: str = "policy-extract-v1",
    rule_version: str = "policy-rules-v1",
    schema_version: str = "policy-schema-v1",
    limit: Optional[int] = None,
) -> str:
    """创建抽取运行，并为限定范围内的实质性条款建立可恢复任务。"""
    ensure_policy_tables()
    run_id = f"policy_run_{uuid.uuid4().hex}"
    documents = get_policy_documents_for_batch(batch_id)
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_POLICY_RUNS}"
            (run_id, batch_id, status, parser_version, embedding_model, llm_model,
             pipeline_version, prompt_version, rule_version, schema_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (
            run_id,
            batch_id,
            PolicyExtractionStatus.PENDING.value,
            versions.parser_version,
            versions.embedding_model,
            versions.llm_model,
            versions.pipeline_version,
            prompt_version,
            rule_version,
            schema_version,
        ),
    )
    selected_clauses = select_policy_clauses_for_extraction(documents, limit)
    for clause in selected_clauses:
        item_id = f"policy_item_{uuid.uuid4().hex}"
        storage.relational.execute(
            f'''INSERT INTO "{TABLE_POLICY_ITEMS}"
                (item_id, run_id, clause_id, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, clause_id) DO NOTHING''',
            (item_id, run_id, clause["clause_id"], PolicyExtractionItemStatus.PENDING.value),
        )
    storage.relational.update_rows(
        TABLE_POLICY_RUNS,
        {"total_count": len(selected_clauses)},
        '"run_id" = %s',
        (run_id,),
    )
    return run_id


def get_policy_extraction_run(run_id: str) -> Optional[dict[str, Any]]:
    ensure_policy_tables()
    rows = storage.relational.query(TABLE_POLICY_RUNS, '"run_id" = %s', 1, (run_id,))
    return rows[0] if rows else None


def get_policy_extraction_items(run_id: str) -> list[dict[str, Any]]:
    ensure_policy_tables()
    return storage.relational.query(
        TABLE_POLICY_ITEMS,
        '"run_id" = %s ORDER BY item_id',
        100000,
        (run_id,),
    )


def get_policy_clause(clause_id: str) -> Optional[dict[str, Any]]:
    ensure_policy_tables()
    rows = storage.relational.query(TABLE_POLICY_CLAUSES, '"clause_id" = %s', 1, (clause_id,))
    return rows[0] if rows else None


def update_policy_extraction_item(item_id: str, values: dict[str, Any]) -> None:
    ensure_policy_tables()
    storage.relational.update_rows(TABLE_POLICY_ITEMS, values, '"item_id" = %s', (item_id,))


def update_policy_extraction_run(run_id: str, values: dict[str, Any]) -> None:
    ensure_policy_tables()
    storage.relational.update_rows(TABLE_POLICY_RUNS, values, '"run_id" = %s', (run_id,))


def delete_clause_outputs(run_id: str, clause_id: str) -> None:
    """重试同一条款前清除该运行产生的旧结果，避免重复记录。"""
    ensure_policy_tables()
    storage.relational.execute(
        f'DELETE FROM "{TABLE_POLICY_REVIEWS}" WHERE run_id = %s AND clause_id = %s',
        (run_id, clause_id),
    )
    storage.relational.execute(
        f'DELETE FROM "{TABLE_POLICY_RELATIONS}" WHERE run_id = %s AND clause_id = %s',
        (run_id, clause_id),
    )
    storage.relational.execute(
        f'DELETE FROM "{TABLE_POLICY_ENTITIES}" WHERE run_id = %s AND clause_id = %s',
        (run_id, clause_id),
    )


def insert_policy_entity(entity: PolicyEntity) -> None:
    ensure_policy_tables()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_POLICY_ENTITIES}"
            (entity_id, run_id, clause_id, entity_type, name, raw_text,
             normalized_value, evidence_text, confidence, review_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)''',
        (
            entity.entity_id,
            entity.run_id,
            entity.clause_id,
            entity.entity_type.value,
            entity.name,
            entity.raw_text,
            json.dumps(entity.normalized_value, ensure_ascii=False),
            entity.evidence_text,
            entity.confidence,
            entity.review_status.value,
        ),
    )


def insert_policy_relation(relation: PolicyRelation) -> None:
    ensure_policy_tables()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_POLICY_RELATIONS}"
            (relation_id, run_id, clause_id, relation_type, subject_entity_id,
             object_entity_id, target_policy_id, target_text, evidence_text,
             confidence, review_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        (
            relation.relation_id,
            relation.run_id,
            relation.clause_id,
            relation.relation_type.value,
            relation.subject_entity_id,
            relation.object_entity_id,
            relation.target_policy_id,
            relation.target_text,
            relation.evidence_text,
            relation.confidence,
            relation.review_status.value,
        ),
    )


def insert_review_item(item: ReviewItem) -> None:
    ensure_policy_tables()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_POLICY_REVIEWS}"
            (review_id, run_id, policy_id, clause_id, entity_id, relation_id,
             issue_type, description, evidence_text, payload, status, reviewer, review_note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (review_id) DO NOTHING''',
        (
            item.review_id,
            item.run_id,
            item.policy_id,
            item.clause_id,
            item.entity_id,
            item.relation_id,
            item.issue_type,
            item.description,
            item.evidence_text,
            json.dumps(item.payload, ensure_ascii=False),
            item.status.value,
            item.reviewer,
            item.review_note,
        ),
    )


def get_policy_review_candidate(
    run_id: str,
    candidate_kind: PolicyCandidateKind,
    candidate_id: str,
) -> Optional[dict[str, Any]]:
    """按运行和候选 ID 获取待人工审核的实体或关系。"""
    ensure_policy_tables()
    if candidate_kind == PolicyCandidateKind.ENTITY:
        table_name, id_column = TABLE_POLICY_ENTITIES, "entity_id"
    else:
        table_name, id_column = TABLE_POLICY_RELATIONS, "relation_id"
    rows = storage.relational.query(
        table_name,
        f'"run_id" = %s AND "{id_column}" = %s',
        1,
        (run_id, candidate_id),
    )
    return rows[0] if rows else None


def list_policy_review_candidates(run_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """列出尚未由人工处理的模型候选，并附带制度和条款原文。"""
    ensure_policy_tables()
    if limit < 1:
        raise ValueError("limit 必须大于等于 1")
    pending_statuses = (
        PolicyReviewStatus.PENDING.value,
        PolicyReviewStatus.AUTO_APPROVED.value,
    )
    candidates: list[dict[str, Any]] = []
    source_tables = (
        (PolicyCandidateKind.ENTITY, TABLE_POLICY_ENTITIES, "entity_id"),
        (PolicyCandidateKind.RELATION, TABLE_POLICY_RELATIONS, "relation_id"),
    )
    for candidate_kind, table_name, id_column in source_tables:
        rows = storage.relational.query(
            table_name,
            '"run_id" = %s AND "review_status" IN (%s, %s)',
            100000,
            (run_id, *pending_statuses),
        )
        for row in rows:
            clause = get_policy_clause(row["clause_id"])
            if not clause:
                continue
            document = get_policy_document(policy_id=clause["policy_id"])
            candidates.append({
                "candidate_kind": candidate_kind.value,
                "candidate_id": row[id_column],
                "candidate": row,
                "clause": clause,
                "document": document or {},
            })
    candidates.sort(key=lambda item: (
        item["document"].get("file_name", ""),
        int(item["clause"].get("sequence_no") or 0),
        item["candidate_kind"],
        item["candidate_id"],
    ))
    return candidates[:limit]


def insert_manual_annotation(annotation: PolicyManualAnnotation) -> None:
    """保存人工标注；不修改或覆盖原始模型候选。"""
    ensure_policy_tables()
    storage.relational.execute(
        f'''INSERT INTO "{TABLE_POLICY_MANUAL_ANNOTATIONS}"
            (annotation_id, run_id, clause_id, candidate_kind, candidate_id, decision,
             label_data, evidence_start, evidence_end, reviewer, review_note, guideline_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)''',
        (
            annotation.annotation_id,
            annotation.run_id,
            annotation.clause_id,
            annotation.candidate_kind.value,
            annotation.candidate_id,
            annotation.decision.value,
            json.dumps(annotation.label_data, ensure_ascii=False),
            annotation.evidence_start,
            annotation.evidence_end,
            annotation.reviewer,
            annotation.review_note,
            annotation.guideline_version,
        ),
    )


def update_policy_candidate_review_status(
    candidate_kind: PolicyCandidateKind,
    candidate_id: str,
    review_status: PolicyReviewStatus,
) -> None:
    """更新候选的人工审核状态，不改动候选内容。"""
    ensure_policy_tables()
    if candidate_kind == PolicyCandidateKind.ENTITY:
        table_name, id_column = TABLE_POLICY_ENTITIES, "entity_id"
    else:
        table_name, id_column = TABLE_POLICY_RELATIONS, "relation_id"
    storage.relational.update_rows(
        table_name,
        {"review_status": review_status.value},
        f'"{id_column}" = %s',
        (candidate_id,),
    )


def mark_extraction_run_current(run_id: str, batch_id: str) -> None:
    """切换当前抽取版本，旧运行保留用于追溯。"""
    ensure_policy_tables()
    storage.relational.execute(
        f'UPDATE "{TABLE_POLICY_RUNS}" SET is_current = FALSE WHERE batch_id = %s',
        (batch_id,),
    )
    storage.relational.update_rows(
        TABLE_POLICY_RUNS,
        {"is_current": True},
        '"run_id" = %s',
        (run_id,),
    )
    storage.relational.execute(
        f'''UPDATE "{TABLE_POLICY_DOCUMENTS}"
            SET current_extraction_run_id = %s, updated_at = NOW()
            WHERE file_id IN (
                SELECT file_id FROM "pdf_batch_items"
                WHERE batch_id = %s AND file_id IS NOT NULL
            )''',
        (run_id, batch_id),
    )
