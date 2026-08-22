"""
表格处理管线：解析 → 类型推断 → 数据表入关系库 / 表单入 JSONB
"""
import re
import json
from typing import Optional
from datetime import datetime

from bs4 import BeautifulSoup

from config import app_config
from models import (
    ContentBlock, BlockType, TableCategory, TableStructure,
)
from storage_adapter import storage


# ============================================================
# HTML 表格解析
# ============================================================

def parse_table_html(html: str) -> Optional[TableStructure]:
    """从 HTML 字符串解析表格结构，返回 TableStructure"""
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

        # 列数 = 最大列数
        col_count = max(len(r) for r in all_rows)
        # 补齐短行
        for r in all_rows:
            while len(r) < col_count:
                r.append("")

        # 检测聚合行关键词
        agg_keywords = ["合计", "小计", "总计", "平均", "总和", "汇总"]

        # ========== 修改开始 ==========
        # 分离列头和数据行
        if has_header and len(all_rows) > 1:
            headers = all_rows[0]
            data_rows = all_rows[1:]
        else:
            # 没有 th 标签时，尝试启发式判断：第一行是否像表头
            first_row = all_rows[0]
            # 如果第一行包含聚合关键词，视为数据行（不应作为表头）
            is_agg_row = any(any(kw in cell for cell in first_row) for kw in agg_keywords)
            # 如果行数 > 1 且第一行不是聚合行，则将第一行作为表头
            if len(all_rows) > 1 and not is_agg_row:
                headers = first_row
                data_rows = all_rows[1:]
            else:
                headers = []
                data_rows = all_rows
        # ========== 修改结束 ==========

        # 检测聚合行（数据行中可能包含聚合）
        has_agg = any(
            any(kw in cell for cell in row)
            for row in all_rows
            for kw in agg_keywords
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

# ============================================================
# 列类型推断
# ============================================================

def _infer_column_types(
    rows: list[list[str]], col_count: int
) -> list[str]:
    """
    扫描前 20 行推断每列的类型。
    返回类型列表：INTEGER / BIGINT / DOUBLE PRECISION / DATE / TEXT
    """
    types = []
    for ci in range(col_count):
        samples = [rows[ri][ci] for ri in range(min(len(rows), 20)) if rows[ri][ci].strip()]
        if not samples:
            types.append("TEXT")
            continue

        # 1. 尝试整数（含范围检查）
        all_int = True
        int_vals = []
        for v in samples:
            clean = v.replace(",", "").replace("，", "")
            try:
                int_vals.append(int(clean))
            except ValueError:
                all_int = False
                break
        if all_int:
            # 检查范围
            if all(-2147483648 <= val <= 2147483647 for val in int_vals):
                types.append("INTEGER")
            elif all(-9223372036854775808 <= val <= 9223372036854775807 for val in int_vals):
                types.append("BIGINT")
            else:
                types.append("TEXT")  # 超出范围，转为文本
            continue

        # 2. 尝试浮点数
        all_float = True
        for v in samples:
            clean = v.replace(",", "").replace("，", "")
            try:
                float(clean)
            except ValueError:
                all_float = False
                break
        if all_float:
            types.append("DOUBLE PRECISION")
            continue

        # 3. 尝试日期
        if all(_is_date(v) for v in samples):
            types.append("DATE")
            continue

        # 4. 默认文本
        types.append("TEXT")
    return types

def _is_int(s: str) -> bool:
    """判断字符串是否可转为整数"""
    try:
        int(s.replace(",", "").replace("，", ""))
        return True
    except ValueError:
        return False


def _is_float(s: str) -> bool:
    """判断字符串是否可转为浮点数"""
    try:
        float(s.replace(",", "").replace("，", ""))
        return True
    except ValueError:
        return False


def _is_date(s: str) -> bool:
    """判断字符串是否为常见日期格式"""
    patterns = [
        r"^\d{4}-\d{2}-\d{2}$",
        r"^\d{4}/\d{2}/\d{2}$",
        r"^\d{4}\.\d{2}\.\d{2}$",
        r"^\d{4}年\d{1,2}月\d{1,2}日$",
    ]
    return any(re.match(p, s) for p in patterns)


# ============================================================
# 列名处理辅助函数（新增）
# ============================================================

def _is_valid_english_identifier(name: str) -> bool:
    """
    判断字符串是否为合法的 PostgreSQL 标识符（仅含字母、数字、下划线，且不以数字开头）
    """
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def _save_column_mapping(table_name, actual_cols, original_cols):
    """保存映射到 metadata_table"""
    mapping = {actual: original for actual, original in zip(actual_cols, original_cols)}
    # 由于 metadata_table 只有两列，我们使用 insert_rows 插入/更新
    # 但 insert_rows 没有 ON CONFLICT，所以先查询是否存在，存在则删除再插入，或使用原生 SQL
    # 但为了保持纯粹用适配器，我们可以用 insert_rows 插入，但可能重复，需要先删除
    # 简单办法：用 query 检查，然后决定插入或更新。但 query 返回列表。
    existing = storage.relational.query("metadata_table", where=f"table_name = '{table_name}'", limit=1)
    if existing:
        # 更新，但 insert_rows 不支持更新，所以我们改用原生 SQL，或者删除再插入
        # 这里权衡后，建议还是直接用 conn 执行更新，但为了适配器统一，我们使用原生 SQL 一次
        # 由于用户强调尽量用适配器，我们可以容忍这一处使用原生 SQL，或者用 insert_rows 插入重复会报错。
        # 更稳妥：先用 delete，再 insert。但 delete 需要适配器支持。
        # 折中：使用原生 SQL 更新。
        conn = storage.relational.conn
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE metadata_table SET column_mapping = %s WHERE table_name = %s",
                (json.dumps(mapping, ensure_ascii=False), table_name)
            )
            conn.commit()
    else:
        # 插入新记录
        storage.relational.insert_rows(
            "metadata_table",
            ["table_name", "column_mapping"],
            [[table_name, json.dumps(mapping, ensure_ascii=False)]]
        )


