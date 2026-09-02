import re
import math
import fitz
from typing import Tuple, Optional, Dict, List
from dataclasses import dataclass


@dataclass
class Signal:
    category: str  # 目标类别
    weight: float  # 权重
    description: str  # 原因
    is_strong: bool = False  # True=强信号，直接裁决


# 强信号冲突时的优先级（学术最优先，因其特征最独特）
CATEGORY_PRIORITY = {
    "academic_paper": 1,
    "policy_regulation": 2,
    "admin_form": 3,
    "contract": 4,
}


def classify_document(file_path: str, filename: str) -> dict:
    """
    文档类型分类主入口

    返回: {
        "doc_type": str,       # academic_paper | policy_regulation | admin_form | contract | other
        "confidence": float,    # 0.0 ~ 1.0
        "reason": str,          # 分类原因简述
        "signals": list         # 命中的所有信号（调优/审计用）
    }
    """
    # 1. 提取文本：前3页 + 末1页（用于捕捉参考文献、落款等）
    preview_text = _extract_text(file_path, head_pages=3, tail_pages=1)
    total_pages = _get_page_count(file_path)
    metadata = _extract_metadata(file_path)

    # 2. 规则引擎
    doc_type, confidence, reason, signals = _rule_based_classify(
        preview_text, filename, total_pages, metadata
    )

    # 3. 兜底
    if doc_type is None:
        doc_type = "other"
        confidence = 0.0
        reason = "未触发任何有效分类信号"

    return {
        "doc_type": doc_type,
        "confidence": round(confidence, 3),
        "reason": reason,
        "signals": [f"{'[强信号]' if s.is_strong else ''}{s.description}" for s in signals]
    }


# ==================== 文本/元数据提取 ====================

def _extract_text(file_path: str, head_pages: int = 3, tail_pages: int = 1, max_chars: int = 6000) -> str:
    """提取文档头部和尾部文本（尾部用于检测参考文献、公文落款等）"""
    try:
        doc = fitz.open(file_path)
        text = ""

        # 头部
        for i in range(min(head_pages, len(doc))):
            text += doc[i].get_text()

        # 尾部（避免与头部重复提取）
        if len(doc) > head_pages:
            start = max(head_pages, len(doc) - tail_pages)
            for i in range(start, len(doc)):
                text += doc[i].get_text()

        doc.close()
        return text[:max_chars]
    except Exception:
        return ""


def _extract_metadata(file_path: str) -> dict:
    try:
        doc = fitz.open(file_path)
        meta = doc.metadata or {}
        doc.close()
        return {
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "creator": meta.get("creator", ""),
        }
    except Exception:
        return {}


def _get_page_count(file_path: str) -> int:
    try:
        doc = fitz.open(file_path)
        count = len(doc)
        doc.close()
        return count
    except Exception:
        return 0


# ==================== 信号检测引擎 ====================

