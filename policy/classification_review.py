"""条款分类 CSV 导出、回填和质量统计。"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


LABEL_ZH = {
    "obligation": "义务",
    "permission": "许可",
    "prohibition": "禁止",
    "other": "其他",
}
ZH_LABEL = {value: key for key, value in LABEL_ZH.items()}
LABEL_ORDER = ("obligation", "permission", "prohibition", "other")
REVIEW_FIELDS = (
    "分类运行ID", "文档ID", "文件名", "制度名称", "条款ID", "条款号", "页码",
    "系统原文", "系统标签", "判定依据", "判定理由", "置信度", "判定来源",
    "是否正确", "修正标签", "备注",
)
POSITIVE = frozenset({"是", "正确", "对", "yes", "y", "true", "1"})
NEGATIVE = frozenset({"否", "错误", "错", "no", "n", "false", "0"})


def format_labels(labels: list[str]) -> str:
    """按固定顺序把内部标签转换为便于人工阅读的中文。"""
    values = set(labels)
    return "、".join(LABEL_ZH[label] for label in LABEL_ORDER if label in values)


def parse_reviewed_labels(value: str) -> list[str]:
    """解析 CSV 中用顿号或逗号分隔的中文标签。"""
    parts = [part.strip() for part in str(value or "").replace(",", "、").replace("，", "、").split("、") if part.strip()]
    if not parts:
        raise ValueError("修正标签不能为空")
    unknown = [part for part in parts if part not in ZH_LABEL]
    if unknown:
        raise ValueError(f"未知中文标签: {', '.join(unknown)}")
    labels = [label for label in LABEL_ORDER if LABEL_ZH[label] in parts]
    if "other" in labels and len(labels) > 1:
        raise ValueError("其他不能与义务、许可或禁止同时出现")
    return labels


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def effective_labels(result: dict[str, Any]) -> list[str]:
    """人工已复核时返回人工标签，否则返回系统标签。"""
    if str(result.get("review_status") or "") == "approved":
        reviewed = _as_json(result.get("reviewed_labels"))
        if isinstance(reviewed, list) and reviewed:
            return [label for label in LABEL_ORDER if label in reviewed]
    labels = _as_json(result.get("labels"))
    return [label for label in LABEL_ORDER if isinstance(labels, list) and label in labels]


def build_review_rows(run_id: str, candidates: list[dict[str, Any]], limit: int = 50) -> list[dict[str, str]]:
    """构造最多五十条分类复核记录。"""
    rows: list[dict[str, str]] = []
    for candidate in candidates[:limit]:
        clause = dict(candidate.get("clause") or {})
        document = dict(candidate.get("document") or {})
        page_start = int(clause.get("page_start") or 0)
        page_end = int(clause.get("page_end") or page_start)
        labels = list(_as_json(candidate.get("labels")) or [])
        evidence = dict(_as_json(candidate.get("evidence")) or {})
        clause_no = str(clause.get("item_no") or clause.get("paragraph_no") or clause.get("article_no") or "")
        rows.append({
            "分类运行ID": run_id,
            "文档ID": str(document.get("file_id") or ""),
            "文件名": str(document.get("file_name") or ""),
            "制度名称": str(document.get("title") or document.get("file_name") or ""),
            "条款ID": str(clause.get("clause_id") or candidate.get("clause_id") or ""),
            "条款号": clause_no,
            "页码": str(page_start) if page_start == page_end else f"{page_start}-{page_end}",
            "系统原文": str(clause.get("raw_text") or ""),
            "系统标签": format_labels(labels),
            "判定依据": json.dumps({LABEL_ZH[key]: value for key, value in evidence.items()}, ensure_ascii=False),
            "判定理由": str(candidate.get("reason") or ""),
            "置信度": str(candidate.get("confidence") or ""),
            "判定来源": str(candidate.get("source") or ""),
            "是否正确": "",
            "修正标签": "",
            "备注": "",
        })
    return rows


def export_classification_review(run_id: str, output_dir: str | Path) -> Path:
    """导出 UTF-8 BOM CSV，便于 Excel 直接打开。"""
    from policy.storage import get_policy_classification_review_rows

    rows = build_review_rows(run_id, get_policy_classification_review_rows(run_id))
    path = Path(output_dir) / "条款分类复核.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def calculate_classification_metrics(
    pairs: list[tuple[set[str], set[str]]],
) -> dict[str, Any]:
    """计算多标签完全一致率及三类规范标签的精确率、召回率。"""
    exact = sum(predicted == actual for predicted, actual in pairs)
    label_metrics: dict[str, dict[str, float | int]] = {}
    for label in ("obligation", "permission", "prohibition"):
        true_positive = sum(label in predicted and label in actual for predicted, actual in pairs)
        predicted_positive = sum(label in predicted for predicted, _ in pairs)
        actual_positive = sum(label in actual for _, actual in pairs)
        label_metrics[label] = {
            "precision": true_positive / predicted_positive if predicted_positive else 0.0,
            "recall": true_positive / actual_positive if actual_positive else 0.0,
            "support": actual_positive,
        }
    return {
        "reviewed_count": len(pairs),
        "exact_match_rate": exact / len(pairs) if pairs else 0.0,
        "labels": label_metrics,
    }


def import_classification_review(csv_path: str | Path, reviewer: str) -> dict[str, Any]:
    """校验并幂等导入人工标签，返回本次复核质量统计。"""
    from policy.storage import review_policy_classification_result
    from storage_adapter import storage

    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("审核人不能为空")
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    prepared: list[tuple[int, dict[str, str], list[str], set[str]]] = []
    run_ids: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        decision = str(row.get("是否正确") or "").strip().lower()
        if not decision:
            continue
        run_id = str(row.get("分类运行ID") or "").strip()
        clause_id = str(row.get("条款ID") or "").strip()
        if not run_id or not clause_id:
            raise ValueError(f"第 {line_no} 行缺少分类运行ID或条款ID")
        run_ids.add(run_id)
        if decision in POSITIVE:
            reviewed = parse_reviewed_labels(str(row.get("系统标签") or ""))
        elif decision in NEGATIVE:
            reviewed = parse_reviewed_labels(str(row.get("修正标签") or ""))
        else:
            raise ValueError(f"第 {line_no} 行‘是否正确’只能填写是或否")
        predicted = set(parse_reviewed_labels(str(row.get("系统标签") or "")))
        prepared.append((line_no, row, reviewed, predicted))
    if len(run_ids) > 1:
        raise ValueError("一份复核表不能包含多个分类运行ID")
    with storage.relational.transaction():
        for line_no, row, reviewed, _ in prepared:
            try:
                review_policy_classification_result(
                    str(row["分类运行ID"]), str(row["条款ID"]), reviewed,
                    reviewer, str(row.get("备注") or ""),
                )
            except ValueError as exc:
                raise ValueError(f"第 {line_no} 行导入失败: {exc}") from exc
    metrics = calculate_classification_metrics([
        (predicted, set(reviewed)) for _, _, reviewed, predicted in prepared
    ])
    report_path = path.with_name("条款分类质量报告.json")
    report_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**metrics, "report_path": str(report_path)}
