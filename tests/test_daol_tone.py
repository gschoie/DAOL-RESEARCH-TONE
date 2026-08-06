"""DAOL 리서치 톤 수집 필터(is_report) 계약 테스트.

build_daol_tone_dashboard 는 requests/bs4/pypdf 를 함수 안에서 지연 임포트하므로
is_report 는 표준 라이브러리만으로 임포트되어 여기서 바로 검증할 수 있다.
"""
import unittest

from build_daol_tone_dashboard import is_report, kst_dt


def msg(text, links=None):
    return {"id": 1, "date": "2026-06-28T00:00:00+00:00", "text": text,
            "links": links or [], "post_url": "https://t.me/daolresearch/1"}


class IsReportTest(unittest.TestCase):
    def test_weekly_with_report_link_is_captured(self):
        # 최광식 조선 Weekly: 컴플라이언스 문구 없이 < ☞ bit.ly/DOS > 링크만 붙는 형식.
        text = ("[조선 Weekly/다올투자증권 조선/기계/방산 최광식 ☎️ 2184-2308] "
                "★ 다올 선박: 탱커 수주 ⚓ 주시 뉴스 ▶ 선가 ... < ☞ https://bit.ly/DOS828 >")
        self.assertTrue(is_report(msg(text, ["https://bit.ly/DOS828", "https://t.me/HI_GS"])))

    def test_standard_compliance_report_is_captured(self):
        text = ("[Fixed Income / 다올투자증권 채권분석 허정인] ★ 5월 금통위 Review ... "
                "♣️ 보고서 원문 및 Compliance Notice: https://buly.kr/x")
        self.assertTrue(is_report(msg(text, ["https://buly.kr/x"])))

    def test_daily_news_is_excluded(self):
        text = ("[다올투자증권 조선/기계/방산 최광식] 7/2(목) 데일리뉴스 🛳 조선 "
                "... 보고서 원문 Compliance Notice")
        self.assertFalse(is_report(msg(text, ["https://buly.kr/x"])))

    def test_daily_morning_brief_is_excluded(self):
        text = "★ DAOL Daily Morning Brief (7/2) ▶️ 미국증시 ... 보고서 원문"
        self.assertFalse(is_report(msg(text, ["https://buly.kr/x"])))

    def test_data_snippet_without_link_is_not_report(self):
        # 링크 없는 데이터 단신(수출데이터/의료관광 등)은 리포트가 아니다.
        text = "[다올 의료기기 박종현] - 2026.05월 의료관광 소비 금액 현황 전체 2,512억원"
        self.assertFalse(is_report(msg(text, [])))

    def test_plain_news_roundup_is_excluded(self):
        text = "[다올 인터넷·게임·레저 김혜영] 7/2(목) 뉴스 🟡 인터넷 [네이버] AI 에이전트 출시"
        self.assertFalse(is_report(msg(text, ["https://tinyurl.com/x"])))


class KstDateTest(unittest.TestCase):
    """발간일은 UTC가 아니라 KST 기준이어야 한다(아침 발간이 전날로 밀리는 문제)."""

    def test_morning_publication_keeps_same_kst_day(self):
        # KST 8/6 07:30 발간 = UTC 8/5 22:30 → 날짜는 8/6이어야 한다.
        dt = kst_dt("2026-08-05T22:30:00+00:00")
        self.assertEqual(dt.date().isoformat(), "2026-08-06")
        self.assertEqual(dt.strftime("%Y-%m"), "2026-08")

    def test_month_rolls_over_with_kst(self):
        # UTC 5/31 23:00 = KST 6/1 08:00 → 월 버킷도 6월이어야 한다.
        self.assertEqual(kst_dt("2025-05-31T23:00:00+00:00").strftime("%Y-%m"), "2025-06")

    def test_naive_timestamp_treated_as_utc(self):
        self.assertEqual(kst_dt("2026-08-05T22:30:00").date().isoformat(), "2026-08-06")

    def test_afternoon_publication_unchanged(self):
        # KST 8/6 16:00 = UTC 8/6 07:00 → 그대로 8/6.
        self.assertEqual(kst_dt("2026-08-06T07:00:00+00:00").date().isoformat(), "2026-08-06")


if __name__ == "__main__":
    unittest.main()
