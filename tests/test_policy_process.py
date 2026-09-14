"""报销、差旅、采购流程条款分流的行为测试。"""
from __future__ import annotations

import unittest
import os
import subprocess
import sys
import csv
import tempfile
import json
from unittest.mock import patch
from pathlib import Path

from policy.process import (
    PolicyProcessDecision,
    build_process_label,
    classify_process_clause,
    is_process_label_eligible,
)

# 存储层导入时会校验服务配置；本测试仅通过注入加载器验证筛选逻辑。
for _key, _value in {
    "CLOUDMINERU_API_KEY": "test",
    "PG_HOST": "test",
    "PG_PORT": "5432",
    "PG_USER": "test",
    "PG_PASSWORD": "test",
    "PG_DATABASE": "test",
    "MINIO_ENDPOINT": "test",
    "MINIO_ACCESS_KEY": "test",
    "MINIO_SECRET_KEY": "test",
    "MINIO_BUCKET_PDF": "test",
    "MINIO_BUCKET_IMAGES": "test",
}.items():
    os.environ.setdefault(_key, _value)

from models import ProcessingVersion
from policy.storage import (
    create_policy_extraction_run,
    create_policy_process_run,
    get_eligible_process_clauses,
    get_policy_process_graph_candidates,
)
from policy.process_runner import run_policy_process_classification
from policy.extraction import create_and_run_process_policy_extraction
from policy.quality import (
    build_process_review_rows,
    export_process_graph_review,
    export_process_review,
    summarize_process_reviews,
    write_quality_report,
)


