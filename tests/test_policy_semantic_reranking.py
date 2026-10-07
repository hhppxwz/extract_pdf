"""验证通用语义重排及异常降级。"""
import json
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from types import SimpleNamespace

from policy.semantic_reranking import semantic_rerank_candidates, invalidate_semantic_rerank_cache


class SemanticRerankingTests(unittest.TestCase):
    def setUp(self):
        invalidate_semantic_rerank_cache()

    def scores(self, *values):
        return json.dumps({"scores": [{"index": i, "score": value} for i, value in enumerate(values)]})

    def candidates(self):
        return [
            {"metadata": {"clause_id": "a", "title": "人员管理", "raw_text": "岗位聘期三年"}, "score": 0.03},
            {"metadata": {"clause_id": "b", "title": "培养规定", "raw_text": "普通研究生学制三年"}, "score": 0.01},
        ]

    def test_candidate_limit_bounds_model_input(self):
        from config import app_config
        with patch.object(app_config.answer_llm, "rerank_candidate_limit", 1), patch(
            "policy.semantic_reranking.call_rerank_llm", return_value=self.scores(90)
        ) as call:
            result = semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual(len(call.call_args.args[1]), 1)
        self.assertEqual(len(result), 1)

    def test_same_query_reuses_scores_and_does_not_cache_evidence(self):
        rows = self.candidates()
        with patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)) as call:
            first = semantic_rerank_candidates("问题", rows)
            first[0]["score"] = -1
            rows[1]["score"] = 0.02
            second = semantic_rerank_candidates("问题", rows)
        call.assert_called_once()
        self.assertEqual(second[0]["score"], 95)
        self.assertEqual(second[0]["retrieval_score"], 0.02)
        self.assertTrue(second[0]["rerank_cache_hit"])

    def test_changed_text_model_date_and_invalidation_do_not_reuse_cache(self):
        from config import app_config
        with patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)) as call:
            rows = self.candidates()
            semantic_rerank_candidates("问题", rows)
            rows[0]["metadata"]["raw_text"] += "修订"
            semantic_rerank_candidates("问题", rows)
            with patch.object(app_config.answer_llm, "model", "another"):
                semantic_rerank_candidates("问题", rows)
            semantic_rerank_candidates("问题", rows, cache_context="2025-01-01")
            invalidate_semantic_rerank_cache()
            semantic_rerank_candidates("问题", rows)
        self.assertEqual(call.call_count, 5)

    def test_cache_expiry_and_failure_retry(self):
        from config import app_config
        with patch.object(app_config.answer_llm, "rerank_cache_ttl_seconds", 10), patch(
            "policy.semantic_reranking.time.monotonic", return_value=0
        ) as clock, patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)) as call:
            semantic_rerank_candidates("问题", self.candidates())
            clock.return_value = 11
            semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual(call.call_count, 2)
        invalidate_semantic_rerank_cache()
        with patch("policy.semantic_reranking.call_rerank_llm", side_effect=[TimeoutError(), self.scores(0, 95)]) as call:
            semantic_rerank_candidates("问题", self.candidates())
            result = semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result[0]["rerank_mode"], "semantic")

    def test_cache_capacity_evicts_least_recently_used_scores(self):
        from config import app_config
        with patch.object(app_config.answer_llm, "rerank_cache_max_entries", 1), patch(
            "policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)
        ) as call:
            for question in ("问题一", "问题二", "问题一"):
                semantic_rerank_candidates(question, self.candidates())
        self.assertEqual(call.call_count, 3)

    def test_identical_concurrent_requests_share_model_call(self):
        started, release, waiting = threading.Event(), threading.Event(), threading.Event()
        from policy import semantic_reranking as module
        original_result = module.Future.result

        def shared_result(future, *args, **kwargs):
            waiting.set()
            return original_result(future, *args, **kwargs)

        def answer(*args):
            started.set()
            self.assertTrue(release.wait(3))
            return self.scores(0, 95)

        with patch("policy.semantic_reranking.call_rerank_llm", side_effect=answer) as call, patch.object(
            module.Future, "result", shared_result
        ), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(semantic_rerank_candidates, "问题", self.candidates())
            self.assertTrue(started.wait(3))
            second = pool.submit(semantic_rerank_candidates, "问题", self.candidates())
            try:
                self.assertTrue(waiting.wait(3))
            finally:
                release.set()
            self.assertEqual(first.result()[0]["score"], 95)
            self.assertTrue(second.result()[0]["rerank_cache_hit"])
        call.assert_called_once()

    def test_invalidation_during_request_prevents_cache_repopulation(self):
        def answer(*args):
            invalidate_semantic_rerank_cache()
            return self.scores(0, 95)
        with patch("policy.semantic_reranking.call_rerank_llm", side_effect=answer) as call:
            semantic_rerank_candidates("问题", self.candidates())
            semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual(call.call_count, 2)

    def test_index_invalidation_clears_semantic_cache(self):
        from policy.retrieval import invalidate_policy_clause_bm25_index
        with patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)) as call:
            semantic_rerank_candidates("问题", self.candidates())
            invalidate_policy_clause_bm25_index()
            semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual(call.call_count, 2)

    def test_semantic_score_overrides_fusion_score_without_changing_evidence(self):
        rows = self.candidates()
        with patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)):
            ranked = semantic_rerank_candidates("要读几年", rows)
        self.assertEqual(ranked[0]["metadata"], rows[1]["metadata"])
        self.assertEqual(ranked[0]["score"], 95)
        self.assertEqual(ranked[0]["retrieval_score"], 0.01)
        self.assertEqual(rows[1]["score"], 0.01)

    def test_reimbursement_question_uses_same_interface(self):
        rows = self.candidates()
        rows[1]["metadata"]["raw_text"] = "报销须提交发票和审批单"
        with patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)) as call:
            ranked = semantic_rerank_candidates("报销要带什么", rows)
        self.assertEqual(call.call_args.args[0], "报销要带什么")
        self.assertEqual(ranked[0]["metadata"]["clause_id"], "b")

    def test_invalid_scores_and_service_failure_preserve_original_order(self):
        for raw in ['{}', '{"scores": [3]}', self.scores(True, 3), self.scores(101, 0), self.scores(float('nan'), 3),
                    '{"scores":[{"index":0,"score":80},{"index":0,"score":90}]}']:
            with self.subTest(raw=raw), patch("policy.semantic_reranking.call_rerank_llm", return_value=raw):
                ranked = semantic_rerank_candidates("问题", self.candidates())
            self.assertEqual([r["metadata"]["clause_id"] for r in ranked], ["a", "b"])
            self.assertIn("降级", ranked[0]["rerank_warning"])
        with patch("policy.semantic_reranking.call_rerank_llm", side_effect=TimeoutError()):
            ranked = semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual(ranked[0]["score"], 0.03)

    def test_empty_candidates_do_not_call_model(self):
        with patch("policy.semantic_reranking.call_rerank_llm") as call:
            self.assertEqual(semantic_rerank_candidates("问题", []), [])
        call.assert_not_called()

    def test_search_response_exposes_fallback_without_date_filter(self):
        from policy.retrieval import build_policy_search_response
        rows = self.candidates()
        rows[0]["rerank_warning"] = "语义重排不可用，已降级。"
        rows[0]["rerank_mode"] = "fusion_fallback"
        response = build_policy_search_response("问题", rows)
        self.assertIn("语义重排不可用，已降级。", response["warnings"])
        self.assertEqual(response["results"][0]["rerank_mode"], "fusion_fallback")

    def test_equal_semantic_scores_keep_retrieval_order(self):
        with patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(70, 70)):
            ranked = semantic_rerank_candidates("问题", self.candidates())
        self.assertEqual([r["metadata"]["clause_id"] for r in ranked], ["a", "b"])

    def test_search_reranks_before_top_k_cutoff(self):
        from config import app_config
        from policy.retrieval import search_indexed_policy_clauses
        rows = self.candidates()
        for row in rows:
            row["rrf_score"] = row["score"]
        storage = SimpleNamespace(vector=SimpleNamespace(count_vectors=lambda name: 2))
        with (
            patch("storage_adapter.storage", storage),
            patch("policy.retrieval._encode_policy_clause_texts", return_value=[[1.0]]),
            patch.object(storage.vector, "search", return_value=rows, create=True),
            patch("policy.retrieval.search_policy_clauses_bm25", return_value=[]),
            patch("policy.retrieval.rrf_fusion", return_value=rows),
            patch.object(app_config.answer_llm, "rerank_enabled", True),
            patch("policy.semantic_reranking.call_rerank_llm", return_value=self.scores(0, 95)),
        ):
            result = search_indexed_policy_clauses("问题", top_k=1)
        self.assertEqual(result[0]["metadata"]["clause_id"], "b")
