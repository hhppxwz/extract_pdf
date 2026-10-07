"""验证无章条编号层级，并保护既有正式条款父子关系。"""
import unittest

from models import BlockType, ContentBlock
from policy.pipeline import build_policy_clauses


def clauses_from(*lines):
    return build_policy_clauses("policy_test", [
        ContentBlock(type=BlockType.TEXT, content=line, page_num=1) for line in lines
    ])


class PolicyClauseHierarchyTests(unittest.TestCase):
    def test_formal_article_nested_numbers_do_not_require_colon(self):
        clauses = clauses_from('第一章 条件', '第七条 申报资格', '一、学历学位条件',
            '（一）教授要求。', '（二）副教授要求。', '二、任职年限',
            '（一）教授年限。', '1. 具体要求。', '第八条 业绩', '（一）论文要求。')
        self.assert_parents(clauses, [None, 0, 1, 2, 2, 1, 5, 6, 0, 8])

    def test_formal_headings_without_spaces_keep_hierarchy(self):
        clauses = clauses_from('第一编管理', '第一章总 则', '第一节适用范围',
                              '第一条适用对象。', '第二章岗位设置', '第二条岗位要求。')
        self.assert_parents(clauses, [None, 0, 1, 2, 0, 4])
        self.assertEqual(clauses[3].chapter_path, ['第一编', '第一章', '第一节'])

    def test_formal_article_numbered_duties_keep_parent_and_siblings(self):
        clauses = clauses_from('第一章 职责', '第一条 委员分工',
            '（一）组织委员职责是：', '1. 组织工作。', '2. 管理工作。',
            '（二）宣传委员职责是：', '1. 宣传工作。', '2. 教育工作。',
            '第二条 其他事项', '1. 第一项。')
        self.assert_parents(clauses, [None, 0, 1, 2, 2, 1, 5, 5, 0, 8])

    def assert_parents(self, clauses, indexes):
        expected = [clauses[index].clause_id if index is not None else None for index in indexes]
        self.assertEqual([clause.parent_clause_id for clause in clauses], expected)

    def test_chinese_headings_parent_arabic_items_and_reset_between_sections(self):
        clauses = clauses_from(
            "制定说明", "一、总体要求", "1. 坚持质量导向。", "2. 坚持创新引领。",
            "二、具体措施", "1. 明确学制与学习年限。", "2. 优化培养过程。",
            "三、实施保障", "1. 深入研究。",
        )
        self.assert_parents(clauses, [None, None, 1, 1, None, 4, 4, None, 7])
        self.assertEqual(clauses[5].item_no, "1.")
        self.assertEqual(clauses[5].raw_text, "1. 明确学制与学习年限。")

    def test_four_numbering_styles_form_nested_levels_without_chaining_siblings(self):
        clauses = clauses_from(
            "一、工作要求", "（一）申请要求", "1. 提交材料", "（1）身份证明", "（2）申请表",
            "2. 审核材料", "（二）审批要求", "1. 主管部门审批", "二、附则", "（一）解释部门",
        )
        self.assert_parents(clauses, [None, 0, 1, 2, 2, 1, 0, 6, None, 8])

    def test_numbered_items_without_higher_heading_remain_siblings(self):
        for lines in [("1. 第一项。", "2. 第二项。"), ("（一）第一项。", "（二）第二项。")]:
            with self.subTest(lines=lines):
                self.assert_parents(clauses_from(*lines), [None, None])

    def test_formal_book_chapter_section_article_paragraph_item_hierarchy_is_unchanged(self):
        clauses = clauses_from(
            "第一编 管理", "第一章 总则", "第一节 范围", "第一条 申请要求", "第一款 材料要求",
            "一、身份证明", "1. 材料复印件。", "（一）申请表。", "（二）证明材料。",
            "第二款 审核要求", "（一）审核身份。", "第二条 办理要求", "（一）办理登记。",
            "第二章 附则", "第三条 生效规定。",
        )
        self.assert_parents(clauses, [None, 0, 1, 2, 3, 4, 5, 5, 5, 3, 9, 2, 11, 0, 13])
        self.assertEqual(clauses[8].chapter_path, ["第一编", "第一章", "第一节"])
        self.assertEqual(clauses[8].article_no, "一")
        self.assertEqual(clauses[12].article_no, "二")

    def test_multiline_blocks_use_same_numbered_hierarchy(self):
        clauses = clauses_from("一、总体要求\n1. 质量导向。\n说明正文。\n2. 创新引领。\n二、具体措施\n1. 明确学制。")
        self.assert_parents(clauses, [None, 0, 0, None, 3])
        self.assertIn("说明正文。", clauses[1].raw_text)

    def test_chapter_only_document_keeps_existing_rules(self):
        clauses = clauses_from("第一章 总则", "一、管理要求", "1. 办理申请。")
        self.assert_parents(clauses, [None, None, None])

    def test_same_numbering_style_never_becomes_a_child_of_previous_item(self):
        clauses = clauses_from("一、要求", "二、措施", "三、保障")
        self.assert_parents(clauses, [None, None, None])
