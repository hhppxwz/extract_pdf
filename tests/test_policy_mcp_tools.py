"""手动联调真实 MCP 服务；自动回归测试见 test_policy_mcp_tools_script.py。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx2
from dotenv import load_dotenv
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_URL = "http://127.0.0.1:8001/mcp"


class MCPConnectionError(Exception):
    """联调的连接或配置错误，提供明确的操作提示。"""


async def probe_endpoint(http, url: str) -> None:
    """先识别 HTTP 层错误，避免 SDK 将其包装为难读的异常组。"""
    try:
        response = await http.post(
            url,
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0", "id": 1, "method": "server/discover",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            },
            timeout=httpx2.Timeout(10.0, connect=5.0),
        )
    except httpx2.TimeoutException as exc:
        raise MCPConnectionError("MCP 服务连接或响应超时，请检查地址和服务状态") from exc
    except httpx2.RequestError as exc:
        raise MCPConnectionError(f"无法连接 {url}，请先启动 MCP 服务：python -m policy_mcp") from exc

    messages = {
        401: "MCP 认证失败：请确保客户端与服务端使用相同的 POLICY_MCP_TOKEN 凭证",
        403: "MCP 拒绝请求：请检查服务端 Origin 白名单或访问权限",
        404: "MCP 地址不存在：请检查 URL 是否指向 /mcp",
        421: (
            "MCP 服务拒绝当前 Host（域名及端口）：请检查 POLICY_MCP_ALLOWED_HOSTS；"
            "本机默认配置已允许 127.0.0.1:*，修改配置后需要重启 MCP 服务。"
            "请使用项目虚拟环境运行 python -m policy_mcp"
        ),
    }
    if response.status_code in messages:
        raise MCPConnectionError(messages[response.status_code])
    if not response.is_success:
        raise MCPConnectionError(f"MCP 服务返回 HTTP {response.status_code}，请检查地址和服务日志")
    # 旧版服务不支持 server/discover，但仍可由官方客户端自动回退握手。
    # 这里只检查 HTTP 状态，后续协议协商和工具调用交给 SDK。


async def main(url: str | None = None, query: str = "本科生奖学金申请条件",
               top_k: int = 5, as_of: str | None = None):
    # 明确加载项目根目录配置，不依赖 IDE 或命令行的工作目录。
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("POLICY_MCP_TOKEN", "")
    if not token:
        raise MCPConnectionError("缺少 POLICY_MCP_TOKEN，请在项目 .env 或当前进程环境中设置凭证")
    url = url or os.getenv("POLICY_MCP_URL") or (
        f"http://127.0.0.1:{os.getenv('POLICY_MCP_PORT', '8001')}/mcp"
    )
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(200.0, connect=5.0),
        trust_env=False,
    ) as http:
        await probe_endpoint(http, url)
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, read_timeout_seconds=200.0) as client:
            tools = await client.list_tools()
            print("可用工具：", [tool.name for tool in tools.tools])
            arguments = {"query": query, "top_k": top_k}
            if as_of is not None:
                arguments["as_of"] = as_of
            result = await client.call_tool("search_policy_clauses", arguments)
    # 在 SDK 的任务组退出后处理业务错误，避免异常被包装为 ExceptionGroup。
    if result.is_error:
        details = "；".join(getattr(item, "text", str(item)) for item in result.content)
        raise MCPConnectionError(
            f"检索工具失败：{details}。请检查 MCP 进程的 POLICY_API_BASE_URL、业务服务和检索索引"
        )
    print("结果：", json.dumps(result.structured_content, ensure_ascii=False, indent=2))
    return result


def run():
    parser = argparse.ArgumentParser(description="联调已启动的制度 MCP 服务")
    parser.add_argument("--url", help="MCP 地址，默认读取 POLICY_MCP_URL 或本机 MCP 端口")
    parser.add_argument("--query", default="本科生奖学金申请条件")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--as-of", help="可选的适用日期 YYYY-MM-DD")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.url, args.query, args.top_k, args.as_of))
    except MCPConnectionError as exc:
        print(f"联调失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())

