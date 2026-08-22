"""
表格分类决策模块
三级递进分类：结构规则 → LLM 语义判断 → 人工复核兜底
"""
import re
import json
from typing import Optional
from bs4 import BeautifulSoup

from config import app_config
from models import ContentBlock, TableCategory, BlockType, TableStructure

# ============================================================
# 规则引擎：第一级分类
# ============================================================

# 数据表列头关键词：出现这些词更可能是有列头的结构表
HEADER_KEYWORDS = [
    "学号", "姓名", "编号", "序号", "日期", "时间", "金额", "数量",
    "成绩", "分数", "部门", "单位", "年级", "班级", "专业", "课程",
    "代码", "名称", "类型", "状态", "备注", "说明", "地址", "电话",
]

# 聚合关键词：出现这些词强烈指示数据表
AGGREGATION_KEYWORDS = ["合计", "小计", "总计", "平均", "总和", "汇总"]

# 表单标签关键词：出现这些词更可能是填信息的表单
FORM_LABEL_KEYWORDS = [
    "签名", "签章", "审批意见", "审批", "意见", "说明", "申请理由",
    "承诺", "声明", "照片", "粘贴处", "贴照片", "盖章",
]

def _has_form_strong_signal(html: str) -> bool:
    """
    检测 HTML 表格是否具有表单的强信号：
    - 存在合并单元格（colspan/rowspan）
    - 空单元格比例 >= 30%
    返回 True 表示极可能是表单，False 表示无强信号
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return False

        # 1. 检查合并单元格
        has_span = any(
            cell.has_attr("colspan") or cell.has_attr("rowspan")
            for cell in table.find_all(["td", "th"])
        )
        if has_span:
            return True

        # 2. 检查空单元格比例
        cells = table.find_all(["td", "th"])
        if not cells:
            return False
        empty_count = sum(1 for c in cells if not c.get_text(strip=True))
        empty_ratio = empty_count / len(cells)
        return empty_ratio >= 0.3
    except Exception:
        return False

def _parse_html_table(html: str) -> Optional[TableStructure]:
    """从 HTML 字符串解析表格结构"""
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return None

        rows = table.find_all("tr")
        if not rows:
            return None

        # 解析所有行
        all_rows: list[list[str]] = []
        has_header = False

        for ri, tr in enumerate(rows):
            cells = tr.find_all(["th", "td"])
            row_data = [cell.get_text(strip=True) for cell in cells]
            if row_data:
                all_rows.append(row_data)
            # 第一行含 th 标签视为列头
            if ri == 0 and tr.find("th"):
                has_header = True

        if not all_rows:
            return None

        # 确定列数（取最大列数）
        col_count = max(len(r) for r in all_rows)
        # 补齐短行
        for r in all_rows:
            while len(r) < col_count:
                r.append("")

        # 区分列头与数据行
        if has_header and len(all_rows) > 1:
            headers = all_rows[0]
            data_rows = all_rows[1:]
        else:
            headers = []
            data_rows = all_rows

        # 检测是否有合并单元格（通过 colspan/rowspan）
        has_merged = any(
            cell.has_attr("colspan") or cell.has_attr("rowspan")
            for tr in rows
            for cell in tr.find_all(["th", "td"])
        )

        # 检测聚合行
        has_agg = any(
            any(kw in cell_text for cell_text in row)
            for row in all_rows
            for kw in AGGREGATION_KEYWORDS
        )

        return TableStructure(
            headers=headers,
            rows=data_rows,
            col_count=col_count,
            row_count=len(data_rows),
            has_aggregation=has_agg,
        )
    except Exception:
        return None


def _classify_by_rules(structure: TableStructure) -> tuple[str, float]:
    """
    基于结构规则打分分类
    返回 (类型, 评分)
    """
    score = 0.0

    # 列数 ≥ 3 → 数据表信号
    if structure.col_count >= 3:
        score += 2
    else:
        score -= 2

    # 行数 ≥ 5 → 数据表信号
    if structure.row_count >= 5:
        score += 2
    else:
        score -= 2

    # 合并单元格 → 表单信号（注意这里需要 HTML 信息，简化处理：通过 headers
    #   和 rows 的差异间接推断）
    #   实际上 merged 信息在结构解析时一并提取，此处先跳过

    # 含列头关键词
    header_text = "".join(structure.headers)
    if any(kw in header_text for kw in HEADER_KEYWORDS):
        score += 2

    # 含聚合关键词
    all_text = header_text + "".join(
        cell for row in structure.rows for cell in row
    )
    if structure.has_aggregation:
        score += 3

    # 含表单标签关键词
    if any(kw in all_text for kw in FORM_LABEL_KEYWORDS):
        score -= 2

    if score > 0:
        return ("data_table", score)
    elif score < 0:
        return ("form", score)
    else:
        return ("uncertain", score)


# ============================================================
# LLM 分类：第二级
# ============================================================

CLASSIFICATION_PROMPT = """判断以下表格是「数据表」还是「表单」。

