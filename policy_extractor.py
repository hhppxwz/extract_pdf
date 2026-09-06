"""制度条款实体与关系抽取管线。

该阶段读取已入库的 PolicyClause，不会再次调用 CloudMinerU。
规则负责明确的金额、期限等事实；配置大模型时再补充语义实体和关系。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from openai import OpenAI

from batch_processor import current_processing_versions
from config import app_config
from models import (
    PolicyEntity,
    PolicyEntityType,
    PolicyExtractionItemStatus,
    PolicyExtractionStatus,
    PolicyRelation,
    PolicyRelationType,
    PolicyReviewStatus,
    ReviewItem,
)
from policy_storage import (
    create_policy_extraction_run,
    delete_clause_outputs,
    get_policy_clause,
    get_policy_extraction_items,
    get_policy_extraction_run,
    insert_policy_entity,
    insert_policy_relation,
    insert_review_item,
    mark_extraction_run_current,
    update_policy_extraction_item,
    update_policy_extraction_run,
)


MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2, 4, 8)
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("POLICY_EXTRACTION_HEARTBEAT_SECONDS", "1800"))
CONFIDENCE_THRESHOLD = 0.85
PROMPT_VERSION = "policy-extract-v1"
RULE_VERSION = "policy-rules-v1"
SCHEMA_VERSION = "policy-schema-v1"
RULE_ONLY_PROMPT_VERSION = "policy-rule-only-v1"


class PermanentExtractionError(RuntimeError):
    """格式不合法或业务不满足条件，不应继续自动重试。"""


_ENTITY_ALIASES = {
    "matter": PolicyEntityType.MATTER,
    "事项": PolicyEntityType.MATTER,
    "办理事项": PolicyEntityType.MATTER,
    "department": PolicyEntityType.DEPARTMENT,
    "部门": PolicyEntityType.DEPARTMENT,
    "责任部门": PolicyEntityType.DEPARTMENT,
    "audience": PolicyEntityType.AUDIENCE,
    "适用对象": PolicyEntityType.AUDIENCE,
    "人员": PolicyEntityType.AUDIENCE,
    "material": PolicyEntityType.MATERIAL,
    "材料": PolicyEntityType.MATERIAL,
    "amount": PolicyEntityType.AMOUNT,
    "金额": PolicyEntityType.AMOUNT,
    "deadline": PolicyEntityType.DEADLINE,
    "期限": PolicyEntityType.DEADLINE,
    "时间期限": PolicyEntityType.DEADLINE,
    "approval_action": PolicyEntityType.APPROVAL_ACTION,
    "审批动作": PolicyEntityType.APPROVAL_ACTION,
    "审批": PolicyEntityType.APPROVAL_ACTION,
}

_RELATION_ALIASES = {
    "applies_to": PolicyRelationType.APPLIES_TO,
    "适用于": PolicyRelationType.APPLIES_TO,
    "handled_by": PolicyRelationType.HANDLED_BY,
    "由部门办理": PolicyRelationType.HANDLED_BY,
    "requires_material": PolicyRelationType.REQUIRES_MATERIAL,
    "需要材料": PolicyRelationType.REQUIRES_MATERIAL,
    "has_amount": PolicyRelationType.HAS_AMOUNT,
    "金额": PolicyRelationType.HAS_AMOUNT,
    "has_deadline": PolicyRelationType.HAS_DEADLINE,
    "期限": PolicyRelationType.HAS_DEADLINE,
    "requires_approval": PolicyRelationType.REQUIRES_APPROVAL,
    "需要审批": PolicyRelationType.REQUIRES_APPROVAL,
    "cites": PolicyRelationType.CITES,
    "引用": PolicyRelationType.CITES,
    "based_on": PolicyRelationType.BASED_ON,
    "依据": PolicyRelationType.BASED_ON,
    "revises": PolicyRelationType.REVISES,
    "修订": PolicyRelationType.REVISES,
    "abolishes": PolicyRelationType.ABOLISHES,
    "废止": PolicyRelationType.ABOLISHES,
    "replaces": PolicyRelationType.REPLACES,
    "替代": PolicyRelationType.REPLACES,
}


def _compact(text: str) -> str:
    """去除空白，便于验证模型证据是否来自原文。"""
    return re.sub(r"\s+", "", str(text or ""))


def _evidence_supported(evidence: str, source: str) -> bool:
    evidence_compact = _compact(evidence)
    source_compact = _compact(source)
    return bool(evidence_compact) and evidence_compact in source_compact


def _entity_status(
    confidence: float,
    valid_evidence: bool,
    requires_manual_review: bool = False,
) -> PolicyReviewStatus:
    """根据抽取来源决定是否允许自动通过。"""
    if requires_manual_review:
        return PolicyReviewStatus.PENDING
    if valid_evidence and confidence >= CONFIDENCE_THRESHOLD:
        return PolicyReviewStatus.AUTO_APPROVED
    return PolicyReviewStatus.PENDING


def _rule_entities(text: str) -> list[dict[str, Any]]:
    """抽取金额、期限和明显的部门/人员/动作等实体。"""
    entities: list[dict[str, Any]] = []

    def add(entity_type: PolicyEntityType, name: str, evidence: str, normalized: Optional[dict[str, Any]] = None) -> None:
        name, evidence = str(name or "").strip(), str(evidence or "").strip()
        if not name or not evidence or not _evidence_supported(evidence, text):
            return
        key = (entity_type.value, name, evidence)
        if any((item["type"], item["name"], item["evidence_text"]) == key for item in entities):
            return
        entities.append({
            "type": entity_type,
            "name": name,
            "raw_text": evidence,
            "normalized_value": normalized or {},
            "evidence_text": evidence,
            "confidence": 0.98,
            "source": "rule",
        })

    amount_pattern = re.compile(
        r"(?P<evidence>.{0,25}?(?:不超过|不高于|最高|上限|限额|标准为|金额为|不得超过|不少于|每人每天)"
        r"[^，。；;\n]{0,25}?(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?P<unit>万元|人民币|元|%)?)"
    )
    for match in amount_pattern.finditer(text):
        evidence = match.group("evidence").strip(" ，,。；;：:")
        value = float(match.group("value"))
        unit = match.group("unit") or "元"
        add(
            PolicyEntityType.AMOUNT,
            f"{match.group('value')}{unit}",
            evidence,
            {"value": value, "unit": unit, "condition": evidence},
        )

    deadline_pattern = re.compile(
        r"(?P<evidence>.{0,25}(?:应当|须|需|在|于|期限|限期|之内).{0,25}"
        r"(?P<value>\d+)\s*(?P<unit>个工作日|工作日|日|天|个月|月|小时|年))"
    )
    for match in deadline_pattern.finditer(text):
        evidence = match.group("evidence").strip(" ，,。；;：:")
        add(
            PolicyEntityType.DEADLINE,
            f"{match.group('value')}{match.group('unit')}",
            evidence,
            {"value": int(match.group("value")), "unit": match.group("unit"), "condition": evidence},
        )

    for match in re.finditer(r"[一-龥]{2,15}(?:处|部|办|院|中心|科|室)", text):
        department = re.sub(r"^(?:由|向|在|经|报送|提交)", "", match.group(0))
        add(PolicyEntityType.DEPARTMENT, department, match.group(0))

    for audience in ("教职工", "教师", "学生", "在编人员", "申请人", "各单位", "各部门"):
        if audience in text:
            add(PolicyEntityType.AUDIENCE, audience, audience)

    # 从“提交/提供/附具/材料包括”等固定表达中提取材料名称。
    material_pattern = re.compile(
        r"(?:提交|提供|附具|附上|报送|材料(?:包括|为|如下)?)\s*[：:]?\s*"
        r"(?P<items>[^。；;\n]{2,80})"
    )
    for match in material_pattern.finditer(text):
        evidence_text = match.group("items").strip(" ，,、；;：:。")
        for item in re.split(r"[、，,]|(?:和|及|以及)", evidence_text):
            material = item.strip(" \t\r\n：:()（）【】[]")
            # 到达金额、期限或审批条件时，后面的内容不再视为材料清单。
            if re.search(r"^(?:金额|期限)|元|万元|不超过|不高于|限额|工作日|日内|天内", material):
                break
            if not material or len(material) > 40 or material in {"相关材料", "有关材料", "如下"}:
                continue
            add(PolicyEntityType.MATERIAL, material, material)

    for action in ("审批", "审核", "批准", "备案", "签批"):
        if action in text:
            add(PolicyEntityType.APPROVAL_ACTION, action, action)

    for match in re.finditer(r"[一-龥]{1,16}(?:报销|申请|审批|采购|借款|预算|差旅)", text):
        add(PolicyEntityType.MATTER, match.group(0), match.group(0))
    return entities


def _extract_reference_relations(text: str) -> list[dict[str, Any]]:
    """用规则保留明确的制度引用、依据、修订和废止关系。"""
    relations: list[dict[str, Any]] = []
    for match in re.finditer(r"《[^》]{2,100}》(?:（[^）]{1,30}）)?", text):
        evidence = match.group(0)
        before = text[max(0, match.start() - 8):match.start()]
        relation_type = PolicyRelationType.BASED_ON if any(word in before for word in ("依据", "根据", "按照")) else PolicyRelationType.CITES
        relations.append({
            "type": relation_type,
            "target_text": evidence,
            "evidence_text": evidence,
            "confidence": 0.98,
            "source": "rule",
        })
    for word, relation_type in (("修订", PolicyRelationType.REVISES), ("废止", PolicyRelationType.ABOLISHES), ("替代", PolicyRelationType.REPLACES)):
        if word in text:
            match = re.search(rf".{0,30}{word}.{0,50}", text)
            if match:
                relations.append({
                    "type": relation_type,
                    "target_text": match.group(0).strip(" ，,。；;"),
                    "evidence_text": match.group(0).strip(" ，,。；;"),
                    "confidence": 0.90,
                    "source": "rule",
                })
    return relations


def _parse_json_response(raw: str) -> dict[str, Any]:
    """解析模型输出中的JSON对象。"""
    text = str(raw or "").strip()
    if "```" in text:
        text = re.sub(r"```(?:json)?", "", text, flags=re.I).replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PermanentExtractionError("大模型未返回JSON对象")
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PermanentExtractionError(f"大模型JSON解析失败: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PermanentExtractionError("大模型JSON根节点不是对象")
    return parsed


def _call_llm(clause: dict[str, Any], parent_text: str = "") -> dict[str, Any]:
    """调用一次模型，格式错误时再用修复提示重试一次。"""
    if not app_config.llm.api_key:
        raise PermanentExtractionError("LLM_API_KEY 未配置，无法启动实体关系抽取")
    client = OpenAI(base_url=app_config.llm.api_url, api_key=app_config.llm.api_key)
    prompt = f"""请从下面的制度条款中抽取实体和关系，只返回JSON，不要解释。

