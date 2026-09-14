"""制度条款和图谱候选的人工质检数据工具。"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable


CLAUSE_REVIEW_FIELDS = (
    "文档ID",
    "文件名",
    "制度名称",
    "条款ID",
    "页码",
    "系统层级",
    "父条款ID",
    "章节路径",
    "系统原文",
    "边界正确",
    "层级正确",
    "文字完整",
    "页码正确",
    "错误类型",
    "修正说明",
)

GRAPH_REVIEW_FIELDS = (
    "抽取运行ID",
    "文档ID",
    "文件名",
    "制度名称",
    "条款ID",
    "页码",
    "系统原文",
    "流程判定状态",
    "流程事项域",
    "流程判定理由",
    "候选类型",
    "候选ID",
    "候选内容",
    "原文证据",
    "置信度",
    "当前审核状态",
    "正确性",
    "缺失内容",
    "备注",
)

PROCESS_REVIEW_FIELDS = (
    "流程判定运行ID",
    "文档ID",
    "文件名",
    "制度名称",
    "条款ID",
    "页码",
    "系统原文",
    "系统流程判定",
    "业务事项",
    "判定证据",
    "判定理由",
    "置信度",
    "判定来源",
    "当前审核状态",
    "分流正确",
    "正确判定",
    "错分原因",
    "备注",
)

ERROR_CATALOG_FIELDS = ("错误代码", "类型", "含义")

ERROR_CATALOG_ROWS = (
    {"错误代码": "S01", "类型": "拆分", "含义": "相邻两条被合并"},
    {"错误代码": "S02", "类型": "拆分", "含义": "同一条被错误拆开"},
    {"错误代码": "H01", "类型": "层级", "含义": "条、款、项的父子关系错误"},
    {"错误代码": "T01", "类型": "文本", "含义": "OCR 漏字、乱码或表格文本丢失"},
    {"错误代码": "L01", "类型": "定位", "含义": "页码或坐标无法回到正确原文"},
    {"错误代码": "G01", "类型": "图谱", "含义": "实体或关系没有原文证据"},
    {"错误代码": "G02", "类型": "图谱", "含义": "条款中应抽取的信息被遗漏"},
)

POSITIVE_DECISIONS = frozenset({"对", "是", "正确", "yes", "y", "true", "1"})
NEGATIVE_DECISIONS = frozenset({"错", "否", "错误", "no", "n", "false", "0"})
CLAUSE_CHECK_FIELDS = ("边界正确", "层级正确", "文字完整", "页码正确")


def select_clause_sample(clauses: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按顺序均匀选择条款，覆盖一份制度的开头、中段和末尾。"""
    if limit < 1:
        raise ValueError("每份制度的抽检条款数必须大于等于 1")
    if len(clauses) <= limit:
        return list(clauses)
    if limit == 1:
        return [clauses[0]]

    indexes = [
        round(position * (len(clauses) - 1) / (limit - 1))
        for position in range(limit)
    ]
    return [clauses[index] for index in dict.fromkeys(indexes)]


def build_clause_review_rows(
    documents: list[dict[str, Any]],
    clauses_by_policy: dict[str, list[dict[str, Any]]],
    clauses_per_policy: int,
) -> list[dict[str, str]]:
    """构造预填系统信息、留空人工结论的条款抽检行。"""
    if clauses_per_policy < 1:
        raise ValueError("每份制度的抽检条款数必须大于等于 1")

    rows: list[dict[str, str]] = []
    for document in sorted(documents, key=lambda item: str(item.get("policy_id") or "")):
        policy_id = str(document.get("policy_id") or "")
        clauses = sorted(
            clauses_by_policy.get(policy_id, []),
            key=lambda item: int(item.get("sequence_no") or 0),
        )
        for clause in select_clause_sample(clauses, clauses_per_policy):
            label = str(
                clause.get("item_no")
                or clause.get("paragraph_no")
                or clause.get("article_no")
                or ""
            )
            level = " ".join(part for part in (str(clause.get("level") or ""), label) if part)
            page_start = clause.get("page_start") or 0
            page_end = clause.get("page_end") or page_start
            page_text = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
            rows.append(
                {
                    "文档ID": str(document.get("file_id") or ""),
                    "文件名": str(document.get("file_name") or ""),
                    "制度名称": str(document.get("title") or document.get("file_name") or ""),
                    "条款ID": str(clause.get("clause_id") or ""),
                    "页码": page_text,
                    "系统层级": level,
                    "父条款ID": str(clause.get("parent_clause_id") or ""),
                    "章节路径": " / ".join(str(item) for item in clause.get("chapter_path", []) if item),
                    "系统原文": str(clause.get("raw_text") or ""),
                    "边界正确": "",
                    "层级正确": "",
                    "文字完整": "",
                    "页码正确": "",
                    "错误类型": "",
                    "修正说明": "",
                }
            )
    return rows


