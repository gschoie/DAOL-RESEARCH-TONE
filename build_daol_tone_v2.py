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
PRICE_CACHE = DATA_DIR / 'price_close_cache.json'

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


OPINION_POSITIVE = re.compile(r'매수|비중\s*확대|Overweight|BUY', re.I)
OPINION_NEGATIVE = re.compile(r'매도|비중\s*축소|Underweight|SELL|Reduce', re.I)
OPINION_NEUTRAL = re.compile(r'중립|Neutral|Hold', re.I)


def opinion_class(op):
    """'비중확대'와 '매수'처럼 표기만 다른 같은 방향 의견을 한 클래스로 묶는다."""
    if not op: return ''
    if OPINION_POSITIVE.search(op): return '긍정'
    if OPINION_NEGATIVE.search(op): return '부정'
    if OPINION_NEUTRAL.search(op): return '중립'
    return ''


def fmt_won(v):
    return f'{int(v):,}원' if v else None


def make_return_fn(price_cache):
    """(code, date, horizon영업일) → 수익률%. 이벤트일(비거래일이면 다음 거래일) 종가 대비."""
    series = {code: e.get('closes', []) for code, e in price_cache.items()}

    def fwd(code, day, horizon):
        rows = series.get(code)
        if not rows: return None
        idx = next((i for i, (d, _) in enumerate(rows) if d >= day), None)
        if idx is None: return None
        j = idx + horizon
        if j >= len(rows): return None  # 미래 미도래
        base, fut = rows[idx][1], rows[j][1]
        return round((fut / base - 1) * 100, 1) if base else None
    return fwd


def _pair(our, cons, fix_scale=False):
    # 비교의 주체는 애널 본인 추정 — 추정이 없으면 컨센만 보여줄 이유가 없다.
    if our is None: return None
    # 영업이익 단위 보정: 십억원 표를 억원으로 환산 못 한 LLM 출력(정확히 ~10배 차이)을 잡는다.
    # 진짜 추정이 컨센의 1/10 수준일 가능성은 사실상 없으므로 10배 창에서만 교정한다.
    if fix_scale and our and cons:
        for factor in (10, 0.1):
            if 0.55 <= (our * factor) / cons <= 1.8 and not 0.4 <= our / cons <= 2.5:
                our = round(our * factor, 1)
                break
    diff = round((our / cons - 1) * 100, 1) if our and cons else None
    return {'our': our, 'cons': cons, 'diff_pct': diff}


def est_compare(estimates, consensus):
    """애널 추정 vs 네이버 컨센(인제스트 시점 스냅샷). 표시할 게 없으면 None."""
    if not estimates or not consensus: return None
    out = {}
    q_cons = consensus.get('quarter') or {}
    if estimates.get('quarter_label') and estimates['quarter_label'] == q_cons.get('label'):
        op, eps = _pair(estimates.get('quarter_op'), q_cons.get('op'), fix_scale=True), _pair(estimates.get('quarter_eps'), q_cons.get('eps'))
        if op or eps: out['quarter'] = {'label': estimates['quarter_label'], 'op': op, 'eps': eps}
    a_cons = consensus.get('annual') or {}
    op, eps = _pair(estimates.get('year_op'), a_cons.get('op'), fix_scale=True), _pair(estimates.get('year_eps'), a_cons.get('eps'))
    if (op or eps) and a_cons:
        out['annual'] = {'label': a_cons.get('label') or '당해E', 'op': op, 'eps': eps}
    return out or None


