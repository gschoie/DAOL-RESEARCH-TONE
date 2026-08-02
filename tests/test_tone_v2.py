"""톤 v2 diff 엔진과 AI 분석 정규화의 계약 테스트 (네트워크 없음)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_report_analyzer
import build_daol_tone_v2 as v2


def make_report(rid, date, **kw):
    base = {'id': rid, 'date': date, 'month': date[:7], 'analyst': '최광식',
            'sector': '조선/기계/방산', 'company': 'HD현대중공업', 'code': '329180',
            'report_type': '기업자료', 'opinion': '', 'tp_changes': [],
            'title': '제목', 'post_url': 'https://t.me/daolresearch/1', 'pdf_url': '', 'source_url': ''}
    base.update(kw)
    return base


def make_ai(**kw):
    base = {'report_scope': '기업', 'company': 'HD현대중공업', 'code': '329180', 'opinion': '매수',
            'tp': {'direction': '상향', 'value': 500000.0, 'prior': 430000.0,
                   'reasons': ['실적추정', '멀티플'], 'evidence': '이익 추정 상향과 목표배수 조정'},
            'earnings': {'direction': '상향', 'evidence': '26년 영업이익 추정 상향'},
            'conviction': 4, 'tone_label': '긍정', 'strong_phrases': ['확실한 업사이드'],
            'hedge_phrases': [], 'negative_phrases': [],
            'investment_points': [{'label': '미 함정 MRO', 'summary': 'MRO 수주 확대'}],
            'one_line': '강한 어조 유지'}
    base.update(kw)
    return {'result': base}


class ReasonClassification(unittest.TestCase):
    def test_v1_regex_reasons_map_to_four_categories(self):
        self.assertEqual(v2.classify_regex_reasons(['어닝/실적 추정 상향']), ['실적추정'])
        self.assertEqual(v2.classify_regex_reasons(['적용 멀티플 조정']), ['멀티플'])
        self.assertEqual(v2.classify_regex_reasons(['밸류에이션 기준연도/방법 변경']), ['시점롤포워드'])
        self.assertEqual(v2.classify_regex_reasons(['본문상 명시 배경 추가 확인 필요']), ['기타'])
        self.assertEqual(v2.classify_regex_reasons([]), ['기타'])


class MergeReport(unittest.TestCase):
    def test_ai_overrides_unattributed_company(self):
        report = make_report('1', '2026-07-01', company='산업/기타', code='', report_type='산업자료')
        merged = v2.merge_report(report, make_ai(company='삼성전자', code='005930'))
        self.assertEqual(merged['company'], '삼성전자')
        self.assertEqual(merged['report_type'], '기업자료')

    def test_regex_only_report_keeps_v1_tp_change(self):
        report = make_report('2', '2026-07-02', tp_changes=[{
            'direction': '상향', 'old': 430000, 'new': 500000,
            'display': '430,000원 → 500,000원', 'reasons': ['적용 멀티플 조정'], 'evidence': 'x'}])
        merged = v2.merge_report(report, None)
        self.assertIsNone(merged['conviction'])
        self.assertEqual(merged['tp_event']['reasons'], ['멀티플'])


class PointDiffs(unittest.TestCase):
    def test_added_and_dropped_points(self):
        timeline = [
            v2.merge_report(make_report('1', '2026-06-01'),
                            make_ai(investment_points=[{'label': '후판가 하락', 'summary': 's'},
                                                       {'label': '미 함정 MRO', 'summary': 's'}])),
            v2.merge_report(make_report('2', '2026-07-01'),
                            make_ai(investment_points=[{'label': '미함정 MRO', 'summary': 's'},
                                                       {'label': 'HBM 증설', 'summary': 's'}])),
        ]
        v2.attach_point_diffs(timeline)
        self.assertEqual(timeline[0]['points_added'], [])
        # '미 함정 MRO' ↔ '미함정 MRO'는 표기 차이로 같은 포인트 취급
        self.assertEqual(timeline[1]['points_added'], ['HBM 증설'])
        self.assertEqual(timeline[1]['points_dropped'], ['후판가 하락'])


class TimelineEvents(unittest.TestCase):
    def test_tp_opinion_tone_events(self):
        timeline = [
            v2.merge_report(make_report('1', '2026-06-01'),
                            make_ai(conviction=4, opinion='매수',
                                    tp={'direction': '없음', 'value': None, 'prior': None, 'reasons': [], 'evidence': ''},
                                    earnings={'direction': '유지', 'evidence': ''})),
            v2.merge_report(make_report('2', '2026-07-01'),
                            make_ai(conviction=2, opinion='중립', tone_label='신중',
                                    tp={'direction': '하향', 'value': 400000.0, 'prior': 500000.0,
                                        'reasons': ['실적추정'], 'evidence': 'e'},
                                    earnings={'direction': '하향', 'evidence': 'e'})),
        ]
        v2.attach_point_diffs(timeline)
        events = v2.timeline_events('329180', 'HD현대중공업', timeline)
        types = {e['type'] for e in events}
        self.assertIn('TP 하향', types)
        self.assertIn('의견 변경', types)
        self.assertIn('실적추정 하향', types)
        self.assertIn('톤 급변↓', types)

    def test_equivalent_opinions_do_not_emit_change(self):
        # 비중확대(산업 표기)와 매수(종목 표기)는 같은 방향 — 의견 변경 아님
        timeline = [
            v2.merge_report(make_report('1', '2026-06-01'), make_ai(opinion='비중확대')),
            v2.merge_report(make_report('2', '2026-07-01'), make_ai(opinion='매수')),
        ]
        v2.attach_point_diffs(timeline)
        events = v2.timeline_events('k', 'n', timeline)
        self.assertNotIn('의견 변경', {e['type'] for e in events})

    def test_hold_tp_display_is_single_value(self):
        merged = v2.merge_report(make_report('3', '2026-07-02'), make_ai(
            tp={'direction': '유지', 'value': 6000.0, 'prior': 6000.0, 'reasons': [], 'evidence': ''}))
        self.assertEqual(merged['tp_event']['display'], '6,000원')

    def test_no_events_without_change(self):
        timeline = [v2.merge_report(make_report('1', '2026-06-01'), None)]
        v2.attach_point_diffs(timeline)
        self.assertEqual(v2.timeline_events('k', 'n', timeline), [])


class EstCompare(unittest.TestCase):
    CONS = {'quarter': {'label': '2Q26', 'op': 5000.0, 'eps': 1400.0},
            'annual': {'label': '2026E', 'op': 22000.0, 'eps': 6300.0}}

    def test_matching_quarter_produces_diff(self):
        est = {'quarter_label': '2Q26', 'quarter_op': 5500.0, 'quarter_eps': None,
               'year_op': 23000.0, 'year_eps': 6300.0}
        c = v2.est_compare(est, self.CONS)
        self.assertEqual(c['quarter']['op']['diff_pct'], 10.0)
        self.assertEqual(c['annual']['eps']['diff_pct'], 0.0)

    def test_quarter_label_mismatch_skips_quarter(self):
        est = {'quarter_label': '3Q26', 'quarter_op': 5500.0, 'quarter_eps': None,
               'year_op': None, 'year_eps': None}
        c = v2.est_compare(est, self.CONS)
        self.assertNotIn('quarter', c or {})

    def test_billion_won_scale_autocorrect(self):
        # 232.4(십억원 미환산) vs 컨센 2,706억 → 2,324억으로 보정되어 -14.1%
        est = {'quarter_label': '2Q26', 'quarter_op': 232.4, 'quarter_eps': None,
               'year_op': None, 'year_eps': None}
        cons = {'quarter': {'label': '2Q26', 'op': 2706.0, 'eps': None}, 'annual': None}
        c = v2.est_compare(est, cons)
        self.assertEqual(c['quarter']['op']['our'], 2324.0)
        self.assertAlmostEqual(c['quarter']['op']['diff_pct'], -14.1)

    def test_none_when_no_data(self):
        self.assertIsNone(v2.est_compare(None, self.CONS))
        self.assertIsNone(v2.est_compare({'quarter_label': '', 'quarter_op': None, 'quarter_eps': None,
                                          'year_op': None, 'year_eps': None}, self.CONS))


class ForwardReturns(unittest.TestCase):
    CACHE = {'005930': {'closes': [['2026-07-01', 100.0], ['2026-07-02', 102.0], ['2026-07-03', 104.0],
                                   ['2026-07-06', 106.0], ['2026-07-07', 108.0], ['2026-07-08', 110.0]]}}

    def test_five_day_return_from_event_date(self):
        fwd = v2.make_return_fn(self.CACHE)
        self.assertEqual(fwd('005930', '2026-07-01', 5), 10.0)

    def test_weekend_event_uses_next_trading_day(self):
        fwd = v2.make_return_fn(self.CACHE)
        # 7/4(토) 이벤트 → 기준일 7/6 종가 106
        self.assertEqual(fwd('005930', '2026-07-04', 2), round((110/106-1)*100, 1))

    def test_insufficient_future_returns_none(self):
        fwd = v2.make_return_fn(self.CACHE)
        self.assertIsNone(fwd('005930', '2026-07-08', 5))
        self.assertIsNone(fwd('999999', '2026-07-01', 5))


class NormalizeResult(unittest.TestCase):
    def test_out_of_range_values_are_clamped(self):
        raw = make_ai()['result']
        raw.update({'conviction': 9, 'tone_label': '이상한 라벨', 'code': 'abc',
                    'tp': {'direction': '엉뚱', 'value': 'x', 'prior': None, 'reasons': ['멀티플', '없는분류'],
                           'evidence': 'e'}})
        out = ai_report_analyzer.normalize_result(raw)
        self.assertEqual(out['conviction'], 5)
        self.assertEqual(out['tone_label'], '중립·건조')
        self.assertEqual(out['code'], '')
        self.assertEqual(out['tp']['direction'], '없음')
        self.assertEqual(out['tp']['reasons'], ['멀티플'])


if __name__ == '__main__':
    unittest.main()
