"""조회 로직 단위 테스트 — 네트워크·API 키 없이 실행된다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402

TONE = {
    "companies": {
        "003230": {
            "name": "삼양식품", "code": "003230", "sector": "음식료",
            "timeline": [
                {"id": "100", "date": "2026-07-01", "analyst": "김유정", "sector": "음식료",
                 "company": "삼양식품", "title": "[삼양식품(003230)/BUY] 수출 서프라이즈",
                 "opinion": "BUY", "one_line": "수출 호조로 컨센 상회 전망.",
                 "points": ["수출 성장"], "source_url": "https://buly.kr/AAA"},
                {"id": "101", "date": "2026-08-01", "analyst": "김유정", "sector": "음식료",
                 "company": "삼양식품", "title": "[삼양식품(003230)/BUY] TP 상향",
                 "opinion": "BUY", "one_line": "실적추정 상향을 반영해 TP 상향.",
                 "points": ["수출 성장", "증설 효과"], "source_url": "https://buly.kr/BBB"},
            ],
            "street": {"broker_count": 5},
        },
        "IND:조선": {
            "name": "조선",
            "timeline": [
                {"id": "200", "date": "2026-08-05", "analyst": "최광식", "sector": "조선",
                 "title": "[조선] 위클리", "one_line": "신조선가 강세 지속.",
                 "source_url": "https://buly.kr/CCC"},
            ],
        },
    },
    "sectors": [
        {"sector": "조선", "analyst": "최광식", "recent_tone": "긍정", "baseline": "중립"},
    ],
}

KED = [
    {"company": "에코프로", "broker": "AA증권", "title": "양극재 반등"},
    {"company": "삼성전자", "broker": "BB증권", "title": "HBM 전망"},
]


class CompanyViewTest(unittest.TestCase):
    def test_resolve_by_name(self):
        result = tools.company_view("삼양식품", TONE)
        self.assertEqual(result["code"], "003230")
        self.assertEqual(len(result["timeline_recent"]), 2)
        self.assertEqual(result["street_position"], {"broker_count": 5})

    def test_resolve_by_code(self):
        self.assertEqual(tools.company_view("003230", TONE)["name"], "삼양식품")

    def test_miss_guides_to_street(self):
        result = tools.company_view("없는종목", TONE)
        self.assertIn("search_street", result["error"])


class SectorToneTest(unittest.TestCase):
    def test_sector_rows_and_industry_timeline(self):
        result = tools.sector_tone("조선", TONE)
        self.assertEqual(len(result["sector_rows"]), 1)
        self.assertEqual(result["industry_key"], "IND:조선")
        self.assertEqual(len(result["industry_timeline_recent"]), 1)

    def test_miss_lists_available(self):
        result = tools.sector_tone("바이오", TONE)
        self.assertIn("error", result)
        self.assertIn("IND:조선", result["available_industries"])


class StreetSearchTest(unittest.TestCase):
    def test_match(self):
        result = tools.street_search("에코프로", KED)
        self.assertEqual(result["total_matches"], 1)

    def test_miss(self):
        self.assertIn("error", tools.street_search("없는종목", KED))


class SearchReportsTest(unittest.TestCase):
    def test_keyword_and_matching(self):
        result = tools.search_reports("TP 상향", TONE)
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["results"][0]["id"], "101")

    def test_analyst_filter_newest_first(self):
        result = tools.search_reports("", TONE, analyst="김유정")
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(result["results"][0]["id"], "101")

    def test_date_range(self):
        result = tools.search_reports("", TONE, date_from="2026-07-15", date_to="2026-08-03")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["results"][0]["id"], "101")

    def test_card_drops_heavy_fields(self):
        tone = {"companies": {"X": {"timeline": [
            {"id": "1", "date": "2026-01-01", "title": "t", "points_detail": [{"big": "x"}]}]}}}
        result = tools.search_reports("", tone)
        self.assertNotIn("points_detail", result["results"][0])

    def test_miss(self):
        self.assertIn("error", tools.search_reports("존재하지않는키워드", TONE))


class SerializeTest(unittest.TestCase):
    def test_truncates(self):
        text = tools.serialize({"x": "가" * 20000})
        self.assertLessEqual(len(text), tools.MAX_RESULT_CHARS + 60)
        self.assertIn("생략", text)


if __name__ == "__main__":
    unittest.main()
