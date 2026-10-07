"""用真实 SQL 验证默认效力回填不会恢复已废止或存在冲突的制度。"""
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from policy.storage import backfill_default_policy_validity, upsert_policy_document


class MigrationDatabase:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript('''
            CREATE TABLE policy_documents(policy_id TEXT PRIMARY KEY, validity_status TEXT,
                structure_status TEXT, expiry_date TEXT, updated_at TEXT);
            CREATE TABLE policy_document_relations(target_policy_id TEXT, review_status TEXT);
            CREATE TABLE policy_review_items(policy_id TEXT, status TEXT, issue_type TEXT);
            INSERT INTO policy_documents VALUES
                ('normal','unknown','succeeded',NULL,NULL),
                ('abolished','unknown','succeeded',NULL,NULL),
                ('invalid','invalid','succeeded',NULL,NULL),
                ('conflict','unknown','succeeded',NULL,NULL),
                ('pending','unknown','pending',NULL,NULL),
                ('future_abolition','unknown','succeeded','2030-01-01',NULL);
            INSERT INTO policy_document_relations VALUES ('abolished','approved');
            INSERT INTO policy_review_items VALUES ('conflict','pending','effective_date_conflict');
        ''')

    def execute(self, sql, params=()):
        self.conn.execute(sql.replace('NOW()', 'CURRENT_TIMESTAMP'), params)

    @contextmanager
    def transaction(self):
        with self.conn:
            yield


class DefaultValidityTests(unittest.TestCase):
    def test_backfill_preserves_abolitions_conflicts_and_unimported_records(self):
        from policy.storage import TABLE_POLICY_REVIEWS
        db = MigrationDatabase()
        if TABLE_POLICY_REVIEWS != 'policy_review_items':
            db.conn.execute(f'ALTER TABLE policy_review_items RENAME TO {TABLE_POLICY_REVIEWS}')
        with patch('policy.storage.ensure_policy_tables'), patch('policy.storage.storage.relational', db):
            backfill_default_policy_validity()
            backfill_default_policy_validity()
        self.assertEqual(dict(db.conn.execute('SELECT policy_id,validity_status FROM policy_documents')), {
            'normal': 'current', 'abolished': 'invalid', 'invalid': 'invalid',
            'conflict': 'unknown', 'pending': 'unknown', 'future_abolition': 'invalid'})
        db.conn.close()

    def test_new_document_is_current_and_reimport_preserves_invalid_state(self):
        with patch('policy.storage.ensure_policy_tables'), patch('policy.storage.storage.relational.execute') as execute:
            upsert_policy_document('pdf_test', 'test.pdf', {}, 1, 'v1')
        sql, params = execute.call_args.args
        self.assertEqual(params[11], 'current')
        self.assertIn("THEN 'invalid'", sql)
