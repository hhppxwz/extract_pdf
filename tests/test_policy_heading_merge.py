import unittest

from models import BlockType, ContentBlock
from text_pipeline import _merge_by_article, _merge_by_policy_heading


def block(text):
    return ContentBlock(type=BlockType.TEXT, content=text)


class PolicyHeadingMergeTests(unittest.TestCase):
    def test_numbered_headings_group_following_paragraphs(self):
        blocks = [block(text) for text in [
            "武汉大学全员聘用制实施意见", "引言", "一、指导思想", "指导思想正文。",
            "二、基本原则", "（一）按需设岗", "原则说明。", "（二）公开招聘", "招聘说明。",
            "三、适用范围", "适用范围正文。",
        ]]
        units = _merge_by_policy_heading(blocks)
        self.assertEqual(len(units), 6)
        self.assertTrue(units[2]["text"].startswith("二、基本原则"))
        self.assertIn("原则说明。", units[3]["text"])
        self.assertNotIn("招聘说明。", units[3]["text"])

    def test_article_documents_keep_article_grouping(self):
        units = _merge_by_article([block("第一条 总则"), block("内容"), block("第二条 范围")])
        self.assertEqual(len(units), 2)

    def test_numbered_full_sentences_remain_separate_units(self):
        first = "一、本通告所指航空器主要包括轻型飞机、无人机、航空模型等多种类型，应遵守校园安全管理规定。"
        blocks = [block(text) for text in [
            "航空器管理通告", "现将有关事项通告如下：", first,
            "二、放飞前须报学校审批。", "五、禁止下列行为：",
            "（一）未经许可拍摄保密设施；", "（二）扰乱教学、科研秩序。",
            "六、违规行为按规定处理。",
        ]]
        units = _merge_by_policy_heading(blocks)
        self.assertEqual([unit["text"] for unit in units], [
            "航空器管理通告\n现将有关事项通告如下：", first,
            "二、放飞前须报学校审批。", "五、禁止下列行为：",
            "（一）未经许可拍摄保密设施；", "（二）扰乱教学、科研秩序。",
            "六、违规行为按规定处理。",
        ])

    def test_arabic_numbering_and_multiline_blocks_keep_boundaries(self):
        blocks = [
            ContentBlock(type=BlockType.TEXT, page_num=1,
                         content="管理说明\n1. 审核材料。\n补充说明\n（1）提交申请；\n2、办理审批。"),
            ContentBlock(type=BlockType.TEXT, page_num=2, content="1.1 适用范围\n范围正文。"),
        ]
        units = _merge_by_policy_heading(blocks)
        self.assertEqual([unit["text"] for unit in units], [
            "管理说明", "1. 审核材料。\n补充说明", "（1）提交申请；", "2、办理审批。", "1.1 适用范围\n范围正文。",
        ])
        self.assertEqual([unit["page_num"] for unit in units], [1, 1, 1, 1, 2])

    def test_unnumbered_text_and_decimal_values_stay_together(self):
        units = _merge_by_policy_heading([block("管理说明"), block("3.14米为示例数值。"), block("普通正文。")])
        self.assertEqual([unit["text"] for unit in units], ["管理说明\n3.14米为示例数值。\n普通正文。"])


if __name__ == "__main__":
    unittest.main()