数据表：存储多条同构记录，通常有列头和多行数据，如成绩表、名单、统计表。
表单：收集单条填写信息，通常是标签-值成对出现，如申请表、审批表、登记表。

表格内容（前3行 + 列头）：
{table_preview}

请输出 JSON 格式：
{{"type": "data_table" | "form", "confidence": 0.0~1.0}}"""


def _classify_by_llm(structure: TableStructure) -> tuple[str, float]:
    """
    通过 LLM API 进行语义分类
    返回 (类型, 置信度)
    """
    # 构造表格预览
    preview_lines = []
    if structure.headers:
        preview_lines.append(" | ".join(structure.headers))
        preview_lines.append("-" * 40)
    for row in structure.rows[:3]:
        preview_lines.append(" | ".join(row))
    table_preview = "\n".join(preview_lines)

    prompt = CLASSIFICATION_PROMPT.format(table_preview=table_preview)

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=app_config.llm.api_url,
            api_key=app_config.llm.api_key,
        )
        response = client.chat.completions.create(
            model=app_config.llm.model,
            messages=[
                {"role": "system", "content": "你是表格分类专家。只输出 JSON，不要额外说明。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        raw = response.choices[0].message.content.strip()
        # 提取 JSON（可能包裹在 ``` 中）
        if "```" in raw:
            raw = re.sub(r"```\w*\n?", "", raw).replace("```", "").strip()
        result = json.loads(raw)
        table_type = result.get("type", "uncertain")
        confidence = float(result.get("confidence", 0.5))
        if table_type not in ("data_table", "form"):
            table_type = "uncertain"
        return (table_type, confidence)
    except Exception as e:
        # LLM 调用失败，回退到 uncertain
        return ("uncertain", 0.0)


# ============================================================
# 三级递进分类入口
# ============================================================

def classify_table_block(block: ContentBlock) -> ContentBlock:
    """
    对单个表格类 ContentBlock 执行三级递进分类
    修改 block 的 table_category 和 table_score 字段并返回
    """
    if block.type != BlockType.TABLE:
        return block

    html = block.table_html or block.content
    structure = _parse_html_table(html)

    if structure is None:
        # 无法解析结构，标记为 uncertain
        block.table_category = TableCategory.UNCERTAIN
        block.table_score = 0.0
        return block

    # ========== 新增：强信号检测（直接拦截表单） ==========
    if _has_form_strong_signal(html):
        block.table_category = TableCategory.FORM
        block.table_score = 0.0  # 强信号，分数置零
        print(f"检测到表单强信号，判定为表单")
        return block

    # 第一级：规则分类
    rule_type, score = _classify_by_rules(structure)
    if rule_type != "uncertain":
        block.table_category = (
            TableCategory.DATA_TABLE if rule_type == "data_table" else TableCategory.FORM
        )
        block.table_score = score
        return block

    # 第二级：LLM 语义判断
    llm_type, confidence = _classify_by_llm(structure)
    threshold = app_config.llm.confidence_threshold
    if confidence >= threshold and llm_type != "uncertain":
        block.table_category = (
            TableCategory.DATA_TABLE if llm_type == "data_table" else TableCategory.FORM
        )
        block.table_score = confidence
        return block

    # 第三级：人工复核
    block.table_category = TableCategory.UNCERTAIN
    block.table_score = confidence if confidence > 0 else 0.0
    return block


def classify_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """对一批 ContentBlock 中所有的表格块进行分类"""
    for block in blocks:
        if block.type == BlockType.TABLE:
            classify_table_block(block)
    return blocks


# ============================================================
# 图片分类（辅助）
# ============================================================

def classify_image_block(block: ContentBlock) -> ContentBlock:
    """
    对图片块做简单分类（图表/照片/印章/图标）
    当前用尺寸启发式规则，后续可接入视觉 LLM
    """
    if block.type != BlockType.IMAGE:
        return block

    from models import ImageCategory

    # 尺寸启发式：小图标过滤，大图保留
    if block.image_size:
        w, h = block.image_size
        if w < 100 and h < 100:
            block.image_category = ImageCategory.ICON
        else:
            block.image_category = ImageCategory.PHOTO
    else:
        block.image_category = ImageCategory.PHOTO

    return block
