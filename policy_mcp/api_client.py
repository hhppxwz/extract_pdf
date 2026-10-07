"""访问现有制度 HTTP 接口，不加载数据库或模型。"""
import os
from urllib.parse import quote
import httpx


class PolicyAPIError(Exception):
    """携带稳定错误码的业务接口异常。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class PolicyAPIClient:
    """在服务生命周期内共享异步客户端。"""

    def __init__(self, base_url: str | None = None, *, transport=None):
        address = base_url or os.getenv("POLICY_API_BASE_URL", "http://127.0.0.1:8000")
        url = httpx.URL(address)
        if url.scheme not in {"http", "https"} or not url.host or url.query or url.fragment:
            raise ValueError("POLICY_API_BASE_URL 必须是无查询参数的 HTTP 或 HTTPS 地址")
        self.client = httpx.AsyncClient(
            base_url=address.rstrip("/") + "/",
            timeout=httpx.Timeout(60.0, connect=5.0),
            transport=transport, follow_redirects=False,
            # 内部业务请求直接连接配置地址，避免被进程中的全局代理转发。
            trust_env=False,
        )

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)

    async def _request(self, method: str, path: str, *, read_timeout=60.0, **kwargs) -> dict:
        try:
            response = await self.client.request(
                method, path, timeout=httpx.Timeout(read_timeout, connect=5.0), **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise PolicyAPIError("timeout", "制度服务响应超时，请稍后重试") from exc
        except httpx.RequestError as exc:
            raise PolicyAPIError("service_unavailable", "无法连接制度服务") from exc

        # 不透传后端错误正文，避免泄漏数据库等内部信息。
        if not response.is_success:
            errors = {
                400: ("invalid_parameters", "请求参数不符合制度接口要求"),
                422: ("invalid_parameters", "请求参数不符合制度接口要求"),
                404: ("not_found", "请求的制度或接口不存在"),
                409: ("index_not_ready", "制度检索索引尚未就绪"),
            }
            code, message = errors.get(response.status_code, (
                "service_unavailable", "制度服务暂时不可用",
            ))
            raise PolicyAPIError(code, f"{message}（HTTP {response.status_code}）")
        try:
            result = response.json()
        except ValueError as exc:
            raise PolicyAPIError("invalid_response", "制度服务返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise PolicyAPIError("invalid_response", "制度服务返回结果必须是 JSON 对象")
        return result

    async def search_clauses(self, query: str, top_k: int = 10, as_of: str | None = None) -> dict:
        params = {"q": query, "top_k": top_k}
        if as_of is not None:
            params["as_of"] = as_of
        return await self._request("GET", "policy-search", params=params)

    async def list_policies(self, query: str = "", status: str = "", limit: int = 30, offset: int = 0) -> dict:
        return await self._request("GET", "api/policies", params={
            "q": query, "status": status, "limit": limit, "offset": offset,
        })

    async def get_policy_detail(self, policy_id: str) -> dict:
        return await self._request("GET", f"api/policies/{quote(policy_id, safe='')}")

    async def answer_question(self, question: str, as_of: str | None = None) -> dict:
        data = {"question": question}
        if as_of is not None:
            data["as_of"] = as_of
        return await self._request("POST", "policy-answer", data=data, read_timeout=180.0)

