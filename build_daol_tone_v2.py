"""다올 리서치 톤 v2 — 기업/산업별 타임라인과 변화 이벤트를 만든다.

입력: daol_tone_history.json(정규식 파이프라인 산출물) + daol_ai_analysis.json(LLM 분석 캐시).
AI 캐시가 없어도 정규식 필드만으로 동작한다(톤 점수 등 AI 전용 필드는 비움).

산출: data/daol_tone_v2.json
  - sectors: 섹터→애널리스트 표 데이터(최근 의견·톤·TP 이벤트·커버 기업)
  - companies: 기업(또는 산업) 키→시간순 타임라인(TP·의견·톤·투자포인트 변화)
  - events: 최근 변화 이벤트 피드(TP·의견·톤·실적·포인트)

변화 감지는 전부 이 파일의 순수 파이썬 — LLM은 리포트 1건 읽기에만 쓴다(ai_report_analyzer).
"""
import json, re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
HISTORY = DATA_DIR / 'daol_tone_history.json'
AI_CACHE = DATA_DIR / 'daol_ai_analysis.json'
OUT = DATA_DIR / 'daol_tone_v2.json'

# v1 정규식 배경 문구 → v2 4분류(실적추정/멀티플/방법론/시점롤포워드/기타)
REASON_MAP = {'어닝/실적 추정 상향': '실적추정', '적용 멀티플 조정': '멀티플',
              '밸류에이션 기준연도/방법 변경': '시점롤포워드', '본문상 명시 배경 추가 확인 필요': '기타'}


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def flat_reports(history):
    seen, out = set(), []
    for month in history.get('months', []):
        for analyst_group in month.get('analysts', []):
            for report in analyst_group.get('reports', []):
                if report['id'] in seen: continue
                seen.add(report['id']); out.append(report)
    return sorted(out, key=lambda x: (x['date'], str(x['id'])))


def norm_label(s):
    return re.sub(r'[\s·/&()\-]+', '', (s or '').lower())


def same_point(a, b):
    x, y = norm_label(a), norm_label(b)
    return bool(x and y) and (x == y or x in y or y in x)


def classify_regex_reasons(reasons):
    return sorted({REASON_MAP.get(r, '기타') for r in (reasons or [])}) or ['기타']


def fmt_won(v):
    return f'{int(v):,}원' if v else None


