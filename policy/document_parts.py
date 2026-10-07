"""根据印发通知书名及独立正文标题，划分同一源文件中的制度。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from models import BlockType, ContentBlock

_BODY_START_RE = re.compile(
    r'^(?:第[\d一二三四五六七八九十百千万零〇]+[编章节条]|'
    r'[一二三四五六七八九十百千万]+、|\d+[.．、]|[（(][\d一二三四五六七八九十百千万]+[）)])'
)


def split_policy_document(blocks: list[ContentBlock], metadata: dict, file_name: str) -> list[dict]:
    fallback = [{'blocks': blocks, 'metadata': dict(metadata)}]
    notice = ''.join(str(metadata.get('notice_title') or metadata.get('title') or Path(file_name).stem).split())
    if not re.fullmatch(r'关于.*(?:印发|发布).*的通知', notice):
        return fallback
    titles = list(dict.fromkeys(re.findall(r'《([^《》]+)》', notice)))
    if len(titles) < 2:
        return fallback
    # 按原解析顺序保留表格和图片；只将文本块按行展开，方便识别跨行标题。
    expanded = []
    for block in sorted(blocks, key=lambda b: b.page_num):
        if block.type == BlockType.TEXT:
            expanded.extend(block.model_copy(update={'content': line.strip()})
                            for line in block.content.splitlines() if line.strip())
        else:
            expanded.append(block)
    starts = []
    title_end = 0
    for i, block in enumerate(expanded):
        if block.type != BlockType.TEXT or i < title_end:
            continue
        candidate = ''
        for count in range(1, 5):
            if i + count > len(expanded) or expanded[i+count-1].type != BlockType.TEXT:
                break
            candidate += ''.join(expanded[i+count-1].content.split())
            title = re.sub(r'^附件[一二三四五六七八九十\d]*[：:]?', '', candidate)
            if title not in titles:
                continue
            # 允许标题后的简短前言及中文、括号、数字编号；不越过下一附件标题。
            following = []
            for following_block in expanded[i+count:i+count+8]:
                if following_block.type != BlockType.TEXT:
                    continue
                compact = ''.join(following_block.content.split())
                if compact in titles:
                    break
                following.append(compact)
            if any(_BODY_START_RE.match(text) for text in following):
                starts.append((i, title))
                title_end = i + count
            break
    # 引用书名或缺失附件不能充当边界；同一标题出现多次时拒绝猜选。
    if len(starts) != len(titles) or {title for _,title in starts} != set(titles):
        return fallback
    parts = []
    for index, (start,title) in enumerate(starts):
        end = starts[index+1][0] if index+1 < len(starts) else len(expanded)
        body = expanded[start:end]
        meta = dict(metadata)
        # 聚合字段属于源文件，不能复制到每项制度的元数据中。
        meta.pop('policies', None)
        meta.update(title=title, notice_title=notice,
                    document_key='' if index == 0 else hashlib.sha256(title.encode('utf-8')).hexdigest()[:24],
                    source_page_start=min(b.page_num for b in body),
                    source_page_end=max(b.page_num for b in body))
        # 施行、废止日期由各附件正文独立识别，不能继承另一个制度的效力信息。
        for field in ('effective_date', 'expiry_date', 'validity_status'):
            meta.pop(field, None)
        parts.append({'blocks': body, 'metadata': meta})
    return parts
