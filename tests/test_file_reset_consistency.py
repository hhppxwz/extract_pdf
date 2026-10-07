"""通过真实 SQL 验证重置时的跨制度一致性及事务回滚。"""
import sqlite3
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from file_reset import load_folder_reset_plan, reset_file_database_artifacts, reset_folder_database_artifacts


class MissingTable(Exception):
    pgcode = "42P01"


class SqlStorage:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript('''
            CREATE TABLE pdf_files (file_id TEXT PRIMARY KEY, status TEXT, error_message TEXT, last_attempt_at TEXT);
            CREATE TABLE pdf_batch_items (file_id TEXT, reuse_file_id TEXT, status TEXT);
            CREATE TABLE policy_documents (policy_id TEXT PRIMARY KEY, file_id TEXT, validity_status TEXT,
                expiry_date TEXT, structure_status TEXT, parse_quality REAL);
            CREATE TABLE policy_clauses (clause_id TEXT PRIMARY KEY, policy_id TEXT);
            CREATE TABLE policy_document_relations (relation_id TEXT PRIMARY KEY, source_policy_id TEXT,
                target_policy_id TEXT, evidence_clause_id TEXT REFERENCES policy_clauses(clause_id),
                review_status TEXT, effective_date TEXT);
            CREATE TABLE policy_family_candidates (candidate_id TEXT, relation_id TEXT REFERENCES policy_document_relations(relation_id),
                source_policy_id TEXT, target_policy_id TEXT);
            CREATE TABLE policy_clause_classification_items (clause_id TEXT REFERENCES policy_clauses(clause_id),
                status TEXT DEFAULT 'succeeded', run_id TEXT DEFAULT 'class_run');
            CREATE TABLE policy_clause_classification_results (clause_id TEXT REFERENCES policy_clauses(clause_id));
            INSERT INTO pdf_files VALUES ('pdf_a', 'done', '', NULL), ('pdf_b', 'done', '', NULL), ('pdf_c', 'done', '', NULL);
            INSERT INTO policy_documents VALUES ('a','pdf_a','unknown',NULL,'succeeded',1),
                ('b','pdf_b','invalid','2025-01-01','succeeded',1), ('c','pdf_c','unknown',NULL,'succeeded',1);
            INSERT INTO policy_clauses VALUES ('a1','a'), ('b1','b'), ('c1','c');
            INSERT INTO policy_document_relations VALUES ('ab','a','b','a1','approved','2025-01-01');
        ''')

    def execute(self, sql, params=()):
        try:
            return self.conn.execute(sql.replace("%s", "?"),
                                     tuple(value.isoformat() if isinstance(value, datetime) else value for value in params))
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                raise MissingTable(str(error)) from error
            raise

    def query(self, table_name, where="", limit=100, params=()):
        return [dict(row) for row in self.execute(
            f'SELECT * FROM "{table_name}"' + (f" WHERE {where}" if where else "") + f" LIMIT {limit}", params)]

    query_for_update = query

    def update_rows(self, table_name, values, where, params=()):
        return self.execute(f'UPDATE "{table_name}" SET ' + ", ".join(f'"{key}" = %s' for key in values)
                            + f" WHERE {where}", (*values.values(), *params)).rowcount

    @contextmanager
    def transaction(self):
        self.execute("BEGIN")
        try:
            yield
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise


