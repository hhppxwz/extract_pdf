"""制度文件首页元数据抽取测试。"""
from __future__ import annotations

import unittest

from metadata.policy import extract_policy_metadata
from models import BlockType, ContentBlock
from policy.pipeline import _guess_title


class PolicyMetadataTests(unittest.TestCase):
    """验证首页公文元数据能够容忍 PDF 排版空白。"""

    def test_doc_number_allows_space_before_hao(self) -> None:
        """防止数字与“号”之间的排版空格导致文号漏识别。"""
        blocks = [ContentBlock(
            type=BlockType.TEXT,
            page_num=0,
            content=(
                "武汉大学文件\n"
                "武大财字〔2022〕25 号\n"
                "关于印发武汉大学财务管理办法的通知"
            ),
        )]

        metadata = extract_policy_metadata(blocks)

        self.assertEqual(metadata["doc_number"], "武大财字〔2022〕25 号")

    def test_semantic_pdf_file_name_has_priority_over_page_text(self) -> None:
        """防止红头或正文标题覆盖可信的 PDF 文件名。"""
        blocks = [ContentBlock(
            type=BlockType.TEXT, page_num=0,
            content="武汉大学文件\n关于印发财务管理办法的通知",
        )]

        self.assertEqual(
            _guess_title("武汉大学财务管理办法.pdf", blocks),
            "武汉大学财务管理办法",
        )

    def test_notice_cover_uses_title_from_second_page_for_generic_file_name(self) -> None:
        """防止把印发通知页当成制度正文标题。"""
        blocks = [
            ContentBlock(
                type=BlockType.TEXT, page_num=0,
                content="武汉大学文件\n关于印发《武汉大学财务管理办法》的通知",
            ),
            ContentBlock(
                type=BlockType.TEXT, page_num=1,
                content="武汉大学财务管理办法\n第一章 总则",
            ),
        ]

        self.assertEqual(_guess_title("document.pdf", blocks), "武汉大学财务管理办法")

    def test_policy_without_notice_uses_title_from_first_page(self) -> None:
        """普通制度 PDF 应直接从第一页提取正式标题。"""
        blocks = [ContentBlock(
            type=BlockType.TEXT, page_num=0,
            content="武汉大学文件\n武汉大学采购管理办法\n第一章 总则",
        )]

        self.assertEqual(_guess_title("download.pdf", blocks), "武汉大学采购管理办法")


if __name__ == "__main__":
    unittest.main()
