"""制度知识 V2 的模型调用与可恢复运行器。"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable

from openai import APIConnectionError

from config import app_config
from policy.extraction import create_llm_client
from policy.knowledge_v2 import (
    build_assertion_prompt,
    build_clause_context,
    build_explicit_matters,
    extract_document_type_item,
    is_substantive_clause,
    normalize_assertions,
)


PROMPT_VERSION = "policy-knowledge-v2-prompt-v1"
RULE_VERSION = "policy-knowledge-v2-rules-v2"
SCHEMA_VERSION = "policy-knowledge-v2-schema-v2"


def parse_assertion_response(raw: str) -> dict[str, Any]:
    """解析模型 JSON，并要求根节点包含断言数组。"""
    text = str(raw or "").strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("模型未返回合法 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("assertions"), list):
        raise ValueError("模型结果必须包含 assertions 数组")
    return payload


def call_assertion_llm(prompt: str) -> dict[str, Any]:
    """调用项目现有 OpenAI 兼容模型，格式错误时仅修复一次 JSON。"""
    if not str(app_config.llm.api_key or "").strip():
        raise RuntimeError("LLM_API_KEY 未配置，无法执行制度知识 V2 抽取")
    client = create_llm_client(app_config.llm.api_url, app_config.llm.api_key)
    try:
        response = client.chat.completions.create(
            model=app_config.llm.model,
            messages=[
                {"role": "system", "content": "你是学校规章制度断言抽取器，只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=3000,
        )
        raw = str(response.choices[0].message.content or "")
        try:
            return parse_assertion_response(raw)
        except ValueError:
            repair = client.chat.completions.create(
                model=app_config.llm.model,
                messages=[
                    {"role": "system", "content": "只修复 JSON 格式，不增加、删除或改写事实。"},
                    {"role": "user", "content": raw},
                ],
                temperature=0.0,
                max_tokens=3000,
            )
            return parse_assertion_response(str(repair.choices[0].message.content or ""))
    finally:
        client.close()


def build_batch_assertion_prompt(contexts: list[dict[str, Any]]) -> str:
    """构造多目标提示词，公共规则只发送一次以降低本地模型调用开销。"""
    targets = [
        {"target_clause_id": str((context.get("current") or {}).get("clause_id") or ""), "context": context}
        for context in contexts
    ]
    return f"""从学校规章制度中批量抽取制度断言，只输出合法 JSON，不解释。