class PolicyProcessTests(unittest.TestCase):
    """验证首期仅把有流程证据的三类事项送入图谱。"""

    def test_classify_reimbursement_material_submission_as_process(self) -> None:
        """防止把明确要求提交材料的报销条款遗漏在流程图谱外。"""
        result = classify_process_clause("出差人员报销差旅费时，应提交发票和审批单。")

        self.assertEqual(result.decision, PolicyProcessDecision.PROCESS)
        self.assertIn("reimbursement", result.domains)
        self.assertIn("travel", result.domains)
        self.assertIn(result.evidence_text, "出差人员报销差旅费时，应提交发票和审批单。")

    def test_classify_procurement_approval_as_process(self) -> None:
        """防止采购申请的审批办理链路被当作一般描述排除。"""
        result = classify_process_clause("采购申请经部门负责人审批后，报采购中心办理。")

        self.assertEqual(result.decision, PolicyProcessDecision.PROCESS)
        self.assertEqual(result.domains, ["procurement"])
        self.assertIn("审批", result.evidence_text)

    def test_selected_domains_limit_process_routing_scope(self) -> None:
        """防止仅试点报销时，差旅关键词也把条款送入流程图谱。"""
        result = classify_process_clause(
            "出差人员应提交行程单办理差旅费报销。",
            selected_domains=["reimbursement"],
        )

        self.assertEqual(result.decision, PolicyProcessDecision.PROCESS)
        self.assertEqual(result.domains, ["reimbursement"])

    def test_classify_general_principle_as_non_process(self) -> None:
        """防止含有报销关键词的总则进入流程实体关系抽取。"""
        result = classify_process_clause("为加强财务管理，规范报销行为，制定本办法。")

        self.assertEqual(result.decision, PolicyProcessDecision.NON_PROCESS)
        self.assertEqual(result.domains, [])
        self.assertTrue(result.reason)

    def test_classify_execution_statement_as_non_process(self) -> None:
        """防止没有办事动作和材料的执行性表述制造无意义图谱节点。"""
        result = classify_process_clause("各单位应认真执行本办法。")

        self.assertEqual(result.decision, PolicyProcessDecision.NON_PROCESS)
        self.assertEqual(result.domains, [])

    def test_only_confirmed_process_label_with_literal_evidence_is_eligible(self) -> None:
        """防止待确认或引用了不存在证据的流程标签绕过分流直接进入图谱。"""
        clause_text = "出差人员报销差旅费时，应提交发票和审批单。"

        self.assertTrue(is_process_label_eligible(clause_text, {
            "decision": "process",
            "domains": ["reimbursement", "travel"],
            "evidence_text": "提交发票和审批单",
            "review_status": "auto_approved",
        }))
        self.assertFalse(is_process_label_eligible(clause_text, {
            "decision": "pending",
            "domains": ["reimbursement"],
            "evidence_text": "提交发票",
            "review_status": "approved",
        }))
        self.assertFalse(is_process_label_eligible(clause_text, {
            "decision": "process",
            "domains": ["reimbursement"],
            "evidence_text": "提交不存在的材料",
            "review_status": "approved",
        }))

    def test_get_eligible_process_clauses_only_returns_confirmed_evidenced_items(self) -> None:
        """防止抽取运行从流程判定运行中取到待确认或无证据的条款。"""
        labels = [
            {
                "clause_id": "c2",
                "decision": "process",
                "domains": ["procurement"],
                "evidence_text": "审批后报采购中心办理",
                "review_status": "approved",
            },
            {
                "clause_id": "c1",
                "decision": "non_process",
                "domains": [],
                "evidence_text": "",
                "review_status": "auto_approved",
            },
            {
                "clause_id": "c3",
                "decision": "process",
                "domains": ["reimbursement"],
                "evidence_text": "不存在的证据",
                "review_status": "approved",
            },
        ]
        clauses = {
            "c1": {"clause_id": "c1", "raw_text": "为加强财务管理，制定本办法。", "sequence_no": 1},
            "c2": {"clause_id": "c2", "raw_text": "采购申请经审批后报采购中心办理。", "sequence_no": 2},
            "c3": {"clause_id": "c3", "raw_text": "报销时应提交发票。", "sequence_no": 3},
        }

        selected = get_eligible_process_clauses(
            "process_run_1",
            label_loader=lambda run_id: labels,
            clause_loader=lambda clause_id: clauses[clause_id],
        )

        self.assertEqual([item["clause_id"] for item in selected], ["c2"])

    def test_build_process_label_marks_only_evidenced_process_as_auto_approved(self) -> None:
        """防止流程运行把待确认条款错误标成自动通过。"""
        confirmed = build_process_label({
            "clause_id": "c1",
            "raw_text": "出差人员报销差旅费时，应提交发票和审批单。",
        })
        pending = build_process_label({
            "clause_id": "c2",
            "raw_text": "差旅费报销范围按照学校有关规定执行。",
        })

        self.assertEqual(confirmed["clause_id"], "c1")
        self.assertEqual(confirmed["decision"], "process")
        self.assertEqual(confirmed["review_status"], "auto_approved")
        self.assertEqual(pending["decision"], "pending")
        self.assertEqual(pending["review_status"], "pending")

    def test_run_policy_process_classification_records_each_clause_decision(self) -> None:
        """防止一个流程判定运行遗漏非流程条款或不更新单条款状态。"""
        labels: list[dict[str, object]] = []
        item_updates: list[tuple[str, dict[str, object]]] = []
        run_updates: list[dict[str, object]] = []
        items = [
            {"item_id": "item_1", "clause_id": "c1", "status": "pending"},
            {"item_id": "item_2", "clause_id": "c2", "status": "pending"},
        ]
        clauses = {
            "c1": {"clause_id": "c1", "raw_text": "报销时应提交发票。"},
            "c2": {"clause_id": "c2", "raw_text": "各单位应认真执行本办法。"},
        }

        with (
            patch("policy.process_runner.get_policy_process_run", return_value={"run_id": "process_run_1"}),
            patch("policy.process_runner.get_policy_process_items", return_value=items),
            patch("policy.process_runner.get_policy_clause", side_effect=lambda clause_id: clauses[clause_id]),
            patch("policy.process_runner.upsert_policy_process_label", side_effect=labels.append),
            patch("policy.process_runner.update_policy_process_item", side_effect=lambda item_id, values: item_updates.append((item_id, values))),
            patch("policy.process_runner.update_policy_process_run", side_effect=lambda run_id, values: run_updates.append(values)),
            patch("policy.process_runner.refresh_policy_process_run", return_value={"run_id": "process_run_1", "status": "succeeded"}),
        ):
            result = run_policy_process_classification("process_run_1")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([label["decision"] for label in labels], ["process", "non_process"])
        self.assertEqual(
            [(item_id, values["status"]) for item_id, values in item_updates],
            [
                ("item_1", "running"),
                ("item_1", "succeeded"),
                ("item_2", "running"),
                ("item_2", "succeeded"),
            ],
        )
        self.assertEqual(run_updates[0]["status"], "running")

    def test_process_runner_uses_selected_domains_saved_on_run(self) -> None:
        """防止报销试点运行恢复时又按全部事项域把差旅条款送入图谱。"""
        labels: list[dict[str, object]] = []
        items = [{"item_id": "item_1", "clause_id": "c1", "status": "pending"}]
        clause = {"clause_id": "c1", "raw_text": "出差人员应提交行程单。"}

        with (
            patch("policy.process_runner.get_policy_process_run", return_value={
                "run_id": "process_run_1",
                "selected_domains": ["reimbursement"],
                "domain_catalog_version": "process-domain-catalog-v1",
            }),
            patch("policy.process_runner.get_policy_process_items", return_value=items),
            patch("policy.process_runner.get_policy_clause", return_value=clause),
            patch("policy.process_runner.upsert_policy_process_label", side_effect=labels.append),
            patch("policy.process_runner.update_policy_process_item"),
            patch("policy.process_runner.update_policy_process_run"),
            patch("policy.process_runner.refresh_policy_process_run", return_value={"run_id": "process_run_1", "status": "succeeded"}),
        ):
            run_policy_process_classification("process_run_1")

        self.assertEqual(labels[0]["decision"], "non_process")
        self.assertEqual(labels[0]["domains"], [])

    def test_create_process_run_persists_selected_domains_and_catalog_version(self) -> None:
        """防止后续审核时无法知道一次流程分流实际使用了哪套事项范围。"""
        commands: list[tuple[str, tuple[object, ...]]] = []
        versions = ProcessingVersion(
            parser_version="parser-v1",
            embedding_model="embedding-v1",
            llm_model="llm-v1",
            pipeline_version="pipeline-v1",
        )

        with (
            patch("policy.storage.ensure_policy_tables"),
            patch("policy.storage.get_policy_documents_for_batch", return_value=[]),
            patch("policy.storage.select_policy_clauses_for_process", return_value=[]),
            patch(
                "policy.storage.storage.relational.execute",
                side_effect=lambda sql, params: commands.append((sql, params)),
            ),
        ):
            create_policy_process_run(
                "batch_1",
                versions,
                selected_domains=["reimbursement"],
                domain_catalog_version="process-domain-catalog-v1",
            )

        insert_sql, insert_params = commands[0]
        self.assertIn("selected_domains", insert_sql)
        self.assertIn("domain_catalog_version", insert_sql)
        self.assertIn('["reimbursement"]', insert_params)
        self.assertIn("process-domain-catalog-v1", insert_params)

    def test_process_extraction_run_uses_only_eligible_process_clauses(self) -> None:
        """防止新流程图谱路径回退到旧的全量条款抽取选择器。"""
        commands: list[tuple[str, tuple[object, ...]]] = []
        versions = ProcessingVersion(
            parser_version="parser-v1",
            embedding_model="embedding-v1",
            llm_model="llm-v1",
            pipeline_version="pipeline-v1",
        )
        process_clause = {"clause_id": "c2", "raw_text": "采购申请经审批后报采购中心办理。"}

        with (
            patch("policy.storage.ensure_policy_tables"),
            patch("policy.storage.get_eligible_process_clauses", return_value=[process_clause]),
            patch("policy.storage.select_policy_clauses_for_extraction", side_effect=AssertionError("不应调用全量选择器")),
            patch("policy.storage.storage.relational.execute", side_effect=lambda sql, params: commands.append((sql, params))),
            patch("policy.storage.storage.relational.update_rows"),
        ):
            run_id = create_policy_extraction_run(
                "batch_1",
                versions,
                process_run_id="process_run_1",
            )

        self.assertTrue(run_id.startswith("policy_run_"))
        self.assertTrue(any("process_run_id" in sql for sql, _ in commands))
        self.assertTrue(any("c2" in params for _, params in commands))

    def test_process_extraction_uses_batch_owned_by_process_run(self) -> None:
        """防止流程抽取命令要求用户重复填写批次或从错误批次取条款。"""
        expected = {"run": {"run_id": "policy_run_1"}}
        with (
            patch("policy.extraction.get_policy_process_run", return_value={"batch_id": "batch_1"}),
            patch("policy.extraction.create_and_run_policy_extraction", return_value=expected) as create_run,
        ):
            result = create_and_run_process_policy_extraction("process_run_1", limit=5)

        self.assertEqual(result, expected)
        create_run.assert_called_once_with("batch_1", limit=5, process_run_id="process_run_1")

    def test_process_graph_candidates_keep_process_decision_for_review_export(self) -> None:
        """防止流程图谱抽检表无法显示条款为何进入 Qwen 抽取。"""
        graph_candidates = [{
            "candidate_type": "entity",
            "candidate_id": "e1",
            "clause_id": "c1",
            "clause": {"clause_id": "c1", "raw_text": "申请人提交发票。"},
        }]
        labels = [{
            "clause_id": "c1",
            "decision": "process",
            "domains": ["reimbursement"],
            "reason": "命中报销事项和提交动作",
            "review_status": "auto_approved",
        }]
        with (
            patch("policy.storage.get_policy_extraction_run", return_value={"process_run_id": "process_run_1"}),
            patch("policy.storage.get_policy_graph_candidates", return_value=graph_candidates),
            patch("policy.storage.get_policy_process_labels", return_value=labels),
        ):
            candidates = get_policy_process_graph_candidates("policy_run_1")

        self.assertEqual(candidates[0]["process_decision"], "process")
        self.assertEqual(candidates[0]["process_domains"], ["reimbursement"])
        self.assertEqual(candidates[0]["process_reason"], "命中报销事项和提交动作")

    def test_cli_core_help_prioritizes_pdf_to_entity_relation_workflow(self) -> None:
        """防止默认帮助重新退化为堆满高级参数的命令清单。"""
        project_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--help"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )

        self.assertIn(b"--process-dir", completed.stdout)
        self.assertIn(b"--extract-policy-batch", completed.stdout)
        self.assertIn(b"--review-policy-run", completed.stdout)
        self.assertIn(b"--export-policy-graph-review", completed.stdout)
        self.assertIn(b"--help-all", completed.stdout)
        self.assertNotIn(b"--classify-policy-process-batch", completed.stdout)
        self.assertNotIn(b"--rebuild-policy-clause-index", completed.stdout)

    def test_cli_full_help_lists_process_graph_commands(self) -> None:
        """防止进阶流程图谱命令在完整帮助中不可发现。"""
        project_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-B", "main.py", "--help-all"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )

        self.assertIn(b"--classify-policy-process-batch", completed.stdout)
        self.assertIn(b"--extract-policy-process-run", completed.stdout)
        self.assertIn(b"--export-policy-process-review", completed.stdout)
        self.assertIn(b"--export-policy-process-graph-review", completed.stdout)
        self.assertIn(b"--policy-domains", completed.stdout)

    def test_build_process_review_rows_keeps_system_decision_and_human_columns_separate(self) -> None:
        """防止流程抽检表覆盖系统判定，导致无法判断是规则错误还是人工结论。"""
        candidates = [{
            "clause_id": "c1",
            "decision": "process",
            "domains": ["reimbursement", "travel"],
            "evidence_text": "提交发票和审批单",
            "reason": "命中事项和提交动作",
            "confidence": 0.96,
            "source": "rule",
            "review_status": "auto_approved",
            "clause": {"raw_text": "出差人员报销差旅费时，应提交发票和审批单。", "page_start": 2, "page_end": 2},
            "document": {"file_id": "file_1", "file_name": "差旅费管理办法.pdf", "title": "差旅费管理办法"},
        }]

        rows = build_process_review_rows("process_run_1", candidates)

        self.assertEqual(rows[0]["系统流程判定"], "process")
        self.assertEqual(rows[0]["业务事项"], "reimbursement, travel")
        self.assertEqual(rows[0]["分流正确"], "")
        self.assertEqual(rows[0]["正确判定"], "")

    def test_export_process_review_writes_excel_form_without_database_write(self) -> None:
        """防止单人审核时无法把流程判定、证据和理由导出为独立表。"""
        candidates = [{
            "clause_id": "c1",
            "decision": "non_process",
            "domains": [],
            "evidence_text": "",
            "reason": "总则",
            "confidence": 0.98,
            "source": "rule",
            "review_status": "auto_approved",
            "clause": {"raw_text": "为加强财务管理，制定本办法。", "page_start": 1, "page_end": 1},
            "document": {"file_id": "file_1", "file_name": "办法.pdf", "title": "办法"},
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = export_process_review(
                "process_run_1",
                temp_dir,
                candidate_loader=lambda run_id: candidates,
            )
            with paths["流程条款抽检"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(rows[0]["系统流程判定"], "non_process")
        self.assertEqual(rows[0]["分流正确"], "")

    def test_export_process_graph_review_writes_only_named_flow_graph_form(self) -> None:
        """防止流程图谱审核结果被导出到通用图谱表而与全量运行混淆。"""
        candidates = [{
            "candidate_type": "entity",
            "candidate_id": "e1",
            "entity_type": "material",
            "name": "发票",
            "evidence_text": "发票",
            "confidence": 0.9,
            "review_status": "pending",
            "process_decision": "process",
            "process_domains": ["reimbursement"],
            "process_reason": "命中报销事项和提交动作",
            "clause": {"clause_id": "c1", "raw_text": "报销时应提交发票。", "page_start": 2, "page_end": 2},
            "document": {"file_id": "file_1", "file_name": "报销办法.pdf", "title": "报销办法"},
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = export_process_graph_review(
                "policy_run_1",
                temp_dir,
                candidate_loader=lambda run_id: candidates,
            )
            with paths["流程图谱抽检"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(paths["流程图谱抽检"].name, "流程图谱抽检.csv")
        self.assertEqual(rows[0]["流程判定状态"], "process")
        self.assertEqual(rows[0]["流程事项域"], "reimbursement")
        self.assertEqual(rows[0]["流程判定理由"], "命中报销事项和提交动作")

    def test_summarize_process_reviews_counts_decisions_and_error_reasons(self) -> None:
        """防止单人回填后无法知道错分比例以及需要修哪条规则。"""
        summary = summarize_process_reviews([
            {"系统流程判定": "process", "分流正确": "对", "正确判定": "", "错分原因": ""},
            {"系统流程判定": "non_process", "分流正确": "错", "正确判定": "process", "错分原因": "总则误判"},
            {"系统流程判定": "pending", "分流正确": "", "正确判定": "", "错分原因": ""},
        ])

        self.assertEqual(summary["总导出数"], 3)
        self.assertEqual(summary["已审核数"], 2)
        self.assertEqual(summary["分流正确数"], 1)
        self.assertEqual(summary["分流错误数"], 1)
        self.assertEqual(summary["系统待确认数"], 1)
        self.assertEqual(summary["错分原因"]["总则误判"], 1)

    def test_quality_report_reads_process_review_form(self) -> None:
        """防止只有流程抽检表时报告错误地要求旧版条款或图谱表。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "流程条款抽检.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["系统流程判定", "分流正确", "正确判定", "错分原因"])
                writer.writeheader()
                writer.writerow({
                    "系统流程判定": "process",
                    "分流正确": "错",
                    "正确判定": "non_process",
                    "错分原因": "事项词误判",
                })
            paths = write_quality_report(temp_dir)
            report = json.loads(paths["JSON"].read_text(encoding="utf-8"))

        self.assertEqual(report["流程分流"]["分流错误数"], 1)
        self.assertEqual(report["流程分流"]["错分原因"]["事项词误判"], 1)


if __name__ == "__main__":
    unittest.main()
