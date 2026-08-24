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
PDF_TEXT_CACHE = DATA_DIR / 'daol_pdf_text_cache.json'
KED_STREET = DATA_DIR / 'ked_street.json'

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


OPINION_LABELS = {'산업': {'긍정': '비중확대', '중립': '중립', '부정': '비중축소'},
                  '기업': {'긍정': 'BUY', '중립': 'HOLD', '부정': 'REDUCE'}}


def normalize_opinion(op, report_type):
    """표기 난립(매수/BUY/Overweight/비중확대…)을 스코프별 3단계로 통일한다."""
    cls = opinion_class(op)
    if not cls: return ''
    scope = '산업' if report_type == '산업자료' else '기업'
    return OPINION_LABELS[scope][cls]


def fmt_won(v):
    return f'{int(v):,}원' if v else None


# 인뎁스 종목 페이지의 TP 박스: "적정주가  43,000  24,000  상향" (현재/직전/변동)
BUNDLED_TP_RE = re.compile(r'적정주가\s*([\d,]{3,9})\s+([\d,]{3,9})\s+(상향|하향|유지)')
CODE_RE = re.compile(r'\((\d{6})\)')
# 종목 섹션의 애널 헤더 줄("이다연 음식료 | dayeonlee@daolfn.com") — 섹션 제목이 바로 다음 줄에 온다
ANALYST_HDR_RE = re.compile(r'[가-힣]{2,4}\s+[^\n]{0,40}@daolfn\.com[^\n]*\n')
PITCH_RE = re.compile(r'Pitch\s*\n(.*?)(?:\nRationale|\Z)', re.S)
_SKIP_TITLE = re.compile(r'^(Issue|Pitch|Rationale|BUY|HOLD|REDUCE|EARNINGS|In-?Depth|DAOL)', re.I)


def _bundled_section_meta(text, box_start, owner_pos, owner_is_before):
    """인뎁스 종목 섹션의 자체 제목과 Pitch(설명)를 뽑는다. 레이아웃별 위치:
    A형(코드가 박스 뒤): 코드 다음 줄 = 제목, 이어서 Issue/Pitch — 코드 뒤쪽에서 찾는다.
    B형(코드가 박스 앞): 섹션 본문(애널헤더→제목→Pitch)이 박스 앞에 통째로 온다 — 박스 앞 창에서 찾는다."""
    title, summary = '', ''
    if owner_is_before:
        win = text[max(0, box_start - 3500):box_start]
        heads = list(ANALYST_HDR_RE.finditer(win))
        if heads:
            for ln in win[heads[-1].end():].split('\n'):
                ln = ln.strip()
                if ln and not _SKIP_TITLE.match(ln):
                    title = ln; break
        pitches = PITCH_RE.findall(win)
        if pitches: summary = re.sub(r'\s+', ' ', pitches[-1]).strip()
    else:
        win = text[owner_pos:owner_pos + 2200]
        body = win.split('\n', 1)[1] if '\n' in win else ''
        for ln in body.split('\n'):
            ln = ln.strip()
            if ln and not _SKIP_TITLE.match(ln):
                title = ln; break
        m = PITCH_RE.search(win)
        if m: summary = re.sub(r'\s+', ' ', m.group(1)).strip()
    return title[:90].strip(), summary[:220].strip()


def extract_bundled_tp(text):
    """산업 인뎁스 PDF 텍스트에서 종목별 TP 박스를 뽑는다. [{code, company, value, prior, direction}]"""
    # 페이지 레이아웃이 두 갈래다:
    #  A) 좌측 TP 박스 텍스트가 먼저, 그 종목의 헤더 '회사명 (코드)'가 수백 자 뒤 (GS건설 인뎁스형)
    #  B) '회사명 (코드) BUY 투자의견… 적정주가 …'처럼 코드가 박스 바로 앞 ~60자 (음식료 인뎁스형)
    # → 앞(-120자)·뒤(+1200자) 최근접 코드 중 더 가까운 쪽. 앞 창을 120자로 좁게 잡는 이유:
    #   A형은 박스 앞 ~200자에 전 종목 코드 요약표가 오는 경우가 있어(건설 인뎁스) 그걸 배제해야 한다.
    out, seen = [], set()
    codes = [(m.start(), m.group(1)) for m in CODE_RE.finditer(text)]
    for bm in BUNDLED_TP_RE.finditer(text):
        value, prior = float(bm.group(1).replace(',', '')), float(bm.group(2).replace(',', ''))
        direction = bm.group(3)
        if direction == '상향' and not value > prior: continue
        if direction == '하향' and not value < prior: continue
        if direction == '유지' and value != prior: continue
        after = next(((pos, c) for pos, c in codes if 0 < pos - bm.start() <= 1200), None)
        before = next(((pos, c) for pos, c in reversed(codes) if 0 < bm.start() - pos <= 120), None)
        owner = min((o for o in (after, before) if o), key=lambda o: abs(o[0] - bm.start()), default=None)
        if not owner or owner[1] in seen: continue
        pos, code = owner
        name = re.search(r'([가-힣A-Za-z0-9&\-]+(?: [가-힣A-Za-z0-9&\-]+)?)\s*$', text[max(0, pos - 30):pos])
        sec_title, sec_summary = _bundled_section_meta(text, bm.start(), pos, owner is before)
        seen.add(code)
        out.append({'code': code, 'company': (name.group(1).strip() if name else ''),
                    'value': value, 'prior': prior, 'direction': direction,
                    'title': sec_title, 'summary': sec_summary})
    return out