每个输入必须返回且只返回一个对应 result，target_clause_id 必须原样复制。
断言类型仅限 action, condition, definition, scope, standard, status, reference；模态仅限 required, permitted, prohibited, factual。
禁止事项也必须使用 kind=action、modality=prohibited；不能把 prohibited 写入 kind。
“某部门负责管理”是 action，不是 scope；“本办法适用于……”才是 scope；定义句“某概念包括……”须填写 predicate.text="包括"。
所有类型的 predicate.text 都不能为空，且必须是证据原文中的连续词语。无法确定时该目标返回空 assertions。
每条断言必须给出连续原文 evidence（clause_id、text、左闭右开 start/end）。action 必须包含 subject 和 predicate。
可以从父条款补全省略主体，但必须标记 inferred_from_context=true 和 source_clause_id。不得拼接或改写证据。
qualifiers 固定为 conditions、materials、deadline、amount、location、result、exceptions，没有内容时使用空数组或 null。
每条断言必须使用 kind、subject、predicate、object、receiver、modality、qualifiers、evidence 字段。
kind 不能写成 assert_type 或 assertion_type；evidence 必须是对象，不能是数组；predicate 必须是含 text 的对象。
断言示例：{{"kind":"action","subject":{{"text":"申请人","type":"person","inferred_from_context":false}},"predicate":{{"text":"提交","normalized":"提交"}},"object":{{"text":"申请表","type":"material"}},"receiver":null,"modality":"required","qualifiers":{{"conditions":[],"materials":[],"deadline":null,"amount":null,"location":null,"result":null,"exceptions":[]}},"evidence":{{"clause_id":"目标条款ID","text":"原文中连续片段","start":0,"end":8}}}}。
start/end 是证据 text 在 evidence.clause_id 原文里的字符位置，不是提示词或整批输入中的位置。
输出：{{"results":[{{"target_clause_id":"...","assertions":[...]}}]}}。
输入：{json.dumps(targets, ensure_ascii=False)}"""


def parse_batch_assertion_response(payload: dict[str, Any], target_ids: list[str]) -> dict[str, list[Any]]:
    """校验批量响应完整且目标不重不漏。"""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("批量模型结果必须包含 results 数组")
    mapped: dict[str, list[Any]] = {}
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("assertions"), list):
            raise ValueError("批量模型 result 必须包含 assertions 数组")
        target_id = str(result.get("target_clause_id") or "")
        if target_id not in target_ids or target_id in mapped:
            raise ValueError("批量模型返回了未知或重复的 target_clause_id")
        mapped[target_id] = result["assertions"]
    if set(mapped) != set(target_ids):
        raise ValueError("批量模型未返回全部目标条款")
    return mapped


def call_assertion_batch_llm(prompt: str) -> dict[str, Any]:
    """调用一次本地模型处理多个条款；格式错误时仍只修复一次。"""
    if not str(app_config.llm.api_key or "").strip():
        raise RuntimeError("LLM_API_KEY 未配置，无法执行制度知识 V2 抽取")
    client = create_llm_client(app_config.llm.api_url, app_config.llm.api_key)
    try:
        response = client.chat.completions.create(
            model=app_config.llm.model,
            messages=[
                {"role": "system", "content": "你是学校规章制度批量断言抽取器，只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=int(getattr(app_config.llm, "knowledge_v2_max_tokens", 2400)),
        )
        raw = str(response.choices[0].message.content or "")
        try:
            return json.loads(re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw.strip(), re.DOTALL | re.IGNORECASE).group(1)) if raw.strip().startswith("```") else json.loads(raw)
        except (AttributeError, json.JSONDecodeError):
            repair = client.chat.completions.create(
                model=app_config.llm.model,
                messages=[
                    {"role": "system", "content": "只修复 JSON 格式，不增加、删除或改写事实。"},
                    {"role": "user", "content": raw},
                ],
                temperature=0.0,
                max_tokens=int(getattr(app_config.llm, "knowledge_v2_max_tokens", 2400)),
            )
            repaired = str(repair.choices[0].message.content or "").strip()
            match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", repaired, re.DOTALL | re.IGNORECASE)
            return json.loads(match.group(1) if match else repaired)
    finally:
        client.close()


def extract_clause_assertions(
    clause: dict[str, Any],
    clauses: list[dict[str, Any]],
    document: dict[str, Any],
    extractor: Callable[[str], dict[str, Any]] = call_assertion_llm,
    ancestor_depth: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """抽取单条制度正文，并把合法与非法候选严格分离。"""
    if not is_substantive_clause(clause):
        return [], []
    by_id = {str(item.get("clause_id") or ""): item for item in clauses}
    rule_assertion = extract_document_type_item(clause, by_id.get(str(clause.get("parent_clause_id") or "")))
    if rule_assertion:
        return normalize_assertions([rule_assertion], {str(clause["clause_id"]): clause, str(clause["parent_clause_id"]): by_id[str(clause["parent_clause_id"])]})
    context = build_clause_context(clause, clauses, document, ancestor_depth=app_config.llm.knowledge_v2_ancestor_depth if ancestor_depth is None else ancestor_depth)
    payload = extractor(build_assertion_prompt(context))
    if not isinstance(payload, dict) or not isinstance(payload.get("assertions"), list):
        raise ValueError("模型结果必须包含 assertions 数组")
    context_clauses = _context_clause_map(context, clauses)
    return normalize_assertions(payload["assertions"], context_clauses)


def _context_clause_map(context: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """证据校验仅允许提示词中实际提供的条款。"""
    parts = [context.get(key) for key in ("previous", "current", "next")]
    parts.extend(context.get("ancestors") or [])
    context_ids = {str(part.get("clause_id") or "") for part in parts if isinstance(part, dict)}
    return {str(item.get("clause_id") or ""): item for item in clauses if str(item.get("clause_id") or "") in context_ids}


def retry_clause_assertions_with_feedback(
    request: tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]],
    failed_result: tuple[list[dict[str, Any]], list[dict[str, Any]]],
    extractor: Callable[[str], dict[str, Any]] = call_assertion_llm,
    ancestor_depth: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把明确的校验错误反馈给模型，重新抽取当前条款。"""
    clause, clauses, document = request
    context = build_clause_context(clause, clauses, document, ancestor_depth=app_config.llm.knowledge_v2_ancestor_depth if ancestor_depth is None else ancestor_depth)
    failures = [
        {"error": str(item.get("error") or ""), "model_output": item.get("payload")}
        for item in failed_result[1]
    ]
    prompt = build_assertion_prompt(context) + "\n上次输出未通过校验，请从当前条款重新抽取，不要机械改名或补写原文没有的事实。失败详情：" + json.dumps(failures, ensure_ascii=False)
    payload = extractor(prompt)
    if not isinstance(payload, dict) or not isinstance(payload.get("assertions"), list):
        raise ValueError("模型结果必须包含 assertions 数组")
    context_clauses = _context_clause_map(context, clauses)
    return normalize_assertions(payload["assertions"], context_clauses)


