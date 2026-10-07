"""验证预热执行顺序及失败后的服务降级。"""
import asyncio
import unittest
from unittest.mock import patch

from policy.warmup import warmup_policy_qa


class PolicyWarmupTests(unittest.TestCase):
    def test_warmup_encodes_text_and_initializes_bm25(self):
        with patch('policy.retrieval._encode_policy_clause_texts', return_value=[[0.1]]) as encode, \
             patch('policy.retrieval._get_bm25_index_snapshot', return_value=(object(), [{}, {}])) as bm25:
            result = warmup_policy_qa()
        self.assertEqual(result['embedding']['status'], 'ready')
        self.assertEqual(result['bm25']['document_count'], 2)
        encode.assert_called_once()
        self.assertTrue(encode.call_args.args[0][0])
        bm25.assert_called_once()

    def test_embedding_failure_does_not_prevent_bm25_warmup(self):
        with patch('policy.retrieval._encode_policy_clause_texts', side_effect=RuntimeError('模型不可用')), \
             patch('policy.retrieval._get_bm25_index_snapshot', return_value=(object(), [])) as bm25, \
             self.assertLogs('policy.warmup', level='WARNING'):
            result = warmup_policy_qa()
        self.assertEqual(result['embedding']['status'], 'failed')
        self.assertEqual(result['bm25']['status'], 'ready')
        bm25.assert_called_once()

    def test_bm25_failure_is_reported_without_raising(self):
        with patch('policy.retrieval._encode_policy_clause_texts', return_value=[[0.1]]), \
             patch('policy.retrieval._get_bm25_index_snapshot', side_effect=RuntimeError('数据库不可用')), \
             self.assertLogs('policy.warmup', level='WARNING'):
            result = warmup_policy_qa()
        self.assertEqual(result['embedding']['status'], 'ready')
        self.assertEqual(result['bm25']['status'], 'failed')

    def test_app_startup_waits_for_warmup(self):
        from api import app
        result = {'embedding': {'status': 'ready'}, 'bm25': {'status': 'ready'}}
        async def run():
            async with app.router.lifespan_context(app):
                self.assertEqual(app.state.policy_qa_warmup, result)
        with patch('policy.warmup.warmup_policy_qa', return_value=result) as warmup:
            asyncio.run(run())
        warmup.assert_called_once()
