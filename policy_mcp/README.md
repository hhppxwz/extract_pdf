# 制度 MCP 工具服务

本包给外部 Agent 提供四个工具，通过内部 HTTP 请求复用现有 FastAPI 服务。它不直接连接数据库，也不加载向量模型。需要同时运行业务服务和 MCP 服务。

## 启动

在项目根目录使用现有虚拟环境，依赖采用官方 MCP SDK 2.3.0：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

第一个终端启动业务服务：

```powershell
.\.venv\Scripts\python.exe main.py
```

第二个终端配置凭证并启动 MCP：

```powershell
# 生成本次使用的随机凭证；调用方必须使用同一个值。
$env:POLICY_MCP_TOKEN = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
$env:POLICY_API_BASE_URL = 'http://127.0.0.1:8000'
.\.venv\Scripts\python.exe -m policy_mcp
```

默认 MCP 地址为 `http://127.0.0.1:8001/mcp`，传输方式为 Streamable HTTP。调用方在 MCP 客户端中设置 `Authorization: Bearer <上述凭证>` 请求头；不把凭证放在 URL 中。它是 MCP 协议入口，直接在浏览器访问不能执行工具。

环境变量也可保存在项目 `.env` 中，启动入口会读取；已有进程环境变量优先。凭证不要提交到版本库。

| 配置 | 默认值 | 用途 |
|---|---|---|
| `POLICY_API_BASE_URL` | `http://127.0.0.1:8000` | 现有业务 API 地址，可带路径前缀 |
| `POLICY_MCP_TOKEN` | 无 | HTTP 必填，使用无空白字符的随机 ASCII 凭证 |
| `POLICY_MCP_HOST` | `127.0.0.1` | 监听地址 |
| `POLICY_MCP_PORT` | `8001` | 监听端口 |
| `POLICY_MCP_ALLOWED_HOSTS` | 本机 Host 列表 | 允许的请求 Host，逗号分隔，支持 `域名:*` |
| `POLICY_MCP_ALLOWED_ORIGINS` | 本机 Origin 列表 | 允许的浏览器 Origin，逗号分隔 |

监听地址和端口也可通过 `--host`、`--port` 指定，优先于环境变量。显式本机进程模式：

```powershell
.\.venv\Scripts\python.exe -m policy_mcp stdio
```

stdio 模式由 MCP 客户端启动子进程，不要求 `POLICY_MCP_TOKEN`。工具执行仍需业务 API 可用。`tcp` 不是本服务支持的传输方式。

## 工具

| 工具 | 参数 | 业务接口 |
|---|---|---|
| `search_policy_clauses` | 必填 `query`；`top_k=10`（1–20）；可选 `as_of` | `GET /policy-search` |
| `list_policies` | `query=""`（最多 200 字符）；`status=""`；`limit=30`（1–100）；`offset=0` | `GET /api/policies` |
| `get_policy_detail` | 必填 `policy_id`，使用目录或检索结果中的 ID | `GET /api/policies/{policy_id}` |
| `answer_policy_question` | 必填 `question`；可选 `as_of` | `POST /policy-answer`，表单提交 |

`status` 允许空值、`current`、`invalid`、`unknown`；`offset` 必须非负。`as_of` 必须是有效的 `YYYY-MM-DD` 日期。

需要自行分析或综合其他工具时，先调用条款检索；它返回原文、来源、父子条款上下文和效力提示。相似度不证明条款适用或条件已经找齐，需要结合警告并按需读取全文。

需要复用项目现有问答逻辑时，调用问答工具。它会使用业务服务配置的大模型，产生相应成本和耗时，返回回答、引用证据及警告。MCP 层不重新生成或改写结果。

两个日期默认值保持现有业务语义：检索省略日期时沿用 `/policy-search` 的处理；问答省略日期时由现有流程解析问题时间或使用当前日期。需要明确的历史适用判断时，请显式传入 `as_of`。

## Python 客户端示例

以下示例使用同版本官方 SDK。SDK 2.3.0 的 MCP HTTP 客户端使用 `httpx2`；本包请求业务 API 使用原有的 `httpx`，两者不要混淆。

```python
import asyncio
import os

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {os.environ['POLICY_MCP_TOKEN']}"},
        timeout=httpx2.Timeout(200.0, connect=5.0),
    ) as http:
        transport = streamable_http_client(
            "http://127.0.0.1:8001/mcp", http_client=http,
        )
        async with Client(transport, read_timeout_seconds=200.0) as client:
            tools = await client.list_tools()
            print([tool.name for tool in tools.tools])

            result = await client.call_tool("search_policy_clauses", {
                "query": "本科生奖学金申请条件",
                "top_k": 5,
                "as_of": "2026-10-07",
            })
            if result.is_error:
                print(result.content)
            else:
                print(result.structured_content)


if __name__ == "__main__":
    asyncio.run(main())
```

SDK 协议发现、工具列表和调用由 SDK 处理，不需要自行拼接 JSON-RPC。已验证同版本客户端的现代协议模式及 `mode="legacy"` 握手模式；其他平台接入需确认其 Streamable HTTP 和自定义请求头支持。