def build_bundled_records(records, pdf_cache, sector_map):
    """산업자료(인뎁스)의 종목 페이지 TP를 합성 레코드로 만들어 종목 타임라인에 주입한다."""
    synth = []
    for r in records:
        if r['report_type'] != '산업자료' or not r.get('source_url'): continue
        entry = pdf_cache.get(r['source_url']) or {}
        text = entry.get('text', '') if entry.get('status') == 'pdf' else ''
        if '적정주가' not in text:
            # 인뎁스로 보이는데 TP 박스가 전혀 안 읽히면 조용히 넘기지 말고 경고를 남긴다
            if re.search(r'In-?Depth|인뎁스', r['title'], re.I) and len(text) > 30000:
                print(f"::warning::인뎁스 의심 자료에서 종목 TP 미검출: {r['date']} {r['title'][:50]}")
            continue
        for item in extract_bundled_tp(text):
            display = (fmt_won(item['value']) if item['direction'] == '유지'
                       else f"{fmt_won(item['prior'])} → {fmt_won(item['value'])}")
            synth.append({
                'id': f"{r['id']}-b{item['code']}", 'date': r['date'], 'month': r['month'],
                'analyst': r['analyst'], 'sector': sector_map.get(item['code'], r['sector']),
                'company': item['company'], 'code': item['code'], 'report_type': '기업자료',
                # 인뎁스 안 종목 섹션의 자체 제목·Pitch가 있으면 그걸 쓴다(없으면 모(母)자료 제목)
                'title': item.get('title') or r['title'], 'post_url': r['post_url'], 'source_url': r['source_url'],
                'pdf_url': r['pdf_url'], 'opinion': '', 'ai': False, 'bundled': True,
                'conviction': None, 'tone_label': '', 'one_line': item.get('summary') or '',
                'strong_phrases': [], 'hedge_phrases': [], 'negative_phrases': [],
                'points': [], 'points_detail': [], 'earnings_direction': '', 'earnings_evidence': '',
                'estimates': None, 'est_compare': None,
                'tp_event': {'direction': item['direction'], 'value': item['value'],
                             'prior': None if item['direction'] == '유지' else item['prior'],
                             'display': display, 'reasons': [],
                             'evidence': f"산업 인뎁스 종목 페이지에서 추출 ({r['title'][:60]})"},
            })
    # 같은 날짜·같은 종목·같은 값의 정식 기업자료가 이미 있으면 합성본은 뺀다(중복 방지)
    existing = {(x['code'], x['date'], (x.get('tp_event') or {}).get('value'))
                for x in records if x.get('code') and x.get('tp_event')}
    return [x for x in synth if (x['code'], x['date'], x['tp_event']['value']) not in existing]


def enrich_from_cover(records, pdf_cache):
    """기업 리포트 표지의 '현재/직전/변동' 박스를 읽어 TP를 보강한다(직전값 포함, 최우선 소스)."""
    for r in records:
        if r.get('bundled') or r['report_type'] == '산업자료' or not r.get('source_url'): continue
        entry = pdf_cache.get(r['source_url']) or {}
        if entry.get('status') != 'pdf': continue
        m = BUNDLED_TP_RE.search(entry.get('text', '')[:5000])
        if not m: continue
        value, prior = float(m.group(1).replace(',', '')), float(m.group(2).replace(',', ''))
        direction = m.group(3)
        if direction == '상향' and not value > prior: continue
        if direction == '하향' and not value < prior: continue
        if direction == '유지' and value != prior: continue
        tp = r.get('tp_event') or {}
        evidence = tp.get('evidence') or '표지 적정주가 박스'
        reasons = tp.get('reasons') or []
        display = fmt_won(value) if direction == '유지' else f"{fmt_won(prior)} → {fmt_won(value)}"
        r['tp_event'] = {'direction': direction, 'value': value,
                         'prior': None if direction == '유지' else prior,
                         'display': display, 'reasons': reasons, 'evidence': evidence}