def build_graph_review_rows(
    run_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """构造带原文证据的实体和关系候选审核行。"""
    rows: list[dict[str, str]] = []
    for candidate in candidates:
        clause = dict(candidate.get("clause") or {})
        document = dict(candidate.get("document") or {})
        candidate_type = str(candidate.get("candidate_type") or "")
        if candidate_type == "entity":
            candidate_content = (
                f"{candidate.get('entity_type') or 'entity'}: "
                f"{candidate.get('name') or ''}"
            )
        elif candidate_type == "relation":
            subject_name = str(candidate.get("subject_name") or "(未解析主体)")
            object_name = str(
                candidate.get("object_name")
                or candidate.get("target_text")
                or candidate.get("target_policy_id")
                or "(未解析客体)"
            )
            candidate_content = (
                f"{subject_name} --{candidate.get('relation_type') or 'relation'}--> {object_name}"
            )
        else:
            candidate_content = str(candidate.get("candidate_content") or "")

        page_start = clause.get("page_start") or 0
        page_end = clause.get("page_end") or page_start
        page_text = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
        rows.append(
            {
                "抽取运行ID": str(run_id),
                "文档ID": str(document.get("file_id") or ""),
                "文件名": str(document.get("file_name") or ""),
                "制度名称": str(document.get("title") or document.get("file_name") or ""),
                "条款ID": str(clause.get("clause_id") or candidate.get("clause_id") or ""),
                "页码": page_text,
                "系统原文": str(clause.get("raw_text") or ""),
                "流程判定状态": str(candidate.get("process_decision") or ""),
                "流程事项域": _format_domains(candidate.get("process_domains")),
                "流程判定理由": str(candidate.get("process_reason") or ""),
                "候选类型": candidate_type,
                "候选ID": str(candidate.get("candidate_id") or ""),
                "候选内容": candidate_content,
                "原文证据": str(candidate.get("evidence_text") or ""),
                "置信度": str(candidate.get("confidence") or ""),
                "当前审核状态": str(candidate.get("review_status") or ""),
                "正确性": "",
                "缺失内容": "",
                "备注": "",
            }
        )
    return rows


def _format_domains(domains: Any) -> str:
    """兼容 PostgreSQL JSONB 和测试数据中的事项域列表。"""
    if isinstance(domains, list):
        return ", ".join(str(domain) for domain in domains)
    if isinstance(domains, str):
        try:
            parsed = json.loads(domains)
        except json.JSONDecodeError:
            return domains
        return _format_domains(parsed)
    return ""


def build_process_review_rows(
    process_run_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """构造流程条款分流抽检行，系统结论与人工回填列严格分开。"""
    rows: list[dict[str, str]] = []
    for candidate in candidates:
        clause = dict(candidate.get("clause") or {})
        document = dict(candidate.get("document") or {})
        page_start = clause.get("page_start") or 0
        page_end = clause.get("page_end") or page_start
        page_text = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
        rows.append({
            "流程判定运行ID": str(process_run_id),
            "文档ID": str(document.get("file_id") or ""),
            "文件名": str(document.get("file_name") or ""),
            "制度名称": str(document.get("title") or document.get("file_name") or ""),
            "条款ID": str(candidate.get("clause_id") or clause.get("clause_id") or ""),
            "页码": page_text,
            "系统原文": str(clause.get("raw_text") or ""),
            "系统流程判定": str(candidate.get("decision") or ""),
            "业务事项": _format_domains(candidate.get("domains")),
            "判定证据": str(candidate.get("evidence_text") or ""),
            "判定理由": str(candidate.get("reason") or ""),
            "置信度": str(candidate.get("confidence") or ""),
            "判定来源": str(candidate.get("source") or ""),
            "当前审核状态": str(candidate.get("review_status") or ""),
            "分流正确": "",
            "正确判定": "",
            "错分原因": "",
            "备注": "",
        })
    return rows


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """以 Excel 可直接识别的 UTF-8 with BOM 写入 CSV。"""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_review_csvs(
    output_dir: str | Path,
    clause_rows: list[dict[str, Any]],
    graph_rows: list[dict[str, Any]],
) -> dict[str, Path]:
    """写出两类审核表和统一错误字典，返回每个产物的路径。"""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "条款抽检": destination / "条款抽检.csv",
        "图谱抽检": destination / "图谱抽检.csv",
        "错误字典": destination / "错误字典.csv",
    }
    _write_csv(paths["条款抽检"], CLAUSE_REVIEW_FIELDS, clause_rows)
    _write_csv(paths["图谱抽检"], GRAPH_REVIEW_FIELDS, graph_rows)
    _write_csv(paths["错误字典"], ERROR_CATALOG_FIELDS, list(ERROR_CATALOG_ROWS))
    return paths


def export_clause_review(
    batch_id: str,
    output_dir: str | Path,
    clauses_per_policy: int = 10,
    document_loader: Callable[[str], list[dict[str, Any]]] | None = None,
    clause_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Path]:
    """从一个已完成批次导出条款抽检表，不写回制度数据库。"""
    if not str(batch_id or "").strip():
        raise ValueError("批次 ID 不能为空")
    if clauses_per_policy < 1:
        raise ValueError("每份制度的抽检条款数必须大于等于 1")

    if document_loader is None or clause_loader is None:
        from policy.storage import get_policy_clauses, get_policy_documents_for_batch

        document_loader = document_loader or get_policy_documents_for_batch
        clause_loader = clause_loader or get_policy_clauses

    documents = document_loader(batch_id)
    if not documents:
        raise ValueError("该批次没有结构化完成的制度文档，无法导出条款抽检表")
    clauses_by_policy = {
        str(document.get("policy_id") or ""): clause_loader(str(document.get("policy_id") or ""))
        for document in documents
    }
    rows = build_clause_review_rows(documents, clauses_by_policy, clauses_per_policy)
    if not rows:
        raise ValueError("该批次的制度没有可抽检条款，无法导出条款抽检表")

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "条款抽检": destination / "条款抽检.csv",
        "错误字典": destination / "错误字典.csv",
    }
    _write_csv(paths["条款抽检"], CLAUSE_REVIEW_FIELDS, rows)
    _write_csv(paths["错误字典"], ERROR_CATALOG_FIELDS, list(ERROR_CATALOG_ROWS))
    return paths


