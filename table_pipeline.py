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
from table_catalog import record_table_catalog


# ============================================================
# HTML 表格解析
# ============================================================

def _parse_span(value: object) -> int:
    """将 HTML 的 rowspan/colspan 转成至少为 1 的整数。"""
    try:
        return max(int(str(value)), 1)
    except (TypeError, ValueError):
        return 1


def _expand_table_grid(table) -> tuple[list[list[str]], list[list[bool]]]:
    """展开 rowspan 和 colspan，返回单元格网格及其 th 标记。"""
    grid: list[list[str]] = []
    header_grid: list[list[bool]] = []

    for row_index, tr in enumerate(table.find_all("tr")):
        while len(grid) <= row_index:
            grid.append([])
            header_grid.append([])

        col_index = 0
        cells = tr.find_all(["th", "td"], recursive=False)
        for cell in cells:
            while col_index < len(grid[row_index]) and grid[row_index][col_index] != "":
                col_index += 1

            text = cell.get_text(" ", strip=True)
            rowspan = _parse_span(cell.get("rowspan", 1))
            colspan = _parse_span(cell.get("colspan", 1))
            is_header = cell.name == "th"

            for row_offset in range(rowspan):
                target_row = row_index + row_offset
                while len(grid) <= target_row:
                    grid.append([])
                    header_grid.append([])
                required_width = col_index + colspan
                if len(grid[target_row]) < required_width:
                    grid[target_row].extend([""] * (required_width - len(grid[target_row])))
                    header_grid[target_row].extend([False] * (required_width - len(header_grid[target_row])))

                for col_offset in range(colspan):
                    target_col = col_index + col_offset
                    grid[target_row][target_col] = text
                    header_grid[target_row][target_col] = is_header

            col_index += colspan

    col_count = max((len(row) for row in grid), default=0)
    for row, header_row in zip(grid, header_grid):
        row.extend([""] * (col_count - len(row)))
        header_row.extend([False] * (col_count - len(header_row)))
    return grid, header_grid