def extract_clause_batch_assertions(
    requests: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]],
    extractor: Callable[[str], dict[str, Any]] = call_assertion_batch_llm,
    ancestor_depth: int | None = None,
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """一次模型请求抽取多个目标条款，逐目标独立执行严格校验。"""
    output: list[tuple[list[dict[str, Any]], list[dict[str, Any]]] | None] = [None] * len(requests)
    remaining: list[tuple[int, tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]]] = []
    for index, request in enumerate(requests):
        clause, clauses, _ = request
        by_id = {str(item.get("clause_id") or ""): item for item in clauses}
        rule_assertion = extract_document_type_item(clause, by_id.get(str(clause.get("parent_clause_id") or "")))
        if rule_assertion:
            output[index] = normalize_assertions([rule_assertion], {str(clause["clause_id"]): clause, str(clause["parent_clause_id"]): by_id[str(clause["parent_clause_id"])]})
        else:
            remaining.append((index, request))
    if not remaining:
        return [item for item in output if item is not None]
    depth = app_config.llm.knowledge_v2_ancestor_depth if ancestor_depth is None else ancestor_depth
    contexts = [build_clause_context(*request, ancestor_depth=depth) for _, request in remaining]
    target_ids = [str(request[0].get("clause_id") or "") for _, request in remaining]
    mapped = parse_batch_assertion_response(extractor(build_batch_assertion_prompt(contexts)), target_ids)
    for (index, (clause, clauses, _)), context in zip(remaining, contexts):
        context_clauses = _context_clause_map(context, clauses)
        output[index] = normalize_assertions(mapped[str(clause["clause_id"])], context_clauses)
    return [item for item in output if item is not None]


def extract_batch_with_fallback(
    requests: list[Any], batch_extractor: Callable[[list[Any]], list[Any]], single_extractor: Callable[[Any], Any],
) -> list[Any]:
    """批量响应失败时逐条回退，避免整批任务丢失。"""
    try:
        return batch_extractor(requests)
    except APIConnectionError:
        # 服务不可达时逐条回退只会重复等待同一个故障。
        raise
    except Exception:
        results: list[Any] = []
        for request in requests:
            try:
                results.append(single_extractor(request))
            except APIConnectionError:
                raise
            except Exception as exc:
                results.append(exc)
        return results


def retry_invalid_batch_items(
    requests: list[Any], results: list[Any], single_extractor: Callable[[Any, Any], Any],
) -> list[Any]:
    """只对格式无效的批量结果单条复试；复试仍无效则保留原失败记录。"""
    improved = list(results)
    for index, (request, result) in enumerate(zip(requests, results)):
        if not isinstance(result, tuple) or len(result) != 2 or not result[1]:
            continue
        try:
            retried = single_extractor(request, result)
        except APIConnectionError:
            raise
        except Exception:
            continue
        if isinstance(retried, tuple) and len(retried) == 2 and retried[0] and not retried[1]:
            improved[index] = retried
    return improved


def format_extraction_progress(
    done: int, total: int, elapsed_seconds: float, processed_this_session: int | None = None,
) -> str:
    """生成可直接观察的运行进度、平均耗时和剩余时间。"""
    average = elapsed_seconds / max(processed_this_session if processed_this_session is not None else done, 1)
    remaining = round(max(total - done, 0) * average)
    return f"V2 抽取进度 [{done}/{total}]，平均 {average:.1f} 秒/条，预计剩余 {remaining} 秒"


def should_process_knowledge_item(item: dict[str, Any], resume: bool) -> bool:
    """判断任务是否应执行；恢复时也接管进程中断遗留的 running 任务。"""
    status = str(item.get("status") or "pending")
    return status == "pending" or (resume and status in {"failed", "running"})


