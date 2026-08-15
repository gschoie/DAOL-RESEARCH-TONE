"""PDF 원문 검색 로직 단위 테스트 — 네트워크·API 키 없이 실행된다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fulltext  # noqa: E402

PDF_CACHE = {
    "https://buly.kr/AAA": {"status": "pdf", "text": "삼양식품 수출 호조. " + "본문 " * 50 + "미국 채널 확장이 핵심."},
    "https://buly.kr/BBB": {"status": "pdf", "text": "조선 신조선가 강세. 수주잔고 슬롯은 2029년까지 찼다. 슬롯 부족."},
    "https://buly.kr/ERR": {"status": "error", "error": "download failed"},
}

TONE = {
    "companies": {
        "003230": {"timeline": [
            {"id": "100", "date": "2026-07-01", "analyst": "김유정", "sector": "음식료",
             "company": "삼양식품", "title": "[삼양식품] 수출 서프라이즈",
             "source_url": "https://buly.kr/AAA", "post_url": "https://t.me/daolresearch/100"},
        ]},
    },
}

MESSAGES = [
    {"id": 200, "date": "2026-08-05T00:00:00+00:00", "text": "[다올 조선 위클리] 신조선가",
     "links": ["https://buly.kr/BBB"], "post_url": "https://t.me/daolresearch/200"},
]


class MetaIndexTest(unittest.TestCase):
    def test_timeline_first_message_fallback(self):
        index = fulltext.build_meta_index(TONE, MESSAGES)
        self.assertEqual(index["https://buly.kr/AAA"]["analyst"], "김유정")
        self.assertEqual(index["https://buly.kr/BBB"]["date"], "2026-08-05")


class SearchFulltextTest(unittest.TestCase):
    def setUp(self):
        self.index = fulltext.build_meta_index(TONE, MESSAGES)

    def test_and_match_with_snippets(self):
        result = fulltext.search_fulltext("수주잔고 슬롯", PDF_CACHE, self.index)
        self.assertEqual(result["total_matches"], 1)
        hit = result["results"][0]
        self.assertEqual(hit["source_url"], "https://buly.kr/BBB")
        self.assertTrue(any("수주잔고" in s for s in hit["snippets"]))

    def test_analyst_filter(self):
        result = fulltext.search_fulltext("수출", PDF_CACHE, self.index, analyst="최광식")
        self.assertIn("error", result)
        result = fulltext.search_fulltext("수출", PDF_CACHE, self.index, analyst="김유정")
        self.assertEqual(result["total_matches"], 1)

    def test_error_entry_skipped(self):
        result = fulltext.search_fulltext("download", PDF_CACHE, self.index)
        self.assertIn("error", result)

    def test_empty_query(self):
        self.assertIn("error", fulltext.search_fulltext("   ", PDF_CACHE, self.index))


class GetFulltextTest(unittest.TestCase):
    def setUp(self):
        self.index = fulltext.build_meta_index(TONE, MESSAGES)

    def test_by_source_url(self):
        result = fulltext.get_fulltext("https://buly.kr/BBB", PDF_CACHE, self.index)
        self.assertIn("신조선가", result["text"])

    def test_by_report_id(self):
        result = fulltext.get_fulltext("100", PDF_CACHE, self.index)
        self.assertEqual(result["source_url"], "https://buly.kr/AAA")
        self.assertEqual(result["analyst"], "김유정")

    def test_around_keyword(self):
        result = fulltext.get_fulltext("https://buly.kr/AAA", PDF_CACHE, self.index, around="미국 채널")
        self.assertIn("미국 채널 확장", result["text"])

    def test_miss(self):
        self.assertIn("error", fulltext.get_fulltext("99999", PDF_CACHE, self.index))


if __name__ == "__main__":
    unittest.main()