def _looks_like_data_row(row: list[str]) -> bool:
    """用数值占比识别无 th 标记表格的首条数据行。"""
    values = [value.strip() for value in row if value.strip()]
    if not values:
        return False

    numeric_count = sum(1 for value in values if _is_float(value))
    return numeric_count >= max(1, len(values) // 2)


def _infer_header_row_count(
    grid: list[list[str]], header_grid: list[list[bool]]
) -> int:
    """确定表头占用的连续行数，兼容 MinerU 仅输出 td 的表格。"""
    if not grid:
        return 0

    marked_header_rows = 0
    for header_row in header_grid:
        if any(header_row):
            marked_header_rows += 1
        else:
            break
    if marked_header_rows:
        return marked_header_rows

    for row_index, row in enumerate(grid):
        if row_index > 0 and _looks_like_data_row(row):
            return row_index

    # 纯文本数据表无法仅凭内容可靠区分多级表头，保留原有的首行表头策略。
    return 1 if len(grid) > 1 else 0


def _build_header_paths(header_rows: list[list[str]], col_count: int) -> list[str]:
    """合并每一列的多级表头，并去除 rowspan 带来的重复名称。"""
    headers: list[str] = []
    for col_index in range(col_count):
        parts: list[str] = []
        for row in header_rows:
            value = row[col_index].strip()
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        headers.append(" / ".join(parts))
    return headers


def parse_table_html(html: str) -> Optional[TableStructure]:
    """从 HTML 字符串解析表格结构，返回 TableStructure"""
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return None

        grid, header_grid = _expand_table_grid(table)
        if not grid:
            return None

        # 检测聚合行关键词
        agg_keywords = ["合计", "小计", "总计", "平均", "总和", "汇总"]

        col_count = len(grid[0])
        header_row_count = _infer_header_row_count(grid, header_grid)
        headers = _build_header_paths(grid[:header_row_count], col_count)
        data_rows = grid[header_row_count:]

        # 检测聚合行（数据行中可能包含聚合）
        has_agg = any(
            any(kw in cell for cell in row)
            for row in grid
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


def _coerce_value_for_storage(value: str, column_type: str) -> str | int | float | None:
    """按推断列类型转换单元格值，使带千分位的数字可写入 PostgreSQL。"""
    cleaned = value.strip()
    if not cleaned:
        return None

    normalized_number = cleaned.replace(",", "").replace("，", "")
    if column_type in ("INTEGER", "BIGINT"):
        return int(normalized_number)
    if column_type == "DOUBLE PRECISION":
        return float(normalized_number)
    return value


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


def build_table_column_definitions(structure: TableStructure) -> list[dict[str, str]]:
    """生成数据库列名与展示表头的映射，供入库和 API 复用。"""
    raw_headers = structure.headers if structure.headers else [
        f"col_{i}" for i in range(structure.col_count)
    ]
    cleaned_headers = [_sanitize_col_name(header) for header in raw_headers]

    seen: dict[str, int] = {}
    deduped_headers: list[str] = []
    for header in cleaned_headers:
        if header in seen:
            seen[header] += 1
            deduped_headers.append(f"{header}_{seen[header]}")
        else:
            seen[header] = 1
            deduped_headers.append(header)

    if app_config.column_naming_style == "original":
        final_headers = deduped_headers
    else:
        final_headers = [
            header if _is_valid_english_identifier(header) else f"col_{index + 1}"
            for index, header in enumerate(deduped_headers)
        ]

    column_types = _infer_column_types(structure.rows, structure.col_count)
    return [
        {
            "key": key,
            "label": label,
            "data_type": data_type,
        }
        for key, label, data_type in zip(final_headers, raw_headers, column_types)
    ]


def _table_caption(block: ContentBlock) -> str:
    """从 MinerU 原始块中读取表格 caption。"""
    raw = block.raw if isinstance(block.raw, dict) else {}
    caption = raw.get("table_caption")
    if isinstance(caption, list):
        return " ".join(str(item).strip() for item in caption if str(item).strip())
    return str(caption or "").strip()


def _split_table_heading(value: str) -> tuple[str, str]:
    """拆分 X010102 经费数额形式的表格编号和名称。"""
    normalized = " ".join(value.split())
    match = re.match(r"^([A-Za-z]\d{6})\s+(.+)$", normalized)
    if not match:
        return "", normalized
    return match.group(1), match.group(2).strip()


def _heading_identity(block: ContentBlock) -> tuple[str, str] | None:
    """提取可作为表格名称来源的标题块。"""
    if block.type != BlockType.TEXT:
        return None
    text = " ".join(block.content.split())
    if not text:
        return None
    raw = block.raw if isinstance(block.raw, dict) else {}
    try:
        is_heading = int(raw.get("text_level", 99)) <= 2
    except (TypeError, ValueError):
        is_heading = False
    if re.match(r"^[A-Za-z]\d{6}\s+.+$", text) or is_heading:
        return _split_table_heading(text)
    return None


def resolve_table_identity(blocks: list[ContentBlock], table_index: int) -> tuple[str, str]:
    """按 caption、同页相邻标题的优先级确定表格编号和名称。"""
    block = blocks[table_index]
    caption = _table_caption(block)
    if caption:
        return _split_table_heading(caption)

    # 常见版式是标题在表格之前，优先使用同页最近的前方标题。
    for candidate in reversed(blocks[:table_index]):
        if candidate.page_num != block.page_num:
            continue
        identity = _heading_identity(candidate)
        if identity:
            return identity

    # 部分解析器会把相邻标题排在表格之后，作为同页回退来源。
    for candidate in blocks[table_index + 1:]:
        if candidate.page_num != block.page_num:
            continue
        identity = _heading_identity(candidate)
        if identity:
            return identity
    return "", ""


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

    column_definitions = build_table_column_definitions(structure)
    final_headers = [column["key"] for column in column_definitions]
    col_types = [column["data_type"] for column in column_definitions]

    # normalized 模式下保留原始表头映射，便于查询结果还原展示名称。
    if naming_style != 'original':
        raw_headers = [column["label"] for column in column_definitions]
        if any(
            not _is_valid_english_identifier(_sanitize_col_name(header))
            for header in raw_headers
        ):
            # 存储映射到 metadata_table
            _ensure_metadata_table()
            table_name = f"pdf_{file_id}_tbl_{block_id}"
            _save_column_mapping(table_name, final_headers, raw_headers)

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
        # 数值列先转为 Python 数值，避免 PostgreSQL 无法解析 28,003 等千分位写法。
        row_values = [
            _coerce_value_for_storage(value, column_type)
            for value, column_type in zip(
                row[:structure.col_count],
                col_types,
            )
        ]
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
    blocks: list[ContentBlock], file_id: str, file_name: str = ""
) -> tuple[int, int, int]:
    """
    表格块处理主入口。
    根据 classifier 的分类结果，将数据表和表单分别路由到对应的存储。
    返回：(data_tables_stored, forms_stored, uncertain_count)
    """
    dt_count = 0
    form_count = 0
    uncertain_count = 0

    for block_index, block in enumerate(blocks):
        if block.type != BlockType.TABLE or block.table_category is None:
            continue
        html = block.table_html or block.content
        structure = parse_table_html(html)
        if structure is None:
            continue

        table_code, table_title = resolve_table_identity(blocks, block_index)

        if block.table_category == TableCategory.DATA_TABLE:
            table_name = store_data_table(
                file_id, block.block_id, structure, block.page_num
            )
            if table_name:
                record_table_catalog(
                    file_id=file_id,
                    file_name=file_name,
                    block=block,
                    table_code=table_code,
                    table_title=table_title,
                    table_category=block.table_category.value,
                    storage_target="pg_relational",
                    storage_location=table_name,
                    columns=build_table_column_definitions(structure),
                    row_count=len(structure.rows),
                )
                dt_count += 1

        elif block.table_category == TableCategory.FORM:
            doc_id = store_form(
                file_id, block.block_id, structure, block.page_num
            )
            if doc_id:
                record_table_catalog(
                    file_id=file_id,
                    file_name=file_name,
                    block=block,
                    table_code=table_code,
                    table_title=table_title,
                    table_category=block.table_category.value,
                    storage_target="pg_jsonb",
                    storage_location=doc_id,
                    columns=[
                        {"key": "field", "label": "字段", "data_type": "TEXT"},
                        {"key": "value", "label": "值", "data_type": "TEXT"},
                    ],
                    row_count=len(structure.rows),
                )
                form_count += 1

        else:
            uncertain_count += 1

    return dt_count, form_count, uncertain_count
