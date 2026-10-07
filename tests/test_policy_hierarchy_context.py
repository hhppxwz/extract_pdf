import unittest
from unittest.mock import patch
from policy.retrieval import attach_clause_hierarchy_context
from policy.answering import select_answer_evidence, _build_answer_prompt


class HierarchyContextTests(unittest.TestCase):
    def test_parent_hit_includes_descendants_and_child_hit_includes_parent(self):
        rows = [dict(clause_id='p', parent_clause_id=None, raw_text='宣传委员职责：', sequence_no=1),
                dict(clause_id='c', parent_clause_id='p', raw_text='1. 宣传党的政策。', sequence_no=2),
                dict(clause_id='s', parent_clause_id=None, raw_text='其他职责', sequence_no=3)]
        candidates = [dict(score=1, metadata=dict(policy_id='policy', clause_id='p', raw_text=rows[0]['raw_text']))]
        with patch('policy.storage.get_policy_clauses', return_value=rows):
            result = attach_clause_hierarchy_context(candidates)
        evidence = select_answer_evidence(result)
        self.assertEqual(evidence[0]['raw_text'], rows[0]['raw_text'])
        self.assertEqual([x['clause_id'] for x in evidence[0]['related_clauses']], ['c'])
        self.assertIn('宣传党的政策', _build_answer_prompt('职责？', '2026-09-28', evidence))
        child = [dict(score=1, metadata=dict(policy_id='policy', clause_id='c', raw_text=rows[1]['raw_text']))]
        with patch('policy.storage.get_policy_clauses', return_value=rows):
            result = attach_clause_hierarchy_context(child)
        self.assertEqual(result[0]['metadata']['parent_clause_id'], 'p')
        self.assertEqual([x['clause_id'] for x in result[0]['metadata']['related_clauses']], ['p'])
