import unittest
from unittest.mock import patch
from models import BlockType, ContentBlock
from policy.document_parts import split_policy_document


class DocumentPartTests(unittest.TestCase):
    def test_formal_and_numbered_policy_with_preamble_can_split(self):
        titles = ['武汉大学公文处理办法', '武汉大学公文版头及发文字号实施办法']
        notice = '关于修订印发' + ''.join('《'+t+'》' for t in titles) + '的通知'
        blocks = [ContentBlock(type=BlockType.TEXT, page_num=p, content=t) for p,t in [
            (0,notice), (1,titles[0]), (1,'第一章 总则'), (1,'第一条 正文'),
            (17,titles[1]), (17,'为加强公文规范化管理，现作如下规定。'),
            (17,'一、公文版头及适用范围'), (17,'（一）适用范围。')]]
        parts = split_policy_document(blocks, {'notice_title':notice}, 'notice.pdf')
        self.assertEqual([p['metadata']['title'] for p in parts], titles)
        self.assertEqual(parts[1]['metadata']['source_page_start'],17)

    def test_five_policy_attachments_have_unique_stable_keys(self):
        titles = ['武汉大学'+t+'管理办法' for t in ['甲','乙','丙','丁','戊']]
        notice = '关于印发' + '和'.join('《'+t+'》' for t in titles) + '的通知'
        blocks = []
        for page,title in enumerate(titles,1):
            blocks.extend([ContentBlock(type=BlockType.TEXT,page_num=page,content=title),
                           ContentBlock(type=BlockType.TEXT,page_num=page,content='一、管理职责')])
        parts = split_policy_document(blocks, {'notice_title':notice}, 'notice.pdf')
        self.assertEqual([p['metadata']['title'] for p in parts],titles)
        self.assertEqual(len({p['metadata']['document_key'] for p in parts}),5)
        self.assertEqual([p['metadata']['document_key'] for p in parts],
                         [p['metadata']['document_key'] for p in split_policy_document(blocks,{'notice_title':notice},'notice.pdf')])
    def test_policy_ids_are_stable_and_different_for_one_source(self):
        from policy.storage import upsert_policy_document
        with patch('policy.storage.ensure_policy_tables'), patch('policy.storage.storage.relational.execute'):
            first = upsert_policy_document('pdf_same', 'source.pdf', {}, 1, 'v1')
            second = upsert_policy_document('pdf_same', 'source.pdf', {'document_key': 'attachment_b'}, 1, 'v1')
            repeated = upsert_policy_document('pdf_same', 'source.pdf', {'document_key': 'attachment_b'}, 1, 'v1')
        self.assertNotEqual(first, second)
        self.assertEqual(second, repeated)

    def test_two_titles_split_clauses_and_keep_shared_notice_metadata(self):
        meta = {'title': '关于印发《武汉大学甲管理办法》和《武汉大学乙管理办法》的通知',
                'doc_number': '武大字〔2026〕2号'}
        blocks = [ContentBlock(type=BlockType.TEXT, page_num=page, content=text) for page,text in [
            (0,'通知正文'), (1,'武汉大学甲\n管理办法'), (1,'第一章 总则'),
            (1,'第一条 甲正文。'), (7,'武汉大学乙管理办法'), (7,'第一章 总则'), (7,'第一条 乙正文。')]]
        parts = split_policy_document(blocks, meta, 'notice.pdf')
        self.assertEqual([p['metadata']['title'] for p in parts], ['武汉大学甲管理办法','武汉大学乙管理办法'])
        self.assertEqual(parts[0]['metadata']['document_key'], '')
        self.assertNotEqual(parts[1]['metadata']['document_key'], '')
        self.assertTrue(all(p['metadata']['doc_number']==meta['doc_number'] for p in parts))
        self.assertEqual(parts[0]['metadata']['notice_title'], meta['title'])
        self.assertNotIn('乙正文', '\n'.join(b.content for b in parts[0]['blocks']))
        self.assertEqual(parts[1]['metadata']['source_page_start'],7)

    def test_quoted_references_do_not_split_document(self):
        meta={'title':'关于执行《武汉大学甲管理办法》和《武汉大学乙管理办法》的通知'}
        blocks=[ContentBlock(type=BlockType.TEXT,content='依据《武汉大学甲管理办法》和《武汉大学乙管理办法》办理。')]
        self.assertEqual(len(split_policy_document(blocks,meta,'notice.pdf')),1)
