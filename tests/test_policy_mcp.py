"""验证 MCP 参数转换、错误边界和协议调用，不访问真实数据库。"""
import os
import asyncio
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters
import uvicorn

from policy_mcp.api_client import PolicyAPIClient, PolicyAPIError
from policy_mcp.server import create_http_app, create_server


class PolicyMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_api_mappings_preserve_results_and_timeouts(self):
        requests = []
        payload = {"results": [{"clause": {"raw_text": "原文"}}], "warnings": ["效力未知"]}

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=payload)

        async with PolicyAPIClient(transport=httpx.MockTransport(handler)) as api:
            self.assertEqual(await api.search_clauses("奖学金", 5, "2026-10-07"), payload)
            await api.list_policies("奖学金", "current", 10, 20)
            await api.get_policy_detail("policy_1")
            self.assertEqual(await api.answer_question("能否申请？", "2026-10-07"), payload)
            client = api.client
        self.assertTrue(client.is_closed)
        self.assertEqual([r.url.path for r in requests], [
            "/policy-search", "/api/policies", "/api/policies/policy_1", "/policy-answer",
        ])
        self.assertEqual(dict(requests[0].url.params), {
            "q": "奖学金", "top_k": "5", "as_of": "2026-10-07",
        })
        self.assertEqual(dict(requests[1].url.params), {
            "q": "奖学金", "status": "current", "limit": "10", "offset": "20",
        })
        self.assertEqual(requests[3].method, "POST")
        self.assertIn("application/x-www-form-urlencoded", requests[3].headers["content-type"])
        from urllib.parse import parse_qs
        self.assertEqual(parse_qs(requests[3].content.decode()), {
            "question": ["能否申请？"], "as_of": ["2026-10-07"],
        })
        self.assertEqual(requests[0].extensions["timeout"]["connect"], 5)
        self.assertEqual(requests[0].extensions["timeout"]["read"], 60)
        self.assertEqual(requests[3].extensions["timeout"]["read"], 180)

    async def test_errors_are_distinct_and_not_retried(self):
        for status, code in [(422, "invalid_parameters"), (404, "not_found"),
                             (409, "index_not_ready"), (503, "service_unavailable"),
                             (500, "service_unavailable")]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"detail": "数据库密码不应暴露"})
            with self.subTest(status=status):
                async with PolicyAPIClient(transport=httpx.MockTransport(handler)) as api:
                    with self.assertRaises(PolicyAPIError) as caught:
                        await api.search_clauses("测试")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("密码", str(caught.exception))
                self.assertEqual(len(calls), 1)

    async def test_network_errors_and_invalid_response(self):
        for error, code in [(httpx.ReadTimeout("secret"), "timeout"),
                            (httpx.ConnectError("secret"), "service_unavailable")]:
            def handler(request):
                raise error
            async with PolicyAPIClient(transport=httpx.MockTransport(handler)) as api:
                with self.assertRaises(PolicyAPIError) as caught:
                    await api.search_clauses("测试")
                self.assertEqual(caught.exception.code, code)
        async with PolicyAPIClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="不是 JSON")
        )) as api:
            with self.assertRaises(PolicyAPIError) as caught:
                await api.search_clauses("测试")
            self.assertEqual(caught.exception.code, "invalid_response")

    async def test_protocol_discovery_calls_validation_and_empty_results(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"results": [], "warnings": [], "result_count": 0})
        server = create_server(api_transport=httpx.MockTransport(handler))
        async with Client(server) as client:
            tools = await client.list_tools()
            self.assertEqual({t.name for t in tools.tools}, {
                "search_policy_clauses", "list_policies", "get_policy_detail", "answer_policy_question",
            })
            self.assertNotIn("ctx", tools.tools[0].input_schema["properties"])
            result = await client.call_tool("search_policy_clauses", {"query": "奖学金"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["results"], [])
            for arguments in [{"query": " "}, {"query": "测试", "top_k": 21},
                              {"query": "测试", "as_of": "2026-02-30"},
                              {"query": "测试", "as_of": "20261007"}]:
                result = await client.call_tool("search_policy_clauses", arguments)
                self.assertTrue(result.is_error)
            for name, arguments in [
                ("list_policies", {"status": "错误"}),
                ("list_policies", {"offset": -1}),
                ("get_policy_detail", {"policy_id": "../policy-answer"}),
                ("answer_policy_question", {"question": " "}),
            ]:
                result = await client.call_tool(name, arguments)
                self.assertTrue(result.is_error)
            self.assertEqual(len(calls), 1)

    async def test_http_token_required_and_checked_on_all_methods(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "POLICY_MCP_TOKEN"):
                create_http_app()
        app = create_http_app(token="测试-token")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8001") as client:
            for method in ["GET", "POST", "DELETE"]:
                for headers in [{}, {"Authorization": "Bearer wrong"},
                                {"Authorization": "Basic wrong"},
                                [("Authorization", "Bearer wrong"), ("Authorization", "Bearer wrong")]]:
                    response = await client.request(method, "/mcp", headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")

    async def test_http_mcp_client_calls_all_tools_in_modern_and_legacy_modes(self):
        calls = []
        payload = {"citations": [{"raw_text": "制度依据", "page_start": 2}], "warnings": ["效力提示"]}
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=payload)

        for mode in ["auto", "legacy"]:
            with self.subTest(mode=mode):
                server = create_server(api_transport=httpx.MockTransport(handler))
                app = create_http_app(token="test-token", server=server)
                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app=app),
                        headers={"Authorization": "Bearer test-token"},
                    ) as http:
                        async with Client(streamable_http_client(
                            "http://127.0.0.1:8001/mcp", http_client=http,
                        ), mode=mode) as client:
                            tools = await client.list_tools()
                            self.assertEqual(len(tools.tools), 4)
                            for name, args in [
                                ("search_policy_clauses", {"query": "奖学金"}),
                                ("list_policies", {}),
                                ("get_policy_detail", {"policy_id": "policy_1"}),
                                ("answer_policy_question", {"question": "能否申请？"}),
                            ]:
                                result = await client.call_tool(name, args)
                                self.assertFalse(result.is_error)
                                self.assertEqual(result.structured_content, payload)
                        bad_host = await http.post("http://evil.example/mcp", json={})
                        self.assertEqual(bad_host.status_code, 421)
                        bad_origin = await http.post("http://127.0.0.1:8001/mcp", json={},
                                                     headers={"Origin": "https://evil.example"})
                        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(len(calls), 8)
        self.assertTrue(all("as_of" not in r.url.params for r in calls))
        self.assertTrue(all("authorization" not in r.headers for r in calls))

    async def test_http_tool_error_is_returned_as_mcp_error(self):
        server = create_server(api_transport=httpx.MockTransport(
            lambda r: httpx.Response(409, json={"detail": "内部信息"})
        ))
        app = create_http_app(token="test-token", server=server)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                         headers={"Authorization": "Bearer test-token"}) as http:
                async with Client(streamable_http_client(
                    "http://127.0.0.1:8001/mcp", http_client=http,
                )) as client:
                    result = await client.call_tool("search_policy_clauses", {"query": "测试"})
                    self.assertTrue(result.is_error)
                    self.assertIn("index_not_ready", result.content[0].text)
                    self.assertNotIn("内部信息", result.content[0].text)

    async def test_real_http_connection_and_authentication(self):
        payload = {"results": [{"clause": {"raw_text": "条款原文", "page_start": 1}}]}
        server = create_server(api_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=payload)
        ))
        app = create_http_app(token="test-token", server=server)
        # 只占用操作系统分配的临时端口，不影响用户的 8000/8001 服务。
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            http_server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
            task = asyncio.create_task(http_server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not http_server.started:
                        if task.done():
                            await task
                            self.fail("HTTP 服务未启动")
                        await asyncio.sleep(0.01)
                url = f"http://127.0.0.1:{port}/mcp"
                async with httpx2.AsyncClient(timeout=5, trust_env=False) as http:
                    response = await http.post(url, json={})
                    self.assertEqual(response.status_code, 401)
                    http.headers["Authorization"] = "Bearer test-token"
                    async with Client(streamable_http_client(url, http_client=http)) as client:
                        self.assertEqual(len((await client.list_tools()).tools), 4)
                        result = await client.call_tool("search_policy_clauses", {"query": "奖学金"})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content, payload)
            finally:
                http_server.should_exit = True
                await asyncio.wait_for(task, timeout=5)

    async def test_stdio_entrypoint_and_import_need_no_token_or_backend(self):
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "policy_mcp", "stdio"], cwd=root,
            env={**os.environ, "POLICY_MCP_TOKEN": "", "POLICY_API_BASE_URL": "http://127.0.0.1:1"},
        )
        async with asyncio.timeout(10):
            async with Client(params) as client:
                self.assertEqual(len((await client.list_tools()).tools), 4)

    async def test_api_address_configuration_and_optional_dates(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})
        with patch.dict(os.environ, {"POLICY_API_BASE_URL": "http://backend.example/internal"}):
            async with PolicyAPIClient(transport=httpx.MockTransport(handler)) as api:
                await api.search_clauses("测试")
                await api.answer_question("测试")
        self.assertEqual(calls[0].url.path, "/internal/policy-search")
        self.assertNotIn("as_of", calls[0].url.params)
        self.assertNotIn(b"as_of", calls[1].content)
        for address in ["file:///tmp", "http://example.com?q=test", "http://example.com/#fragment"]:
            with self.assertRaises(ValueError):
                PolicyAPIClient(address)

    async def test_http_lifecycle_shares_and_closes_business_client(self):
        created = []
        def make_api(*args, **kwargs):
            api = PolicyAPIClient(*args, **kwargs)
            created.append(api)
            return api

        with patch("policy_mcp.server.PolicyAPIClient", side_effect=make_api):
            server = create_server(api_transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"results": []})
            ))
            app = create_http_app(token="test-token", server=server)
            self.assertEqual(created, [])
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                             headers={"Authorization": "Bearer test-token"}) as http:
                    async with Client(streamable_http_client(
                        "http://127.0.0.1:8001/mcp", http_client=http,
                    )) as client:
                        for query in ["奖学金", "助学金"]:
                            result = await client.call_tool("search_policy_clauses", {"query": query})
                            self.assertFalse(result.is_error)
                        self.assertEqual(len(created), 1)
                        self.assertFalse(created[0].client.is_closed)
            self.assertTrue(created[0].client.is_closed)


if __name__ == "__main__":
    unittest.main()
