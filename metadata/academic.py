# metadata/academic.py
"""
学术论文元数据抽取模块
负责从 ContentBlock 列表中提取：标题、作者（含通讯作者、邮箱、单位）、年份、DOI、摘要
"""

import re
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from models import ContentBlock, BlockType


# ============================================================
# 数据模型
# ============================================================
@dataclass
class Author:
    """作者信息"""
    name: str                          # 原始名字
    normalized_name: str               # 标准化名字 (First Last)
    order: int                         # 顺序(从0开始)
    is_corresponding: bool = False
    email: Optional[str] = None
    affiliation: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'normalized_name': self.normalized_name,
            'order': self.order,
            'is_corresponding': self.is_corresponding,
            'email': self.email,
            'affiliation': self.affiliation
        }


# ============================================================
# 名字处理工具函数（原 MetadataExtractor 的核心逻辑）
# ============================================================
def _normalize_author_name(name: str) -> str:
    """标准化作者名：移除头衔、处理 Last, First 格式、展开缩写"""
    name = name.strip()
    # 移除学位/头衔
    name = re.sub(r',?\s*(Ph\.?D\.?|M\.?S\.?|B\.?S\.?|Dr\.?|Prof\.?)', '', name)
    # 移除括号内的机构信息
    name = re.sub(r'\s*\([^)]*\)', '', name)

    # 处理 "Last, First" 格式
    if ',' in name:
        parts = name.split(',')
        if len(parts) == 2:
            last = parts[0].strip()
            first = parts[1].strip()
            first = _expand_initials(first)
            name = f"{first} {last}"

    # 展开缩写 (J. -> J)
    name = _expand_initials(name)
    # 清理多余空格
    return ' '.join(name.split())


def _expand_initials(name_part: str) -> str:
    """展开名字缩写，如 J. D. -> J D"""
    # 处理 "J. D." 或 "J D"
    name_part = re.sub(r'\b([A-Z])\.\s*([A-Z])\.?\s*', r'\1 \2 ', name_part)
    # 处理单个字母后跟点号 "J." -> "J"
    name_part = re.sub(r'\b([A-Z])\.\s*', r'\1 ', name_part)
    return name_part.strip()


def _names_match(name1: str, name2: str) -> bool:
    """判断两个名字是否匹配（基于姓氏）"""
    n1 = name1.lower().split()
    n2 = name2.lower().split()
    if not n1 or not n2:
        return False
    # 比较最后一个单词（姓氏）
    return n1[-1] == n2[-1] or n1[-1].startswith(n2[-1][:3])


# ============================================================
# 邮箱与通讯作者识别
# ============================================================
def _extract_emails(text: str) -> List[Tuple[str, int]]:
    """从文本中提取所有邮箱及其位置"""
    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    return [(m.group(), m.start()) for m in re.finditer(email_pattern, text)]


