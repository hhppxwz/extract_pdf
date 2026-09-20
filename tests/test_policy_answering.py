"""制度问答编排测试。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from policy import answering


class PolicyAnsweringTests(unittest.TestCase):
    @staticmethod
    def _candidate(policy_id: str, clause_id: str, score: float, status: str) -> dict:
        return {
            "score": score,
            "temporal_status": status,
            "metadata": {
                "policy_id": policy_id,
                "clause_id": clause_id,
                "title": f"制度{policy_id}",
                "raw_text": f"条款{clause_id}",
                "page_start": 1,
                "page_end": 1,
            },
            "policy_document": {"policy_id": policy_id},
        }

    def test_answering_module_is_available(self) -> None:
        """防止问答接口缺少独立、可测试的领域编排模块。"""
        self.assertIsNotNone(importlib.util.find_spec("policy.answering"))

    def test_policy_answer_route_is_registered(self) -> None:
        from api import app
        self.assertIn("/policy-answer", {route.path for route in app.routes})

    def test_policy_answer_openapi_uses_form_fields_with_date_format(self) -> None:
        from api import app

        operation = app.openapi()["paths"]["/policy-answer"]["post"]
        content = operation["requestBody"]["content"]
        self.assertIn("application/x-www-form-urlencoded", content)
        schema_ref = content["application/x-www-form-urlencoded"]["schema"]["$ref"]
        schema_name = schema_ref.rsplit("/", 1)[-1]
        schema = app.openapi()["components"]["schemas"][schema_name]

        self.assertIn("question", schema["required"])
        self.assertEqual(schema["properties"]["question"]["type"], "string")
        as_of_schema = schema["properties"]["as_of"]
        date_schema = next(item for item in as_of_schema["anyOf"] if item.get("type") == "string")
        self.assertEqual(as_of_schema.get("format", date_schema.get("format")), "date")

    def test_policy_qa_page_is_registered(self) -> None:
        from api import app
        self.assertIn("/policy-qa", {route.path for route in app.routes})

    def test_policy_qa_page_contains_question_date_and_answer_regions(self) -> None:
        from api import policy_qa_page
        response = asyncio.run(policy_qa_page())
        html = bytes(response.body).decode("utf-8")

        self.assertIn('<textarea id="question"', html)
        self.assertIn('<input id="as-of" type="date"', html)
        self.assertIn('<button id="submit"', html)
        self.assertIn('id="answer-panel"', html)
        self.assertIn('id="citations"', html)
        self.assertIn("/policy-answer", html)
        self.assertIn("new FormData", html)
        self.assertNotIn("JSON.stringify(payload)", html)

    def test_requested_date_has_priority(self) -> None:
        result = answering.resolve_answer_date(
            "请查询2020年1月1日的规定", "2022-10-01", date(2026, 9, 16)
        )
        self.assertEqual(result, {
            "as_of": "2022-10-01", "source": "request", "needs_clarification": False,
        })

    def test_complete_question_date_is_recognized(self) -> None:
        result = answering.resolve_answer_date(
            "2020年6月1日的差旅标准是什么？", None, date(2026, 9, 16)
        )
        self.assertEqual(result, {
            "as_of": "2020-06-01", "source": "question", "needs_clarification": False,
        })

    def test_ambiguous_historical_question_requires_date(self) -> None:
        result = answering.resolve_answer_date(
            "2020年的差旅标准是什么？", None, date(2026, 9, 16)
        )
        self.assertTrue(result["needs_clarification"])
        self.assertIsNone(result["as_of"])

    def test_question_without_time_uses_today(self) -> None:
        result = answering.resolve_answer_date(
            "差旅住宿标准是什么？", None, date(2026, 9, 16)
        )
        self.assertEqual(result, {
            "as_of": "2026-09-16", "source": "today", "needs_clarification": False,
        })

    def test_evidence_selection_limits_documents_and_clauses(self) -> None:
        candidates = [
            self._candidate(f"p{policy}", f"p{policy}c{clause}", 1 - policy / 10 - clause / 100, "applicable")
            for policy in range(1, 5)
            for clause in range(1, 5)
        ]
        selected = answering.select_answer_evidence(candidates)

        self.assertEqual(len(selected), 8)
        self.assertEqual(len({item["policy_id"] for item in selected}), 3)
        self.assertLessEqual(
            max(sum(item["policy_id"] == policy_id for item in selected)
                for policy_id in {item["policy_id"] for item in selected}),
            3,
        )

    def test_applicable_evidence_precedes_higher_scoring_unknown(self) -> None:
        candidates = [
            self._candidate("unknown", "c_unknown", 0.99, "unknown"),
            self._candidate("known", "c_known", 0.50, "applicable"),
        ]
        selected = answering.select_answer_evidence(candidates)

        self.assertEqual([item["clause_id"] for item in selected], ["c_known", "c_unknown"])
        self.assertEqual([item["evidence_id"] for item in selected], ["E1", "E2"])

    def test_duplicate_clause_is_selected_once(self) -> None:
        duplicate = self._candidate("p1", "c1", 0.9, "applicable")
        selected = answering.select_answer_evidence([duplicate, {**duplicate, "score": 0.8}])
        self.assertEqual(len(selected), 1)

    def test_evidence_uses_live_document_title(self) -> None:
        candidate = self._candidate("p1", "c1", 0.9, "applicable")
        candidate["metadata"]["title"] = "旧索引标题"
        candidate["policy_document"]["title"] = "最新制度标题"
        selected = answering.select_answer_evidence([candidate])
        self.assertEqual(selected[0]["title"], "最新制度标题")

    def test_validated_answer_uses_server_side_evidence_text(self) -> None:
        evidence = answering.select_answer_evidence([
            self._candidate("p1", "c1", 0.9, "applicable")
        ])
        result = answering.validate_model_answer(json.dumps({
            "conclusion": "compliant",
            "answer": "符合现有规定。",
            "conditions": [],
            "cited_evidence_ids": ["E1"],
        }, ensure_ascii=False), evidence)

        self.assertEqual(result["conclusion"], "compliant")
        self.assertEqual(result["citations"][0]["raw_text"], "条款c1")
        self.assertTrue(result["requires_human_review"])

    def test_unknown_evidence_reference_is_rejected(self) -> None:
        evidence = answering.select_answer_evidence([
            self._candidate("p1", "c1", 0.9, "applicable")
        ])
        with self.assertRaises(answering.PolicyAnswerValidationError):
            answering.validate_model_answer(json.dumps({
                "conclusion": "non_compliant",
                "answer": "不符合。",
                "conditions": [],
                "cited_evidence_ids": ["E99"],
            }, ensure_ascii=False), evidence)

    def test_definite_answer_without_citation_is_rejected(self) -> None:
        evidence = answering.select_answer_evidence([
            self._candidate("p1", "c1", 0.9, "applicable")
        ])
        with self.assertRaises(answering.PolicyAnswerValidationError):
            answering.validate_model_answer(json.dumps({
                "conclusion": "conditionally_compliant",
                "answer": "满足条件时符合。",
                "conditions": ["完成审批"],
                "cited_evidence_ids": [],
            }, ensure_ascii=False), evidence)

    def test_no_applicable_evidence_forces_undetermined_without_llm(self) -> None:
        candidates = [self._candidate("p1", "c1", 0.9, "unknown")]
        with (
            patch("policy.answering.search_indexed_policy_clauses", return_value=candidates),
            patch("policy.answering.call_answer_llm") as call_llm,
        ):
            result = answering.answer_policy_question(
                "差旅标准是什么？", today=date(2026, 9, 16)
            )

        call_llm.assert_not_called()
        self.assertEqual(result["conclusion"], "undetermined")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["citations"][0]["clause_id"], "c1")

    def test_ambiguous_time_returns_clarification_without_search(self) -> None:
        with patch("policy.answering.search_indexed_policy_clauses") as search:
            result = answering.answer_policy_question(
                "2020年的差旅标准是什么？", today=date(2026, 9, 16)
            )
        search.assert_not_called()
        self.assertEqual(result["conclusion"], "undetermined")
        self.assertIn("具体日期", result["degraded_reason"])

    def test_valid_model_output_completes_answer_flow(self) -> None:
        candidates = [self._candidate("p1", "c1", 0.9, "applicable")]
        raw = json.dumps({
            "conclusion": "conditionally_compliant",
            "answer": "完成审批后符合规定。",
            "conditions": ["完成审批"],
            "cited_evidence_ids": ["E1"],
        }, ensure_ascii=False)
        with (
            patch("policy.answering.search_indexed_policy_clauses", return_value=candidates),
            patch("policy.answering.call_answer_llm", return_value=raw),
        ):
            result = answering.answer_policy_question(
                "是否符合规定？", "2026-09-16"
            )
        self.assertEqual(result["conclusion"], "conditionally_compliant")
        self.assertFalse(result["degraded"])
        self.assertEqual(result["as_of_source"], "request")

    def test_missing_llm_configuration_degrades_with_evidence(self) -> None:
        from config import app_config
        candidates = [self._candidate("p1", "c1", 0.9, "applicable")]
        with (
            patch("policy.answering.search_indexed_policy_clauses", return_value=candidates),
            patch.object(app_config.answer_llm, "api_key", ""),
        ):
            result = answering.answer_policy_question(
                "是否符合规定？", "2026-09-16"
            )
        self.assertTrue(result["degraded"])
        self.assertEqual(result["citations"][0]["clause_id"], "c1")
        self.assertIn("ANSWER_LLM_API_KEY", result["degraded_reason"])

    def test_answer_llm_uses_independent_deepseek_configuration(self) -> None:
        from config import app_config

        evidence = answering.select_answer_evidence([
            self._candidate("p1", "c1", 0.9, "applicable")
        ])
        response = MagicMock()
        response.choices[0].message.content = '{"conclusion":"undetermined"}'
        client = MagicMock()
        client.chat.completions.create.return_value = response

        with (
            patch.object(app_config.answer_llm, "api_url", "https://deepseek.test"),
            patch.object(app_config.answer_llm, "api_key", "deepseek-key"),
            patch.object(app_config.answer_llm, "model", "deepseek-flash"),
            patch.object(app_config.answer_llm, "thinking", False),
            patch("policy.extraction.create_llm_client", return_value=client) as create_client,
        ):
            raw = answering.call_answer_llm("问题", "2026-09-16", evidence)

        self.assertEqual(raw, '{"conclusion":"undetermined"}')
        create_client.assert_called_once_with("https://deepseek.test", "deepseek-key")
        call = client.chat.completions.create.call_args
        self.assertEqual(call.kwargs["model"], "deepseek-flash")
        self.assertEqual(call.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(call.kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        client.close.assert_called_once_with()

    def test_llm_client_creation_failure_is_wrapped_for_degradation(self) -> None:
        from config import app_config
        evidence = answering.select_answer_evidence([
            self._candidate("p1", "c1", 0.9, "applicable")
        ])
        with (
            patch.object(app_config.answer_llm, "api_key", "configured"),
            patch("policy.extraction.create_llm_client", side_effect=RuntimeError("连接失败")),
        ):
            with self.assertRaises(answering.PolicyAnswerGenerationError):
                answering.call_answer_llm("问题", "2026-09-16", evidence)

    def test_invalid_model_json_degrades_with_retrieved_evidence(self) -> None:
        candidates = [self._candidate("p1", "c1", 0.9, "applicable")]
        with (
            patch("policy.answering.search_indexed_policy_clauses", return_value=candidates),
            patch("policy.answering.call_answer_llm", return_value="不是JSON"),
        ):
            result = answering.answer_policy_question("是否符合？", "2026-09-16")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["citations"][0]["raw_text"], "条款c1")

    def test_policy_answer_api_returns_orchestrated_payload(self) -> None:
        from api import policy_answer
        expected = {
            "question": "差旅标准是什么？", "conclusion": "undetermined",
            "citations": [], "degraded": True,
        }
        with patch("policy.answering.answer_policy_question", return_value=expected):
            response = asyncio.run(policy_answer(question="差旅标准是什么？", as_of=None))
        self.assertEqual(json.loads(response.body), expected)

    def test_policy_answer_form_converts_date_before_orchestration(self) -> None:
        from api import policy_answer
        expected = {"question": "差旅标准是什么？", "as_of": "2026-09-16"}
        with patch("policy.answering.answer_policy_question", return_value=expected) as answer:
            response = asyncio.run(policy_answer(
                question="差旅标准是什么？", as_of="2026-09-16"
            ))

        answer.assert_called_once_with("差旅标准是什么？", "2026-09-16")
        self.assertEqual(json.loads(response.body), expected)

    def test_policy_answer_accepts_browser_multipart_form(self) -> None:
        import httpx
        from api import app
        expected = {"question": "差旅标准是什么？", "as_of": "2026-09-16"}

        async def post_form():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post("/policy-answer", files={
                    "question": (None, "差旅标准是什么？"),
                    "as_of": (None, "2026-09-16"),
                })

        with patch("policy.answering.answer_policy_question", return_value=expected) as answer:
            response = asyncio.run(post_form())

        self.assertEqual(response.status_code, 200)
        answer.assert_called_once_with("差旅标准是什么？", "2026-09-16")
        self.assertEqual(response.json(), expected)

    def test_policy_answer_api_rejects_invalid_form_date(self) -> None:
        import httpx
        from api import app

        async def post_invalid_date(value: str):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post("/policy-answer", data={
                    "question": "差旅标准是什么？", "as_of": value,
                })

        for invalid_date in (
            "2026/09/16", "0", "1704067200", "2026-09-16T00:00:00",
            "20260916", "2026-W38-3", "2026-W38",
        ):
            with self.subTest(as_of=invalid_date):
                with patch("policy.answering.answer_policy_question") as answer:
                    response = asyncio.run(post_invalid_date(invalid_date))

                self.assertEqual(response.status_code, 422)
                answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
