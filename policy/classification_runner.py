"""制度条款规范类型分类运行器。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from config import app_config
from policy.classification import (
    ClauseClassification,
    classify_clause_by_rules,
    parse_model_classification,
)
from policy.extraction import create_llm_client


PROMPT_VERSION = "policy-clause-classification-v1"
RULE_VERSION = "policy-clause-classification-rules-v1"
SCHEMA_VERSION = "policy-clause-classification-schema-v1"
CLASSIFIABLE_LEVELS = frozenset({"article", "paragraph", "item"})


def select_classifiable_clauses(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留正文非空的条、款、项。"""
    return [
        clause
        for clause in clauses
        if str(clause.get("level") or "") in CLASSIFIABLE_LEVELS
        and str(clause.get("raw_text") or clause.get("search_text") or "").strip()
    ]


def _parse_json_object(raw: str) -> dict[str, Any]:
    """兼容模型偶尔返回的 Markdown JSON 代码块。"""
    text = str(raw or "").strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("模型未返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("模型分类结果必须是 JSON 对象")
    return payload


def call_classification_llm(text: str) -> ClauseClassification:
    """调用现有 OpenAI 兼容模型完成规则无法判断的条款。"""
    if not str(app_config.llm.api_key or "").strip():
        raise RuntimeError("LLM_API_KEY 未配置，无法完成待模型判断的条款")
    prompt = f"""请判断以下学校制度条款包含哪些规范类型。
允许标签仅为 obligation（义务）、permission（许可）、prohibition（禁止）、other（其他）。
允许多标签；other 不能和其他标签共存。
对每个分类标签，必须从条款原文中摘录一段连续文字作为 evidence。
不允许留空、不允许改写、不允许拼接。
- obligation：必须有义务主体 + "应当/必须/不得" + 具体行为。仅有目的、宗旨、依据的不算。
- permission：出现"可以/有权/允许"，或明确授权某主体做某事。
- prohibition：出现"不得/禁止/严禁"。
- other：立法目的、制定依据、定义、程序性规定、组织性规定等。
只输出 JSON：{{"labels":[],"evidence":{{}},"reason":"","confidence":0.0}}

条款原文：
{text}"""
    client = create_llm_client(app_config.llm.api_url, app_config.llm.api_key)
    try:
        response = client.chat.completions.create(
            model=app_config.llm.model,
            messages=[
                {"role": "system", "content": "你是制度条款分类器，只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=800,
        )
        raw = response.choices[0].message.content or ""
        return parse_model_classification(text, _parse_json_object(raw))
    finally:
        client.close()


def classify_policy_clause(clause: dict[str, Any]) -> ClauseClassification:
    """规则优先，只有没有明确规范词时才调用模型。"""
    text = str(clause.get("raw_text") or clause.get("search_text") or "")
    rule_result = classify_clause_by_rules(text)
    if not rule_result.needs_model:
        return rule_result
    return call_classification_llm(text)


def run_policy_clause_classification(run_id: str, resume: bool = False) -> dict[str, Any]:
    """执行或恢复一次条款分类运行。"""
    from policy.storage import (
        get_policy_classification_items,
        get_policy_classification_run,
        get_policy_clause,
        refresh_policy_classification_run,
        update_policy_classification_item,
        update_policy_classification_run,
        upsert_policy_classification_result,
    )

    run = get_policy_classification_run(run_id)
    if not run:
        raise ValueError(f"分类运行不存在: {run_id}")
    update_policy_classification_run(run_id, {
        "status": "running",
        "started_at": run.get("started_at") or datetime.now(),
        "finished_at": None,
    })
    for item in get_policy_classification_items(run_id):
        status = str(item.get("status") or "pending")
        if status != "pending" and not (resume and status == "failed"):
            continue
        item_id = str(item["item_id"])
        update_policy_classification_item(item_id, {
            "status": "running", "started_at": datetime.now(), "last_error": "",
        })
        try:
            clause = get_policy_clause(str(item["clause_id"]))
            if not clause:
                raise ValueError(f"条款不存在: {item['clause_id']}")
            result = classify_policy_clause(clause)
            upsert_policy_classification_result({
                "run_id": run_id,
                "clause_id": clause["clause_id"],
                "labels": result.labels,
                "evidence": result.evidence,
                "reason": result.reason,
                "confidence": result.confidence,
                "source": result.source,
            })
            update_policy_classification_item(item_id, {
                "status": "succeeded", "finished_at": datetime.now(),
            })
        except Exception as exc:
            update_policy_classification_item(item_id, {
                "status": "failed", "last_error": str(exc)[:2000], "finished_at": datetime.now(),
            })
    return refresh_policy_classification_run(run_id)


def create_and_run_policy_clause_classification(batch_id: str) -> dict[str, Any]:
    """创建并立即运行一个新的条款分类版本。"""
    from batch_processor import current_processing_versions
    from policy.storage import create_policy_classification_run

    run_id = create_policy_classification_run(
        batch_id,
        current_processing_versions(),
        PROMPT_VERSION,
        RULE_VERSION,
        SCHEMA_VERSION,
    )
    return run_policy_clause_classification(run_id)


def get_policy_clause_classification_status(run_id: str) -> dict[str, Any]:
    from policy.storage import get_policy_classification_items, get_policy_classification_run

    run = get_policy_classification_run(run_id)
    if not run:
        raise ValueError(f"分类运行不存在: {run_id}")
    return {"run": run, "items": get_policy_classification_items(run_id)}