def merge_report(report, ai_entry):
    """v1 정규식 리포트 + AI 분석을 화면용 단일 레코드로 합친다."""
    ai = (ai_entry or {}).get('result')
    company, code, scope = report.get('company') or '', report.get('code') or '', report.get('report_type')
    if ai:
        # 정규식이 기업을 못 잡았거나(산업/기타), 코드 없이 산업자료로 오인한 경우
        # (제목 조각 '조선' 따위가 company로 들어옴) AI 귀속으로 보정한다.
        if ai['report_scope'] == '기업' and ai['company'] and (
                company in ('', '산업/기타') or (scope == '산업자료' and not code)):
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
        # 역산된 직전값 방어: 실제 TP는 100원 단위 라운드 숫자다. 222,222원 같은 비라운드
        # prior는 '10% 하향'을 역산한 흔적이므로 버린다(방향·새값만 표시).
        if tp.get('prior') and tp['prior'] % 100:
            tp = {**tp, 'prior': None}
        record.update({
            'conviction': ai['conviction'], 'tone_label': ai['tone_label'], 'one_line': ai['one_line'],
            'strong_phrases': ai['strong_phrases'], 'hedge_phrases': ai['hedge_phrases'],
            'negative_phrases': ai['negative_phrases'],
            'points': [p['label'] for p in ai['investment_points']],
            'points_detail': ai['investment_points'],
            'earnings_direction': ai['earnings']['direction'], 'earnings_evidence': ai['earnings']['evidence'],
            'estimates': ai.get('estimates'),
            'est_compare': est_compare(ai.get('estimates'), (ai_entry or {}).get('consensus')),
            'tp_event': None if tp['direction'] in ('없음', '유지') and not tp['value'] else {
                'direction': tp['direction'], 'value': tp['value'], 'prior': tp['prior'],
                # 유지·동일값은 '6,000원 → 6,000원' 대신 단일 표기
                'display': (fmt_won(tp['value']) if tp['direction'] == '유지' or tp['prior'] == tp['value']
                            else ' → '.join(x for x in (fmt_won(tp['prior']), fmt_won(tp['value'])) if x))
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
            'estimates': None, 'est_compare': None,
            'tp_event': first and {
                'direction': first['direction'], 'value': first.get('new'), 'prior': first.get('old'),
                'display': first.get('display') or '', 'reasons': classify_regex_reasons(first.get('reasons')),
                'evidence': first.get('evidence') or ''},
        })
    # 산업자료(대상 종목 미상)의 TP는 어느 종목 것인지 알 수 없어 노이즈 — 이벤트에서 뺀다.
    # (예: 위클리 본문의 신조선가·타 종목 숫자가 '조선 ▼ 112,000→91,000원'처럼 잡히던 문제)
    if record['report_type'] == '산업자료' or record['company'] in ('', '산업/기타'):
        record['tp_event'] = None
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
        # 방향 클래스(긍정/중립/부정)가 실제로 바뀔 때만 의견 변경 — '비중확대→매수'는 변경 아님.
        cur_class = opinion_class(record['opinion'])
        if cur_class and prev.get('opinion_class') and cur_class != prev['opinion_class']:
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
        if opinion_class(record['opinion']):
            prev['opinion'] = record['opinion']; prev['opinion_class'] = opinion_class(record['opinion'])
        if record['conviction'] is not None: prev['conviction'] = record['conviction']
    return events


# 섹터는 코스피200 컨센 트래커와 같은 분류(krx_sectors.json: 종목코드→섹터 라벨)를 쓴다.
# 한 애널리스트가 여러 섹터를 커버하면(예: 조선+방산·항공우주+기계) 섹터마다 행이 생긴다.
SECTOR_MAP_FILE = DATA_DIR / 'krx_sectors.json'
# 종목코드가 없거나 매핑에 없는 리포트(산업자료 등)는 제목·헤더 키워드로 분류한다. 순서 = 우선순위.
SECTOR_KEYWORDS = [
    (r'로봇', '로봇'), (r'2차전지|이차전지', '2차전지'),
    (r'조선|선박|신조|탱커|컨테이너선|LNG선', '조선'),
    (r'방산|防産|항공우주|무기|국방', '방산·항공우주'),
    (r'건설기계|굴착기|공작기계|기계', '기계'),
    (r'건설|부동산|주택|분양', '건설'),
    (r'은행|은행지주', '금융-은행지주'), (r'(?<!투자)증권', '금융-증권'), (r'보험', '금융-보험'), (r'카드|캐피탈', '금융-카드기타'),
    (r'소부장', '반도체소부장'), (r'반도체|HBM|파운드리', '반도체'),
    (r'전기전자|전자부품|디스플레이|MLCC', '전기전자'),
    (r'자동차|타이어|모빌리티', '자동차'),
    (r'운송|물류|항공|해상운임|해운|벌크선|택배', '운송·물류'),
    (r'의료기기', '의료기기'), (r'화장품|뷰티', '화장품'), (r'제약|바이오|신약', '제약·바이오'),
    (r'음식료|식품|라면|음료', '음식료'), (r'통신', '통신'),
    (r'게임', '게임'), (r'인터넷|플랫폼|\bAI\b', '인터넷/AI'), (r'레저|카지노|면세|여행', '레저/카지노/면세'),
    (r'엔터', '엔터'), (r'미디어|광고', '미디어·광고'),
    (r'철강|비철|철광석|후판', '철강·비철'), (r'정유|화학|石化|유가', '정유·화학'),
    (r'유틸리티|전력|한전', '유틸리티'), (r'유통|리테일', '유통'), (r'지주', '지주회사'),
    (r'섬유|의류', '섬유·의류'), (r'그린|풍력|태양광|수소', '그린인프라'), (r'IT서비스|SW|소프트웨어', 'IT서비스·SW'),
    (r'시황|투자전략|매크로|채권|파생|경제|FOMC|리서치본부|콜라보|퀀트', '전략·시황'),
]


# 보드에서 제외할 애널리스트(명단 이탈·비커버리지 등). 여기 추가하면 표·이벤트·타임라인 전부에서 빠진다.
EXCLUDED_ANALYSTS = {'김지현'}

# 키워드가 엉뚱하게 매기는 기업의 수동 교정(기업명 기준). 예: 리브스메드는 수술기구 = 의료기기.
COMPANY_SECTOR_OVERRIDES = {'리브스메드': '의료기기'}


def resolve_sector(record, sector_map):
    if record['company'] in COMPANY_SECTOR_OVERRIDES:
        return COMPANY_SECTOR_OVERRIDES[record['company']]
    if record['code'] and record['code'] in sector_map:
        return sector_map[record['code']]
    # 1순위: 제목(그 리포트가 실제 다루는 주제). 2순위: 애널 헤더의 원시 섹터 문자열 —
    # '자동차/이차전지'처럼 복수 표기는 앞 세그먼트가 주 커버리지이므로 세그먼트 순서대로 본다.
    for pattern, label in SECTOR_KEYWORDS:
        if re.search(pattern, record['title'][:160], re.I):
            return label
    for segment in re.split(r'[/·,|]+', record['sector'] or ''):
        for pattern, label in SECTOR_KEYWORDS:
            if re.search(pattern, segment, re.I):
                return label
    return record['sector'] or '기타'


def build():
    history = load_json(HISTORY, {'months': []})
    ai_cache = load_json(AI_CACHE, {})
    sector_map = load_json(SECTOR_MAP_FILE, {})
    records = [merge_report(r, ai_cache.get(str(r['id']))) for r in flat_reports(history)]
    records = [r for r in records if r['analyst'] not in EXCLUDED_ANALYSTS]
    for r in records: r['sector'] = resolve_sector(r, sector_map)

    # 애널리스트 확신도 베이스라인(개인 평균±표준편차) — '평소 대비'가 진짜 신호다.
    from statistics import mean, pstdev
    conv_by_analyst = defaultdict(list)
    for r in records:
        if r['conviction'] is not None:
            conv_by_analyst[r['analyst']].append(r['conviction'])
    baselines = {a: {'mean': round(mean(v), 2), 'std': round(pstdev(v), 2), 'n': len(v)}
                 for a, v in conv_by_analyst.items()}
    for r in records:
        b = baselines.get(r['analyst'])
        r['conv_base'] = b['mean'] if (b and r['conviction'] is not None) else None


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
                             'one_line': ai_items[-1]['one_line'], 'date': ai_items[-1]['date'],
                             'base': ai_items[-1].get('conv_base')} if ai_items else None),
            'tp_events': tp_events[-6:][::-1],
            'companies': sorted(covered.values(), key=lambda x: (-x['count'], x['name'])),
            'report_count': len(items), 'last_report': {'date': latest['date'], 'title': latest['title'][:120],
                                                        'company_key': company_key(latest)[0],
                                                        'post_url': latest['post_url']},
        })
    sectors = [{'sector': sector, 'analysts': sorted(rows, key=lambda x: x['analyst'])}
               for sector, rows in sorted(sectors_map.items(),
                                          key=lambda x: (x[0] in ('전략·시황', '기타', '미분류'), x[0]))]

    # 개인 평소 대비 ±1.5σ 이상 벗어난 톤은 '톤 이례' 이벤트(표본 10건·표준편차 0.4 이상일 때만)
    for r in records:
        b = baselines.get(r['analyst'])
        if r['conviction'] is None or not b or b['n'] < 10 or b['std'] < 0.4: continue
        dev = (r['conviction'] - b['mean']) / b['std']
        if abs(dev) >= 1.5:
            key, name, _ = company_key(r)
            all_events.append({'date': r['date'], 'company_key': key, 'company': name,
                               'analyst': r['analyst'], 'sector': r['sector'], 'report_id': r['id'],
                               'source': r['post_url'], 'type': '톤 이례' + ('↑' if dev > 0 else '↓'),
                               'detail': f"확신도 {r['conviction']} — 평소 {b['mean']}±{b['std']}",
                               'evidence': r['one_line']})

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

    # 이벤트 사후 주가 검증: +5/+20영업일 수익률(종가 캐시 기반, 네트워크 없음)
    fwd = make_return_fn(load_json(PRICE_CACHE, {}))
    for e in events + steady:
        code = e.get('company_key') or ''
        if re.fullmatch(r'[0-9]{6}', code):
            e['ret5'] = fwd(code, e['date'], 5)
            e['ret20'] = fwd(code, e['date'], 20)

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