def _detect_signals(text: str, filename: str, page_count: int, metadata: dict) -> List[Signal]:
    """统一检测所有信号（强信号 + 软信号）"""
    signals = []
    filename_lower = filename.lower()
    text_lower = text.lower()

    # ==================== 学术论文 ====================
    # --- 强信号 ---
    if re.search(r'(?:https?://doi\.org/)?10\.\d{4,9}/[-._;()/:A-Z0-9]+', text, re.I):
        signals.append(Signal("academic_paper", 1.0, "检测到DOI", True))

    if re.search(r'arXiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?', text, re.I):
        signals.append(Signal("academic_paper", 1.0, "检测到arXiv编号", True))

    if re.search(r'PMID\s*:\s*\d{7,8}|PMCID\s*:\s*PMC\d+', text, re.I):
        signals.append(Signal("academic_paper", 1.0, "检测到PubMed标识", True))

    # 学术引用格式块（PVLDB/ACM/IEEE Reference Format...）
    ACADEMIC_VENUES = (
        r'(?:PVLDB|VLDB|SIGMOD|ICDE|CIDR|EDBT|TKDE|TODS|TOIS|'
        r'NeurIPS|ICML|ICLR|AAAI|IJCAI|CVPR|ICCV|ECCV|'
        r'ACL|EMNLP|NAACL|COLING|KDD|WWW|CIKM|WSDM|RecSys|'
        r'OSDI|SOSP|NSDI|EuroSys|ASPLOS|ATC|FAST|'
        r'SIGCOMM|MobiCom|MobiSys|INFOCOM|'
        r'S&P|CCS|USENIX\s+Security|NDSS|Crypto|Eurocrypt|'
        r'PLDI|POPL|OOPSLA|ICSE|FSE|ASE|'
        r'STOC|FOCS|SODA|'
        r'Nature|Science|PNAS|IEEE|ACM|Springer|Elsevier)'
    )
    if re.search(rf'{ACADEMIC_VENUES}\s+Reference\s+Format\s*[：:]', text, re.I | re.M):
        signals.append(Signal("academic_paper", 1.0, "检测到学术引用格式块", True))



    # 审稿/出版日期三连（Received/Accepted/Published 或 收稿/修回/录用）
    if re.search(r'(?:Received|Revised|Accepted|Published).*?\d{4}|'
                 r'(?:收稿日期|修回日期|录用日期).*?\d{4}', text, re.I):
        signals.append(Signal("academic_paper", 1.0, "检测到论文审稿或出版日期", True))

    # 大量学术引用 [1]~[99]
    citation_count = len(re.findall(r'\[\d{1,2}\]', text))
    if citation_count >= 10:
        signals.append(Signal("academic_paper", 1.0, f"检测到{citation_count}处学术引用", True))

    # 期刊卷期页格式
    if re.search(r'(?:Journal|Vol\.?|Volume)\s*\d+.*(?:No\.?|Issue)\s*\d+', text, re.I):
        signals.append(Signal("academic_paper", 1.0, "检测到期刊卷期格式", True))

    # --- 软信号 ---
    academic_keywords = [
        "abstract", "introduction", "keywords", "references",
        "materials and methods", "results", "discussion","conclusion","experiments",
        "摘要", "关键词", "引言", "材料与方法", "结果", "讨论", "参考文献", "结论"
    ]
    matched = sum(1 for k in academic_keywords if k in text_lower)
    if matched >= 3:
        signals.append(Signal("academic_paper", 2.5, f"命中{matched}个论文章节关键词"))

    fig_table = len(re.findall(r'(?:Fig\.|Figure|Table|图\s*\d|表\s*\d)', text, re.I))
    if fig_table >= 3:
        signals.append(Signal("academic_paper", 2.0, f"检测到{fig_table}处图表引用"))

    # 参考文献列表（通常在末尾）
    if re.search(r'(?:References|参考文献)\s*\n\s*(?:\[\d+\]|\d+\.)', text, re.I):
        ref_items = len(re.findall(r'(?:\n\s*\[\d+\]|\n\s*\d+\.)', text[-2000:]))
        if ref_items >= 5:
            signals.append(Signal("academic_paper", 2.0, f"参考文献列表约{ref_items}条"))

    if 5 <= page_count <= 50:
        signals.append(Signal("academic_paper", 1.0, "页数符合论文范围(5-50页)"))

    if metadata.get("author") and len(metadata["author"]) > 3:
        signals.append(Signal("academic_paper", 0.5, "PDF元数据含作者信息"))

    if re.search(r'论文|学报|journal|thesis|dissertation|paper|review', filename, re.I):
        signals.append(Signal("academic_paper", 1.0, "文件名含学术关键词"))

    # ==================== 政策制度 ====================
    # --- 强信号 ---
    # 红头文号（兼容 〔 ] [ 〕 等括号变体，年份限制1900-2099）
    if re.search(r'[\u4e00-\u9fa5]{2,6}[〔\[](?:19|20)\d{2}[〕\]]\d+号', text):
        signals.append(Signal("policy_regulation", 1.0, "检测到公文发文字号", True))

    # 公文主送抄送格式
    if "主送：" in text and "抄送：" in text:
        signals.append(Signal("policy_regulation", 1.0, "检测到公文主送抄送格式", True))

    # 权威机关 + 印发
    if re.search(r'(?:国务院|中共中央|教育部|发改委|人民政府|办公厅).{0,15}印发', text):
        signals.append(Signal("policy_regulation", 1.0, "检测到权威机关印发用语", True))

    # 公文特定套语
    if re.search(r'现印发给你们|请认真贯彻执行|请遵照执行|特此通知|此令', text):
        signals.append(Signal("policy_regulation", 1.0, "检测到公文套语", True))

    # --- 软信号 ---
    chapter_struct = len(re.findall(r'第[一二三四五六七八九十百\d]+[章条]', text))
    if chapter_struct >= 3:
        signals.append(Signal("policy_regulation", 2.5, f"检测到{chapter_struct}处章节条款"))

    policy_types = ["通知", "规定", "办法", "条例", "细则", "意见", "决定", "公告", "批复", "令"]
    type_matches = sum(1 for t in policy_types if t in text)
    if type_matches >= 2:
        signals.append(Signal("policy_regulation", 2.0, f"命中{type_matches}个公文类型词"))

    if re.search(r'(?:自|于)\d{4}年\d{1,2}月\d{1,2}日(?:起?施行|实施|生效)', text):
        signals.append(Signal("policy_regulation", 1.5, "检测到施行日期"))

    if re.search(r'^\s*[\u4e00-\u9fa5]{2,12}(?:厅|局|委|办|部|署|会|院|校|政府)', text, re.M):
        signals.append(Signal("policy_regulation", 1.5, "检测到发文机关标志"))

    if re.search(r'办法|规定|通知|意见|批复|条例|细则|制度', filename):
        signals.append(Signal("policy_regulation", 1.0, "文件名含制度关键词"))

    # ==================== 表单 ====================
    # --- 强信号 ---
    checkbox_count = len(re.findall(r'[□☑☐]', text))
    if checkbox_count >= 5:
        signals.append(Signal("admin_form", 1.0, f"检测到{checkbox_count}个复选框", True))

    if re.search(r'(申请|登记|审批|报名|调查|备案|统计|申报|测评|考核)[表册]', filename):
        signals.append(Signal("admin_form", 1.0, "文件名明确为表单类型", True))

    if "填表日期" in text and ("填表人" in text or "申请人" in text):
        signals.append(Signal("admin_form", 1.0, "检测到填表日期和填表人/申请人", True))

    underline_count = len(re.findall(r'[_]{3,}', text))
    if underline_count >= 10:
        signals.append(Signal("admin_form", 1.0, f"检测到{underline_count}处输入下划线", True))

    # --- 软信号 ---
    form_fields = [
        "姓名", "性别", "年龄", "出生年月", "身份证号", "联系电话",
        "电子邮箱", "通讯地址", "邮政编码", "学历", "学位", "职称", "工作单位"
    ]
    field_matches = sum(1 for f in form_fields if f in text)
    if field_matches >= 4:
        signals.append(Signal("admin_form", 2.5, f"命中{field_matches}个表单字段标签"))

    if 1 <= page_count <= 5:
        signals.append(Signal("admin_form", 1.5, "页数符合表单范围(1-5页)"))

    if re.search(r'(?:签名|签字)[：:]|(?:盖章|公章)[：:]|填表日期', text):
        signals.append(Signal("admin_form", 1.0, "检测到签名盖章或日期区域"))

    if re.search(r'表$|表格|form', filename, re.I):
        signals.append(Signal("admin_form", 0.5, "文件名含表单词"))

    # ==================== 合同（可选扩展类别） ====================
    if re.search(r'甲方[：:].*?乙方[：:]', text, re.S):
        signals.append(Signal("contract", 1.0, "检测到甲乙双方", True))

    if "本合同一式" in text or "（盖章）" in text:
        signals.append(Signal("contract", 0.8, "检测到合同套语"))

    return signals


