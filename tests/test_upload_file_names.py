"""上传文件名保真与真实名称判定的回归测试。"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

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


class UploadFileNameTests(unittest.TestCase):
    """验证上传名不会在临时文件边界丢失。"""

    def test_upload_forwards_original_file_name_to_processing_pipeline(self) -> None:
        """防止上传接口只传临时路径，表格目录最终显示 tmp*.pdf。"""
        from api import app
        from models import ProcessingResult, ProcessingStatus

        result = ProcessingResult(file_id="pdf_test", status=ProcessingStatus.DONE)

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/upload",
                    files={"file": ("Cracking SQL Barriers.pdf", b"%PDF-test", "application/pdf")},
                )

        with (
            patch("api.record_file_start", return_value="pdf_test"),
            patch("api.process_pdf", return_value=result) as process,
        ):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            process.call_args.kwargs.get("source_file_name"),
            "Cracking SQL Barriers.pdf",
        )

    def test_temporary_file_name_uses_cleaned_pdf_title(self) -> None:
        """防止低可信 tmp 文件名覆盖 PDF 中可识别的论文标题。"""
        import metadata_service

        choose_name = getattr(
            metadata_service,
            "choose_display_file_name",
            lambda source_name, _title: source_name,
        )

        self.assertEqual(
            choose_name(
                "tmpan2sxyv7.pdf",
                "Cracking SQL Barriers: An LLM-based Dialect Translation",
            ),
            "Cracking SQL Barriers An LLM-based Dialect Translation.pdf",
        )

    def test_generic_download_name_uses_pdf_title(self) -> None:
        """防止下载器生成的通用文件名压过可识别的 PDF 标题。"""
        from metadata_service import choose_display_file_name

        self.assertEqual(
            choose_display_file_name("download.pdf", "Cracking SQL Barriers"),
            "Cracking SQL Barriers.pdf",
        )

    def test_semantic_source_file_name_beats_different_pdf_title(self) -> None:
        """防止标题抽取误识别时覆盖本来可靠的原始文件名。"""
        from metadata_service import choose_display_file_name

        self.assertEqual(
            choose_display_file_name(
                "Cracking SQL Barriers An LLM-based Dialect Translation.pdf",
                "ACM Reference Format",
            ),
            "Cracking SQL Barriers An LLM-based Dialect Translation.pdf",
        )

    def test_upload_returns_accepted_before_background_processing_finishes(self) -> None:
        """防止长时间解析占用上传连接，令浏览器在拿到任务 ID 前断开。"""
        from api import app
        from models import ProcessingResult, ProcessingStatus

        result = ProcessingResult(file_id="pdf_queued", status=ProcessingStatus.DONE)

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/upload",
                    files={"file": ("slow-paper.pdf", b"%PDF-test", "application/pdf")},
                )

        with (
            patch("api.record_file_start", return_value="pdf_queued", create=True),
            patch("api.process_pdf", return_value=result),
        ):
            response = asyncio.run(request())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {
            "file_id": "pdf_queued",
            "status": "pending",
            "status_url": "/status/pdf_queued",
        })

    def test_upload_preflight_allows_cross_origin_browser_request(self) -> None:
        """防止前端跨域上传在发出 POST 前被浏览器的 CORS 预检拦截。"""
        from api import app

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.options(
                    "/upload",
                    headers={
                        "Origin": "http://localhost:5173",
                        "Access-Control-Request-Method": "POST",
                    },
                )

        response = asyncio.run(request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "*")


if __name__ == "__main__":
    unittest.main()
