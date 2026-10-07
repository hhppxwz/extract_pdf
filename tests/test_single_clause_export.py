import json
import tempfile
import unittest
from pathlib import Path
from policy.clause_export import export_file_clause_structure


class SingleClauseExportTests(unittest.TestCase):
    def test_one_file_exports_all_policies_with_separate_clauses(self):
        documents = [dict(policy_id=key, file_id='pdf_1', source_page_start=page)
                     for key, page in [('policy_a', 0), ('policy_b', 7)]]
        with tempfile.TemporaryDirectory() as folder:
            path = export_file_clause_structure('pdf_1', Path(folder)/'result.json',
                document_loader=lambda _: documents,
                clause_loader=lambda key: [dict(clause_id=key+'_clause', raw_text=key)])
            result = json.loads(path.read_text(encoding='utf-8'))['documents']
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['source_page_start'], 0)
        self.assertEqual(result[1]['clauses'][0]['raw_text'], 'policy_b')

    def test_export_keeps_parent_ids_and_original_text(self):
        document = dict(policy_id='policy_1', file_id='pdf_1', file_name='sample.doc', title='制度')
        clauses = [dict(clause_id='child', parent_clause_id='parent', sequence_no=2, raw_text='子条款'),
                   dict(clause_id='parent', sequence_no=1, raw_text='父条款')]
        with tempfile.TemporaryDirectory() as folder:
            path = export_file_clause_structure('pdf_1', Path(folder)/'result.json',
                document_loader=lambda file_id: document, clause_loader=lambda policy_id: clauses)
            payload = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(payload['file_id'], 'pdf_1')
        self.assertEqual(payload['documents'][0]['clauses'][1]['parent_clause_id'], 'parent')
        self.assertEqual(payload['documents'][0]['clauses'][1]['raw_text'], '子条款')

    def test_non_policy_file_has_empty_documents(self):
        with tempfile.TemporaryDirectory() as folder:
            path = export_file_clause_structure('pdf_1', Path(folder)/'result.json',
                document_loader=lambda _: None, clause_loader=lambda _: [])
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['documents'], [])