def export_graph_review(
    run_id: str,
    output_dir: str | Path,
    candidate_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Path]:
    """从一次抽取运行导出实体和关系候选抽检表，不写回审核结论。"""
    if not str(run_id or "").strip():
        raise ValueError("抽取运行 ID 不能为空")
    if candidate_loader is None:
        from policy.storage import get_policy_graph_candidates

        candidate_loader = get_policy_graph_candidates

    candidates = candidate_loader(run_id)
    if not candidates:
        raise ValueError("该抽取运行没有实体或关系候选，无法导出图谱抽检表")
    rows = build_graph_review_rows(run_id, candidates)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "图谱抽检.csv"
    _write_csv(path, GRAPH_REVIEW_FIELDS, rows)
    return {"图谱抽检": path}


def export_process_review(
    process_run_id: str,
    output_dir: str | Path,
    candidate_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Path]:
    """导出流程判定的单人审核表，不写回任何数据库记录。"""
    if not str(process_run_id or "").strip():
        raise ValueError("流程判定运行 ID 不能为空")
    if candidate_loader is None:
        from policy.storage import get_policy_process_review_candidates

        candidate_loader = get_policy_process_review_candidates
    candidates = candidate_loader(process_run_id)
    if not candidates:
        raise ValueError("该流程判定运行没有条款标签，无法导出流程条款抽检表")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "流程条款抽检.csv"
    _write_csv(path, PROCESS_REVIEW_FIELDS, build_process_review_rows(process_run_id, candidates))
    return {"流程条款抽检": path}


