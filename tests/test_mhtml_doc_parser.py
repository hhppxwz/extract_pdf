import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parsers.dispatcher import PDFParser
from policy.pipeline import build_policy_clauses
from models import BlockType
from table_pipeline import parse_table_html


class MhtmlDocParserTests(unittest.TestCase):
    def test_disguised_doc_uses_local_html_and_preserves_articles(self) -> None:
        content = (
            'Mime-Version: 1.0\n'
            'Content-Type: Multipart/related; boundary="NEXT.ITEM-BOUNDARY";type="text/html"\n'
            '\n--NEXT.ITEM-BOUNDARY\n'
            'Content-Type: text/html; charset="utf-8"\n'
            'Content-Transfer-Encoding: 8bit\n\n'
            '<html><body><div class="key-content">'
            '<p>武汉大学分散采购实施细则</p><p>第一章 总则</p>'
            '<p>第一条为规范采购工作，制定本细则。</p>'
            '<p>第二条本细则适用于学校各单位。</p>'
            '</div><script>第一条不应出现在正文</script></body></html>\n'
            '--NEXT.ITEM-BOUNDARY--\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "采购细则.doc"
            path.write_bytes(content.encode("utf-8"))
            blocks = PDFParser(backend="pymupdf").parse(str(path))

        clauses = build_policy_clauses("policy_1", blocks)
        self.assertEqual([clause.level for clause in clauses], ["preamble", "chapter", "article", "article"])
        self.assertIn("为规范采购工作", clauses[2].raw_text)
        self.assertNotIn("<p>", " ".join(block.content for block in blocks))
        self.assertNotIn("不应出现在正文", " ".join(block.content for block in blocks))

    def test_binary_word_is_not_mistaken_for_mhtml(self) -> None:
        from parsers.mhtml_doc import is_mhtml_document

        self.assertFalse(is_mhtml_document(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))
        self.assertFalse(is_mhtml_document(b"PK\x03\x04"))

    def test_body_table_is_a_table_block_without_page_metadata_table(self) -> None:
        content = (
            'Mime-Version: 1.0\nContent-Type: Multipart/related; boundary="b";type="text/html"\n'
            '\n--b\nContent-Type: text/html; charset="utf-8"\nContent-Transfer-Encoding: 8bit\n\n'
            '<html><body><div class="info-intro"><table><tr><td>发布日期</td></tr></table></div>'
            '<div class="key-content"><p>附件：采购登记表</p>'
            '<table><tr><th>项目</th><th>金额</th></tr><tr><td>设备</td><td>100元</td></tr></table>'
            '</div></body></html>\n--b--\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "采购细则.doc"
            path.write_bytes(content.encode("utf-8"))
            blocks = PDFParser(backend="pymupdf").parse(str(path))

        tables = [block for block in blocks if block.type == BlockType.TABLE]
        self.assertEqual(len(tables), 1)
        self.assertNotIn("发布日期", " ".join(block.content for block in blocks))
        structure = parse_table_html(tables[0].table_html)
        self.assertEqual(structure.headers, ["项目", "金额"])
        self.assertEqual(structure.rows, [["设备", "100元"]])

    def test_single_file_command_does_not_open_mhtml_doc_as_pdf(self) -> None:
        from main import run_process
        from models import ProcessingResult, ProcessingStatus

        result = ProcessingResult(file_id="pdf_1", status=ProcessingStatus.DONE)
        with (
            patch("fitz.open", side_effect=AssertionError("不应作为 PDF 打开")),
            patch("pipeline.process_pdf", return_value=result) as process,
            patch("policy.abolition.prompt_abolition_relation_insertion"),
            patch("policy.clause_export.export_file_clause_structure"),
        ):
            run_process("采购细则.doc")

        self.assertEqual(process.call_args.args[:2], ("采购细则.doc", 0))

    def test_explicit_web_header_overrides_cited_document_number(self) -> None:
        from metadata import extract_document_metadata

        content = (
            'Mime-Version: 1.0\nContent-Type: Multipart/related; boundary="b";type="text/html"\n'
            '\n--b\nContent-Type: text/html; charset="utf-8"\nContent-Transfer-Encoding: 8bit\n\n'
            '<html><body><div class="info-title">武汉大学分散采购实施细则</div>'
            '<div class="info-intro">发布机构：采购与招投标管理中心 文号：武大采购字[2023]2号</div>'
            '<div class="key-content"><p>第一条根据《武汉大学采购管理办法》（武大采购字〔2021〕5号）制定本细则。</p>'
            '</div></body></html>\n--b--\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "采购细则.doc"
            path.write_bytes(content.encode("utf-8"))
            blocks = PDFParser(backend="pymupdf").parse(str(path))

        metadata = extract_document_metadata(blocks, "policy_regulation")
        self.assertEqual(metadata["doc_number"], "武大采购字[2023]2号")
        self.assertEqual(metadata["issuer"], "采购与招投标管理中心")
        self.assertNotIn("issue_date", metadata)

    def test_policy_metadata_does_not_treat_legal_basis_as_issuer(self) -> None:
        from metadata.policy import extract_policy_metadata
        from models import ContentBlock

        block = ContentBlock(type=BlockType.TEXT, content="第一条 根据《相关规定办法》制定本细则。")

        self.assertNotIn("issuer", extract_policy_metadata([block]))


if __name__ == "__main__":
    unittest.main()