def _find_corresponding_author_indices(text: str, author_names: List[str]) -> Set[int]:
    """
    从文本中识别通讯作者索引
    策略：查找 * 或 † 标记，或 "corresponding author" 等关键词
    """
    indices = set()
    text_lower = text.lower()

    # 1. 查找名字后跟 * 或 †
    # 匹配类似 "John Smith*" 或 "Smith, J.*" 等
    for match in re.finditer(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*[*†]', text):
        name_candidate = match.group(1).strip()
        for i, raw_name in enumerate(author_names):
            if _names_match(name_candidate, raw_name):
                indices.add(i)
                break

    # 2. 查找 "corresponding author" 等后面的名字
    corr_patterns = [
        r'corresponding\s+author\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        r'correspondence\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        r'contact\s+author\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
    ]
    for pattern in corr_patterns:
        for match in re.finditer(pattern, text_lower):
            name_candidate = match.group(1).strip()
            for i, raw_name in enumerate(author_names):
                if _names_match(name_candidate, raw_name):
                    indices.add(i)
                    break

    return indices


def _match_email_to_author(author: Author, emails: List[Tuple[str, int]], context: str) -> Optional[str]:
    """
    将邮箱匹配到作者：在作者名附近（前后500字符）查找邮箱
    """
    if not emails:
        return None
    # 在上下文中找作者名位置
    name_pattern = re.escape(author.normalized_name)
    for match in re.finditer(name_pattern, context, re.IGNORECASE):
        start = max(0, match.end())
        end = min(len(context), match.end() + 500)
        region = context[start:end]
        for email, _ in emails:
            if email in region:
                return email
    return None


# ============================================================
# 主抽取函数
# ============================================================
def extract_academic_metadata(blocks: List[ContentBlock]) -> Dict[str, str]:
    """
    从 ContentBlock 列表中提取学术论文的元数据
    返回字典，包含 title, authors (列表), year, doi, abstract
    若某字段无法提取则留空
    """
    # 1. 收集前两页的文本块（通常首页包含标题、作者、摘要）
    header_blocks = [
        b for b in blocks
        if b.page_num <= 1
           and b.type == BlockType.TEXT
           and hasattr(b, 'raw') and b.raw is not None
    ]
    # 如果前两页没有文本块，尝试用所有块（极少情况）
    if not header_blocks:
        header_blocks = [b for b in blocks if b.type == BlockType.TEXT]
    if not header_blocks:
        return {}

    # 合并文本，同时保留每个块的原始信息以备后用
    header_text = "\n".join([b.content for b in header_blocks])
    meta: Dict[str, str] = {}

    # ===== 2. 提取标题 =====
    # 优先从 raw 中取 type='title' 的块
    title = None
    for b in header_blocks:
        if b.raw.get('type') == 'title':
            title = b.content.strip()
            break
    # 若没找到，取第一个长度 > 20 且不含句号的长句（启发式）
    if not title:
        for b in header_blocks:
            content = b.content.strip()
            if len(content) > 20 and '.' not in content:
                title = content
                break
    if title:
        meta['title'] = title

    # ===== 3. 提取摘要 =====
    # ===== 3. 1定位 intro 索引（正文开始） =====
    intro_idx = -1
    for i, b in enumerate(header_blocks):
        if b.raw.get('text_level') == 2:
            text = b.content.strip().lower().replace(' ', '')
            if 'introduction' in text:
                intro_idx = i
                break
    # 若没找到，尝试找 text_level=2 的第一个块
    if intro_idx == -1:
        for i, b in enumerate(header_blocks):
            if b.raw.get('text_level') == 2:
                intro_idx = i
                break

    # 找到标题块索引（第一个 text_level=1 或 type='title'）
    title_idx = -1
    for i, b in enumerate(header_blocks):
        if b.raw.get('text_level') == 1 or b.raw.get('type') == 'title':
            title_idx = i
            break
    if title_idx == -1:
        title_idx = 0  # 极端情况，从第一个块开始

    # 确定候选区间：从标题之后到 intro 之前
    start_idx = title_idx + 1
    end_idx = intro_idx if intro_idx != -1 else len(header_blocks)
    between_blocks = header_blocks[start_idx:end_idx]

    # ===== 3.2. 提取摘要 =====
    abstract = None
    abstract_parts = []

    # 策略A：在 between_blocks 中找以 abstract 开头的块
    abstract_block_idx = -1
    for i, b in enumerate(between_blocks):
        text = b.content.strip()
        # 去除所有空白字符，转为小写，检查是否以 'abstract' 开头
        if re.sub(r'\s+', '', text).lower().startswith('abstract'):
            abstract_block_idx = i
            break

    if abstract_block_idx != -1:
        # 取找到的块内容
        candidate_text = between_blocks[abstract_block_idx].content.strip()
        # 若该块长度 > 50 字符，认为是摘要本身（可能包含 Abstract 前缀）
        if len(candidate_text) > 50:
            abstract = re.sub(r'^abstract\b\s*[:：\-]?\s*', '', candidate_text, flags=re.I).strip()
        else:
            # 否则取下一个块作为摘要
            if abstract_block_idx + 1 < len(between_blocks):
                abstract = between_blocks[abstract_block_idx + 1].content.strip()

    # 策略B：若未找到，尝试收集非作者信息作为摘要
    if not abstract:
        # 收集 between_blocks 中所有块，排除明显是作者/机构的行
        for b in between_blocks:
            text = b.content.strip()
            if not text:
                continue
            # 跳过 CCS Concepts, ACM Reference, 关键词等元数据
            if re.search(r'CCS Concepts|ACM Reference|Additional Key Words|https?://', text, re.I):
                break  # 通常这些元数据在摘要之后，遇到则停止
            # 跳过纯机构行（含 university/institute 等且不含姓名模式）
            has_inst = bool(
                re.search(r'\b(university|school|institute|college|department|laboratory|centre|center|tech)\b', text,
                          re.I))
            has_name = bool(re.search(r'\b[A-Z][a-z]+\s+[A-Z][a-z]', text))
            if has_inst and not has_name:
                continue
            # 跳过邮箱、短行（可能为作者）
            if '@' in text or len(text) < 80:
                continue
            # 否则认为是摘要的一部分
            abstract_parts.append(text)
        if abstract_parts:
            abstract = " ".join(abstract_parts)

    # 策略C：回退到正则（但改进终止条件）
    if not abstract:
        full_text = "\n".join([b.content for b in header_blocks])
        # 更全面的终止条件
        abstract_match = re.search(
            r'Abstract\s*[:：\-]?\s*(.*?)(?=\n\s*(?:1\.|I\.|Introduction|Keywords|Index Terms|—|\Z))',
            full_text,
            re.DOTALL | re.IGNORECASE
        )
        if abstract_match:
            abstract = abstract_match.group(1).strip()

    # 限制长度
    if abstract:
        meta['abstract'] = abstract[:1000]

    # ===== 4. 提取作者列表 =====
    # 策略：取标题后、摘要前的文本，按常见分隔符拆分
    # 先尝试找到标题在文本中的位置，然后取标题和摘要之间的内容
    # 但更简单的方式：使用启发式，比如提取包含多个大写字母的连续行
    # 这里采用稳健方法：使用正则匹配常见作者格式
    # 匹配 "A. B. Author, C. D. Author, and E. F. Author" 或 "Author1, Author2, Author3"
    # 也可能包含上标数字（表示单位）
    # 我们尝试提取所有看起来像人名的片段
    raw_authors = []
    # 在标题之后的部分寻找作者行（通常紧挨标题）
    # 先找标题位置
    if title:
        title_index = header_text.find(title)
        if title_index != -1:
            # 取标题之后 3000 字符（一般作者在附近）
            search_zone = header_text[title_index + len(title): title_index + 3000]
            # 匹配可能包含多个作者的行
            # 模式：由字母、点、逗号、and 等组成，不含换行，且长度适中
            lines = search_zone.split('\n')
            for line in lines[:20]:  # 只检查前20行
                line = line.strip()
                if not line:
                    continue
                # 排除明显是摘要或章节标题的行
                if re.match(r'(Abstract|Introduction|1\.|Keywords|—)', line, re.I):
                    break
                # 如果行中包含多个大写单词或缩写点，可能是作者行
                if re.search(r'[A-Z]\.?\s*[A-Z]', line) and not re.search(r'\d+', line):
                    # 按逗号、分号、and 分隔
                    parts = re.split(r'\s*[,;]\s*|\s+and\s+', line)
                    candidates = []
                    for p in parts:
                        p = p.strip()
                        # 过滤掉过短或含数字的片段
                        if len(p) > 2 and not re.search(r'\d', p):
                            candidates.append(p)
                    if candidates:
                        raw_authors.extend(candidates)
                        break  # 找到第一行作者就停止
            # 如果上述没找到，尝试从整段文本中用正则找
            if not raw_authors:
                # 匹配类似 "John Smith, Jane Doe, and Bob Lee"
                author_line_match = re.search(
                    r'([A-Z][a-z]*(?:\s+[A-Z][a-z]*)*\s*(?:,\s*[A-Z][a-z]*(?:\s+[A-Z][a-z]*)*)*\s*(?:and\s+[A-Z][a-z]*(?:\s+[A-Z][a-z]*)*)?)',
                    search_zone[:1000]
                )
                if author_line_match:
                    raw = author_line_match.group(1)
                    # 分割
                    parts = re.split(r'\s*,\s*|\s+and\s+', raw)
                    raw_authors = [p.strip() for p in parts if p.strip()]

    # 如果尚未找到，尝试从整个文本中提取连续的以大写开头的词组成的片段
    if not raw_authors:
        # 取前 2000 字符，匹配模式：多个连续的大写字母开头的单词（可能是缩写）
        # 但容易误匹配，暂时跳过，这里使用后备方案：提取所有可能的人名
        # 简单：提取所有包含字母和点号且长度适中的片段
        for match in re.finditer(r'\b([A-Z][a-z]*\.?\s*[A-Z][a-z]*\.?\s*[A-Z][a-z]*)\b', header_text[:2000]):
            candidate = match.group(1).strip()
            if len(candidate) > 2 and not re.search(r'\d', candidate):
                raw_authors.append(candidate)

    # 去重并保留顺序
    seen = set()
    unique_authors = []
    for name in raw_authors:
        if name not in seen:
            seen.add(name)
            unique_authors.append(name)
    raw_authors = unique_authors

    # 如果没有作者，只能放弃
    if not raw_authors:
        meta['authors'] = ""

    # ===== 5. 标准化作者信息 =====
    authors_objs = []
    for order, raw_name in enumerate(raw_authors):
        normalized = _normalize_author_name(raw_name)
        # 如果有单位信息（暂时从raw中获取，如果有的话）
        affiliation = None
        # 尝试在块中找单位（略）
        author_obj = Author(
            name=raw_name,
            normalized_name=normalized,
            order=order,
            affiliation=affiliation
        )
        authors_objs.append(author_obj)

    # ===== 6. 通讯作者与邮箱识别 =====
    if authors_objs:
        # 提取所有邮箱
        all_emails = _extract_emails(header_text)
        # 识别通讯作者索引
        corr_indices = _find_corresponding_author_indices(header_text, raw_authors)
        for idx in corr_indices:
            if idx < len(authors_objs):
                authors_objs[idx].is_corresponding = True

        # 匹配邮箱：依次为每个作者匹配最近邮箱
        if all_emails:
            for author in authors_objs:
                email = _match_email_to_author(author, all_emails, header_text)
                if email:
                    author.email = email

        # 转换为字典列表存储
        meta['authors'] = [a.to_dict() for a in authors_objs]
    else:
        meta['authors'] = []

    # ===== 7. 年份 =====
    year_match = re.search(r'(19|20)\d{2}', header_text)
    if year_match:
        meta['year'] = year_match.group()

    # ===== 8. DOI =====
    doi_match = re.search(r'10\.\d+/[-._;()/:A-Z0-9]+', header_text, re.I)
    if doi_match:
        meta['doi'] = doi_match.group()

    return meta