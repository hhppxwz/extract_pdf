import unittest

from metadata.policy import extract_policy_metadata
from models import BlockType, ContentBlock


class PolicyMetadataDocumentNumberTests(unittest.TestCase):
    def blocks(self, *texts):
        return [ContentBlock(type=BlockType.TEXT, page_num=0, content=text) for text in texts]

    def test_notice_with_attached_policy_uses_body_title(self):
        blocks = self.blocks("关于修订印发", "《武汉大学学术委员会章程》的通知", "全校各单位：",
                             "现将修订后的章程印发，请遵照执行。", "武 汉 大 学", "2026年8月1日",
                             "武汉大学学术委员会章程", "第一章 总则", "第一条 正文",
                             "第三十条 原《旧章程》（武大发字〔2020〕1号）同时废止。")
        meta = extract_policy_metadata(blocks)
        self.assertEqual(meta['title'], '武汉大学学术委员会章程')
        self.assertEqual(meta['notice_title'], '关于修订印发《武汉大学学术委员会章程》的通知')
        self.assertEqual(meta['issue_date'], '2026-08-01')
        self.assertNotIn('doc_number', meta)

    def test_quoted_reference_without_body_heading_is_not_source_title(self):
        meta = extract_policy_metadata(self.blocks("关于执行《旧办法》的通知", "按照《旧办法》办理。"))
        self.assertEqual(meta.get('title'), '关于执行《旧办法》的通知')

    def test_split_body_title_is_confirmed_by_notice(self):
        for parts in [("武汉大学中层领导班子和领导干部", "考核工作办法"),
                      ("武汉大学中层领导班子", "和领导干部", "考核工作办法")]:
            with self.subTest(parts=parts):
                meta = extract_policy_metadata(self.blocks(
                    "关于印发《武汉大学中层领导班子和领导干部考核工作办法》的通知",
                    "全校各单位：", "现印发给你们，请认真执行。", "2026年8月3日",
                    *parts, "第一章 总则", "第一条 正文"))
                self.assertEqual(meta['title'], '武汉大学中层领导班子和领导干部考核工作办法')

    def test_notice_title_does_not_replace_different_body_title(self):
        meta = extract_policy_metadata(self.blocks("关于印发《武汉大学旧管理办法》的通知",
            "武汉大学新管理办法", "第一章 总则", "第一条 正文"))
        self.assertEqual(meta['title'], '武汉大学新管理办法')

    def test_mhtml_notice_with_preamble_and_numbered_policy(self):
        title = '武汉大学关于促进毕业生高质量充分就业的实施意见'
        notice = f'关于印发《{title}》的通知'
        blocks = self.blocks('全校各单位：', f'学校制定了《{title}》，现印发给你们。',
            '武汉大学', '2026年6月24日', title, '就业是民生之本。现提出如下意见。',
            '一、总体要求和目标', '正文', '二、优化培养供给')
        blocks[0].raw = {'source_format': 'mhtml', 'source_metadata': {
            'title': notice, 'issuer': '学生就业指导与服务中心', 'doc_number': '武大毕字〔2026〕4号'}}
        meta = extract_policy_metadata(blocks)
        self.assertEqual(meta['title'], title)
        self.assertEqual(meta['notice_title'], notice)
        self.assertEqual(meta['doc_number'], '武大毕字〔2026〕4号')
        self.assertEqual(meta['issuer'], '学生就业指导与服务中心')

    def test_mhtml_notice_with_only_quoted_policy_keeps_notice_title(self):
        notice = '关于印发《武汉大学管理办法》的通知'
        blocks = self.blocks('现将《武汉大学管理办法》印发给你们，请认真执行。')
        blocks[0].raw = {'source_format': 'mhtml', 'source_metadata': {'title': notice}}
        self.assertEqual(extract_policy_metadata(blocks)['title'], notice)

    def test_mhtml_source_title_removes_all_whitespace(self):
        blocks = self.blocks('第一条 正文')
        blocks[0].raw = {'source_format': 'mhtml', 'source_metadata': {
            'title': '武汉大学教职工代表大会提案工作规则 \t（修订）'}}
        self.assertEqual(extract_policy_metadata(blocks)['title'],
                         '武汉大学教职工代表大会提案工作规则（修订）')

    def test_notice_with_multiple_policies_does_not_pick_one(self):
        meta = extract_policy_metadata(self.blocks("关于印发两项办法的通知", "武汉大学甲办法",
                                                  "第一条 甲正文", "武汉大学乙办法", "第一条 乙正文"))
        self.assertEqual(meta.get('title'), '关于印发两项办法的通知')

    def test_confirmed_multiple_policies_have_individual_metadata(self):
        notice = '关于印发《武汉大学甲办法》和《武汉大学乙办法》的通知'
        blocks = self.blocks(notice, '武大字〔2026〕1号', '武汉大学甲办法',
                             '第一条 甲正文', '武汉大学乙办法', '第一条 乙正文')
        meta = extract_policy_metadata(blocks)
        self.assertNotIn('title', meta)
        self.assertEqual(meta['notice_title'], notice)
        self.assertEqual([p['title'] for p in meta['policies']], ['武汉大学甲办法', '武汉大学乙办法'])
        self.assertTrue(all(p['doc_number'] == '武大字〔2026〕1号' for p in meta['policies']))

    def test_five_policy_notice_can_span_more_than_three_lines(self):
        titles = ['武汉大学'+name+'管理办法' for name in ['甲','乙','丙','丁','戊']]
        header = ['关于印发《'+titles[0]+'》'] + ['《'+t+'》' for t in titles[1:]] + ['的通知']
        body = [part for title in titles for part in [title,'一、管理职责']]
        meta = extract_policy_metadata(self.blocks(*(header+body)))
        self.assertEqual([p['title'] for p in meta['policies']],titles)

    def test_standalone_policy_title_and_header_date_only(self):
        meta = extract_policy_metadata(self.blocks("武汉大学管理办法", "第一条 依据2020年1月1日发布的规定制定。"))
        self.assertEqual(meta.get('title'), '武汉大学管理办法')
        self.assertNotIn('issue_date', meta)

    def test_chapter_names_are_not_additional_policy_titles(self):
        meta = extract_policy_metadata(self.blocks("关于印发章程的通知", "武汉大学学术委员会章程",
            "第一章 总则", "第一条 正文", "第二章 组成规则", "第二条 正文",
            "第四章 运行制度", "第三条 正文"))
        self.assertEqual(meta['title'], '武汉大学学术委员会章程')

    def test_mhtml_source_number_accepts_black_lenticular_brackets(self) -> None:
        blocks = [ContentBlock(type=BlockType.TEXT, content="正文", raw={
            "source_format": "mhtml", "source_metadata": {"doc_number": "武大字【 2006 】 17号"},
        })]
        self.assertEqual(extract_policy_metadata(blocks)["doc_number"], "武大字〔2006〕17号")

    def test_mhtml_source_number_uses_standard_brackets_and_no_spaces(self) -> None:
        blocks = [ContentBlock(
            type=BlockType.TEXT, content="正文",
            raw={"source_format": "mhtml", "source_metadata": {
                "title": "本科人才培养方案", "doc_number": "武大本字[ 2024 ] 41 号",
            }},
        )]
        metadata = extract_policy_metadata(blocks)
        self.assertEqual(metadata["doc_number"], "武大本字〔2024〕41号")
        self.assertEqual(metadata["title"], "本科人才培养方案")

    def test_header_number_accepts_fullwidth_square_brackets(self) -> None:
        blocks = [ContentBlock(type=BlockType.TEXT, page_num=0,
                               content="武大本字［ 2024 ］ 41 号\n关于培养质量的通知")]
        self.assertEqual(extract_policy_metadata(blocks)["doc_number"], "武大本字〔2024〕41号")

    def test_keeps_complete_five_character_issuer_prefix(self) -> None:
        blocks = [ContentBlock(
            type=BlockType.TEXT, page_num=0,
            content="武大学工字〔2026〕11 号\n关于修订印发办法的通知\n依据财教〔2024〕188号文件制定。",
        )]

        metadata = extract_policy_metadata(blocks)

        self.assertEqual(metadata["doc_number"], "武大学工字〔2026〕11号")

    def test_accepts_bracketed_document_number_without_word_zi(self) -> None:
        blocks = [ContentBlock(type=BlockType.TEXT, page_num=0, content="财教〔2024〕188号")]

        self.assertEqual(extract_policy_metadata(blocks)["doc_number"], "财教〔2024〕188号")


if __name__ == "__main__":
    unittest.main()