def merge_report(report, ai_entry):
    """v1 정규식 리포트 + AI 분석을 화면용 단일 레코드로 합친다."""
    ai = (ai_entry or {}).get('result')
    company, code, scope = report.get('company') or '', report.get('code') or '', report.get('report_type')
    if ai:
        # 정규식이 기업을 못 잡은 경우(산업/기타) AI 귀속으로 보정한다.
        if ai['report_scope'] == '기업' and ai['company'] and company in ('', '산업/기타'):
            company, code = ai['company'], ai['code'] or code
            scope = '기업자료'
        elif ai['report_scope'] == '산업' and not report.get('code'):
            scope = '산업자료'
    # 애널 귀속 표기는 행에 애널명이 따로 있어 중복 — 세 가지 형태를 잘라낸다.
    #  ① 꼬리 "…]/다올 건설·부동산 박영도"  ② 대괄호 안 "｜다올 금융 김지원 ☎…"
    #  ③ 선두 "[다올투자증권 반도체 고영민]" (단, ｜로 주제가 이어지는 콜라보 헤더는 유지)
    title = report.get('title') or ''
    title = re.sub(r'\s*/\s*다올.*$', '', title)
    title = re.sub(r'\s*[｜|]\s*다올[^\]｜|]*', '', title)
    title = re.sub(r'^\[다올(?:투자증권)?[^\]｜|]*\]\s*', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    record = {
        'id': str(report['id']), 'date': report['date'], 'month': report['month'],
        'analyst': report['analyst'], 'sector': report['sector'],
        'company': company, 'code': code, 'report_type': scope,
        'title': title, 'post_url': report.get('post_url') or '',
        'pdf_url': report.get('pdf_url') or report.get('source_url') or '',
        'opinion': (ai and ai['opinion'] not in ('', '없음') and ai['opinion']) or report.get('opinion') or '',
        'ai': bool(ai),
    }
    if ai:
        tp = ai['tp']
        record.update({
            'conviction': ai['conviction'], 'tone_label': ai['tone_label'], 'one_line': ai['one_line'],
            'strong_phrases': ai['strong_phrases'], 'hedge_phrases': ai['hedge_phrases'],
            'negative_phrases': ai['negative_phrases'],
            'points': [p['label'] for p in ai['investment_points']],
            'points_detail': ai['investment_points'],
            'earnings_direction': ai['earnings']['direction'], 'earnings_evidence': ai['earnings']['evidence'],
            'tp_event': None if tp['direction'] in ('없음', '유지') and not tp['value'] else {
                'direction': tp['direction'], 'value': tp['value'], 'prior': tp['prior'],
                'display': ' → '.join(x for x in (fmt_won(tp['prior']), fmt_won(tp['value'])) if x)
                           or f"{tp['direction']}",
                'reasons': tp['reasons'] or (['기타'] if tp['direction'] in ('상향', '하향') else []),
                'evidence': tp['evidence']},
        })
    else:
        first = (report.get('tp_changes') or [None])[0]
        record.update({
            'conviction': None, 'tone_label': '', 'one_line': '',
            'strong_phrases': [], 'hedge_phrases': [], 'negative_phrases': [],
            'points': [], 'points_detail': [],
            'earnings_direction': '', 'earnings_evidence': '',
            'tp_event': first and {
                'direction': first['direction'], 'value': first.get('new'), 'prior': first.get('old'),
                'display': first.get('display') or '', 'reasons': classify_regex_reasons(first.get('reasons')),
                'evidence': first.get('evidence') or ''},
        })
    return record


def company_key(record):
    if record['report_type'] == '산업자료' or record['company'] in ('', '산업/기타'):
        return f"IND:{record['sector']}", f"{record['sector']} 산업", ''
    key = record['code'] or f"NM:{norm_label(record['company'])}"
    return key, record['company'], record['code']


def attach_point_diffs(timeline):
    """같은 기업 타임라인에서 직전 AI 리포트 대비 투자포인트 추가/소멸을 계산한다."""
    prev_points = None
    for record in timeline:
        if not record['ai']:
            record['points_added'], record['points_dropped'] = [], []
            continue
        current = record['points']
        if prev_points is None:
            record['points_added'], record['points_dropped'] = [], []
        else:
            record['points_added'] = [p for p in current if not any(same_point(p, q) for q in prev_points)]
            record['points_dropped'] = [q for q in prev_points if not any(same_point(q, p) for p in current)]
        prev_points = current


def timeline_events(key, name, timeline):
    """타임라인을 시간순으로 훑으며 변화 이벤트를 뽑는다."""
    events = []
    prev = {}
    for record in timeline:
        base = {'date': record['date'], 'company_key': key, 'company': name,
                'analyst': record['analyst'], 'sector': record['sector'],
                'report_id': record['id'], 'source': record['post_url']}
        tp = record.get('tp_event')
        if tp and tp['direction'] in ('상향', '하향'):
            reasons = '·'.join(tp['reasons'])
            events.append({**base, 'type': f"TP {tp['direction']}",
                           'detail': f"{tp['display']} ← {reasons}" if reasons else tp['display'],
                           'evidence': tp['evidence']})
        if record['opinion'] and prev.get('opinion') and record['opinion'] != prev['opinion']:
            events.append({**base, 'type': '의견 변경',
                           'detail': f"{prev['opinion']} → {record['opinion']}", 'evidence': ''})
        if record['ai'] and record['earnings_direction'] in ('상향', '하향'):
            events.append({**base, 'type': f"실적추정 {record['earnings_direction']}",
                           'detail': record['earnings_evidence'][:160], 'evidence': record['earnings_evidence']})
        if record['ai'] and prev.get('conviction') is not None and record['conviction'] is not None:
            delta = record['conviction'] - prev['conviction']
            if abs(delta) >= 2:
                events.append({**base, 'type': '톤 급변' + ('↑' if delta > 0 else '↓'),
                               'detail': f"확신도 {prev['conviction']} → {record['conviction']} ({record['tone_label']})",
                               'evidence': record['one_line']})
        if record.get('points_added') or record.get('points_dropped'):
            added, dropped = record.get('points_added') or [], record.get('points_dropped') or []
            if added or dropped:
                bits = ([f"신규: {', '.join(added)}"] if added else []) + ([f"소멸: {', '.join(dropped)}"] if dropped else [])
                events.append({**base, 'type': '투자포인트 변화', 'detail': ' · '.join(bits), 'evidence': ''})
        if record['opinion']: prev['opinion'] = record['opinion']
        if record['conviction'] is not None: prev['conviction'] = record['conviction']
    return events


def canonical_sectors(records):
    """애널리스트별 최빈 섹터로 통일한다 — '철강'·'철강/비철금속'·'철강금속' 같은
    표기 흔들림으로 표가 쪼개지는 것을 막는다(동률이면 최근 사용 우선)."""
    usage = defaultdict(lambda: defaultdict(lambda: [0, '']))
    for r in records:
        slot = usage[r['analyst']][r['sector']]
        slot[0] += 1
        slot[1] = max(slot[1], r['date'])
    return {analyst: max(sectors.items(), key=lambda x: (x[1][0], x[1][1]))[0]
            for analyst, sectors in usage.items()}


def build():
    history = load_json(HISTORY, {'months': []})
    ai_cache = load_json(AI_CACHE, {})
    records = [merge_report(r, ai_cache.get(str(r['id']))) for r in flat_reports(history)]
    canon = canonical_sectors(records)
    for r in records: r['sector'] = canon.get(r['analyst'], r['sector'])

    companies = {}
    for record in records:
        key, name, code = company_key(record)
        entry = companies.setdefault(key, {'key': key, 'name': name, 'code': code,
                                           'sector': record['sector'], 'analysts': [], 'timeline': []})
        if record['analyst'] not in entry['analysts']: entry['analysts'].append(record['analyst'])
        entry['timeline'].append(record)

    all_events = []
    for key, entry in companies.items():
        entry['timeline'].sort(key=lambda x: (x['date'], x['id']))
        attach_point_diffs(entry['timeline'])
        all_events.extend(timeline_events(key, entry['name'], entry['timeline']))
        entry['report_count'] = len(entry['timeline'])
        entry['last_date'] = entry['timeline'][-1]['date']

    # 섹터 → 애널리스트 표
    by_analyst = defaultdict(list)
    for record in records: by_analyst[(record['sector'], record['analyst'])].append(record)
    sectors_map = defaultdict(list)
    for (sector, analyst), items in by_analyst.items():
        items.sort(key=lambda x: (x['date'], x['id']))
        latest = items[-1]
        opinions = [x for x in items if x['opinion']]
        ai_items = [x for x in items if x['ai']]
        covered = {}
        for record in items:
            key, name, _ = company_key(record)
            if key.startswith('IND:'): continue
            covered[key] = {'key': key, 'name': name, 'count': covered.get(key, {}).get('count', 0) + 1,
                            'last_date': record['date']}
        tp_events = [{'date': x['date'], 'company': x['company'] or f"{sector} 산업", 'company_key': company_key(x)[0],
                      **x['tp_event']} for x in items if x.get('tp_event') and x['tp_event']['direction'] in ('상향', '하향')]
        sectors_map[sector].append({
            'analyst': analyst, 'sector': sector,
            'opinion': opinions[-1]['opinion'] if opinions else '명시 없음',
            'opinion_date': opinions[-1]['date'] if opinions else '',
            'industry_key': f'IND:{sector}',
            'conviction_series': [{'date': x['date'], 'v': x['conviction'], 'company': x['company'] or sector}
                                  for x in ai_items[-16:]],
            'latest_tone': ({'label': ai_items[-1]['tone_label'], 'conviction': ai_items[-1]['conviction'],
                             'one_line': ai_items[-1]['one_line'], 'date': ai_items[-1]['date']} if ai_items else None),
            'tp_events': tp_events[-6:][::-1],
            'companies': sorted(covered.values(), key=lambda x: (-x['count'], x['name'])),
            'report_count': len(items), 'last_report': {'date': latest['date'], 'title': latest['title'][:120],
                                                        'company_key': company_key(latest)[0],
                                                        'post_url': latest['post_url']},
        })
    sectors = [{'sector': sector, 'analysts': sorted(rows, key=lambda x: x['analyst'])}
               for sector, rows in sorted(sectors_map.items(), key=lambda x: (x[0] == '미분류', x[0]))]

    recent_cut = (datetime.now(timezone.utc) + timedelta(hours=9) - timedelta(days=120)).date().isoformat()
    events = sorted([e for e in all_events if e['date'] >= recent_cut],
                    key=lambda x: x['date'], reverse=True)[:200]

    # 이벤트 피드 오른쪽 열: 변경 없이 TP를 유지(또는 신규 제시)한 보고서.
    steady = []
    for r in records:
        tp = r.get('tp_event')
        if not tp or tp['direction'] not in ('유지', '신규') or r['date'] < recent_cut: continue
        display = tp['display'] or (f"{int(tp['value']):,}원" if tp['value'] else '')
        if display and tp['direction'] not in display: display += f" ({tp['direction']})"
        steady.append({'date': r['date'], 'company_key': company_key(r)[0],
                       'company': r['company'] or f"{r['sector']} 산업", 'analyst': r['analyst'],
                       'sector': r['sector'], 'report_id': r['id'], 'source': r['post_url'],
                       'type': 'TP 유지' if tp['direction'] == '유지' else '신규 커버',
                       'detail': display or 'TP 표기 없음', 'evidence': tp['evidence']})
    steady = sorted(steady, key=lambda x: x['date'], reverse=True)[:200]

    data = {'generated_at': datetime.now(timezone.utc).isoformat(),
            'report_count': len(records), 'ai_analyzed': sum(1 for r in records if r['ai']),
            'sectors': sectors, 'companies': companies, 'events': events, 'steady': steady}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    # 대시보드 overview 패널용 경량 요약(수십 KB) — 전체 v2(1MB+)를 끌어오지 않게 별도 파일로.
    latest = sorted(records, key=lambda x: (x['date'], x['id']), reverse=True)[:20]
    summary = {'generated_at': data['generated_at'], 'report_count': data['report_count'],
               'ai_analyzed': data['ai_analyzed'],
               'events': events[:20],
               'latest': [{'date': r['date'], 'company': r['company'] or f"{r['sector']} 산업",
                           'analyst': r['analyst'], 'sector': r['sector'], 'title': r['title'][:120],
                           'opinion': r['opinion'], 'conviction': r['conviction'],
                           'tp_dir': (r.get('tp_event') or {}).get('direction', ''),
                           'post_url': r['post_url'], 'pdf_url': r['pdf_url']} for r in latest]}
    (DATA_DIR / 'tone_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({'out': str(OUT), 'reports': len(records), 'ai': data['ai_analyzed'],
                      'companies': len(companies), 'events': len(events)}, ensure_ascii=False))
    return data


if __name__ == '__main__':
    build()
