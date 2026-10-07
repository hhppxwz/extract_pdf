import unittest
from unittest.mock import patch
import subprocess
import sys
import os
import tempfile
from pathlib import Path

import policy.retrieval


class PolicyKnowledgeV2Tests(unittest.TestCase):
    def test_full_help_lists_v2_run_resume_and_status_commands(self) -> None:
        result = subprocess.run(
            [sys.executable, "main.py", "--help-all"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--extract-policy-knowledge-v2", result.stdout)
        self.assertIn("--resume-policy-knowledge-v2", result.stdout)
        self.assertIn("--policy-knowledge-v2-status", result.stdout)

    def test_context_keeps_parent_and_siblings_addressable(self) -> None:
        from policy.knowledge_v2 import build_clause_context

        clauses = [
            {"clause_id": "p", "level": "article", "raw_text": "第六条 申请程序如下：", "sequence_no": 1},
            {"clause_id": "a", "parent_clause_id": "p", "level": "item", "raw_text": "（一）提交申请表。", "sequence_no": 2},
            {"clause_id": "b", "parent_clause_id": "p", "level": "item", "raw_text": "（二）学院审核。", "sequence_no": 3},
        ]

        context = build_clause_context(clauses[1], clauses, {"title": "兼职管理办法", "document_no": "武大〔2024〕1号"})

        self.assertEqual(context["current"]["clause_id"], "a")
        self.assertEqual(context["parent"]["clause_id"], "p")
        self.assertEqual(context["next"]["clause_id"], "b")
        self.assertIsNone(context["previous"])

    def test_context_ancestor_depth_controls_visible_levels(self) -> None:
        from policy.knowledge_v2 import build_clause_context

        clauses = [
            {"clause_id": "chapter", "raw_text": "第二章 公文种类", "sequence_no": 1},
            {"clause_id": "article", "parent_clause_id": "chapter", "raw_text": "第七条 公文种类包括：", "sequence_no": 2},
            {"clause_id": "item", "parent_clause_id": "article", "raw_text": "（一）请示。", "sequence_no": 3},
        ]
        document = {"title": "公文处理办法"}

        none = build_clause_context(clauses[2], clauses, document, ancestor_depth=0)
        one = build_clause_context(clauses[2], clauses, document, ancestor_depth=1)
        two = build_clause_context(clauses[2], clauses, document, ancestor_depth=2)

        self.assertEqual(none["ancestors"], [])
        self.assertIsNone(none["parent"])
        self.assertEqual([item["clause_id"] for item in one["ancestors"]], ["article"])
        self.assertEqual([item["clause_id"] for item in two["ancestors"]], ["article", "chapter"])

    def test_prompt_specifies_complete_assertion_shape_and_evidence_boundary(self) -> None:
        from policy.knowledge_v2 import build_assertion_prompt

        prompt = build_assertion_prompt({"current": {"clause_id": "c1", "text": "申请人提交材料。"}})

        self.assertIn('"subject":{"text"', prompt)
        self.assertIn('"qualifiers"', prompt)
        self.assertIn('"start":0', prompt)
        self.assertIn("当前条款或明确标识的上下文条款", prompt)

    def test_validation_rejects_evidence_position_mismatch(self) -> None:
        from policy.knowledge_v2 import validate_assertion

        clause = {"clause_id": "c1", "raw_text": "申请人应提交申请表。"}
        payload = {
            "kind": "action",
            "subject": {"text": "申请人", "type": "person", "inferred_from_context": False},
            "predicate": {"text": "提交", "normalized": "提交"},
            "object": {"text": "申请表", "type": "material"},
            "receiver": None,
            "modality": "required",
            "qualifiers": {},
            "evidence": {"clause_id": "c1", "text": "申请人应提交申请表", "start": 1, "end": 10},
        }

        with self.assertRaisesRegex(ValueError, "字符位置"):
            validate_assertion(payload, {"c1": clause})

    def test_validation_rejects_endpoint_not_supported_by_evidence(self) -> None:
        from policy.knowledge_v2 import validate_assertion

        clause = {"clause_id": "c1", "raw_text": "申请人应提交申请表。"}
        payload = {
            "kind": "action",
            "subject": {"text": "申请人", "type": "person", "inferred_from_context": False},
            "predicate": {"text": "提交", "normalized": "提交"},
            "object": {"text": "身份证", "type": "material"},
            "receiver": None,
            "modality": "required",
            "qualifiers": {},
            "evidence": {"clause_id": "c1", "text": "申请人应提交申请表", "start": 0, "end": 9},
        }

        with self.assertRaisesRegex(ValueError, "object.*证据"):
            validate_assertion(payload, {"c1": clause})

    def test_validation_accepts_context_inferred_subject_with_source_evidence(self) -> None:
        from policy.knowledge_v2 import validate_assertion

        clauses = {
            "parent": {"clause_id": "parent", "raw_text": "申请人办理程序如下："},
            "child": {"clause_id": "child", "raw_text": "提交申请表。"},
        }
        payload = {
            "kind": "action",
            "subject": {"text": "申请人", "type": "person", "inferred_from_context": True, "source_clause_id": "parent"},
            "predicate": {"text": "提交", "normalized": "提交"},
            "object": {"text": "申请表", "type": "material"},
            "receiver": None,
            "modality": "required",
            "qualifiers": {"materials": ["申请表"]},
            "evidence": {"clause_id": "child", "text": "提交申请表", "start": 0, "end": 5},
        }

        result = validate_assertion(payload, clauses)

        self.assertEqual(result["status"], "machine_extracted")
        self.assertTrue(result["subject"]["inferred_from_context"])

    def test_validation_rejects_qualifier_not_supported_by_evidence(self) -> None:
        from policy.knowledge_v2 import validate_assertion

        clause = {"clause_id": "c1", "raw_text": "申请人应提交申请表"}
        payload = {
            "kind": "action",
            "subject": {"text": "申请人", "type": "person"},
            "predicate": {"text": "提交", "normalized": "提交"},
            "object": {"text": "申请表", "type": "material"},
            "receiver": None,
            "modality": "required",
            "qualifiers": {"deadline": "七日内"},
            "evidence": {"clause_id": "c1", "text": "申请人应提交申请表", "start": 0, "end": 9},
        }

        with self.assertRaisesRegex(ValueError, "qualifiers.deadline"):
            validate_assertion(payload, {"c1": clause})

    def test_invalid_kind_reports_modality_confusion(self) -> None:
        from policy.knowledge_v2 import validate_assertion

        with self.assertRaisesRegex(ValueError, "prohibited.*规范模态.*action"):
            validate_assertion({"kind": "prohibited", "modality": "prohibited"}, {})

    def test_normalization_repairs_only_unique_exact_evidence_position(self) -> None:
        from policy.knowledge_v2 import normalize_assertions

        raw = "第一条 申请人应提交申请表。"
        clause = {"clause_id": "c1", "raw_text": raw}
        assertion = {
            "kind": "action", "subject": {"text": "申请人", "type": "person"},
            "predicate": {"text": "提交"}, "object": {"text": "申请表", "type": "material"},
            "receiver": None, "modality": "required", "qualifiers": {},
            "evidence": {"clause_id": "c1", "text": "申请人应提交申请表", "start": 0, "end": 9},
        }
        valid, invalid = normalize_assertions([assertion], {"c1": clause})
        self.assertEqual(len(valid), 1)
        self.assertEqual(invalid, [])
        self.assertEqual(valid[0]["evidence"], {"clause_id": "c1", "text": "申请人应提交申请表", "start": 4, "end": 13})
        repeated = {"c1": {"clause_id": "c1", "raw_text": "申请人提交。申请人提交。"}}
        ambiguous = {**assertion, "evidence": {"clause_id": "c1", "text": "申请人提交", "start": 50, "end": 55}}
        valid, invalid = normalize_assertions([ambiguous], repeated)
        self.assertEqual(valid, [])
        self.assertIn("证据文本与字符位置不一致", invalid[0]["error"])

    def test_review_rows_show_validation_error(self) -> None:
        from policy.knowledge_review_v2 import build_assertion_review_rows

        rows = build_assertion_review_rows("r1", [{"assertion_id": "a1", "clause_id": "c1", "status": "invalid", "error": "证据文本与字符位置不一致"}])
        self.assertEqual(rows[0]["无效原因"], "证据文本与字符位置不一致")

    def test_old_invalid_row_recomputes_reason_for_review_export(self) -> None:
        from policy.storage import get_policy_assertion_review_rows_v2

        clause = {"clause_id": "c1", "policy_id": "p1", "raw_text": "申请人应提交申请表"}
        invalid = {"assertion_id": "a1", "clause_id": "c1", "policy_id": "p1", "status": "invalid", "kind": "invalid", "payload": {"assert_type": "scope"}}
        with patch("policy.storage.get_policy_assertions_v2", return_value=[invalid]), patch(
            "policy.storage.get_policy_clause", return_value=clause
        ), patch("policy.storage.get_policy_clauses", return_value=[clause]), patch(
            "policy.storage.get_policy_document", return_value={"title": "办法"}
        ):
            rows = get_policy_assertion_review_rows_v2("r1")
        self.assertIn("assert_type=scope", rows[0]["error"])

    def test_duplicate_assertions_keep_one_record(self) -> None:
        from policy.knowledge_v2 import normalize_assertions

        clause = {"clause_id": "c1", "raw_text": "申请人应提交申请表。"}
        item = {
            "kind": "action",
            "subject": {"text": "申请人", "type": "person", "inferred_from_context": False},
            "predicate": {"text": "提交", "normalized": "提交"},
            "object": {"text": "申请表", "type": "material"},
            "receiver": None,
            "modality": "required",
            "qualifiers": {},
            "evidence": {"clause_id": "c1", "text": "申请人应提交申请表", "start": 0, "end": 9},
        }

        valid, invalid = normalize_assertions([item, dict(item)], {"c1": clause})

        self.assertEqual(len(valid), 1)
        self.assertEqual(invalid, [])

    def test_explicit_heading_builds_matter_and_workflow_without_claiming_order(self) -> None:
        from policy.knowledge_v2 import build_explicit_matters, build_workflow_view

        clauses = [
            {"clause_id": "h", "level": "section", "raw_text": "第二节 兼职申请办理程序", "sequence_no": 1, "chapter_path": ["第二章"]},
            {"clause_id": "a", "parent_clause_id": "h", "level": "article", "raw_text": "申请人提交申请表。", "sequence_no": 2, "chapter_path": ["第二章", "第二节"]},
            {"clause_id": "b", "parent_clause_id": "h", "level": "article", "raw_text": "学院审核申请。", "sequence_no": 3, "chapter_path": ["第二章", "第二节"]},
        ]
        assertions = [
            {"assertion_id": "e1", "clause_id": "a", "kind": "action", "predicate": {"text": "提交"}, "status": "machine_extracted"},
            {"assertion_id": "e2", "clause_id": "b", "kind": "action", "predicate": {"text": "审核"}, "status": "machine_extracted"},
        ]

        matters = build_explicit_matters("run1", "policy1", clauses, assertions)
        workflow = build_workflow_view(matters[0], clauses, assertions)

        self.assertEqual(matters[0]["name"], "第二节 兼职申请办理程序")
        self.assertEqual(matters[0]["clause_ids"], ["h", "a", "b"])
        self.assertEqual(workflow["workflow_type"], "partial_workflow")
        self.assertFalse(workflow["order_confirmed"])
        self.assertEqual([step["assertion_id"] for step in workflow["steps"]], ["e1", "e2"])

    def test_heading_without_process_goal_does_not_build_matter(self) -> None:
        from policy.knowledge_v2 import build_explicit_matters

        clauses = [
            {"clause_id": "h", "level": "chapter", "raw_text": "第一章 总则", "sequence_no": 1},
            {"clause_id": "a", "parent_clause_id": "h", "level": "article", "raw_text": "本办法适用于教师。", "sequence_no": 2},
        ]

        self.assertEqual(build_explicit_matters("run1", "policy1", clauses, []), [])

    def test_explicit_subheadings_split_parent_container_without_overlap(self) -> None:
        from policy.knowledge_v2 import build_explicit_matters

        clauses = [
            {"clause_id": "chapter", "level": "chapter", "raw_text": "第二章 申请办理", "sequence_no": 1},
            {"clause_id": "first", "parent_clause_id": "chapter", "level": "section", "raw_text": "第一节 首次申请", "sequence_no": 2},
            {"clause_id": "a", "parent_clause_id": "first", "level": "article", "raw_text": "申请人提交材料。", "sequence_no": 3},
            {"clause_id": "change", "parent_clause_id": "chapter", "level": "section", "raw_text": "第二节 变更申请", "sequence_no": 4},
            {"clause_id": "b", "parent_clause_id": "change", "level": "article", "raw_text": "申请人提交变更材料。", "sequence_no": 5},
        ]
        assertions = [
            {"assertion_id": "e1", "clause_id": "a", "status": "machine_extracted"},
            {"assertion_id": "e2", "clause_id": "b", "status": "machine_extracted"},
        ]

        matters = build_explicit_matters("run1", "p1", clauses, assertions)

        self.assertEqual([item["container_clause_id"] for item in matters], ["first", "change"])
        self.assertEqual(matters[0]["clause_ids"], ["first", "a"])

    def test_model_response_must_be_json_object_with_assertion_array(self) -> None:
        from policy.knowledge_runner_v2 import parse_assertion_response

        with self.assertRaisesRegex(ValueError, "assertions"):
            parse_assertion_response('{"items": []}')

    def test_resume_retries_interrupted_running_item(self) -> None:
        from policy.knowledge_runner_v2 import should_process_knowledge_item

        self.assertTrue(should_process_knowledge_item({"status": "running"}, resume=True))
        self.assertFalse(should_process_knowledge_item({"status": "running"}, resume=False))
        self.assertFalse(should_process_knowledge_item({"status": "succeeded"}, resume=True))

    def test_extract_clause_returns_valid_and_invalid_candidates_separately(self) -> None:
        from policy.knowledge_runner_v2 import extract_clause_assertions

        clause = {"clause_id": "c1", "level": "article", "raw_text": "申请人应提交申请表。", "sequence_no": 1}
        document = {"title": "申请管理办法"}
        response = {
            "assertions": [
                {
                    "kind": "action",
                    "subject": {"text": "申请人", "type": "person", "inferred_from_context": False},
                    "predicate": {"text": "提交", "normalized": "提交"},
                    "object": {"text": "申请表", "type": "material"},
                    "receiver": None,
                    "modality": "required",
                    "qualifiers": {"materials": ["申请表"]},
                    "evidence": {"clause_id": "c1", "text": "申请人应提交申请表", "start": 0, "end": 9},
                },
                {
                    "kind": "action",
                    "subject": {"text": "申请人"},
                    "predicate": {"text": "批准"},
                    "modality": "required",
                    "evidence": {"clause_id": "c1", "text": "批准", "start": 0, "end": 2},
                },
            ]
        }

        valid, invalid = extract_clause_assertions(clause, [clause], document, lambda _: response)

        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["clause_id"], "c1")
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["status"], "invalid")

    def test_non_substantive_heading_skips_model_call(self) -> None:
        from policy.knowledge_runner_v2 import extract_clause_assertions

        called = False

        def extractor(_prompt: str):
            nonlocal called
            called = True
            return {"assertions": []}

        clause = {"clause_id": "h", "level": "chapter", "raw_text": "第一章 总则", "sequence_no": 1}
        valid, invalid = extract_clause_assertions(clause, [clause], {"title": "办法"}, extractor)

        self.assertFalse(called)
        self.assertEqual((valid, invalid), ([], []))

    def test_document_type_list_builds_scope_assertions_with_exact_item_evidence(self) -> None:
        from policy.knowledge_v2 import extract_document_type_item

        parent = {"clause_id": "p", "level": "article", "raw_text": "第七条 学校公文种类包括："}
        examples = [
            ("决议", "会议讨论通过的重大决策事项"),
            ("决定", "对重要事项作出决策和部署、奖惩有关单位和人员"),
            ("通告", "公布校内外有关方面应当遵守或者周知的事项"),
            ("意见", "对重要问题提出见解和处理办法"),
            ("通知", "发布规章，任免和聘用干部"),
            ("通报", "表彰先进、批评错误"),
            ("报告", "向上级单位汇报工作、反映情况"),
            ("请示", "向上级单位请求指示、批准"),
            ("批复", "答复下级单位请示事项"),
            ("函", "不相隶属单位之间商洽工作"),
            ("纪要", "记载和传达会议情况和议定事项"),
        ]
        for index, (name, usage) in enumerate(examples, 1):
            clause = {"clause_id": f"c{index}", "level": "item", "parent_clause_id": "p",
                      "raw_text": f"（{index}） {name}。适用于{usage}。\n3"}
            with self.subTest(name=name):
                result = extract_document_type_item(clause, parent)
                self.assertIsNotNone(result)
                self.assertEqual(result["kind"], "scope")
                self.assertEqual(result["modality"], "factual")
                self.assertEqual(result["subject"]["text"], name)
                self.assertEqual(result["predicate"]["text"], "适用于")
                self.assertEqual(result["object"]["text"], usage)
                self.assertEqual(result["category"], {"text": "学校公文种类", "source_clause_id": "p"})
                evidence = result["evidence"]
                self.assertEqual(clause["raw_text"][evidence["start"]:evidence["end"]], evidence["text"])
                self.assertNotIn("\n3", evidence["text"])

    def test_document_type_rule_does_not_capture_unrelated_suitability_clause(self) -> None:
        from policy.knowledge_v2 import extract_document_type_item

        parent = {"clause_id": "p", "raw_text": "第七条 报销条件如下："}
        clause = {"clause_id": "c", "level": "item", "raw_text": "（一） 请示。适用于报销审批。"}
        self.assertIsNone(extract_document_type_item(clause, parent))

    def test_batch_skips_model_for_document_type_items(self) -> None:
        from policy.knowledge_runner_v2 import extract_clause_batch_assertions

        parent = {"clause_id": "p", "level": "article", "raw_text": "第七条 学校公文种类包括：", "sequence_no": 1}
        item = {"clause_id": "c", "level": "item", "parent_clause_id": "p", "raw_text": "（八） 请示。适用于向上级单位请求指示、批准。", "sequence_no": 2}
        result = extract_clause_batch_assertions([(item, [parent, item], {"title": "公文处理办法"})], lambda _: self.fail("规则命中后不应调用模型"))
        self.assertEqual(result[0][1], [])
        self.assertEqual(result[0][0][0]["subject"]["text"], "请示")
        self.assertEqual(result[0][0][0]["category"]["source_clause_id"], "p")

    def test_document_type_index_contains_parent_category_and_item_usage(self) -> None:
        from policy.knowledge_retrieval_v2 import assertion_index_text

        assertion = {"kind": "scope", "subject": {"text": "请示"}, "predicate": {"text": "适用于"},
                     "object": {"text": "向上级单位请求指示、批准"}, "category": {"text": "学校公文种类", "source_clause_id": "p"}}
        text = assertion_index_text(assertion)
        self.assertIn("学校公文种类", text)
        self.assertIn("请示", text)
        self.assertIn("请求指示", text)

    def test_document_type_graph_exports_contains_and_usage_edges(self) -> None:
        from policy.knowledge_v2 import build_document_type_graph, extract_document_type_item

        parent = {"clause_id": "p", "policy_id": "policy", "level": "article", "raw_text": "第七条 学校公文种类包括："}
        child = {"clause_id": "c", "policy_id": "policy", "parent_clause_id": "p", "level": "item",
                 "raw_text": "（八）请示。适用于向上级单位请求指示、批准。"}
        other = {"clause_id": "d", "policy_id": "policy", "parent_clause_id": "p", "level": "item",
                 "raw_text": "（一）决议。适用于会议讨论通过的重大决策事项。"}
        assertion = {**extract_document_type_item(child, parent), "assertion_id": "a", "status": "machine_extracted", "clause_id": "c"}
        other_assertion = {**extract_document_type_item(other, parent), "assertion_id": "b", "status": "machine_extracted", "clause_id": "d"}

        graph = build_document_type_graph([parent, child, other], [assertion, other_assertion])

        contains = [edge for edge in graph["edges"] if edge["predicate"] == "包含"]
        self.assertEqual(len(contains), 1)
        self.assertEqual(contains[0]["source"], "policy:category:p:学校公文种类")
        self.assertEqual({item["name"] for item in contains[0]["members"]}, {"请示", "决议"})
        self.assertEqual({item["evidence"]["clause_id"] for item in contains[0]["members"]}, {"c", "d"})
        usage = [edge for edge in graph["edges"] if edge["predicate"] == "适用于"]
        self.assertEqual(len(usage), 2)
        self.assertEqual({(edge["source"], edge["target"]) for edge in usage}, {
            ("policy:type:c:请示", "policy:usage:c:向上级单位请求指示、批准"),
            ("policy:type:d:决议", "policy:usage:d:会议讨论通过的重大决策事项"),
        })

    def test_connection_failure_stops_batch_without_single_item_retries(self) -> None:
        import httpx
        from openai import APIConnectionError
        from policy.knowledge_runner_v2 import extract_batch_with_fallback

        error = APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"))
        single_calls = []

        with self.assertRaises(APIConnectionError):
            extract_batch_with_fallback(
                ["a", "b"], lambda _: (_ for _ in ()).throw(error),
                lambda item: single_calls.append(item),
            )
        self.assertEqual(single_calls, [])

    def test_run_reports_model_connection_failure_and_keeps_items_resumable(self) -> None:
        import httpx
        from openai import APIConnectionError
        from policy.knowledge_runner_v2 import run_knowledge_extraction_v2

        updates = []
        run_updates = []
        error = APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"))
        clause = {"clause_id": "c", "policy_id": "p", "level": "article", "raw_text": "申请人提交材料。"}
        with (
            patch("policy.storage.get_policy_knowledge_run_v2", return_value={"run_id": "r", "batch_id": "b", "context_ancestor_depth": 1}),
            patch("policy.storage.update_policy_knowledge_run_v2", side_effect=lambda _, data: run_updates.append(data)),
            patch("policy.storage.get_policy_documents_for_batch", return_value=[{"policy_id": "p", "title": "办法"}]),
            patch("policy.storage.get_policy_clauses", return_value=[clause]),
            patch("policy.storage.get_policy_knowledge_items_v2", return_value=[{"item_id": "i", "clause_id": "c", "status": "pending"}]),
            patch("policy.storage.get_policy_clause", return_value=clause),
            patch("policy.storage.update_policy_knowledge_item_v2", side_effect=lambda _, data: updates.append(data)),
            patch("policy.knowledge_runner_v2.extract_clause_batch_assertions", side_effect=error),
        ):
            with self.assertRaisesRegex(RuntimeError, "模型服务连接失败.*r.*resume-policy-knowledge-v2"):
                run_knowledge_extraction_v2("r")

        self.assertEqual(updates[-1]["status"], "pending")
        self.assertEqual(run_updates[-1]["status"], "connection_failed")

    def test_batch_extraction_handles_multiple_target_clauses_in_one_call(self) -> None:
        from policy.knowledge_runner_v2 import extract_clause_batch_assertions

        clauses = [
            {"clause_id": "c1", "policy_id": "p1", "level": "article", "raw_text": "申请人提交申请表", "sequence_no": 1},
            {"clause_id": "c2", "policy_id": "p1", "level": "article", "raw_text": "学院审核申请", "sequence_no": 2},
        ]
        response = {"results": [
            {"target_clause_id": "c1", "assertions": [{
                "kind": "action", "subject": {"text": "申请人", "type": "person"},
                "predicate": {"text": "提交"}, "object": {"text": "申请表", "type": "material"},
                "receiver": None, "modality": "required", "qualifiers": {},
                "evidence": {"clause_id": "c1", "text": "申请人提交申请表", "start": 0, "end": 8},
            }]},
            {"target_clause_id": "c2", "assertions": [{
                "kind": "action", "subject": {"text": "学院", "type": "department"},
                "predicate": {"text": "审核"}, "object": {"text": "申请", "type": "matter"},
                "receiver": None, "modality": "factual", "qualifiers": {},
                "evidence": {"clause_id": "c2", "text": "学院审核申请", "start": 0, "end": 6},
            }]},
        ]}
        calls = 0

        def extractor(_prompt: str):
            nonlocal calls
            calls += 1
            return response

        result = extract_clause_batch_assertions(
            [(clause, clauses, {"title": "申请办法"}) for clause in clauses], extractor,
        )

        self.assertEqual(calls, 1)
        self.assertEqual([item[0][0]["predicate"]["text"] for item in result], ["提交", "审核"])

    def test_batch_failure_falls_back_to_each_clause(self) -> None:
        from policy.knowledge_runner_v2 import extract_batch_with_fallback

        requests = [("c1",), ("c2",)]
        result = extract_batch_with_fallback(
            requests,
            lambda _requests: (_ for _ in ()).throw(ValueError("批量格式错误")),
            lambda request: f"single-{request[0]}",
        )

        self.assertEqual(result, ["single-c1", "single-c2"])

    def test_invalid_batch_item_retries_singly_without_hiding_original_failure(self) -> None:
        from policy.knowledge_runner_v2 import retry_invalid_batch_items

        requests = [("c1",), ("c2",)]
        invalid = ([], [{"status": "invalid", "error": "断言 kind 非法"}])
        valid = ([{"kind": "action"}], [])
        result = retry_invalid_batch_items(requests, [invalid, valid], lambda request, failed: valid if request[0] == "c1" and failed == invalid else invalid)
        self.assertEqual(result, [valid, valid])
        result = retry_invalid_batch_items(requests, [invalid, valid], lambda _request, _failed: ([], []))
        self.assertEqual(result[0], invalid)

    def test_feedback_retry_reextracts_missing_predicate_with_original_evidence(self) -> None:
        from policy.knowledge_runner_v2 import retry_clause_assertions_with_feedback

        clause = {"clause_id": "c1", "level": "article", "raw_text": "本办法所指放射性同位素包括放射源。", "sequence_no": 1}
        invalid = ([], [{"status": "invalid", "error": "predicate 必须包含非空 text", "payload": {"kind": "definition", "predicate": None}}])
        prompts = []

        def extractor(prompt):
            prompts.append(prompt)
            return {"assertions": [{
                "kind": "definition", "subject": {"text": "放射性同位素", "type": "other"},
                "predicate": {"text": "包括"}, "object": {"text": "放射源", "type": "other"},
                "receiver": None, "modality": "factual", "qualifiers": {},
                "evidence": {"clause_id": "c1", "text": "本办法所指放射性同位素包括放射源", "start": 0, "end": 16},
            }]}

        valid, rejected = retry_clause_assertions_with_feedback((clause, [clause], {"title": "办法"}), invalid, extractor)
        self.assertEqual(rejected, [])
        self.assertEqual(valid[0]["predicate"]["text"], "包括")
        self.assertIn("predicate 必须包含非空 text", prompts[0])

    def test_progress_text_includes_speed_and_remaining_time(self) -> None:
        from policy.knowledge_runner_v2 import format_extraction_progress

        text = format_extraction_progress(2, 10, 4.0)

        self.assertIn("2/10", text)
        self.assertIn("2.0 秒/条", text)
        self.assertIn("预计剩余 16 秒", text)

    def test_assertion_matches_boost_existing_clause_without_becoming_citation(self) -> None:
        from policy.knowledge_retrieval_v2 import merge_assertion_matches

        candidates = [
            {"id": "c1", "similarity": 0.70, "score": 0.70, "metadata": {"clause_id": "c1", "raw_text": "申请人提交申请表"}},
            {"id": "c2", "similarity": 0.75, "score": 0.75, "metadata": {"clause_id": "c2", "raw_text": "学院负责管理"}},
        ]
        matches = [{"assertion_id": "a1", "clause_id": "c1", "score": 0.9, "kind": "action", "predicate_text": "提交"}]

        result, info = merge_assertion_matches(candidates, matches)

        self.assertEqual(result[0]["metadata"]["clause_id"], "c1")
        self.assertEqual(info["retrieval_mode"], "clause_plus_knowledge")
        self.assertEqual(info["matched_assertions"][0]["assertion_id"], "a1")
        self.assertNotIn("raw_text", info["matched_assertions"][0])

    def test_no_assertion_match_preserves_clause_order_and_mode(self) -> None:
        from policy.knowledge_retrieval_v2 import merge_assertion_matches

        candidates = [
            {"id": "c1", "score": 0.8, "metadata": {"clause_id": "c1"}},
            {"id": "c2", "score": 0.7, "metadata": {"clause_id": "c2"}},
        ]

        result, info = merge_assertion_matches(candidates, [])

        self.assertEqual([item["id"] for item in result], ["c1", "c2"])
        self.assertEqual(info["retrieval_mode"], "clause_only")

    def test_empty_assertion_index_does_not_load_embedding_model(self) -> None:
        from policy.knowledge_retrieval_v2 import search_assertions_v2

        with patch("storage_adapter.storage.vector.count_vectors", return_value=0), patch(
            "policy.retrieval._encode_policy_clause_texts"
        ) as encoder:
            with self.assertRaisesRegex(RuntimeError, "索引为空"):
                search_assertions_v2("如何申请")
        encoder.assert_not_called()

    def test_assertion_index_replaces_old_vectors_for_same_policy(self) -> None:
        from policy.knowledge_retrieval_v2 import sync_assertion_index_v2

        assertions = [{
            "assertion_id": "a1", "run_id": "new", "policy_id": "p1", "clause_id": "c1",
            "kind": "action", "modality": "required", "subject_text": "申请人",
            "predicate_text": "提交", "object_text": "申请表", "status": "machine_extracted",
        }]
        with patch("policy.storage.is_latest_successful_policy_knowledge_run_v2", return_value=True), patch(
            "policy.storage.get_policy_assertions_v2", return_value=assertions
        ), patch(
            "policy.retrieval._encode_policy_clause_texts", return_value=[[0.1, 0.2]]
        ), patch("storage_adapter.storage.vector") as vector:
            vector.insert_vectors.return_value = 1
            count = sync_assertion_index_v2("new")

        self.assertEqual(count, 1)
        vector.delete_vectors_by_metadata.assert_any_call("policy_assertion_search_v2", "policy_id", "p1")

    def test_matching_assertion_resolves_explicit_matter_and_workflow_summary(self) -> None:
        from policy.knowledge_retrieval_v2 import resolve_match_context

        matches = [{"run_id": "r1", "clause_id": "a"}]
        matters = [{"matter_id": "m1", "name": "兼职申请程序", "policy_id": "p1", "clause_ids": ["h", "a"]}]
        clauses = [{"clause_id": "h", "sequence_no": 1}, {"clause_id": "a", "sequence_no": 2}]
        assertions = [{"assertion_id": "e1", "clause_id": "a", "kind": "action", "status": "machine_extracted", "predicate": {"text": "提交"}}]

        context = resolve_match_context(matches, matters, clauses, assertions)

        self.assertEqual(context["matched_matter"]["matter_id"], "m1")
        self.assertEqual(context["workflow_summary"]["workflow_type"], "partial_workflow")

    def test_review_rows_keep_machine_fields_and_blank_human_decision(self) -> None:
        from policy.knowledge_review_v2 import build_assertion_review_rows

        rows = build_assertion_review_rows("run1", [{
            "assertion_id": "a1", "clause_id": "c1", "kind": "action", "modality": "required",
            "subject": {"text": "申请人"}, "predicate": {"text": "提交"}, "object": {"text": "申请表"},
            "evidence": {"text": "申请人应提交申请表"}, "clause": {"raw_text": "申请人应提交申请表。"},
            "document": {"title": "申请办法", "file_name": "申请办法.pdf"},
        }])

        self.assertEqual(rows[0]["断言内容"], "申请人 --提交--> 申请表")
        self.assertEqual(rows[0]["人工结论"], "")

    def test_review_row_shows_parent_category_for_document_type(self) -> None:
        from policy.knowledge_review_v2 import build_assertion_review_rows

        rows = build_assertion_review_rows("r1", [{
            "assertion_id": "a1", "clause_id": "c1", "kind": "scope", "modality": "factual",
            "subject": {"text": "请示"}, "predicate": {"text": "适用于"},
            "object": {"text": "向上级单位请求批准"},
            "category": {"text": "学校公文种类", "source_clause_id": "p"},
        }])
        self.assertEqual(rows[0]["所属类别"], "学校公文种类")
        self.assertEqual(rows[0]["类别来源条款ID"], "p")

    def test_workflow_export_contains_matter_and_citable_steps(self) -> None:
        from policy.knowledge_review_v2 import export_knowledge_view_v2

        matter = {"matter_id": "m1", "name": "申请程序", "clause_ids": ["h", "a"], "policy_id": "p1"}
        clauses = [{"clause_id": "h", "sequence_no": 1}, {"clause_id": "a", "sequence_no": 2}]
        assertions = [{"assertion_id": "e1", "clause_id": "a", "kind": "action", "status": "machine_extracted", "predicate": {"text": "提交"}}]
        with tempfile.TemporaryDirectory() as directory:
            path = export_knowledge_view_v2(
                "run1", Path(directory) / "view.json",
                matter_loader=lambda _: [matter], clause_loader=lambda _: clauses,
                assertion_loader=lambda _: assertions,
            )
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["matters"][0]["workflow"]["workflow_type"], "partial_workflow")
        self.assertEqual(payload["matters"][0]["workflow"]["steps"][0]["clause_id"], "a")

    def test_view_export_includes_grouped_document_type_graph_without_matters(self) -> None:
        from policy.knowledge_review_v2 import export_knowledge_view_v2
        from policy.knowledge_v2 import extract_document_type_item

        parent = {"clause_id": "p", "policy_id": "policy", "raw_text": "第七条 学校公文种类包括："}
        child = {"clause_id": "c", "policy_id": "policy", "parent_clause_id": "p", "level": "item",
                 "raw_text": "（八）请示。适用于向上级单位请求指示、批准。"}
        assertion = {**extract_document_type_item(child, parent), "assertion_id": "a", "status": "machine_extracted", "clause_id": "c", "policy_id": "policy"}
        with tempfile.TemporaryDirectory() as directory:
            path = export_knowledge_view_v2(
                "run1", Path(directory) / "view.json", matter_loader=lambda _: [],
                clause_loader=lambda _: [parent, child], assertion_loader=lambda _: [assertion],
            )
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["matters"], [])
        self.assertEqual(payload["document_type_graph"]["edges"][0]["predicate"], "包含")
        self.assertEqual(payload["document_type_graph"]["edges"][0]["members"][0]["name"], "请示")


if __name__ == "__main__":
    unittest.main()
