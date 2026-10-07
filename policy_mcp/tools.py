"""面向 Agent 的四个制度工具及参数约束。"""
from datetime import date
import re
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .api_client import PolicyAPIClient, PolicyAPIError


def _text(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise ToolError(f"invalid_parameters: {name}不能为空")
    return value


def _date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ToolError("invalid_parameters: as_of 必须是有效的 YYYY-MM-DD 日期") from exc


async def _invoke(ctx: Context, method: str, *args) -> dict:
    api: PolicyAPIClient = ctx.request_context.lifespan_context
    try:
        return await getattr(api, method)(*args)
    except PolicyAPIError as exc:
        raise ToolError(str(exc)) from exc


def register_tools(server: MCPServer) -> None:
    """注册工具，SDK 自动注入上下文，不向 Agent 暴露客户端参数。"""
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False)

    @server.tool(annotations=read_only)
    async def search_policy_clauses(
        query: Annotated[str, Field(min_length=1, description="制度问题或条款检索关键词")],
        ctx: Context,
        top_k: Annotated[int, Field(ge=1, le=20, description="返回条款数，1 到 20")] = 10,
        as_of: Annotated[str | None, Field(description="适用日期 YYYY-MM-DD；省略时沿用检索接口的效力处理")] = None,
    ) -> dict[str, Any]:
        """检索制度原文证据，适合自行分析或继续查询。返回来源、上下文和效力提示；相似度不代表适用或条件齐全。"""
        return await _invoke(ctx, "search_clauses", _text(query, "查询内容"), top_k, _date(as_of))

    @server.tool(annotations=read_only)
    async def list_policies(
        ctx: Context,
        query: Annotated[str, Field(max_length=200, description="制度名称、文号或发布部门关键词")] = "",
        status: Annotated[Literal["", "current", "invalid", "unknown"], Field(description="效力状态；空值表示不筛选")] = "",
        limit: Annotated[int, Field(ge=1, le=100, description="每页制度数，1 到 100")] = 30,
        offset: Annotated[int, Field(ge=0, description="分页偏移量")] = 0,
    ) -> dict[str, Any]:
        """查询已入库制度目录及效力状态，支持关键词和分页；取得 policy_id 后可读取全文。"""
        return await _invoke(ctx, "list_policies", query.strip(), status, limit, offset)

    @server.tool(annotations=read_only)
    async def get_policy_detail(
        policy_id: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$", description="目录或检索结果中的制度 ID")],
        ctx: Context,
    ) -> dict[str, Any]:
        """读取制度元数据和当前结构版本的有序条款全文，用于核对上下文及完整条件。"""
        return await _invoke(ctx, "get_policy_detail", policy_id)

    @server.tool(annotations=read_only)
    async def answer_policy_question(
        question: Annotated[str, Field(min_length=1, description="制度咨询或合规判断问题")],
        ctx: Context,
        as_of: Annotated[str | None, Field(description="适用日期 YYYY-MM-DD；省略时由现有问答流程解析日期")] = None,
    ) -> dict[str, Any]:
        """使用本系统模型生成制度回答，返回引用证据、适用日期和警告。调用会产生模型成本；综合其他工具时优先检索原文。"""
        return await _invoke(ctx, "answer_question", _text(question, "问题"), _date(as_of))