def run_knowledge_extraction_v2(run_id: str, resume: bool = False) -> dict[str, Any]:
    """执行或恢复 V2 抽取，并在逐条抽取后生成明确结构事项。"""
    from policy import storage

    run = storage.get_policy_knowledge_run_v2(run_id)
    if not run:
        raise ValueError(f"V2 抽取运行不存在: {run_id}")
    storage.update_policy_knowledge_run_v2(run_id, {
        "status": "running", "started_at": run.get("started_at") or datetime.now(), "finished_at": None,
    })
    documents = {
        str(item.get("policy_id") or ""): item
        for item in storage.get_policy_documents_for_batch(str(run["batch_id"]))
    }
    clauses_by_policy = {
        policy_id: storage.get_policy_clauses(policy_id) for policy_id in documents
    }
    items = storage.get_policy_knowledge_items_v2(run_id)
    pending_items = [item for item in items if should_process_knowledge_item(item, resume)]
    total = len(items)
    done = total - len(pending_items)
    processed_this_session = 0
    started = time.monotonic()
    tasks_by_policy: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for item in pending_items:
        clause = storage.get_policy_clause(str(item["clause_id"]))
        if not clause:
            storage.update_policy_knowledge_item_v2(str(item["item_id"]), {
                "status": "failed", "last_error": "V2 抽取条款不存在", "finished_at": datetime.now(),
            })
            done += 1
            processed_this_session += 1
            print(format_extraction_progress(done, total, time.monotonic() - started, processed_this_session), flush=True)
            continue
        tasks_by_policy.setdefault(str(clause.get("policy_id") or ""), []).append((item, clause))

    batch_size = int(getattr(app_config.llm, "knowledge_v2_batch_size", 4))
    ancestor_depth = int(run.get("context_ancestor_depth", 1))
    for policy_id, policy_tasks in tasks_by_policy.items():
        for offset in range(0, len(policy_tasks), batch_size):
            batch = policy_tasks[offset:offset + batch_size]
            requests = [
                (clause, clauses_by_policy[policy_id], documents[policy_id])
                for _, clause in batch
            ]
            for item, _ in batch:
                storage.update_policy_knowledge_item_v2(str(item["item_id"]), {
                    "status": "running", "started_at": datetime.now(), "last_error": "",
                })
            try:
                results = extract_batch_with_fallback(
                    requests,
                    lambda batch_requests: extract_clause_batch_assertions(batch_requests, ancestor_depth=ancestor_depth),
                    lambda request: extract_clause_assertions(*request, ancestor_depth=ancestor_depth),
                )
                if len(batch) > 1:
                    results = retry_invalid_batch_items(
                        requests, results,
                        lambda request, result: retry_clause_assertions_with_feedback(request, result, ancestor_depth=ancestor_depth),
                    )
            except APIConnectionError as exc:
                for item, _ in batch:
                    storage.update_policy_knowledge_item_v2(str(item["item_id"]), {
                        "status": "pending", "last_error": "模型服务连接失败", "finished_at": None,
                    })
                current_items = storage.get_policy_knowledge_items_v2(run_id)
                storage.update_policy_knowledge_run_v2(run_id, {
                    "status": "connection_failed", "finished_at": datetime.now(),
                    "succeeded_count": sum(item.get("status") == "succeeded" for item in current_items),
                    "failed_count": sum(item.get("status") == "failed" for item in current_items),
                })
                raise RuntimeError(
                    f"模型服务连接失败：{app_config.llm.api_url}。已停止 V2 运行 {run_id}，"
                    f"请检查模型服务，恢复后执行 python main.py --resume-policy-knowledge-v2 {run_id}"
                ) from exc
            for (item, clause), result in zip(batch, results):
                item_id = str(item["item_id"])
                if isinstance(result, Exception):
                    storage.update_policy_knowledge_item_v2(item_id, {
                        "status": "failed", "last_error": str(result)[:2000], "finished_at": datetime.now(),
                    })
                else:
                    valid, invalid = result
                    storage.replace_policy_assertions_v2(run_id, str(clause["clause_id"]), valid, invalid)
                    storage.update_policy_knowledge_item_v2(item_id, {
                        "status": "succeeded", "finished_at": datetime.now(),
                    })
                done += 1
                processed_this_session += 1
                print(format_extraction_progress(done, total, time.monotonic() - started, processed_this_session), flush=True)
    for policy_id, clauses in clauses_by_policy.items():
        assertions = storage.get_policy_assertions_v2(run_id, policy_id=policy_id)
        matters = build_explicit_matters(run_id, policy_id, clauses, assertions)
        storage.replace_policy_matters_v2(run_id, policy_id, matters)
    refreshed = storage.refresh_policy_knowledge_run_v2(run_id)
    if str(refreshed.get("status") or "") == "succeeded" and not str(run.get("schema_version") or "").endswith("-sample"):
        from policy.knowledge_retrieval_v2 import sync_assertion_index_v2

        try:
            sync_assertion_index_v2(run_id)
        except Exception:
            # 断言索引属于问答增强，失败不能抹去已持久化的抽取结果。
            pass
    return refreshed


def create_and_run_knowledge_extraction_v2(batch_id: str, sample_path: str | None = None) -> dict[str, Any]:
    from batch_processor import current_processing_versions
    from policy.storage import create_policy_knowledge_run_v2

    run_id = create_policy_knowledge_run_v2(
        batch_id, current_processing_versions(), PROMPT_VERSION, RULE_VERSION, SCHEMA_VERSION,
        sample_path=sample_path, context_ancestor_depth=app_config.llm.knowledge_v2_ancestor_depth,
    )
    print(f"已创建 V2 抽取运行: {run_id}", flush=True)
    return run_knowledge_extraction_v2(run_id)