允许的实体类型：matter, department, audience, material, amount, deadline, approval_action。
允许的关系类型：applies_to, handled_by, requires_material, has_amount, has_deadline,
requires_approval, cites, based_on, revises, abolishes, replaces。

规则：
1. 不得补写原文没有的事实；没有证据就不要抽取。
2. evidence_text 必须是原文连续片段。
3. relation 的 subject_index/object_index 是 entities 数组的0起始下标；制度引用可只填 target_text。
4. amount/deadline 的具体数值放到 normalized_value 对象中。

返回格式：
{{"entities":[{{"type":"matter","name":"","raw_text":"","normalized_value":{{}},"evidence_text":"","confidence":0.0}}],
"relations":[{{"type":"requires_material","subject_index":0,"object_index":1,"target_text":"","evidence_text":"","confidence":0.0}}]}}

章节路径：{" / ".join(clause.get("chapter_path") or [])}
条款：{clause.get("raw_text", "")}
必要的父条款上下文：{parent_text[:1500]}
"""
    response = client.chat.completions.create(
        model=app_config.llm.model,
        messages=[
            {"role": "system", "content": "你是制度条款信息抽取器，只输出合法JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=2500,
    )
    raw = response.choices[0].message.content or ""
    try:
        return _parse_json_response(raw)
    except PermanentExtractionError:
        repair = client.chat.completions.create(
            model=app_config.llm.model,
            messages=[
                {"role": "system", "content": "只修复JSON格式，不改变任何事实；只输出JSON。"},
                {"role": "user", "content": f"请把以下输出修复为合法JSON：\n{raw}"},
            ],
            temperature=0.0,
            max_tokens=2500,
        )
        return _parse_json_response(repair.choices[0].message.content or "")


def _llm_is_configured() -> bool:
    """判断是否配置了实体关系抽取所需的大模型密钥。"""
    return bool(str(app_config.llm.api_key or "").strip())


def _normalize_entity_type(value: Any) -> Optional[PolicyEntityType]:
    return _ENTITY_ALIASES.get(str(value or "").strip())


def _normalize_relation_type(value: Any) -> Optional[PolicyRelationType]:
    return _RELATION_ALIASES.get(str(value or "").strip())


def _build_outputs(
    run_id: str,
    clause: dict[str, Any],
    model_result: dict[str, Any],
    include_rule_candidates: bool,
) -> tuple[list[PolicyEntity], list[PolicyRelation], list[ReviewItem]]:
    """仅按当前抽取模式构建候选结果，并生成需人工确认的项目。"""
    clause_id = clause["clause_id"]
    source_text = str(clause.get("raw_text") or "")
    entities: list[PolicyEntity] = []
    reviews: list[ReviewItem] = []
    local_to_id: dict[int, str] = {}
    dedupe: dict[tuple[str, str], PolicyEntity] = {}

    # 规则模式与 LLM 模式严格分离，避免 LLM 结果被规则候选混入。
    candidates = _rule_entities(source_text) if include_rule_candidates else []
    model_entities = model_result.get("entities")
    if model_entities is not None and not isinstance(model_entities, list):
        raise PermanentExtractionError("entities 必须是数组")
    model_entities = model_entities or []
    for index, raw_entity in enumerate(model_entities):
        if not isinstance(raw_entity, dict):
            reviews.append(ReviewItem(
                review_id=f"review_{uuid.uuid4().hex}", run_id=run_id,
                clause_id=clause_id, issue_type="invalid_entity", description="模型实体不是对象",
                payload={"entity": raw_entity},
            ))
            continue
        entity_type = _normalize_entity_type(raw_entity.get("type"))
        name = str(raw_entity.get("name") or raw_entity.get("raw_text") or "").strip()
        evidence = str(raw_entity.get("evidence_text") or raw_entity.get("raw_text") or "").strip()
        try:
            confidence = max(0.0, min(1.0, float(raw_entity.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        valid = entity_type is not None and bool(name) and _evidence_supported(evidence, source_text)
        if entity_type is None:
            reviews.append(ReviewItem(
                review_id=f"review_{uuid.uuid4().hex}", run_id=run_id, clause_id=clause_id,
                issue_type="invalid_entity_type", description="模型返回了不允许的实体类型",
                evidence_text=evidence, payload={"entity": raw_entity},
            ))
            continue
        entity_id = f"entity_{uuid.uuid4().hex}"
        entity = PolicyEntity(
            entity_id=entity_id,
            run_id=run_id,
            clause_id=clause_id,
            entity_type=entity_type,
            name=name,
            raw_text=str(raw_entity.get("raw_text") or evidence),
            normalized_value=raw_entity.get("normalized_value") if isinstance(raw_entity.get("normalized_value"), dict) else {},
            evidence_text=evidence,
            confidence=confidence,
            review_status=_entity_status(
                confidence,
                valid,
                requires_manual_review=not include_rule_candidates,
            ),
        )
        key = (entity_type.value, name)
        if key not in dedupe or entity.confidence > dedupe[key].confidence:
            dedupe[key] = entity
        local_to_id[index] = entity_id
        if entity.review_status == PolicyReviewStatus.PENDING and include_rule_candidates:
            reviews.append(ReviewItem(
                review_id=f"review_{uuid.uuid4().hex}", run_id=run_id, clause_id=clause_id,
                entity_id=entity_id, issue_type="low_confidence_entity",
                description="实体置信度不足或证据无法在原文中定位",
                evidence_text=evidence, payload={"entity": raw_entity, "confidence": confidence},
            ))

    # 仅规则模式写入规则实体；LLM 模式只保留模型返回的候选。
    for candidate in candidates:
        key = (candidate["type"].value, candidate["name"])
        entity = PolicyEntity(
            entity_id=f"entity_{uuid.uuid4().hex}",
            run_id=run_id,
            clause_id=clause_id,
            entity_type=candidate["type"],
            name=candidate["name"],
            raw_text=candidate["raw_text"],
            normalized_value=candidate["normalized_value"],
            evidence_text=candidate["evidence_text"],
            confidence=candidate["confidence"],
            review_status=PolicyReviewStatus.AUTO_APPROVED,
        )
        if key not in dedupe or entity.confidence > dedupe[key].confidence:
            dedupe[key] = entity

    entities = list(dedupe.values())
    name_to_id = {(item.entity_type.value, item.name): item.entity_id for item in entities}
    # 模型下标指向的实体若被规则去重，按类型和名称重新解析。
    for index, raw_entity in enumerate(model_entities):
        if not isinstance(raw_entity, dict):
            continue
        entity_type = _normalize_entity_type(raw_entity.get("type"))
        name = str(raw_entity.get("name") or raw_entity.get("raw_text") or "").strip()
        if entity_type and (entity_type.value, name) in name_to_id:
            local_to_id[index] = name_to_id[(entity_type.value, name)]

    raw_relations = model_result.get("relations")
    if raw_relations is not None and not isinstance(raw_relations, list):
        raise PermanentExtractionError("relations 必须是数组")
    rule_relations = _extract_reference_relations(source_text) if include_rule_candidates else []
    relation_specs = rule_relations + (raw_relations or [])
    relations: list[PolicyRelation] = []
    relation_seen: set[tuple[str, str, str]] = set()
    for raw_relation in relation_specs:
        if not isinstance(raw_relation, dict):
            continue
        relation_type = raw_relation.get("type")
        if isinstance(relation_type, PolicyRelationType):
            normalized_relation_type = relation_type
        else:
            normalized_relation_type = _normalize_relation_type(relation_type)
        evidence = str(raw_relation.get("evidence_text") or raw_relation.get("target_text") or "").strip()
        try:
            confidence = max(0.0, min(1.0, float(raw_relation.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        subject_id = local_to_id.get(int(raw_relation["subject_index"])) if str(raw_relation.get("subject_index", "")).isdigit() else None
        object_id = local_to_id.get(int(raw_relation["object_index"])) if str(raw_relation.get("object_index", "")).isdigit() else None
        target_text = str(raw_relation.get("target_text") or "").strip()
        document_relation = normalized_relation_type in {
            PolicyRelationType.CITES,
            PolicyRelationType.BASED_ON,
            PolicyRelationType.REVISES,
            PolicyRelationType.ABOLISHES,
            PolicyRelationType.REPLACES,
        }
        endpoints_valid = bool(target_text) if document_relation else bool(subject_id and object_id)
        valid = (
            normalized_relation_type is not None
            and _evidence_supported(evidence, source_text)
            and endpoints_valid
        )
        if normalized_relation_type is None:
            reviews.append(ReviewItem(
                review_id=f"review_{uuid.uuid4().hex}", run_id=run_id, clause_id=clause_id,
                issue_type="invalid_relation_type", description="模型返回了不允许的关系类型",
                evidence_text=evidence, payload={"relation": raw_relation},
            ))
            continue
        key = (normalized_relation_type.value, target_text, evidence)
        if key in relation_seen:
            continue
        relation_seen.add(key)
        relation_id = f"relation_{uuid.uuid4().hex}"
        relation = PolicyRelation(
            relation_id=relation_id,
            run_id=run_id,
            clause_id=clause_id,
            relation_type=normalized_relation_type,
            subject_entity_id=subject_id,
            object_entity_id=object_id,
            target_text=target_text,
            evidence_text=evidence,
            confidence=confidence,
            review_status=_entity_status(
                confidence,
                valid,
                requires_manual_review=not include_rule_candidates,
            ),
        )
        relations.append(relation)
        if not valid or confidence < CONFIDENCE_THRESHOLD:
            reviews.append(ReviewItem(
                review_id=f"review_{uuid.uuid4().hex}", run_id=run_id, clause_id=clause_id,
                relation_id=relation_id, issue_type="low_confidence_relation",
                description="关系置信度不足、证据无法定位或端点缺失",
                evidence_text=evidence, payload={"relation": raw_relation, "confidence": confidence},
            ))
    return entities, relations, reviews


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "timeout", "timed out", "connection", "429", "too many requests",
        "500", "502", "503", "504", "temporarily", "rate limit", "operationalerror",
    ))


def _is_stale(item: dict[str, Any]) -> bool:
    value = item.get("started_at")
    if not value:
        return True
    if isinstance(value, datetime):
        started = value
    else:
        try:
            started = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return True
    return datetime.now() - started > timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)


def _eligible(item: dict[str, Any], resume: bool) -> bool:
    status = item.get("status")
    if status in {PolicyExtractionItemStatus.SUCCEEDED.value, PolicyExtractionItemStatus.FAILED.value}:
        return False
    if status == PolicyExtractionItemStatus.PENDING.value:
        return True
    if status == PolicyExtractionItemStatus.RETRY_WAIT.value:
        next_retry = item.get("next_retry_at")
        if not next_retry:
            return True
        if isinstance(next_retry, datetime):
            return next_retry <= datetime.now()
        try:
            return datetime.fromisoformat(str(next_retry)).replace(tzinfo=None) <= datetime.now()
        except ValueError:
            return True
    return status == PolicyExtractionItemStatus.RUNNING.value and resume and _is_stale(item)


def _process_item(item: dict[str, Any], run_id: str) -> None:
    clause = get_policy_clause(item["clause_id"])
    if not clause:
        update_policy_extraction_item(item["item_id"], {
            "status": PolicyExtractionItemStatus.FAILED.value,
            "last_error": "条款不存在",
            "finished_at": datetime.now(),
        })
        return
    parent_text = ""
    if clause.get("parent_clause_id"):
        parent = get_policy_clause(clause["parent_clause_id"])
        parent_text = str((parent or {}).get("raw_text") or "")

    current_attempt = int(item.get("attempt_count") or 0)
    while current_attempt < MAX_ATTEMPTS:
        current_attempt += 1
        update_policy_extraction_item(item["item_id"], {
            "status": PolicyExtractionItemStatus.RUNNING.value,
            "attempt_count": current_attempt,
            "last_error": "",
            "next_retry_at": None,
            "started_at": item.get("started_at") or datetime.now(),
            "finished_at": None,
        })
        delete_clause_outputs(run_id, clause["clause_id"])
        try:
            # 两种模式严格二选一：有密钥时只采纳 LLM 候选，无密钥时才使用规则。
            include_rule_candidates = not _llm_is_configured()
            if include_rule_candidates:
                model_result = {"entities": [], "relations": []}
            else:
                model_result = _call_llm(clause, parent_text)
            entities, relations, reviews = _build_outputs(
                run_id,
                clause,
                model_result,
                include_rule_candidates=include_rule_candidates,
            )
            for entity in entities:
                insert_policy_entity(entity)
            for relation in relations:
                insert_policy_relation(relation)
            for review in reviews:
                insert_review_item(review)
            update_policy_extraction_item(item["item_id"], {
                "status": PolicyExtractionItemStatus.SUCCEEDED.value,
                "last_error": "",
                "finished_at": datetime.now(),
            })
            return
        except Exception as exc:
            error = str(exc)[:4000]
            if _is_transient(exc) and current_attempt < MAX_ATTEMPTS:
                delay = RETRY_BACKOFF_SECONDS[current_attempt - 1]
                update_policy_extraction_item(item["item_id"], {
                    "status": PolicyExtractionItemStatus.RETRY_WAIT.value,
                    "last_error": error,
                    "next_retry_at": datetime.now() + timedelta(seconds=delay),
                })
                time.sleep(delay)
                continue
            update_policy_extraction_item(item["item_id"], {
                "status": PolicyExtractionItemStatus.FAILED.value,
                "last_error": error,
                "finished_at": datetime.now(),
            })
            insert_review_item(ReviewItem(
                review_id=f"review_{uuid.uuid4().hex}", run_id=run_id,
                clause_id=clause["clause_id"], issue_type="extraction_error",
                description=error, evidence_text=clause.get("raw_text", "")[:1000],
            ))
            return


def _refresh_run(run_id: str) -> dict[str, Any]:
    run = get_policy_extraction_run(run_id)
    if not run:
        raise ValueError(f"抽取运行不存在: {run_id}")
    items = get_policy_extraction_items(run_id)
    succeeded = sum(item.get("status") == PolicyExtractionItemStatus.SUCCEEDED.value for item in items)
    failed = sum(item.get("status") == PolicyExtractionItemStatus.FAILED.value for item in items)
    terminal = bool(items) and all(item.get("status") in {
        PolicyExtractionItemStatus.SUCCEEDED.value,
        PolicyExtractionItemStatus.FAILED.value,
    } for item in items) if items else False
    if not items:
        update_policy_extraction_run(run_id, {
            "status": PolicyExtractionStatus.FAILED.value,
            "succeeded_count": 0,
            "failed_count": 0,
            "finished_at": datetime.now(),
        })
    elif terminal:
        status = PolicyExtractionStatus.SUCCEEDED.value if failed == 0 else PolicyExtractionStatus.PARTIAL_FAILED.value
        update_policy_extraction_run(run_id, {
            "status": status,
            "succeeded_count": succeeded,
            "failed_count": failed,
            "finished_at": datetime.now(),
        })
        if succeeded > 0:
            mark_extraction_run_current(run_id, run["batch_id"])
    else:
        update_policy_extraction_run(run_id, {
            "status": PolicyExtractionStatus.RUNNING.value,
            "succeeded_count": succeeded,
            "failed_count": failed,
        })
    result = get_policy_extraction_run(run_id) or run
    return {"run": result, "items": get_policy_extraction_items(run_id)}


def run_policy_extraction(run_id: str, resume: bool = False) -> dict[str, Any]:
    """运行或恢复实体关系抽取。"""
    run = get_policy_extraction_run(run_id)
    if not run:
        raise ValueError(f"抽取运行不存在: {run_id}")
    update_policy_extraction_run(run_id, {
        "status": PolicyExtractionStatus.RUNNING.value,
        "started_at": run.get("started_at") or datetime.now(),
        "finished_at": None,
    })
    for item in get_policy_extraction_items(run_id):
        if item.get("status") == PolicyExtractionItemStatus.RUNNING.value and resume and _is_stale(item):
            update_policy_extraction_item(item["item_id"], {"status": PolicyExtractionItemStatus.PENDING.value})
        if _eligible(item, resume):
            _process_item(item, run_id)
            _refresh_run(run_id)
    return _refresh_run(run_id)


def create_and_run_policy_extraction(
    batch_id: str,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """为批次创建新的抽取版本并立即执行。"""
    if limit is not None and limit < 1:
        raise ValueError("limit 必须大于等于 1")
    versions = current_processing_versions()
    prompt_version = PROMPT_VERSION
    if not _llm_is_configured():
        # 明确记录本次运行未调用大模型，便于后续与 LLM 版本结果区分。
        versions = versions.model_copy(update={"llm_model": "rule-only"})
        prompt_version = RULE_ONLY_PROMPT_VERSION
        print("未配置 LLM_API_KEY，本次制度抽取使用规则模式")
    else:
        print(f"已配置 LLM_API_KEY，本次制度抽取仅使用 LLM 候选，模型: {app_config.llm.model}")
    run_id = create_policy_extraction_run(
        batch_id=batch_id,
        versions=versions,
        prompt_version=prompt_version,
        rule_version=RULE_VERSION,
        schema_version=SCHEMA_VERSION,
        limit=limit,
    )
    return run_policy_extraction(run_id)


def get_policy_extraction_status(run_id: str) -> dict[str, Any]:
    """查看抽取运行和条款任务状态。"""
    run = get_policy_extraction_run(run_id)
    if not run:
        raise ValueError(f"抽取运行不存在: {run_id}")
    return {"run": run, "items": get_policy_extraction_items(run_id)}