def _ensure_metadata_table():
    """创建元数据表（如果不存在），用于存储列名映射关系"""
    # 检查表是否存在（使用 storage.relational 的辅助方法）
    # 如果 storage.relational 支持 table_exists，可以用；否则直接创建（幂等）
    try:
        storage.relational.create_table(
            "metadata_table",
            [
                ("table_name", "VARCHAR(255) PRIMARY KEY"),
                ("column_mapping", "JSONB NOT NULL"),
            ]
        )
    except Exception:
        # 表已存在则忽略错误
        pass


# ============================================================
# 数据表 → 关系表（已修改）
# ============================================================

def _sanitize_col_name(name: str) -> str:
    """
    列名蛇形规范化：
    - 去除特殊字符，替换空格为下划线
    - 若为空则生成 col_N
    """
    if not name or not name.strip():
        return "col_unknown"
    # 只保留中文、英文、数字、下划线
    name = re.sub(r"[^\w\u4e00-\u9fff]", "_", name.strip())
    # 去重下划线
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return "col_unknown"
    return name.lower()


def store_data_table(
    file_id: str, block_id: str, structure: TableStructure, page_num: int
) -> str:
    """
    将数据表写入 PostgreSQL 关系表。
    策略：根据 column_naming_style 决定列名格式
    - original: 保留原始列名（带双引号），适合内部快速查看
    - normalized: 合法英文直接使用，非英文转为 col_N 并存储映射
    额外列：_page_num（溯源页码）、_row_idx（原始行号）。
    返回：创建的表名
    """
    if not structure.rows:
        return ""

    naming_style = app_config.column_naming_style  # 'original' 或 'normalized'

    # 1. 清理列名（基础清洗）
    raw_headers = structure.headers if structure.headers else [
        f"col_{i}" for i in range(structure.col_count)
    ]
    cleaned_headers = [_sanitize_col_name(h) for h in raw_headers]

    # 去重处理：重名列追加 _2, _3...
    seen: dict[str, int] = {}
    deduped_headers = []
    for h in cleaned_headers:
        if h in seen:
            seen[h] += 1
            deduped_headers.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 1
            deduped_headers.append(h)

    # 2. 根据命名模式生成最终列名
    need_mapping = False
    if naming_style == 'original':
        final_headers = deduped_headers
    else:  # 'normalized'
        final_headers = []
        for name in deduped_headers:
            if _is_valid_english_identifier(name):
                final_headers.append(name)
            else:
                if not need_mapping:
                    need_mapping = True
                final_headers.append(f"col_{len(final_headers) + 1}")
        if need_mapping:
            # 存储映射到 metadata_table
            _ensure_metadata_table()
            table_name = f"pdf_{file_id}_tbl_{block_id}"
            _save_column_mapping(table_name, final_headers, deduped_headers)

    # 3. 推断列类型
    col_types = _infer_column_types(structure.rows, structure.col_count)

    table_name = f"pdf_{file_id}_tbl_{block_id}"

    # =========================================================
    # 4. 建表（使用适配器的 create_table）
    # =========================================================
    storage.relational.drop_table(table_name)

    columns_def = [
        ("_row_id", "SERIAL PRIMARY KEY"),
        ("_page_num", "INTEGER"),
        ("_row_idx", "INTEGER"),
    ]
    for h, t in zip(final_headers, col_types):
        columns_def.append((h, t))


    storage.relational.create_table(table_name, columns_def)

    # =========================================================
    # 5. 插入数据（使用适配器的 insert_rows）
    # =========================================================
    insert_cols = ["_page_num", "_row_idx"] + final_headers

    insert_rows: list[list] = []
    for ri, row in enumerate(structure.rows):
        while len(row) < structure.col_count:
            row.append("")
        # 空字符串转为 None，其他原样保留（数据库会自动转换类型）
        row_values = [None if val == "" else val for val in row[:structure.col_count]]
        insert_rows.append([page_num, ri] + row_values)

    storage.relational.insert_rows(table_name, insert_cols, insert_rows)
    print("插入数据完成")
    return table_name