## 远程接入

生产环境通过 HTTPS 反向代理把 `/mcp` 转发到本机 `8001`。代理保留 `Authorization`，不要转发外部凭证给业务 API；业务 API 的 `8000` 端口保持内部可访问。

若代理保留外部 Host，例如 `policies.example.com`，设置：

```powershell
$env:POLICY_MCP_ALLOWED_HOSTS = '127.0.0.1:*,localhost:*,policies.example.com,policies.example.com:443'
$env:POLICY_MCP_ALLOWED_ORIGINS = 'https://policies.example.com'
```

客户端没有 Origin 时可正常调用。浏览器跨域直接调用还需要按实际前端配置代理的 CORS，当前服务主要面向自有系统的服务端客户端。

代理和调用方的请求超时应大于问答读取超时 180 秒，建议至少 200 秒。本版本使用预配置凭证，不提供 OAuth 发现及登录流程。

## 错误与验证

工具执行失败按 MCP 错误结果返回，调用方检查 `is_error`：

- `invalid_parameters`：业务参数无效；SDK 参数结构校验也会返回错误。
- `not_found`：制度或业务接口不存在。
- `index_not_ready`：检索索引尚未就绪。
- `service_unavailable`：连接失败或业务服务故障。
- `timeout`：后端响应超时。
- `invalid_response`：业务服务返回了无效 JSON 或非对象结果。

HTTP 认证失败返回 `401` 和 `WWW-Authenticate: Bearer`。业务服务连接超时为 5 秒，普通工具读取超时为 60 秒，问答为 180 秒，不自动重试。有效的空检索结果正常返回。

MCP 对业务 API 默认直接连接 `POLICY_API_BASE_URL`，不使用系统代理环境变量。后端 HTTP 错误保留状态码，但不透传内部错误正文。若直接访问 `8000` 正常而 MCP 工具失败，检查 MCP 进程启动时的业务地址配置；修改代码或配置后需要重启 MCP 进程。

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_policy_mcp
```

测试使用模拟业务 HTTP 响应，覆盖参数转换、错误、原文保留、认证、Host/Origin 校验、官方 MCP 客户端现代与旧版协议、真实本机 TCP HTTP 请求及 stdio 启动，不访问真实数据库或调用收费模型。

真实业务联调脚本需要先启动业务服务和 MCP 服务：

```powershell
.\.venv\Scripts\python.exe tests\test_policy_mcp_tools.py
# 指定其他 MCP 地址或适用日期。
.\.venv\Scripts\python.exe tests\test_policy_mcp_tools.py --url http://127.0.0.1:8001/mcp --as-of 2026-10-07
# 分别测试单个工具。
.\.venv\Scripts\python.exe tests\test_policy_mcp_tools.py --tool search_policy_clauses --query '奖学金申请条件'
.\.venv\Scripts\python.exe tests\test_policy_mcp_tools.py --tool list_policies --catalog-query '奖学金'
.\.venv\Scripts\python.exe tests\test_policy_mcp_tools.py --tool get_policy_detail --policy-id policy_xxx
.\.venv\Scripts\python.exe tests\test_policy_mcp_tools.py --tool answer_policy_question --question '本科生奖学金申请条件是什么？'
```

脚本明确读取项目根目录 `.env`，也支持 `POLICY_MCP_URL`，命令行 `--url` 优先。连接失败、认证失败、Host 白名单拒绝及工具错误会返回非零退出码和诊断提示。它是手动联调入口，自动回归测试在 `tests/test_policy_mcp_tools_script.py`。

默认依次测试四个工具，分别展示参数、完整结果、通过或失败及最终汇总。全文读取优先使用 `--policy-id`，否则取目录返回的第一个制度 ID，目录无结果时可使用检索命中的 ID；缺少 ID 时明确跳过。任何失败或跳过均返回非零退出码。问答测试会实际使用业务服务配置的大模型，可能产生调用费用；只检查检索时请指定 `--tool search_policy_clauses`。

通过表示调用及返回结构有效：检索保留原文和制度 ID，目录保留制度 ID，全文对应指定制度并包含条款，问答包含回答和引用列表。空检索、空目录及证据不足的降级问答可合法返回；降级问答会额外提示，需要人工判断业务结论质量。

如果返回 `421 Invalid Host header`，检查 `POLICY_MCP_ALLOWED_HOSTS` 是否包含请求的域名及端口。本机默认配置允许 `127.0.0.1:*`；修改源码或环境变量后，已运行的进程不会自动加载，需要在原启动终端停止并使用项目虚拟环境重启 MCP。请保留 Host 校验，不通过伪造请求头绕过配置问题。

## 文件职责

- `api_client.py`：请求现有业务 API，管理超时和错误转换。
- `tools.py`：给 Agent 提供工具说明、参数约束和调用函数。
- `server.py`：创建服务、管理共享客户端生命周期、校验 HTTP 凭证。
- `__main__.py`：读取配置并启动 HTTP 或 stdio。
