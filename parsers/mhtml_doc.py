"""解析扩展名为 DOC、实际内容为网页归档的学校制度文件。"""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path
import re

from bs4 import BeautifulSoup

from models import BlockType, ContentBlock


def is_mhtml_document(data: bytes) -> bool:
    """只根据文件内容识别 MIME 网页归档，不根据 DOC 后缀猜测。"""
    head = data[:4096].lower().lstrip(b"\xef\xbb\xbf\x00\r\n \t")
    return head.startswith(b"mime-version:") and b"content-type: multipart/related" in head


def parse_mhtml_document(file_path: str) -> list[ContentBlock]:
    """提取网页正文段落，避免把 MIME 头、脚本和 HTML 标签入库。"""
    data = Path(file_path).read_bytes()
    if not is_mhtml_document(data):
        raise ValueError("文件不是 MIME 网页归档")
    message = BytesParser(policy=policy.default).parsebytes(data)
    html_part = next((part for part in message.walk() if part.get_content_type() == "text/html"), None)
    if html_part is None:
        raise ValueError("网页归档中缺少 HTML 正文")
    html = html_part.get_content()
    soup = BeautifulSoup(html, "html.parser")
    source_metadata: dict[str, str] = {}
    title = soup.select_one(".info-title")
    if title:
        source_metadata["title"] = title.get_text(" ", strip=True)
    intro = soup.select_one(".info-intro")
    if intro:
        intro_text = intro.get_text(" ", strip=True)
        for label, field in (("发布机构", "issuer"), ("文号", "doc_number")):
            match = re.search(
                rf"{label}\s*[：:]\s*(.+?)(?=\s*(?:发布机构|文号|发布日期|发布时间)\s*[：:]|$)",
                intro_text,
            )
            if match:
                source_metadata[field] = match.group(1).strip()
    for tag in soup.find_all(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    container = next(
        (found for selector in (".key-content", "article", "main", ".info-content", "body")
         if (found := soup.select_one(selector)) is not None),
        None,
    )
    if container is None:
        raise ValueError("网页归档中缺少可识别正文")
    blocks: list[ContentBlock] = []
    elements = container.find_all(["p", "li", "h1", "h2", "h3", "table"])
    for element in elements:
        if element.find_parent("table"):
            continue
        if element.name == "table":
            html_table = str(element)
            blocks.append(ContentBlock(
                block_id=f"mhtml_{len(blocks)}", type=BlockType.TABLE,
                page_num=0, content=html_table, table_html=html_table,
            ))
        else:
            text = element.get_text(" ", strip=True)
            if text:
                blocks.append(ContentBlock(
                    block_id=f"mhtml_{len(blocks)}", type=BlockType.TEXT,
                    page_num=0, content=text,
                ))
    if not blocks and not elements:
        blocks = [
            ContentBlock(block_id=f"mhtml_{index}", type=BlockType.TEXT, page_num=0, content=text)
            for index, text in enumerate(container.stripped_strings)
        ]
    if not blocks:
        raise ValueError("网页归档正文为空")
    blocks[0].raw = {"source_format": "mhtml", "source_metadata": source_metadata}
    return blocks
