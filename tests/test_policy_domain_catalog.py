"""流程事项域目录的行为测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from policy.domain_catalog import load_process_domain_catalog


class ProcessDomainCatalogTests(unittest.TestCase):
    """验证事项域由可审核配置决定，而非写死在代码中。"""

    def test_load_catalog_returns_enabled_domains_in_configured_order(self) -> None:
        """防止新增事项域后仍被旧代码的固定关键词集合忽略。"""
        catalog_payload = {
            "version": "test-v1",
            "domains": [
                {
                    "code": "reimbursement",
                    "name": "报销",
                    "enabled": True,
                    "keywords": ["报销"],
                    "process_signals": ["提交"],
                    "exclude_signals": ["总则"],
                },
                {
                    "code": "asset",
                    "name": "资产领用",
                    "enabled": False,
                    "keywords": ["领用"],
                    "process_signals": ["登记"],
                    "exclude_signals": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "domains.json"
            path.write_text(json.dumps(catalog_payload, ensure_ascii=False), encoding="utf-8")
            catalog = load_process_domain_catalog(path)

        self.assertEqual(catalog.version, "test-v1")
        self.assertEqual([domain.code for domain in catalog.enabled_domains], ["reimbursement"])

    def test_select_domains_rejects_disabled_and_unknown_domain_codes(self) -> None:
        """防止用户把未启用或不存在的事项域混入一次流程抽取运行。"""
        catalog_payload = {
            "version": "test-v1",
            "domains": [
                {
                    "code": "reimbursement",
                    "name": "报销",
                    "enabled": True,
                    "keywords": ["报销"],
                    "process_signals": ["提交"],
                    "exclude_signals": [],
                },
                {
                    "code": "asset",
                    "name": "资产领用",
                    "enabled": False,
                    "keywords": ["领用"],
                    "process_signals": ["登记"],
                    "exclude_signals": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "domains.json"
            path.write_text(json.dumps(catalog_payload, ensure_ascii=False), encoding="utf-8")
            catalog = load_process_domain_catalog(path)

        with self.assertRaisesRegex(ValueError, "未启用"):
            catalog.select_domains(["asset"])
        with self.assertRaisesRegex(ValueError, "不存在"):
            catalog.select_domains(["unknown"])


if __name__ == "__main__":
    unittest.main()