def export_process_graph_review(
    run_id: str,
    output_dir: str | Path,
    candidate_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Path]:
    """导出仅由流程条款产生的实体关系候选审核表。"""
    if not str(run_id or "").strip():
        raise ValueError("流程图谱抽取运行 ID 不能为空")
    if candidate_loader is None:
        from policy.storage import get_policy_process_graph_candidates

        candidate_loader = get_policy_process_graph_candidates
    candidates = candidate_loader(run_id)
    if not candidates:
        raise ValueError("该流程图谱抽取运行没有实体或关系候选，无法导出流程图谱抽检表")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "流程图谱抽检.csv"
    _write_csv(path, GRAPH_REVIEW_FIELDS, build_graph_review_rows(run_id, candidates))
    return {"流程图谱抽检": path}


def _normalize_decision(value: Any) -> str:
    """标准化人工填写的判断值，兼容中英文与空白。"""
    return re.sub(r"\s+", "", str(value or "")).lower()


def _is_positive(value: Any) -> bool:
    """判断人工填写是否表示正确或通过。"""
    return _normalize_decision(value) in POSITIVE_DECISIONS


def _is_negative(value: Any) -> bool:
    """判断人工填写是否表示错误或不通过。"""
    return _normalize_decision(value) in NEGATIVE_DECISIONS


def _has_value(value: Any) -> bool:
    """判断单元格是否被人工填写。"""
    return bool(str(value or "").strip())


def _split_error_codes(value: Any) -> list[str]:
    """把一个单元格中的多个错误代码拆成可计数的列表。"""
    return [
        code.strip().upper()
        for code in re.split(r"[，,；;\s]+", str(value or ""))
        if code.strip()
    ]


