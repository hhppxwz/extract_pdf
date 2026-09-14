"""通过本地 OpenAI 兼容服务调用 MinerU 的 PDF 解析器。"""
from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import fitz
import httpx

from config import MinerUConfig, app_config
from models import BlockType, ContentBlock, MinerUTaskStatus
from parsers.base import ParserStrategy


logger = logging.getLogger(__name__)


class MinerUParser(ParserStrategy):
    """把 PDF 页面转为图像后交给本地 MinerU 服务解析。"""

    def __init__(
        self,
        config: Optional[MinerUConfig] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or app_config.mineru
        self._client = client
        self._owns_client = client is None
        self._result_cache: Dict[str, List[ContentBlock]] = {}

    @property
    def client(self) -> httpx.Client:
        """延迟创建 HTTP 客户端，避免未使用解析器时建立资源。"""
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.config.timeout_seconds))
        return self._client

    def submit(self, file_path: str) -> str:
        """本地服务同步返回结果，因此提交后任务立即完成。"""
        task_id = f"local_mineru_{uuid.uuid4().hex[:12]}"
        self._result_cache[task_id] = self._parse_pdf(file_path)
        return task_id

    def get_status(self, task_id: str) -> MinerUTaskStatus:
        """读取已完成的本地解析结果。"""
        blocks = self._result_cache.get(task_id)
        if blocks is None:
            return MinerUTaskStatus(
                task_id=task_id,
                status="failed",
                progress=0.0,
                error="未找到本地 MinerU 任务",
            )
        return MinerUTaskStatus(
            task_id=task_id,
            status="done",
            progress=100.0,
            result=blocks,
        )

    def poll_until_done(
        self,
        task_id: str,
        timeout_seconds: Optional[float] = None,
    ) -> MinerUTaskStatus:
        """本地请求是同步调用，无需轮询。"""
        return self.get_status(task_id)

    def parse(self, file_path: str, page_count: int = 0) -> List[ContentBlock]:
        """同步解析 PDF 并返回包含 MinerU Markdown 的文本块。"""
        return self._parse_pdf(file_path)

    def _parse_pdf(self, file_path: str) -> List[ContentBlock]:
        markdown = self._request_mineru(self._pdf_pages_to_base64(file_path))
        return [
            ContentBlock(
                block_id=f"mineru_{hashlib.md5(file_path.encode()).hexdigest()[:12]}",
                type=BlockType.TEXT,
                page_num=0,
                content=markdown,
                metadata={"parser": "mineru", "source_file": Path(file_path).name},
            )
        ]

    def _pdf_pages_to_base64(self, file_path: str) -> List[str]:
        """渲染限定数量的 PDF 页面，并返回 PNG 的 Base64 字符串。"""
        images: List[str] = []
        with fitz.open(file_path) as document:
            for page_index in range(min(len(document), self.config.max_pages)):
                pixmap = document[page_index].get_pixmap(dpi=self.config.dpi)
                images.append(base64.b64encode(pixmap.tobytes("png")).decode("ascii"))

        if not images:
            raise RuntimeError("PDF 不包含可供 MinerU 解析的页面")
        return images

    def _request_mineru(self, pages: List[str]) -> str:
        """发送 OpenAI 兼容的多模态请求并验证 MinerU 返回内容。"""
        if not self.config.api_key.strip():
            raise RuntimeError("MINERU_API_KEY 未配置，无法调用本地 MinerU 服务")

        content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{page}"},
            }
            for page in pages
        ]
        content.append(
            {
                "type": "text",
                "text": "请解析这些文档页面，并以 MinerU Markdown 格式输出。",
            }
        )
        response = self.client.post(
            self.config.api_url,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={
                "model": self.config.model,
                "messages": [{"role": "user", "content": content}],
            },
        )
        response.raise_for_status()

        try:
            response_data = response.json()
        except ValueError as error:
            raise RuntimeError("MinerU 服务返回了无效 JSON") from error

        try:
            markdown = response_data["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as error:
            raise RuntimeError("MinerU 服务响应缺少解析正文") from error

        if not isinstance(markdown, str) or not markdown.strip():
            raise RuntimeError("MinerU 服务响应缺少解析正文")
        return markdown

    def close(self) -> None:
        """关闭由解析器创建的 HTTP 客户端。"""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None
