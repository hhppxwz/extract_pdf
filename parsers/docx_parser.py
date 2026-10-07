"""使用标准库读取 DOCX 正文，避免将 Word 文件送入 PDF 解析接口。"""

from html import escape
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from models import BlockType, ContentBlock


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{W}t")).strip()


def parse_docx(file_path: str) -> list[ContentBlock]:
    """按正文顺序提取段落和表格。"""
    try:
        with ZipFile(file_path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (KeyError, ElementTree.ParseError, OSError) as exc:
        raise ValueError(f"无效的 DOCX 文件: {Path(file_path).name}") from exc

    body = root.find(f"{W}body")
    if body is None:
        raise ValueError("DOCX 缺少正文")
    blocks: list[ContentBlock] = []
    for element in body:
        if element.tag == f"{W}p":
            content = _text(element)
            if content:
                blocks.append(ContentBlock(block_id=f"docx_{len(blocks)}", type=BlockType.TEXT, content=content))
        elif element.tag == f"{W}tbl":
            rows = []
            for row in element.findall(f"{W}tr"):
                cells = [" ".join(filter(None, (_text(p) for p in cell.findall(f".//{W}p"))))
                         for cell in row.findall(f"{W}tc")]
                rows.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>")
            if rows:
                html = "<table>" + "".join(rows) + "</table>"
                blocks.append(ContentBlock(block_id=f"docx_{len(blocks)}", type=BlockType.TABLE,
                                           content=html, table_html=html))
    if not blocks:
        raise ValueError("DOCX 正文为空")
    return blocks