def summarize_process_reviews(process_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总流程分流的人工回填结果，帮助定位规则误判模式。"""
    reviewed = 0
    correct = 0
    incorrect = 0
    pending = 0
    reasons: Counter[str] = Counter()
    for row in process_rows:
        if str(row.get("系统流程判定") or "") == "pending":
            pending += 1
        decision = row.get("分流正确", "")
        correction = row.get("正确判定", "")
        reason = str(row.get("错分原因") or "").strip()
        if _has_value(decision) or _has_value(correction) or bool(reason):
            reviewed += 1
        if _is_positive(decision):
            correct += 1
        if _is_negative(decision):
            incorrect += 1
        if reason:
            reasons[reason] += 1
    return {
        "总导出数": len(process_rows),
        "已审核数": reviewed,
        "分流正确数": correct,
        "分流错误数": incorrect,
        "系统待确认数": pending,
        "错分原因": dict(sorted(reasons.items())),
    }


def summarize_quality_reviews(
    clause_rows: list[dict[str, Any]],
    graph_rows: list[dict[str, Any]],
    process_rows: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """汇总人工回填的条款和图谱审核结果，不修改原始候选。"""
    clause_reviewed = 0
    clause_fully_correct = 0
    clause_errors = {field: 0 for field in CLAUSE_CHECK_FIELDS}
    error_codes: Counter[str] = Counter()

    for row in clause_rows:
        checks = [row.get(field, "") for field in CLAUSE_CHECK_FIELDS]
        if any(_has_value(value) for value in checks) or _has_value(row.get("错误类型")):
            clause_reviewed += 1
        if checks and all(_is_positive(value) for value in checks):
            clause_fully_correct += 1
        for field, value in zip(CLAUSE_CHECK_FIELDS, checks):
            if _is_negative(value):
                clause_errors[field] += 1
        error_codes.update(_split_error_codes(row.get("错误类型")))

    graph_reviewed = 0
    graph_correct = 0
    graph_errors = 0
    graph_omissions = 0
    for row in graph_rows:
        decision = row.get("正确性", "")
        omission = row.get("缺失内容", "")
        if _has_value(decision) or _has_value(omission):
            graph_reviewed += 1
        if _is_positive(decision):
            graph_correct += 1
        if _is_negative(decision):
            graph_errors += 1
        if _has_value(omission):
            graph_omissions += 1

    return {
        "条款": {
            "总导出数": len(clause_rows),
            "已审核数": clause_reviewed,
            "完全正确": clause_fully_correct,
            "边界错误": clause_errors["边界正确"],
            "层级错误": clause_errors["层级正确"],
            "文字错误": clause_errors["文字完整"],
            "页码错误": clause_errors["页码正确"],
            "错误类型": dict(sorted(error_codes.items())),
        },
        "图谱": {
            "总导出数": len(graph_rows),
            "已审核数": graph_reviewed,
            "正确数": graph_correct,
            "错误数": graph_errors,
            "遗漏数": graph_omissions,
        },
        "流程分流": summarize_process_reviews(process_rows or []),
    }


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    """读取审核者回填的 UTF-8 with BOM CSV。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _build_report_markdown(summary: dict[str, dict[str, Any]]) -> str:
    """把汇总数字转换成适合单人查看的 Markdown 报告。"""
    clauses = summary["条款"]
    graph = summary["图谱"]
    process = summary.get("流程分流", {})
    lines = [
        "# 制度质检报告",
        "",
        "## 条款拆分",
        f"- 导出条款：{clauses['总导出数']}",
        f"- 已审核：{clauses['已审核数']}",
        f"- 完全正确：{clauses['完全正确']}",
        f"- 边界错误：{clauses['边界错误']}",
        f"- 层级错误：{clauses['层级错误']}",
        f"- 文字错误：{clauses['文字错误']}",
        f"- 页码错误：{clauses['页码错误']}",
        "",
        "## 图谱候选",
        f"- 导出候选：{graph['总导出数']}",
        f"- 已审核：{graph['已审核数']}",
        f"- 正确：{graph['正确数']}",
        f"- 错误：{graph['错误数']}",
        f"- 遗漏：{graph['遗漏数']}",
        "",
        "## 流程分流",
        f"- 导出条款：{process.get('总导出数', 0)}",
        f"- 已审核：{process.get('已审核数', 0)}",
        f"- 分流正确：{process.get('分流正确数', 0)}",
        f"- 分流错误：{process.get('分流错误数', 0)}",
        f"- 系统待确认：{process.get('系统待确认数', 0)}",
        "",
        "## 错误类型",
    ]
    error_codes = clauses["错误类型"]
    if error_codes:
        lines.extend(f"- {code}：{count}" for code, count in error_codes.items())
    else:
        lines.append("- 暂无已填写的错误代码")
    process_reasons = process.get("错分原因", {})
    lines.extend(["", "## 流程错分原因"])
    if process_reasons:
        lines.extend(f"- {reason}：{count}" for reason, count in process_reasons.items())
    else:
        lines.append("- 暂无已填写的错分原因")
    return "\n".join(lines) + "\n"


def write_quality_report(output_dir: str | Path) -> dict[str, Path]:
    """读取同一目录中的回填表并生成 Markdown 与 JSON 质量报告。"""
    destination = Path(output_dir)
    clause_path = destination / "条款抽检.csv"
    graph_path = destination / "图谱抽检.csv"
    process_path = destination / "流程条款抽检.csv"
    if not clause_path.exists() and not graph_path.exists() and not process_path.exists():
        raise ValueError("未找到条款抽检.csv、图谱抽检.csv或流程条款抽检.csv，请先导出并回填审核表")

    clause_rows = _read_csv_rows(clause_path) if clause_path.exists() else []
    graph_rows = _read_csv_rows(graph_path) if graph_path.exists() else []
    process_rows = _read_csv_rows(process_path) if process_path.exists() else []
    summary = summarize_quality_reviews(clause_rows, graph_rows, process_rows)
    paths = {
        "Markdown": destination / "质量报告.md",
        "JSON": destination / "质量报告.json",
    }
    paths["Markdown"].write_text(_build_report_markdown(summary), encoding="utf-8")
    paths["JSON"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return paths
