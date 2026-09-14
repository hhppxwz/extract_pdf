"""文本重组摘要测试。"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from models import BlockType, ContentBlock
from text_pipeline import process_text_blocks


class TextPipelineTests(unittest.TestCase):
    """验证不同文档类型的文本重组行为。"""

    def test_policy_reorganization_reports_chapter_and_article_counts(self) -> None:
        """防止规章制度重组摘要遗漏章数、条数，导致无法快速核验结构。"""
        blocks = [
            ContentBlock(type=BlockType.TEXT, page_num=1, content="制定说明"),
            ContentBlock(type=BlockType.TEXT, page_num=1, content="第一章 总则"),
            ContentBlock(type=BlockType.TEXT, page_num=1, content="第一条 本办法适用。"),
            ContentBlock(type=BlockType.TEXT, page_num=1, content="第二条 本办法由办公室解释。"),
            ContentBlock(type=BlockType.TEXT, page_num=2, content="第二章 附则"),
            ContentBlock(type=BlockType.TEXT, page_num=2, content="第三条 本办法自发布之日起施行。"),
        ]
        output = io.StringIO()

        with (
            patch("text_pipeline.embed_chunks", side_effect=lambda chunks: chunks),
            patch("text_pipeline.store_chunks", return_value=0),
            redirect_stdout(output),
        ):
            process_text_blocks(blocks, "file_1", doc_type="policy_regulation")

        self.assertIn("2 章，3 条", output.getvalue())


if __name__ == "__main__":
    unittest.main()