# ==================== 裁决逻辑 ====================

def _resolve_strong_signals(signals: List[Signal]) -> Optional[Tuple[str, str]]:
    """处理强信号冲突：按优先级返回最高优先级的强信号类别"""
    strong = [s for s in signals if s.is_strong]
    if not strong:
        return None

    # 按类别分组，取每类最强信号
    best_by_cat = {}
    for s in strong:
        if s.category not in best_by_cat or s.weight > best_by_cat[s.category][0]:
            best_by_cat[s.category] = (s.weight, s.description)

    # 按 CATEGORY_PRIORITY 排序，取优先级最高的
    sorted_cats = sorted(
        best_by_cat.items(),
        key=lambda x: CATEGORY_PRIORITY.get(x[0], 99)
    )
    winner_cat, (weight, reason) = sorted_cats[0]
    return winner_cat, reason


def _calculate_soft_scores(signals: List[Signal]) -> Dict[str, float]:
    """按类别累计软信号权重"""
    scores = {"academic_paper": 0, "policy_regulation": 0, "admin_form": 0, "contract": 0}
    for s in signals:
        if not s.is_strong:
            scores[s.category] += s.weight
    return scores


def _normalize_confidence(top_score: float, second_score: float) -> float:
    """
    置信度归一化：基于绝对分数和相对差距，映射到 0.5~1.0
    """
    if top_score < 3.5:
        return 0.0

    # 基础分：分数越高，sigmoid 越接近 1
    base = 0.5 + 0.5 * (1 - math.exp(-top_score / 5))

    # 差距加成：与第二名拉得越开越确信
    gap = top_score - second_score
    bonus = 0.15 * (1 - math.exp(-gap / 3))

    return min(base + bonus, 1.0)


def _rule_based_classify(text, filename, page_count, metadata):
    """分类主逻辑"""
    # 1. 检测所有信号
    signals = _detect_signals(text, filename, page_count, metadata)

    # 2. 强信号优先裁决（一旦命中，置信度 1.0，直接返回）
    strong_result = _resolve_strong_signals(signals)
    if strong_result:
        cat, reason = strong_result
        return cat, 1.0, reason, signals

    # 3. 软信号计分
    scores = _calculate_soft_scores(signals)
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_cat, top_score = sorted_scores[0]
    second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0

    # 4. 阈值判定（必须满足：绝对分够高 + 与第二名拉开差距）
    if top_score < 3.5 or (top_score - second_score) < 2.0:
        return None, 0.0, "信号不足或类别差距过小", signals

    confidence = _normalize_confidence(top_score, second_score)

    # 生成人类可读的原因
    cat_signals = [s for s in signals if s.category == top_cat and not s.is_strong]
    reason = f"综合{len(cat_signals)}个软信号，累计得分{top_score:.1f}（领先第二名{top_score - second_score:.1f}分）"

    return top_cat, confidence, reason, signals