def attach_street(companies, street_raw):
    """한경 에이셀에서 모은 타사 리서치를 다올 커버 종목에 붙인다(최근 60일, 다올 자체 리포트 제외)."""
    from collections import defaultdict as _dd
    by_name = _dd(list)
    for e in street_raw:
        by_name[norm_label(e['company'])].append(e)
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=9) - timedelta(days=60)).date().isoformat()
    for key, entry in companies.items():
        if key.startswith('IND:'): continue
        matches = [e for e in by_name.get(norm_label(entry['name']), [])
                   if e['date'] >= cutoff and '다올' not in e.get('brokerage', '')]
        if not matches: continue
        matches = sorted(matches, key=lambda x: x['date'], reverse=True)[:12]
        tps = sorted(e['tp_new'] for e in matches if e.get('tp_new'))
        stats = None
        if tps:
            daol_tp = next((r['tp_event']['value'] for r in reversed(entry['timeline'])
                            if r.get('tp_event') and r['tp_event'].get('value')), None)
            pct = round(100 * sum(1 for t in tps if t <= daol_tp) / len(tps)) if daol_tp else None
            stats = {'n': len(matches), 'low': tps[0], 'high': tps[-1],
                     'median': tps[len(tps) // 2], 'daol': daol_tp, 'daol_pct': pct}
        entry['street'] = {'stats': stats, 'items': matches}


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
    # 리포트가 말하는 분기를 스냅샷의 추정(E) 분기들 중에서 찾는다 — 실적 시즌 전환기에
    # 리포트(3Q 프리뷰)와 네이버 최근접 E 분기(2Q)가 어긋나도 복수 분기 목록이 있으면 매칭된다.
    candidates = consensus.get('quarters') or ([consensus['quarter']] if consensus.get('quarter') else [])
    q_cons = next((q for q in candidates if q and q.get('label') == estimates.get('quarter_label')), None)
    if estimates.get('quarter_label') and q_cons:
        op, eps = _pair(estimates.get('quarter_op'), q_cons.get('op'), fix_scale=True), _pair(estimates.get('quarter_eps'), q_cons.get('eps'))
        if op or eps: out['quarter'] = {'label': estimates['quarter_label'], 'op': op, 'eps': eps}
    a_cons = consensus.get('annual') or {}
    op, eps = _pair(estimates.get('year_op'), a_cons.get('op'), fix_scale=True), _pair(estimates.get('year_eps'), a_cons.get('eps'))
    if (op or eps) and a_cons:
        out['annual'] = {'label': a_cons.get('label') or '당해E', 'op': op, 'eps': eps}
    if out and consensus.get('fetched_at'):
        # 컨센 스냅샷 수집 시각(UTC) → KST 날짜 — 화면 각주 "네이버 M.D 수집"용
        try:
            dt = datetime.fromisoformat(consensus['fetched_at'])
            out['as_of'] = (dt + timedelta(hours=9)).date().isoformat()
        except ValueError: pass
    return out or None


PICK_TAIL_RE = re.compile(r'\s*(?:[*※].*|보고서\s*원문.*|Compliance.*)$', re.I)


def clean_top_pick(x):
    """최선호주 문장 정리 — 컴플라이언스 꼬리·선두 페이지번호 제거, 표 행 쓰레기는 버림."""
    x = PICK_TAIL_RE.sub('', str(x))
    x = re.sub(r'^\d{1,3}\s+', '', x).strip()
    if not x: return None
    if len(re.findall(r'\d[\d,.]{2,}', x)) >= 4: return None  # 숫자 나열 = PDF 표 행
    return x[:160]


def merge_report(report, ai_entry):
    """v1 정규식 리포트 + AI 분석을 화면용 단일 레코드로 합친다."""
    ai = (ai_entry or {}).get('result')
    company, code, scope = report.get('company') or '', report.get('code') or '', report.get('report_type')
    # '1Q26'·'CY2Q26' 같은 분기 라벨은 기업명이 아니다 — 비워서 AI 귀속 보정이 진짜 기업명을 채우게 한다
    if re.fullmatch(r'(?:CY|FY)?[1-4]Q\d{2}[EP]?', company):
        company, code = '', ''
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
    title = re.sub(r'\s*/\s*다올[^\]★]*', '', title)
    title = re.sub(r'\s*[｜|]\s*다올[^\]｜|]*', '', title)
    title = re.sub(r'^\[다올(?:투자증권)?[^\]｜|]*\]\s*', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    record = {
        'id': str(report['id']), 'date': report['date'], 'month': report['month'],
        'analyst': report['analyst'], 'sector': report['sector'],
        'company': company, 'code': code, 'report_type': scope,
        'title': title, 'post_url': report.get('post_url') or '',
        'source_url': report.get('source_url') or '',
        'pdf_url': report.get('pdf_url') or report.get('source_url') or '',
        # 위클리/주간 모니터링 포맷은 산업의견을 적지 않는다 — AI 추측('중립')도, 본문 잡단어를 잡은
        # 정규식 값도 쓰지 않고 항상 비운다. 표의 산업의견은 최근 '의견 명시' 산업자료 것이 유지된다.
        'top_picks': [c for c in (clean_top_pick(x) for x in (report.get('preferred_stocks') or report.get('top_picks') or [])) if c][:4],
        'opinion': '' if re.search(r'Weekly|위클리|주간\s*(모니터링|동향)', report.get('title') or '', re.I)
                   else ((ai and ai['opinion'] not in ('', '없음') and ai['opinion']) or report.get('opinion') or ''),
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
    # 직전값 미상인 상향/하향은 '기존 → 새값'으로 표기 통일(타임라인에서 찾으면 실제 값으로 대체됨)
    tp_ev = record.get('tp_event')
    if tp_ev and tp_ev.get('value') and tp_ev.get('prior') is None and tp_ev['direction'] in ('상향', '하향'):
        tp_ev['display'] = f"기존 → {fmt_won(tp_ev['value'])}"
    record['opinion'] = normalize_opinion(record['opinion'], record['report_type'])
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


def fill_tp_priors(timeline):
    """본문에 직전 TP가 없던 변경(상향/하향)은 같은 종목 타임라인의 직전 TP로 채운다.
    방향과 모순되면(하향인데 직전값이 더 낮음 등) 데이터 노이즈로 보고 채우지 않는다."""
    last_tp = None
    for record in timeline:
        tp = record.get('tp_event')
        if not tp or not tp.get('value'):
            continue
        if tp.get('prior') is None and tp['direction'] in ('상향', '하향') and last_tp and last_tp != tp['value']:
            consistent = (tp['direction'] == '상향' and last_tp < tp['value']) or                          (tp['direction'] == '하향' and last_tp > tp['value'])
            if consistent:
                tp['prior'] = last_tp
                tp['display'] = f"{fmt_won(last_tp)} → {fmt_won(tp['value'])}"
        last_tp = tp['value']


def attach_point_diffs(timeline):
    """같은 기업 타임라인에서 직전 AI 리포트 대비 투자포인트 추가/소멸을 계산한다."""
    prev_points, prev_date = None, None
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
            record['points_prev_date'] = prev_date  # 직전 키워드의 출처 날짜(표시용)
        prev_points, prev_date = current, record['date']


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
                prev_lbl = (record.get('points_prev_date') or '')[5:].replace('-', '.')
                bits = ([f"신규: {', '.join(added)}"] if added else []) +                        ([f"{prev_lbl + ' 포인트' if prev_lbl else '직전'}: {', '.join(dropped)}"] if dropped else [])
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
    (r'(?<![가-힣])은행|은행지주', '금융-은행지주'), (r'(?<!투자)증권', '금융-증권'), (r'보험', '금융-보험'), (r'카드|캐피탈', '금융-카드기타'),
    (r'소부장', '반도체소부장'), (r'반도체|HBM|파운드리', '반도체'),
    (r'전기전자|전자부품|디스플레이|MLCC', '전기전자'),
    (r'자동차|타이어|모빌리티', '자동차'),
    (r'운송|물류|항공|해상운임|해운|벌크선|택배', '운송·물류'),
    (r'의료기기|의료\s*AI|미용\s*의료|치과', '의료기기'), (r'화장품|뷰티', '화장품'), (r'제약|바이오|신약', '제약·바이오'),
    (r'음식료|식품|라면|음료', '음식료'), (r'통신', '통신'),
    (r'게임\s*[(/]|게임\s*(업|주|산업|섹터)|^게임$', '게임'), (r'인터넷|플랫폼|\bAI\b', '인터넷/AI'), (r'레저|카지노|면세|여행', '레저/카지노/면세'),
    (r'엔터', '엔터'),  # 미디어·광고는 아직 섹터 담당이 없어 키워드 분류하지 않는다(오분류 방지)
    (r'철강|비철|철광석|후판', '철강·비철'), (r'정유|화학|石化|유가', '정유·화학'),
    (r'유틸리티|전력|한전', '유틸리티'), (r'유통|리테일', '유통'), (r'지주', '지주회사'),
    (r'섬유|의류', '섬유·의류'), (r'그린|풍력|태양광|수소', '그린인프라'), (r'IT서비스|SW|소프트웨어', 'IT서비스·SW'),
    (r'콜라보', '콜라보'),
    (r'시황|투자전략|매크로|채권|파생|경제|FOMC|리서치본부|(?<![가-힣A-Za-z])퀀트', '전략·시황'),
]


# 보드에서 제외할 애널리스트(명단 이탈·비커버리지 등). 여기 추가하면 표·이벤트·타임라인 전부에서 빠진다.
EXCLUDED_ANALYSTS = {'김지현'}

# 키워드가 엉뚱하게 매기는 기업의 수동 교정(기업명 기준). 예: 리브스메드는 수술기구 = 의료기기.
COMPANY_SECTOR_OVERRIDES = {'리브스메드': '의료기기', '실리콘투': '화장품', 'HL홀딩스': '자동차',
                            '오리온홀딩스': '음식료', 'Meta Platforms': '인터넷/AI'}


def resolve_sector(record, sector_map):
    if record['company'] in COMPANY_SECTOR_OVERRIDES:
        return COMPANY_SECTOR_OVERRIDES[record['company']]
    # 박종현은 의료기기·화장품 담당 — 커버 종목(대웅제약 등)이 코드 매핑상 제약·바이오라도
    # 이지수(제약·바이오)와 표에서 합쳐지지 않게 본인 섹터로 귀속한다.
    if record['analyst'] == '박종현':
        base = sector_map.get(record['code'] or '', '')
        if base == '제약·바이오' or re.search(r'제약|바이오|신약', record['title'][:160]):
            return '화장품' if re.search(r'화장품|뷰티|코스메', record['title'][:160]) else '의료기기'
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
    # 기업 리포트 표지 박스로 TP 보강(직전값 확보 — '기존 →' 플레이스홀더 최소화)
    enrich_from_cover(records, load_json(PDF_TEXT_CACHE, {}))

    # 인뎁스(묶음 산업자료) 종목 페이지의 TP를 종목 타임라인에 주입
    records.extend(build_bundled_records(records, load_json(PDF_TEXT_CACHE, {}), sector_map))

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
        fill_tp_priors(entry['timeline'])
        attach_point_diffs(entry['timeline'])
        all_events.extend(timeline_events(key, entry['name'], entry['timeline']))
        entry['report_count'] = len(entry['timeline'])
        entry['last_date'] = entry['timeline'][-1]['date']

    # 타사 리서치(한경 에이셀) 부착
    attach_street(companies, load_json(KED_STREET, []))

    # 표의 '자료 갯수'는 최근 6개월(달 기준) 창으로 센다 — 조회(타임라인·차트)는 전 기간 유지.
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    start_total = now_kst.year * 12 + (now_kst.month - 1) - 5
    win_y, win_m0 = divmod(start_total, 12)
    window_month = f"{win_y:04d}-{win_m0 + 1:02d}"
    window_label = f"'{win_y % 100}.{win_m0 + 1}~'{now_kst.year % 100}.{now_kst.month}"

    # 섹터 → 애널리스트 표
    by_analyst = defaultdict(list)
    for record in records:
        if record.get('bundled'): continue  # 합성 레코드는 표 집계에서 제외
        by_analyst[(record['sector'], record['analyst'])].append(record)
    sectors_map = defaultdict(list)
    for (sector, analyst), items in by_analyst.items():
        items.sort(key=lambda x: (x['date'], x['id']))
        latest = items[-1]
        # '산업의견' 칼럼이므로 산업자료에서 명시된 의견만 쓴다(기업 BUY가 섞이지 않게)
        opinions = [x for x in items if x['opinion'] and x['report_type'] == '산업자료']
        ai_items = [x for x in items if x['ai']]
        recent_items = [x for x in items if x['month'] >= window_month]
        covered = {}
        for record in recent_items:
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
            # 이 섹터의 가장 최근 산업자료에서 뽑은 강한/부정 문구(행에 바로 노출)
            'ind_phrases': (lambda ind: {'date': ind['date'],
                                         'strong': (ind.get('strong_phrases') or [''])[0][:80],
                                         'negative': (ind.get('negative_phrases') or [''])[0][:80]}
                            if ind else None)(next((x for x in reversed(ai_items)
                                                    if x['report_type'] == '산업자료'), None)),
            'tp_events': tp_events[-6:][::-1],
            'companies': sorted(covered.values(), key=lambda x: (-x['count'], x['name'])),
            'report_count': len(recent_items), 'last_report': {'date': latest['date'], 'title': latest['title'][:120],
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
        if r.get('bundled'): continue  # 인뎁스 합성 유지 건으로 피드가 넘치지 않게
        if not tp or tp['direction'] not in ('유지', '신규') or r['date'] < recent_cut: continue
        display = tp['display'] or (f"{int(tp['value']):,}원" if tp['value'] else '')
        if display and tp['direction'] not in display: display += f" ({tp['direction']})"
        # TP는 유지라도 톤의 온도가 보이게 — 부정 문구(경계 신호) 우선, 없으면 강한 문구.
        phrase, phrase_kind = '', ''
        if r.get('negative_phrases'):
            phrase, phrase_kind = r['negative_phrases'][0][:70], '부정'
        elif r.get('strong_phrases'):
            phrase, phrase_kind = r['strong_phrases'][0][:70], '강'
        steady.append({'date': r['date'], 'company_key': company_key(r)[0],
                       'company': r['company'] or f"{r['sector']} 산업", 'analyst': r['analyst'],
                       'sector': r['sector'], 'report_id': r['id'], 'source': r['post_url'],
                       'type': 'TP 유지' if tp['direction'] == '유지' else '신규 커버',
                       'detail': display or 'TP 표기 없음', 'evidence': tp['evidence'],
                       'phrase': phrase, 'phrase_kind': phrase_kind})
    steady = sorted(steady, key=lambda x: x['date'], reverse=True)[:200]

    # 이벤트 사후 주가 검증: +5/+20영업일 수익률(종가 캐시 기반, 네트워크 없음)
    price_cache = load_json(PRICE_CACHE, {})
    fwd = make_return_fn(price_cache)
    for e in events + steady:
        code = e.get('company_key') or ''
        if re.fullmatch(r'[0-9]{6}', code):
            e['ret5'] = fwd(code, e['date'], 5)
            e['ret20'] = fwd(code, e['date'], 20)

    # 발간 당시 상승여력: TP ÷ 발간 전일 종가 − 1. 아침 발간 보고서의 관례(전일 종가 기준)를 따르므로
    # 발간일 "직전" 거래일 종가를 쓴다(종가 캐시는 최근 150일 커버 — 그 밖 과거 리포트는 표기 생략).
    closes_by_code = {c: e.get('closes', []) for c, e in price_cache.items()}

    def prior_close(code, day):
        prev = None
        for d, px in closes_by_code.get(code, []):
            if d >= day: break
            prev = px
        return prev

    for entry in companies.values():
        for r in entry['timeline']:
            tp_ev, code = r.get('tp_event') or {}, r.get('code') or ''
            if tp_ev.get('value') and re.fullmatch(r'[0-9]{6}', code):
                base = prior_close(code, r['date'])
                if base:
                    tp_ev['upside'] = round((tp_ev['value'] / base - 1) * 100)

    data = {'generated_at': datetime.now(timezone.utc).isoformat(),
            'report_count': len(records), 'ai_analyzed': sum(1 for r in records if r['ai']),
            'count_window': {'from': window_month, 'label': window_label},
            'sectors': sectors, 'companies': companies, 'events': events, 'steady': steady}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    # 대시보드 overview 패널용 경량 요약(수십 KB) — 전체 v2(1MB+)를 끌어오지 않게 별도 파일로.
    latest = sorted(records, key=lambda x: (x['date'], x['id']), reverse=True)[:20]
    summary = {'generated_at': data['generated_at'], 'report_count': data['report_count'],
               'ai_analyzed': data['ai_analyzed'],
               'events': events[:20],
               'latest': [{'date': r['date'], 'company': r['company'] or f"{r['sector']} 산업",
                           'company_key': company_key(r)[0],
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