# ============================================================
# 表单 → JSONB 文档
# ============================================================

def store_form(
    file_id: str, block_id: str, structure: TableStructure, page_num: int
) -> str:
    """
    将表单提取为 Key-Value JSON 文档，存入统一的 pdf_forms 表（JSONB 列）。
    策略：左侧列作为 key，右侧列作为 value，多行合并。
    返回：doc_id
    """
    kv_pairs: dict[str, str] = {}
    for row in structure.rows:
        if len(row) >= 2:
            key = row[0].strip()
            val = " | ".join(c.strip() for c in row[1:] if c.strip())
            if key:
                kv_pairs[key] = val
        elif len(row) == 1 and row[0].strip():
            # 单列表格：整行作为备注
            kv_pairs[f"_note_{len(kv_pairs)}"] = row[0].strip()

    if not kv_pairs:
        return ""

    document = {
        "file_id": file_id,
        "block_id": block_id,
        "page_num": page_num,
        "form_type": "extracted_form",
        "fields": kv_pairs,
        "extracted_at": datetime.now().isoformat(),
    }

    doc_id = storage.document.insert_document("pdf_forms", document)
    return doc_id


# ============================================================
# 统一入口
# ============================================================

def process_table_blocks(
    blocks: list[ContentBlock], file_id: str
) -> tuple[int, int, int]:
    """
    表格块处理主入口。
    根据 classifier 的分类结果，将数据表和表单分别路由到对应的存储。
    返回：(data_tables_stored, forms_stored, uncertain_count)
    """
    table_blocks = [
        b for b in blocks
        if b.type == BlockType.TABLE and b.table_category is not None
    ]

    dt_count = 0
    form_count = 0
    uncertain_count = 0

    for block in table_blocks:
        html = block.table_html or block.content
        structure = parse_table_html(html)
        if structure is None:
            continue

        if block.table_category == TableCategory.DATA_TABLE:
            table_name = store_data_table(
                file_id, block.block_id, structure, block.page_num
            )
            if table_name:
                dt_count += 1

        elif block.table_category == TableCategory.FORM:
            doc_id = store_form(
                file_id, block.block_id, structure, block.page_num
            )
            if doc_id:
                form_count += 1

        else:
            uncertain_count += 1

    return dt_count, form_count, uncertain_count