class ResetConsistencyTests(unittest.TestCase):
    def test_reset_cleans_every_policy_linked_to_same_file(self):
        self.db.execute("INSERT INTO policy_documents VALUES ('a_extra','pdf_a','unknown',NULL,'succeeded',1)")
        self.db.execute("INSERT INTO policy_clauses VALUES ('extra_clause','a_extra')")
        reset_file_database_artifacts('pdf_a')
        self.assertEqual(self.db.query('policy_clauses', "policy_id IN ('a','a_extra')"), [])
        self.assertTrue(all(doc['structure_status'] == 'pending'
                            for doc in self.db.query('policy_documents', "file_id='pdf_a'")))

    def test_purge_alone_ignores_recent_file_structure_and_clause_states(self):
        self.add_knowledge_task(datetime.now().isoformat())
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=%s WHERE file_id='pdf_a'", (datetime.now().isoformat(),))
        self.db.execute("UPDATE policy_documents SET structure_status='running' WHERE policy_id='a'")
        self.db.execute("INSERT INTO pdf_batch_items VALUES ('pdf_a',NULL,'running')")
        plan = {**self.folder_plan(), "running_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            result = reset_folder_database_artifacts("folder", confirm=True, release_stale=True, purge=True)
        self.assertTrue(result['force'])
        self.assertEqual(result['reset_file_ids'], ['pdf_a'])
        self.assertEqual(self.db.query('pdf_files', "file_id='pdf_a'")[0]['status'],'pending')
        self.assertEqual(self.db.query('pdf_batch_items')[0]['status'],'pending')
        self.assertEqual(self.db.query('policy_knowledge_items_v2'),[])

    def test_purge_preview_and_unknown_timestamps(self):
        self.add_knowledge_task()
        self.db.execute("UPDATE policy_knowledge_items_v2 SET started_at=NULL")
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=NULL WHERE file_id='pdf_a'")
        plan = {**self.folder_plan(), "running_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            reset_folder_database_artifacts('folder',purge=True)
            self.assertEqual(self.db.query('pdf_files', "file_id='pdf_a'")[0]['status'],'processing')
            reset_folder_database_artifacts('folder',confirm=True,purge=True)
        self.assertEqual(self.db.query('policy_knowledge_runs_v2'), [])

    def test_purge_reset_rolls_back_released_task_states(self):
        self.add_knowledge_task(datetime.now().isoformat())
        self.db.execute("INSERT INTO pdf_batch_items VALUES ('pdf_a',NULL,'running')")
        original = self.db.update_rows
        def fail(table,*args):
            if table == 'pdf_files':
                raise RuntimeError('模拟失败')
            return original(table,*args)
        with patch("file_reset.load_folder_reset_plan",return_value=self.folder_plan()), patch.object(self.db,'update_rows',side_effect=fail):
            with self.assertRaises(RuntimeError):
                reset_folder_database_artifacts('folder',confirm=True,purge=True)
        self.assertEqual(self.db.query('pdf_batch_items')[0]['status'],'running')
        self.assertEqual(self.db.query('policy_knowledge_items_v2')[0]['status'],'running')
        self.assertEqual(len(self.db.query('policy_clauses')),3)

    def add_knowledge_task(self, stamp=None):
        old = stamp or (datetime.now() - timedelta(days=4)).isoformat()
        self.db.conn.executescript('''
            CREATE TABLE policy_knowledge_runs_v2 (run_id TEXT PRIMARY KEY,status TEXT,started_at TEXT,finished_at TEXT);
            CREATE TABLE policy_knowledge_items_v2 (clause_id TEXT REFERENCES policy_clauses(clause_id),
                run_id TEXT,status TEXT,started_at TEXT,finished_at TEXT,last_error TEXT);
        ''')
        self.db.execute("INSERT INTO policy_knowledge_runs_v2 VALUES ('old_run','running',%s,NULL)", (old,))
        self.db.execute("INSERT INTO policy_knowledge_items_v2 VALUES ('a1','old_run','running',%s,NULL,'')", (old,))

    def folder_plan(self):
        return {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": []}

    def test_stale_knowledge_task_is_released_only_with_explicit_flag(self):
        self.add_knowledge_task()
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()):
            with self.assertRaisesRegex(ValueError, "条款任务"):
                reset_folder_database_artifacts("folder", confirm=True)
            result = reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(result["released_stale_policy_run_ids"], ["old_run"])
        self.assertEqual(self.db.query("policy_knowledge_items_v2"), [])
        self.assertEqual(self.db.query("policy_knowledge_runs_v2")[0]["status"], "failed")

    def test_recent_activity_elsewhere_in_run_prevents_stale_release(self):
        self.add_knowledge_task()
        self.db.execute("INSERT INTO policy_knowledge_items_v2 VALUES ('c1','old_run','succeeded',NULL,%s,'')",
                        (datetime.now().isoformat(),))
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()):
            with self.assertRaisesRegex(ValueError, "近期|正在处理"):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_unknown_task_time_is_not_released(self):
        self.add_knowledge_task()
        self.db.execute("UPDATE policy_knowledge_items_v2 SET started_at=NULL")
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()):
            with self.assertRaises(ValueError):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)

    def test_real_clause_search_index_is_cleared(self):
        names = []
        self.storage.vector.delete_vectors_by_metadata = lambda name, *args: names.append(name) or 0
        reset_file_database_artifacts("pdf_a")
        self.assertIn("policy_clause_search", names)

    def test_purge_removes_old_metadata_incoming_relations_and_empty_runs(self):
        self.add_knowledge_task()
        for field in ('title TEXT', 'doc_number TEXT', 'effective_date TEXT', 'family_id TEXT', 'current_extraction_run_id TEXT'):
            self.db.execute('ALTER TABLE policy_documents ADD COLUMN ' + field)
        self.db.execute("UPDATE policy_documents SET title='旧标题',doc_number='旧文号',effective_date='2024-01-01',family_id='old',current_extraction_run_id='old_run' WHERE policy_id='a'")
        self.db.execute("INSERT INTO policy_document_relations VALUES ('ca','c','a','c1','approved','2025-01-01')")
        self.db.execute("INSERT INTO policy_family_candidates (candidate_id,source_policy_id,target_policy_id) VALUES ('ca_title','c','a')")
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()):
            reset_folder_database_artifacts("folder", confirm=True, release_stale=True, purge=True)
        doc = self.db.query("policy_documents", "policy_id='a'")[0]
        self.assertEqual((doc['title'],doc['doc_number'],doc['effective_date'],doc['family_id'],doc['current_extraction_run_id']), ('','',None,None,None))
        self.assertEqual(self.db.query("policy_document_relations"), [])
        self.assertEqual(self.db.query("policy_knowledge_runs_v2"), [])
        self.assertEqual(self.db.query("policy_family_candidates"), [])
        self.assertEqual(len(self.db.query("policy_clauses")), 2)

    def test_purge_preview_does_not_release_tasks_or_delete_artifacts(self):
        self.add_knowledge_task()
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()):
            result = reset_folder_database_artifacts("folder", release_stale=True, purge=True)
        self.assertTrue(result["purge"])
        self.assertTrue(result["force"])
        self.assertEqual(self.db.query("policy_knowledge_items_v2")[0]["status"], "running")
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_cleanup_failure_rolls_back_stale_clause_run_release(self):
        self.add_knowledge_task()
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()), patch(
            "file_reset.reset_file_database_artifacts", side_effect=RuntimeError("清理失败")
        ):
            with self.assertRaises(RuntimeError):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(self.db.query("policy_knowledge_runs_v2")[0]["status"], "running")
        self.assertEqual(self.db.query("policy_knowledge_items_v2")[0]["status"], "running")

    def test_purge_preserves_shared_run_and_unrelated_clauses(self):
        self.add_knowledge_task()
        self.db.execute("UPDATE policy_knowledge_runs_v2 SET status='succeeded'")
        self.db.execute("UPDATE policy_knowledge_items_v2 SET status='succeeded'")
        self.db.execute("INSERT INTO policy_knowledge_items_v2 VALUES ('c1','old_run','succeeded',NULL,NULL,'')")
        with patch("file_reset.load_folder_reset_plan", return_value=self.folder_plan()):
            reset_folder_database_artifacts("folder", confirm=True, purge=True)
        self.assertEqual(len(self.db.query("policy_knowledge_runs_v2")), 1)
        self.assertEqual(self.db.query("policy_knowledge_items_v2")[0]["clause_id"], "c1")

    def setUp(self):
        self.db = SqlStorage()
        self.storage = SimpleNamespace(relational=self.db, vector=SimpleNamespace(delete_vectors_by_metadata=lambda *args: 0))
        self.patcher = patch("file_reset.storage", self.storage)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.db.conn.close)

    def test_source_reset_invalidates_unsupported_status(self):
        reset_file_database_artifacts("pdf_a")
        target = self.db.query("policy_documents", "policy_id='b'")[0]
        self.assertEqual((target["validity_status"], target["expiry_date"]), ("current", None))
        self.assertEqual(self.db.query("policy_clauses"), [{"clause_id": "b1", "policy_id": "b"}, {"clause_id": "c1", "policy_id": "c"}])

    def test_remaining_approved_relation_keeps_target_invalid(self):
        self.db.execute("INSERT INTO policy_document_relations VALUES ('cb','c','b','c1','approved','2026-01-01')")
        reset_file_database_artifacts("pdf_a")
        target = self.db.query("policy_documents", "policy_id='b'")[0]
        self.assertEqual((target["validity_status"], target["expiry_date"]), ("invalid", "2026-01-01"))

    def test_target_reset_preserves_incoming_relation(self):
        reset_file_database_artifacts("pdf_b")
        self.assertEqual(self.db.query("policy_document_relations")[0]["relation_id"], "ab")
        self.assertEqual(self.db.query("policy_documents", "policy_id='b'")[0]["validity_status"], "invalid")

    def test_unrelated_expiry_date_is_preserved(self):
        self.db.execute("UPDATE policy_documents SET expiry_date='2027-01-01' WHERE policy_id='b'")
        reset_file_database_artifacts("pdf_a")
        target = self.db.query("policy_documents", "policy_id='b'")[0]
        self.assertEqual((target["validity_status"], target["expiry_date"]), ("invalid", "2027-01-01"))

    def test_all_foreign_key_dependents_are_removed(self):
        self.db.execute("INSERT INTO policy_family_candidates (candidate_id,relation_id) VALUES ('family_ab','ab')")
        self.db.execute("INSERT INTO policy_clause_classification_items (clause_id) VALUES ('a1')")
        self.db.execute("INSERT INTO policy_clause_classification_results VALUES ('a1')")
        reset_file_database_artifacts("pdf_a")
        self.assertEqual(self.db.query("policy_family_candidates"), [])
        self.assertEqual(self.db.query("policy_clause_classification_results"), [])

    def test_running_file_cannot_be_reset(self):
        self.db.execute("UPDATE pdf_files SET status='processing' WHERE file_id='pdf_a'")
        with self.assertRaisesRegex(ValueError, "正在处理"):
            reset_file_database_artifacts("pdf_a")
        self.assertEqual(len(self.db.query("policy_document_relations")), 1)

    def test_failure_rolls_back_relations_and_clauses(self):
        original = self.db.update_rows
        def fail_at_requeue(table, *args):
            if table == "pdf_files":
                raise RuntimeError("模拟更新失败")
            return original(table, *args)
        with patch.object(self.db, "update_rows", side_effect=fail_at_requeue):
            with self.assertRaises(RuntimeError):
                reset_file_database_artifacts("pdf_a")
        self.assertEqual(len(self.db.query("policy_document_relations")), 1)
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_shared_override_allows_completed_file_reset(self):
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": ["pdf_a"], "running_file_ids": []}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            result = reset_folder_database_artifacts("folder", confirm=True, allow_shared=True)
        self.assertEqual(result["reset_file_ids"], ["pdf_a"])
        self.assertEqual(self.db.query("pdf_files", "file_id='pdf_a'")[0]["status"], "pending")

    def test_shared_override_blocks_active_batch_owner(self):
        self.db.execute("INSERT INTO pdf_batch_items VALUES ('pdf_a',NULL,'running')")
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": ["pdf_a"], "running_file_ids": []}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaisesRegex(ValueError, "正在处理"):
                reset_folder_database_artifacts("folder", confirm=True, allow_shared=True)
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_active_classification_prevents_reset(self):
        self.db.execute("INSERT INTO policy_clause_classification_items (clause_id,status) VALUES ('a1','running')")
        with self.assertRaisesRegex(ValueError, "正在处理"):
            reset_file_database_artifacts("pdf_a")
        self.assertEqual(len(self.db.query("policy_document_relations")), 1)

    def test_folder_failure_rolls_back_previous_files(self):
        self.db.execute("UPDATE pdf_files SET status='processing' WHERE file_id='pdf_b'")
        plan = {"file_ids": ["pdf_a", "pdf_b"], "shared_file_ids": [], "running_file_ids": []}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaises(ValueError):
                reset_folder_database_artifacts("folder", confirm=True)
        self.assertEqual(len(self.db.query("policy_document_relations")), 1)
        self.assertEqual(self.db.query("pdf_files", "file_id='pdf_a'")[0]["status"], "done")

    def test_pending_relation_does_not_change_target_validity(self):
        self.db.execute("UPDATE policy_document_relations SET review_status='pending'")
        reset_file_database_artifacts("pdf_a")
        self.assertEqual(self.db.query("policy_documents", "policy_id='b'")[0]["validity_status"], "invalid")

    def test_remaining_approved_relation_without_date_clears_stale_date(self):
        self.db.execute("INSERT INTO policy_document_relations VALUES ('cb','c','b','c1','approved',NULL)")
        reset_file_database_artifacts("pdf_a")
        target = self.db.query("policy_documents", "policy_id='b'")[0]
        self.assertEqual((target["validity_status"], target["expiry_date"]), ("invalid", None))

    def test_cross_policy_matter_members_do_not_block_reset(self):
        self.db.conn.executescript('''
            CREATE TABLE policy_matters_v2 (matter_id TEXT PRIMARY KEY, policy_id TEXT);
            CREATE TABLE policy_matter_members_v2 (matter_id TEXT REFERENCES policy_matters_v2(matter_id),
                clause_id TEXT REFERENCES policy_clauses(clause_id));
            INSERT INTO policy_matters_v2 VALUES ('c_matter','c');
            INSERT INTO policy_matter_members_v2 VALUES ('c_matter','a1');
        ''')
        reset_file_database_artifacts("pdf_a")
        self.assertEqual(self.db.query("policy_matter_members_v2"), [])
        self.assertEqual(len(self.db.query("policy_matters_v2")), 1)

    def test_preview_reports_external_abolition_target_without_changes(self):
        self.db.execute("ALTER TABLE pdf_batch_items ADD COLUMN file_path TEXT")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "sample"
            folder.mkdir()
            self.db.execute("INSERT INTO pdf_batch_items VALUES ('pdf_a',NULL,'succeeded',%s)", (str(folder / "a.pdf"),))
            plan = load_folder_reset_plan(folder)
        self.assertEqual(plan["file_ids"], ["pdf_a"])
        self.assertEqual(plan["affected_policy_ids"], ["b"])
        self.assertEqual(plan["abolition_relation_count"], 1)
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_shared_reset_requires_explicit_override(self):
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": ["pdf_a"], "running_file_ids": []}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaisesRegex(ValueError, "allow-shared-reset"):
                reset_folder_database_artifacts("folder", confirm=True)
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_conflicting_remaining_dates_roll_back_reset(self):
        self.db.execute("INSERT INTO policy_clauses VALUES ('c2','c')")
        self.db.execute("INSERT INTO policy_document_relations VALUES ('cb','c','b','c1','approved','2026-01-01')")
        self.db.execute("INSERT INTO policy_document_relations VALUES ('cb2','c','b','c2','approved','2027-01-01')")
        with self.assertRaisesRegex(ValueError, "日期冲突"):
            reset_file_database_artifacts("pdf_a")
        self.assertEqual(len(self.db.query("policy_document_relations")), 3)

    def test_invalid_id_is_rejected_before_opening_database(self):
        with patch.object(self.db, "transaction", side_effect=AssertionError("不应访问数据库")):
            with self.assertRaisesRegex(ValueError, "格式非法"):
                reset_file_database_artifacts("bad/id")

    def test_single_import_preview_does_not_require_batch_table(self):
        self.db.execute("DROP TABLE pdf_batch_items")
        self.db.execute("ALTER TABLE pdf_files ADD COLUMN file_hash TEXT")
        self.db.execute("UPDATE pdf_files SET file_hash=%s WHERE file_id='pdf_a'", (hashlib.sha256(b"pdf_a").hexdigest(),))
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "sample"
            folder.mkdir()
            (folder / "a.pdf").write_bytes(b"pdf_a")
            plan = load_folder_reset_plan(folder)
        self.assertEqual(plan["file_ids"], ["pdf_a"])

    def test_active_run_between_items_prevents_reset(self):
        self.db.conn.executescript('''
            CREATE TABLE policy_clause_classification_runs (run_id TEXT, status TEXT);
            INSERT INTO policy_clause_classification_runs VALUES ('class_run','running');
            INSERT INTO policy_clause_classification_items (clause_id,status) VALUES ('a1','pending');
        ''')
        with self.assertRaisesRegex(ValueError, "正在处理"):
            reset_file_database_artifacts("pdf_a")
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_stale_release_is_explicit_and_cannot_release_recent_processing(self):
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=%s WHERE file_id='pdf_a'",
                        ((datetime.now() - timedelta(days=2)).isoformat(),))
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": ["pdf_a"], "stale_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaises(ValueError):
                reset_folder_database_artifacts("folder", confirm=True)
            preview = reset_folder_database_artifacts("folder", release_stale=True)
            self.assertEqual(preview["file_ids"], ["pdf_a"])
            self.assertEqual(self.db.query("pdf_files", "file_id='pdf_a'")[0]["status"], "processing")
            result = reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(result["released_stale_file_ids"], ["pdf_a"])
        self.assertEqual(self.db.query("pdf_files", "file_id='pdf_a'")[0]["status"], "pending")

    def test_stale_preview_cannot_override_fresh_file_attempt(self):
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=%s WHERE file_id='pdf_a'",
                        (datetime.now().isoformat(),))
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": ["pdf_a"], "stale_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaisesRegex(ValueError, "近期|心跳|正在处理"):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(len(self.db.query("policy_clauses")), 3)

    def test_stale_release_rechecks_batch_heartbeat_and_rolls_back_on_cleanup_failure(self):
        for column in ("heartbeat_at TEXT", "last_error TEXT", "next_retry_at TEXT", "finished_at TEXT"):
            self.db.execute(f"ALTER TABLE pdf_batch_items ADD COLUMN {column}")
        old = (datetime.now() - timedelta(days=2)).isoformat()
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=%s WHERE file_id='pdf_a'", (old,))
        self.db.execute("INSERT INTO pdf_batch_items (file_id,status,heartbeat_at) VALUES ('pdf_a','running',%s)", (datetime.now().isoformat(),))
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": ["pdf_a"], "stale_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaises(ValueError):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
            self.db.execute("UPDATE pdf_batch_items SET heartbeat_at=%s", (old,))
            with patch("file_reset.reset_file_database_artifacts", side_effect=RuntimeError("清理失败")):
                with self.assertRaises(RuntimeError):
                    reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
            self.assertEqual(self.db.query("pdf_batch_items")[0]["status"], "running")
            result = reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(result["released_stale_file_ids"], ["pdf_a"])
        self.assertEqual(self.db.query("pdf_batch_items")[0]["status"], "pending")

    def test_stale_flag_cannot_release_processing_without_timestamp(self):
        self.db.execute("UPDATE pdf_files SET status='processing' WHERE file_id='pdf_a'")
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": ["pdf_a"], "stale_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaises(ValueError):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(self.db.query("pdf_files", "file_id='pdf_a'")[0]["status"], "processing")

    def test_stale_file_cannot_release_active_clause_processing(self):
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=%s WHERE file_id='pdf_a'",
                        ((datetime.now() - timedelta(days=2)).isoformat(),))
        self.db.execute("INSERT INTO policy_clause_classification_items (clause_id,status) VALUES ('a1','running')")
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": ["pdf_a"], "stale_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            with self.assertRaisesRegex(ValueError, "正在处理"):
                reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(self.db.query("pdf_files", "file_id='pdf_a'")[0]["status"], "processing")

    def test_stale_structure_status_is_released_with_stale_file(self):
        self.db.execute("UPDATE pdf_files SET status='processing',last_attempt_at=%s WHERE file_id='pdf_a'",
                        ((datetime.now() - timedelta(days=2)).isoformat(),))
        self.db.execute("UPDATE policy_documents SET structure_status='running' WHERE policy_id='a'")
        plan = {"file_ids": ["pdf_a"], "shared_file_ids": [], "running_file_ids": ["pdf_a"], "stale_file_ids": ["pdf_a"]}
        with patch("file_reset.load_folder_reset_plan", return_value=plan):
            result = reset_folder_database_artifacts("folder", confirm=True, release_stale=True)
        self.assertEqual(result["reset_file_ids"], ["pdf_a"])
        self.assertEqual(self.db.query("policy_documents", "policy_id='a'")[0]["structure_status"], "pending")
