"""本地 MinerU 解析器的行为测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import fitz
import httpx


for _key, _value in {
    "CLOUDMINERU_API_KEY": "test",
    "PG_HOST": "test",
    "PG_PORT": "5432",
    "PG_USER": "test",
    "PG_PASSWORD": "test",
    "PG_DATABASE": "test",
    "MINIO_ENDPOINT": "test",
    "MINIO_ACCESS_KEY": "test",
    "MINIO_SECRET_KEY": "test",
    "MINIO_BUCKET_PDF": "test",
    "MINIO_BUCKET_IMAGES": "test",
}.items():
    os.environ.setdefault(_key, _value)


class MinerUParserTests(unittest.TestCase):
    """验证本地 MinerU 的 PDF 图像请求和结果转换。"""

    @staticmethod
    def _create_pdf(path: Path, page_count: int) -> None:
        document = fitz.open()
        for page_number in range(page_count):
            page = document.new_page()
            page.insert_text((72, 72), f"第 {page_number + 1} 页")
        document.save(path)
        document.close()

    @staticmethod
    def _create_parser(handler):
        from config import MinerUConfig
        from parsers.mineru import MinerUParser

        config = MinerUConfig(
            api_url="http://mineru.test/v1/chat/completions",
            api_key="test-key",
            model="mineru2.5-1.2b",
            dpi=72,
            max_pages=20,
            timeout_seconds=12.0,
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return MinerUParser(config=config, client=client)

    def test_parse_sends_rendered_pages_to_mineru_and_returns_markdown_block(self) -> None:
        """防止解析请求漏传认证、图片页或 MinerU 指令，导致服务无法正确解析。"""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request["headers"] = dict(request.headers)
            captured_request["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "# 报销管理办法\n\n第一条"}}]},
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "policy.pdf"
            self._create_pdf(pdf_path, page_count=1)
            parser = self._create_parser(handler)
            try:
                blocks = parser.parse(str(pdf_path))
            finally:
                parser.close()

        payload = captured_request["payload"]
        self.assertEqual(captured_request["headers"]["authorization"], "Bearer test-key")
        self.assertEqual(payload["model"], "mineru2.5-1.2b")
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(len(payload["messages"][0]["content"]), 2)
        self.assertTrue(payload["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("MinerU", payload["messages"][0]["content"][1]["text"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].content, "# 报销管理办法\n\n第一条")
        self.assertEqual(blocks[0].page_num, 0)

    def test_parse_limits_rendered_images_to_configured_maximum_pages(self) -> None:
        """防止大文件突破页数上限并使本地 MinerU 请求超过服务可承受范围。"""
        captured_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "# 仅第一页"}}]},
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "long-policy.pdf"
            self._create_pdf(pdf_path, page_count=2)
            parser = self._create_parser(handler)
            parser.config.max_pages = 1
            try:
                parser.parse(str(pdf_path))
            finally:
                parser.close()

        content = captured_payload["messages"][0]["content"]
        image_parts = [item for item in content if item["type"] == "image_url"]
        self.assertEqual(len(image_parts), 1)

    def test_parse_rejects_success_response_without_mineru_content(self) -> None:
        """防止服务返回缺少正文的成功 JSON 时把空内容当成解析完成。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {}}]})

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "policy.pdf"
            self._create_pdf(pdf_path, page_count=1)
            parser = self._create_parser(handler)
            try:
                with self.assertRaisesRegex(RuntimeError, "缺少解析正文"):
                    parser.parse(str(pdf_path))
            finally:
                parser.close()

    def test_default_dispatcher_backend_is_cloudmineru(self) -> None:
        """防止默认配置误路由到本地 MinerU，绕过 CloudMinerU。"""
        from parsers.dispatcher import PDFParser
        from parsers.cloud_mineru import CloudMineruParser

        parser = PDFParser()
        self.assertEqual(parser.backend, "cloudmineru")
        self.assertIsInstance(parser.strategy, CloudMineruParser)


if __name__ == "__main__":
    unittest.main()
