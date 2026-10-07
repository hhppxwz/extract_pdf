import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

from policy.knowledge_sampling_v2 import FIELDS, load_sample_ids, select_samples


class SamplingTests(unittest.TestCase):
    def test_selection_is_diverse_unique_and_bounded(self):
        clauses = [
            {"clause_id": f"c{i}", "policy_id": f"p{i % 3}", "level": "article", "sequence_no": i,
             "raw_text": ["申请材料应在三日内提交", "学院负责审核", "本办法适用于学生", "不得重复申请"][i % 4]}
            for i in range(30)
        ]
        selected = select_samples([], clauses, 10)
        self.assertEqual(len(selected), 10)
        self.assertEqual(len({c["clause_id"] for c, _ in selected}), 10)
        self.assertEqual({c["policy_id"] for c, _ in selected}, {"p0", "p1", "p2"})
        self.assertGreaterEqual(len({reason for _, reason in selected}), 4)
        self.assertEqual(selected, select_samples([], clauses, 10))

    def test_manifest_rejects_changed_source_and_wrong_batch(self):
        clause = {"clause_id": "c1", "level": "article", "raw_text": "学院负责审核"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerow({"选用": "是", "批次ID": "b1", "条款ID": "c1", "原文指纹": hashlib.sha256(clause["raw_text"].encode()).hexdigest()})
            self.assertEqual(load_sample_ids(path, "b1", [clause]), ["c1"])
            with self.assertRaises(ValueError):
                load_sample_ids(path, "b2", [clause])
            with self.assertRaises(ValueError):
                load_sample_ids(path, "b1", [{**clause, "raw_text": "变更后的内容"}])
