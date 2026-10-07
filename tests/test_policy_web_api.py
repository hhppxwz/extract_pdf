"""制度库和浏览器会话接口的边界测试。"""
import asyncio
import unittest
import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import httpx

from api import app
from policy.web_catalog import _public_row, PUBLIC_DOCUMENT_FIELDS, list_policies


class PolicyWebApiTests(unittest.TestCase):
    def test_catalog_hides_reset_placeholders_and_keeps_real_documents(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE policy_documents (policy_id TEXT, title TEXT, structure_status TEXT, structure_version TEXT);
            CREATE TABLE policy_clauses (policy_id TEXT, structure_version TEXT, is_active BOOLEAN);
            INSERT INTO policy_documents VALUES ('reset','','pending',''),
                ('ready','制度甲','succeeded','v1'), ('untitled','','succeeded','v1'),
                ('pending_title','制度乙','pending','v1'), ('has_clauses','','pending','v1');
            INSERT INTO policy_clauses VALUES ('untitled','v1',TRUE),('has_clauses','v1',TRUE),('reset','old',TRUE);
        ''')
        cursor = MagicMock()
        current = []
        def execute(sql, params):
            current[:] = [db.execute(sql.replace('%s', '?'), params)]
        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = lambda: dict(current[0].fetchone())
        cursor.fetchall.side_effect = lambda: [dict(row) for row in current[0].fetchall()]
        relational = MagicMock()
        relational.conn.cursor.return_value.__enter__.return_value = cursor
        try:
            with patch('policy.web_catalog.ensure_policy_tables'), patch('policy.web_catalog.storage.relational', relational):
                result = list_policies(limit=2)
                remaining = list_policies(limit=2, offset=2)
            self.assertEqual(result['total'], 4)
            ids = {x['policy_id'] for x in result['items'] + remaining['items']}
            self.assertEqual(ids, {'ready', 'untitled', 'pending_title', 'has_clauses'})
        finally:
            db.close()

    def test_catalog_dates_are_json_safe_and_internal_fields_are_hidden(self):
        row = {"policy_id": "p1", "title": "制度", "issue_date": date(2024, 1, 2), "secret": "hidden"}
        result = _public_row(row, PUBLIC_DOCUMENT_FIELDS)
        self.assertEqual(result["issue_date"], "2024-01-02")
        self.assertNotIn("secret", result)

    def test_catalog_uses_parameterized_filters_and_pagination(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"total": 1}
        cursor.fetchall.return_value = [{"policy_id": "p1", "title": "采购办法"}]
        context = MagicMock()
        context.__enter__.return_value = cursor
        relational = MagicMock()
        relational.conn.cursor.return_value = context
        with patch("policy.web_catalog.ensure_policy_tables"), patch(
            "policy.web_catalog.storage.relational", relational
        ):
            result = list_policies("采购", "current", 10, 20)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["policy_id"], "p1")
        self.assertEqual(cursor.execute.call_args_list[1].args[1], ("%采购%", "%采购%", "%采购%", "current", 10, 20))

    def test_policy_catalog_routes_and_validation(self):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return (
                    await client.get("/api/policies", params={"q": "采购", "limit": 10}),
                    await client.get("/api/policies/p1"),
                    await client.get("/api/policies", params={"status": "false"}),
                )

        with patch("policy.web_catalog.list_policies", return_value={"total": 0, "items": []}) as listing, patch(
            "policy.web_catalog.get_policy_detail", return_value={"policy": {"policy_id": "p1"}, "clauses": []}
        ):
            listing_response, detail_response, invalid_response = asyncio.run(call())
        self.assertEqual(listing_response.status_code, 200)
        listing.assert_called_once_with("采购", "", 10, 0)
        self.assertEqual(detail_response.json()["policy"]["policy_id"], "p1")
        self.assertEqual(invalid_response.status_code, 422)

    def test_conversation_cookie_and_owned_message(self):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/conversations")
                message = await client.post("/api/conversations/conv_1/messages", json={"question": "采购流程？"})
                return created, message

        with patch("policy.web_conversations.create_conversation", return_value={"conversation_id": "conv_1"}), patch(
            "policy.web_conversations.get_conversation", return_value={"conversation_id": "conv_1", "messages": []}
        ), patch("policy.answering.answer_policy_question", return_value={"answer": "依据待核实", "citations": []}), patch(
            "policy.web_conversations.save_message", return_value={"message_id": "msg_1", "answer": {"answer": "依据待核实"}}
        ) as save:
            created, message = asyncio.run(call())
        self.assertEqual(created.status_code, 201)
        self.assertIn("httponly", created.headers["set-cookie"].lower())
        self.assertEqual(message.status_code, 201)
        self.assertTrue(save.call_args.args[0])
        self.assertEqual(save.call_args.args[1], "conv_1")

    def test_feedback_rejects_invalid_rating(self):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/api/messages/msg_1/feedback", json={"rating": "perfect"})

        self.assertEqual(asyncio.run(call()).status_code, 422)


if __name__ == "__main__":
    unittest.main()
