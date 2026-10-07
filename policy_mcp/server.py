"""创建 MCP 服务，管理业务客户端生命周期及 HTTP 认证。"""
from contextlib import asynccontextmanager
import hmac
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .api_client import PolicyAPIClient
from .tools import register_tools


def create_server(*, api_base_url: str | None = None, api_transport=None) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server):
        async with PolicyAPIClient(api_base_url, transport=api_transport) as api:
            yield api

    server = MCPServer("Policy Search API Agent Service", lifespan=lifespan)
    register_tools(server)
    return server


class BearerTokenMiddleware:
    """所有 HTTP 请求均校验凭证；不记录或向业务接口转发凭证。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
            authorized = False
            if len(headers) == 1:
                parts = headers[0].split()
                authorized = (
                    len(parts) == 2 and parts[0].lower() == b"bearer"
                    and hmac.compare_digest(parts[1], self.token)
                )
            if not authorized:
                response = JSONResponse(
                    {"error": "unauthorized", "message": "需要有效的 Bearer Token"},
                    status_code=401, headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(*, token: str | None = None, server: MCPServer | None = None):
    token = token if token is not None else os.getenv("POLICY_MCP_TOKEN", "")
    if not token or any(char.isspace() for char in token):
        raise ValueError("HTTP 模式必须设置无空白字符的 POLICY_MCP_TOKEN")

    # 公网反向代理需显式加入实际域名，保留 Host 和 Origin 校验。
    def allowed(name, default):
        return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]

    security = TransportSecuritySettings(
        allowed_hosts=allowed("POLICY_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*"),
        allowed_origins=allowed("POLICY_MCP_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*,http://[::1]:*"),
    )
    server = server if server is not None else create_server()
    app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        transport_security=security,
    )
    app.add_middleware(BearerTokenMiddleware, token=token)
    return app

