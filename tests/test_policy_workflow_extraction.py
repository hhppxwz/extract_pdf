"""办事流程图谱抽取与人工修正的行为测试。"""
from __future__ import annotations

import os
import json
import tempfile
import unittest
from unittest.mock import patch

# 存储层导入时会校验服务配置；本测试只验证纯抽取和审核行为。
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

from config import app_config
from models import PolicyReviewStatus
from policy.extraction import (
    _build_outputs,
    build_extraction_prompt,
    create_llm_client,
    is_local_llm_endpoint,
)
from policy.reviewer import build_manual_annotation, review_policy_candidate
from policy.workflow_graph import build_policy_workflow_graph, export_policy_workflow_graph


class PolicyWorkflowExtractionTests(unittest.TestCase):
    """验证办事步骤语义可被抽取、审核且保留原文证据。"""

    def test_workflow_entity_and_relation_types_are_accepted_as_pending_candidates(self) -> None:
        """防止模型识别到步骤、角色和条件后因旧白名单被静默丢弃。"""
        clause = {
            "clause_id": "c1",
            "raw_text": "申请人提交报销单，财务处审核后支付；材料不全的，退回申请人补充。",
        }
        model_result = {
            "entities": [
                {"type": "process", "name": "报销流程", "evidence_text": "提交报销单，财务处审核后支付", "confidence": 0.91},
                {"type": "step", "name": "提交报销单", "evidence_text": "提交报销单", "confidence": 0.94},
                {"type": "role", "name": "申请人", "evidence_text": "申请人", "confidence": 0.97},
                {"type": "condition", "name": "材料不全", "evidence_text": "材料不全", "confidence": 0.93},
                {"type": "outcome", "name": "退回申请人补充", "evidence_text": "退回申请人补充", "confidence": 0.92},
            ],
            "relations": [
                {"type": "has_step", "subject_index": 0, "object_index": 1, "evidence_text": "提交报销单", "confidence": 0.9},
                {"type": "performed_by", "subject_index": 1, "object_index": 2, "evidence_text": "申请人提交报销单", "confidence": 0.9},
                {"type": "routes_to", "subject_index": 3, "object_index": 4, "evidence_text": "材料不全的，退回申请人补充", "confidence": 0.9},
            ],
        }

        entities, relations, _reviews = _build_outputs(
            "run_1",
            clause,
            model_result,
            include_rule_candidates=False,
        )

        self.assertEqual(
            {entity.entity_type.value for entity in entities},
            {"process", "step", "role", "condition", "outcome"},
        )
        self.assertEqual(
            {relation.relation_type.value for relation in relations},
            {"has_step", "performed_by", "routes_to"},
        )
        self.assertTrue(all(entity.review_status == PolicyReviewStatus.PENDING for entity in entities))
        self.assertTrue(all(relation.review_status == PolicyReviewStatus.PENDING for relation in relations))

    def test_medical_reimbursement_clause_accepts_location_and_step_chain(self) -> None:
        """防止医疗报销条款中的地点和步骤链因白名单缺失而被丢弃或断开。"""
        clause = {
            "clause_id": "c_medical_1",
            "raw_text": (
                "因急危重症在校外武汉市公立医院门诊就医的，学生本人或家属可凭就诊医院急诊相关资料"
                "（外院急诊病历、检查资料及医疗票据）及时到校医院相关科室进行急诊审核，审核通过后开具转诊单，"
                "再到大学生医保办按规定办理报销手续。"
            ),
        }
        model_result = {
            "entities": [
                {"type": "matter", "name": "因急危重症在校外武汉市公立医院门诊就医", "evidence_text": "因急危重症在校外武汉市公立医院门诊就医", "confidence": 0.95},
                {"type": "role", "name": "学生本人或家属", "evidence_text": "学生本人或家属", "confidence": 0.95},
                {"type": "material", "name": "就诊医院急诊相关资料（外院急诊病历、检查资料及医疗票据）", "evidence_text": "就诊医院急诊相关资料（外院急诊病历、检查资料及医疗票据）", "confidence": 0.95},
                {"type": "location", "name": "校医院相关科室", "evidence_text": "校医院相关科室", "confidence": 0.95},
                {"type": "step", "name": "急诊审核", "evidence_text": "进行急诊审核", "confidence": 0.95},
                {"type": "step", "name": "审核通过后开具转诊单", "evidence_text": "审核通过后开具转诊单", "confidence": 0.95},
                {"type": "step", "name": "至大学生医保办按规定办理报销手续", "evidence_text": "再到大学生医保办按规定办理报销手续", "confidence": 0.95},
            ],
            "relations": [
                {"type": "performed_at", "subject_index": 4, "object_index": 3, "evidence_text": "到校医院相关科室进行急诊审核", "confidence": 0.95},
                {"type": "next_step", "subject_index": 4, "object_index": 5, "evidence_text": "进行急诊审核，审核通过后开具转诊单", "confidence": 0.95},
                {"type": "next_step", "subject_index": 5, "object_index": 6, "evidence_text": "审核通过后开具转诊单，再到大学生医保办按规定办理报销手续", "confidence": 0.95},
            ],
        }

        entities, relations, reviews = _build_outputs(
            "run_medical_1",
            clause,
            model_result,
            include_rule_candidates=False,
        )

        self.assertEqual(
            {entity.entity_type.value for entity in entities},
            {"matter", "role", "material", "location", "step"},
        )
        self.assertEqual(
            {relation.relation_type.value for relation in relations},
            {"performed_at", "next_step"},
        )
        self.assertFalse(any(review.issue_type.startswith("invalid_") for review in reviews))

    def test_extraction_prompt_keeps_workflow_relation_constraints_compact(self) -> None:
        """防止精简提示词时丢掉关系方向和自环约束。"""
        prompt = build_extraction_prompt({
            "chapter_path": ["医疗保障"],
            "raw_text": "因急危重症在校外门诊就医的，申请人可办理报销。",
        })

        self.assertIn("performed_by：step/process -> role/department", prompt)
        self.assertIn("禁止自环", prompt)
        self.assertIn("只抽取原文明示事实", prompt)
        self.assertNotIn("学生本人或家属", prompt)

    def test_extraction_prompt_limits_parent_context_by_configuration(self) -> None:
        """防止父条款上下文仍固定发送 1500 字符，拖慢本机模型推理。"""
        with patch.object(app_config.llm, "parent_context_chars", 4, create=True):
            prompt = build_extraction_prompt(
                {"chapter_path": [], "raw_text": "申请人提交材料。"},
                parent_text="甲乙丙丁戊己",
            )

        self.assertIn("父条款上下文：甲乙丙丁", prompt)
        self.assertNotIn("父条款上下文：甲乙丙丁戊", prompt)

    def test_local_ollama_client_ignores_proxy_environment(self) -> None:
        """防止系统代理把指向本机 Ollama 的请求转发后造成 502。"""
        local_client = object()
        with (
            patch("policy.extraction.httpx.Client", return_value=local_client) as http_client_factory,
            patch("policy.extraction.OpenAI", return_value=object()) as openai_factory,
        ):
            create_llm_client("http://127.0.0.1:11434/v1", "ollama")

        http_client_factory.assert_called_once_with(trust_env=False)
        self.assertEqual(openai_factory.call_args.kwargs["http_client"], local_client)

    def test_only_loopback_llm_endpoints_bypass_proxy(self) -> None:
        """防止远程兼容服务被错误地关闭代理，或本机 localhost 漏掉代理绕过。"""
        self.assertTrue(is_local_llm_endpoint("http://127.0.0.1:11434/v1"))
        self.assertTrue(is_local_llm_endpoint("http://localhost:11434/v1"))
        self.assertTrue(is_local_llm_endpoint("http://[::1]:11434/v1"))
        self.assertFalse(is_local_llm_endpoint("https://api.example.com/v1"))

    def test_corrected_candidate_is_approved_with_replacement_label_data(self) -> None:
        """防止人工把“审核”修正为“复核”后，原候选仍被当作拒绝处理。"""
        candidate = {
            "entity_id": "entity_1",
            "clause_id": "c1",
            "entity_type": "approval_action",
            "name": "审核",
            "raw_text": "审核",
            "normalized_value": {},
            "evidence_text": "审核",
        }
        clause = {"clause_id": "c1", "raw_text": "财务处审核报销单。"}
        saved_annotations = []
        status_updates = []

        with (
            patch("policy.reviewer.get_policy_review_candidate", return_value=candidate),
            patch("policy.reviewer.get_policy_clause", return_value=clause),
            patch("policy.reviewer.insert_manual_annotation", side_effect=saved_annotations.append),
            patch(
                "policy.reviewer.update_policy_candidate_review_status",
                side_effect=lambda kind, candidate_id, status: status_updates.append((kind, candidate_id, status)),
            ),
        ):
            annotation = review_policy_candidate(
                "run_1",
                "entity",
                "entity_1",
                "corrected",
                "reviewer_1",
                label_data={
                    "entity_type": "approval_action",
                    "name": "复核",
                    "raw_text": "审核",
                    "normalized_value": {},
                    "evidence_text": "审核",
                },
            )

        self.assertEqual(annotation.label_data["name"], "复核")
        self.assertEqual(len(saved_annotations), 1)
        self.assertEqual(status_updates[0][1:], ("entity_1", PolicyReviewStatus.APPROVED))

    def test_added_non_document_relation_requires_both_endpoints(self) -> None:
        """防止人工补充的流程关系缺少端点，形成无法连接的图谱边。"""
        with self.assertRaisesRegex(ValueError, "主体实体 ID 和客体实体 ID"):
            build_manual_annotation(
                run_id="run_1",
                clause={"clause_id": "c1", "raw_text": "申请人提交报销单。"},
                candidate_kind="relation",
                candidate=None,
                decision="added",
                reviewer="reviewer_1",
                label_data={
                    "relation_type": "performed_by",
                    "subject_entity_id": "entity_step",
                    "evidence_text": "申请人提交报销单",
                },
            )

    def test_graph_projection_uses_approved_candidates_and_corrected_annotations(self) -> None:
        """防止办事图谱混入待审核候选，或忽略人工修正后的流程步骤名称。"""
        candidates = [
            {
                "candidate_type": "entity", "candidate_id": "entity_step", "entity_id": "entity_step",
                "entity_type": "step", "name": "提交报销单", "evidence_text": "提交报销单",
                "review_status": "approved", "clause_id": "c1",
                "clause": {"clause_id": "c1", "policy_id": "p1", "page_start": 2, "page_end": 2},
                "document": {"policy_id": "p1", "file_name": "报销办法.pdf", "title": "报销办法"},
            },
            {
                "candidate_type": "entity", "candidate_id": "entity_role", "entity_id": "entity_role",
                "entity_type": "role", "name": "申请人", "evidence_text": "申请人",
                "review_status": "auto_approved", "clause_id": "c1",
                "clause": {"clause_id": "c1", "policy_id": "p1", "page_start": 2, "page_end": 2},
                "document": {"policy_id": "p1", "file_name": "报销办法.pdf", "title": "报销办法"},
            },
            {
                "candidate_type": "entity", "candidate_id": "entity_pending", "entity_id": "entity_pending",
                "entity_type": "outcome", "name": "支付", "evidence_text": "支付",
                "review_status": "pending", "clause_id": "c1",
                "clause": {"clause_id": "c1", "policy_id": "p1", "page_start": 2, "page_end": 2},
                "document": {"policy_id": "p1", "file_name": "报销办法.pdf", "title": "报销办法"},
            },
            {
                "candidate_type": "relation", "candidate_id": "relation_1", "relation_id": "relation_1",
                "relation_type": "performed_by", "subject_entity_id": "entity_step", "object_entity_id": "entity_role",
                "evidence_text": "申请人提交报销单", "review_status": "approved", "clause_id": "c1",
                "clause": {"clause_id": "c1", "policy_id": "p1", "page_start": 2, "page_end": 2},
                "document": {"policy_id": "p1", "file_name": "报销办法.pdf", "title": "报销办法"},
            },
        ]
        annotations = [{
            "annotation_id": "annotation_1", "candidate_kind": "entity", "candidate_id": "entity_step",
            "decision": "corrected",
            "label_data": {"entity_type": "step", "name": "提交单据", "evidence_text": "提交报销单"},
        }]

        graph = build_policy_workflow_graph("run_1", candidates=candidates, annotations=annotations)

        self.assertEqual([node["id"] for node in graph["nodes"]], ["entity_role", "entity_step"])
        self.assertEqual(graph["nodes"][1]["name"], "提交单据")
        self.assertEqual(graph["edges"][0]["type"], "performed_by")
        self.assertEqual(graph["edges"][0]["source"], "entity_step")
        self.assertEqual(graph["nodes"][0]["citations"][0]["page_start"], 2)

    def test_export_workflow_graph_writes_json_without_database_write(self) -> None:
        """防止图谱浏览或交付时只能依赖数据库，无法导出可交换 JSON。"""
        candidates = [{
            "candidate_type": "entity", "candidate_id": "entity_1", "entity_id": "entity_1",
            "entity_type": "step", "name": "提交报销单", "evidence_text": "提交报销单",
            "review_status": "approved", "clause_id": "c1",
            "clause": {"clause_id": "c1", "page_start": 1, "page_end": 1},
            "document": {"file_name": "报销办法.pdf", "title": "报销办法"},
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = export_policy_workflow_graph(
                "run_1",
                os.path.join(temp_dir, "workflow.json"),
                candidate_loader=lambda run_id: candidates,
                annotation_loader=lambda run_id: [],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["nodes"][0]["name"], "提交报销单")


if __name__ == "__main__":
    unittest.main()
