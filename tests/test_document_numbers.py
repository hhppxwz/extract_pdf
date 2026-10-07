import unittest
from unittest.mock import patch

from metadata.policy import extract_policy_metadata
from models import BlockType, ContentBlock
from policy.abolition import normalize_doc_number, extract_abolition_candidates
from policy.storage import find_policy_documents_by_doc_number, upsert_policy_document


class DocumentNumberTests(unittest.TestCase):
    def test_abolition_recovers_number_without_changing_evidence(self):
        raw = '原《武汉大学本科专业设置与管理规定（2013 年修订）》（武大教字〔201314 号）同时废止。'
        candidates = extract_abolition_candidates({'raw_text': raw})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['target_doc_number'], '武大教字〔2013〕14号')
        self.assertEqual(candidates[0]['evidence_text'], raw)

    def test_missing_year_closing_bracket_is_recovered(self):
        self.assertEqual(normalize_doc_number('武大教字〔201314 号'), '武大教字[2013]14号')
        self.assertEqual(normalize_doc_number('武大产字〔20221 号'), '武大产字[2022]1号')
        self.assertEqual(normalize_doc_number('武大教字〔20130014号'), '武大教字[2013]14号')
        self.assertEqual(normalize_doc_number('编号201314号'), '编号201314号')
        self.assertEqual(normalize_doc_number('武大教字〔2013号'), '武大教字[2013号')

    def test_normalization_changes_only_sequence(self):
        for sequence in ('041', '0041', '41'):
            self.assertEqual(normalize_doc_number(f'武大教务字〔2007〕{sequence}号'), '武大教务字[2007]41号')
        self.assertEqual(normalize_doc_number('武大字〔2007〕000号'), '武大字[2007]0号')
        self.assertEqual(normalize_doc_number('编号0041'), '编号0041')

    def test_metadata_preserves_original_number(self):
        meta = extract_policy_metadata([ContentBlock(type=BlockType.TEXT, content='武大教务字〔2007〕041号')])
        self.assertEqual(meta['doc_number'], '武大教务字〔2007〕41号')
        self.assertEqual(meta['doc_number_raw'], '武大教务字〔2007〕041号')

    def test_lookup_matches_existing_zero_padded_record(self):
        docs = [{'policy_id': 'old', 'doc_number': '武大教务字〔2007〕0041号'},
                {'policy_id': 'other', 'doc_number': '武大教务字〔2007〕42号'}]
        with patch('policy.storage.storage.relational.query', return_value=docs):
            self.assertEqual(find_policy_documents_by_doc_number('武大教务字〔2007〕41号'), docs[:1])

    def test_storage_persists_original_and_normalized_number(self):
        with patch('policy.storage.ensure_policy_tables'), patch('policy.storage.storage.relational.execute') as execute:
            upsert_policy_document('pdf_test', 'test.pdf', {'doc_number': '武大教务字〔2007〕041号'},
                                   parse_quality=1, structure_version='test')
        sql, params = execute.call_args.args
        self.assertIn('doc_number_raw', sql)
        self.assertIn('武大教务字〔2007〕41号', params)
        self.assertIn('武大教务字〔2007〕041号', params)
