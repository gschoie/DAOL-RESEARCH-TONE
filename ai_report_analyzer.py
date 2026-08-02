"""다올 리포트 1건 → LLM 구조화 분석(톤·TP 변경사유·실적추정·투자포인트).

K-DEFENCE 봇과 같은 키 전략: GEMINI_API_KEY(무료 티어) 우선, 없으면 OPENAI_API_KEY.
결과는 data/daol_ai_analysis.json 에 리포트 id 기준으로 캐시되어 증분 실행된다.
표준 라이브러리만 사용(urllib) — CI에 추가 의존성 없음.

단독 실행:
  python ai_report_analyzer.py --max 100          # 미분석 최신 100건 분석
  python ai_report_analyzer.py --dry-run          # 미분석 건수만 확인
"""
import argparse, json, os, re, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
HISTORY = DATA_DIR / 'daol_tone_history.json'
MSG_FILE = DATA_DIR / 'daol_messages.json'
PDF_CACHE = DATA_DIR / 'daol_pdf_text_cache.json'
AI_CACHE = DATA_DIR / 'daol_ai_analysis.json'
NAVER_CACHE = DATA_DIR / 'naver_consensus_cache.json'

OPENAI_RESPONSES_API = 'https://api.openai.com/v1/responses'
GEMINI_API_TEMPLATE = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

TP_REASONS = ['실적추정', '멀티플', '방법론', '시점롤포워드', '기타']
TONE_LABELS = ['강한 확신', '긍정', '중립·건조', '신중', '부정적']
DIRECTIONS = ['상향', '하향', '유지', '신규', '없음']

SYSTEM_PROMPT = '''당신은 한국 셀사이드(다올투자증권) 리서치 보고서의 톤을 분석하는 전문가다.
보고서 텍스트(텔레그램 요약 + PDF 원문 발췌)를 읽고 JSON만 출력한다.

규칙:
- conviction: 애널리스트 확신도 1~5 정수. 5=강한 확신(단정적 전망·공격적 매수 논거), 4=긍정,
  3=중립·건조(사실 나열 위주), 2=신중(헤징 표현 다수), 1=방어적·부정적. 문구를 근거로 판단한다.
- tone_label: [강한 확신, 긍정, 중립·건조, 신중, 부정적] 중 하나.
- strong_phrases / hedge_phrases / negative_phrases: 본문에서 "원문 그대로" 인용(각 80자 이내, 최대 3개).
  강한 문구=단정·확신 표현, 헤징 문구=유보·조건부 표현("~할 가능성", "지켜볼 필요"), 부정 문구=리스크·우려.
  해당 없으면 빈 배열.
- tp: 적정주가(목표주가). direction은 [상향, 하향, 유지, 신규, 없음] 중 하나(언급 없으면 "없음").
  value/prior는 원 단위 숫자(없으면 null). reasons는 변경 배경을 [실적추정, 멀티플, 방법론, 시점롤포워드, 기타]
  중 해당하는 것 전부: 실적추정=이익 추정치 변경 반영, 멀티플=적용 배수(PER 등) 조정,
  방법론=밸류에이션 방식 자체 변경(예: PER→DCF, SOTP 도입), 시점롤포워드=기준연도/12M 선행 이동.
  prior(직전 적정주가)는 본문에 금액이 "명시"된 경우만 채운다 — '10% 하향'처럼 비율만 있으면
  prior는 null로 둔다(비율로 역산해 금액을 만들어내지 말 것). evidence에 근거 문장을 원문 인용(160자 이내).
- earnings: 실적(영업이익/EPS) 추정치 방향 [상향, 하향, 유지, 신규, 없음] + 근거 인용(160자 이내).
- opinion: 명시된 투자의견. 산업자료는 [비중확대, 중립, 비중축소], 기업자료는 [매수, 중립, 매도] 계열.
  명시 없으면 "없음". 위클리/주간 모니터링 자료는 의견 문구가 본문에 없으면 톤으로 추측하지 말고 반드시 "없음".
- report_scope: 특정 기업 분석이면 "기업", 산업/전략/시황이면 "산업".
- company/code: 대상 기업명과 6자리 종목코드(산업자료면 빈 문자열).
- investment_points: 핵심 투자포인트 최대 4개. label은 12자 이내의 재사용 가능한 키워드
  (예: "미 함정 MRO", "HBM 증설", "후판가 하락"), summary는 80자 이내 요지.
- one_line: 이 보고서의 톤을 한 문장으로(60자 이내).
- estimates: 애널리스트 "본인"의 실적 추정치(시장 컨센서스 아님). quarter_label은 이 보고서가 다루는
  당분기(실적 발표를 앞둔/리뷰하는 분기)를 '2Q26' 형식으로. quarter_op/year_op는 그 분기/당해 연도의
  영업이익 추정치(단위: 억원 — 표가 십억원 단위면 10을 곱해 억원으로 환산. 예: '232.4(십억원)' → 2324), quarter_eps/year_eps는
  EPS 추정치(원). 본문·표에서 확인 안 되면 null. 산업자료는 전부 null.
모든 텍스트는 한국어.'''

# ── JSON 스키마 (Gemini responseSchema 형식) ────────────────────────────────
def _g(t, **kw): return {'type': t, **kw}
GEMINI_SCHEMA = _g('OBJECT', properties={
    'report_scope': _g('STRING', enum=['기업', '산업']),
    'company': _g('STRING'), 'code': _g('STRING'),
    'opinion': _g('STRING'),
    'tp': _g('OBJECT', properties={
        'direction': _g('STRING', enum=DIRECTIONS),
        'value': _g('NUMBER', nullable=True), 'prior': _g('NUMBER', nullable=True),
        'reasons': _g('ARRAY', items=_g('STRING', enum=TP_REASONS)),
        'evidence': _g('STRING')}, required=['direction', 'reasons', 'evidence']),
    'earnings': _g('OBJECT', properties={
        'direction': _g('STRING', enum=DIRECTIONS), 'evidence': _g('STRING')},
        required=['direction', 'evidence']),
    'conviction': _g('INTEGER'),
    'tone_label': _g('STRING', enum=TONE_LABELS),
    'strong_phrases': _g('ARRAY', items=_g('STRING')),
    'hedge_phrases': _g('ARRAY', items=_g('STRING')),
    'negative_phrases': _g('ARRAY', items=_g('STRING')),
    'investment_points': _g('ARRAY', items=_g('OBJECT', properties={
        'label': _g('STRING'), 'summary': _g('STRING')}, required=['label', 'summary'])),
    'one_line': _g('STRING'),
    'estimates': _g('OBJECT', properties={
        'quarter_label': _g('STRING'),
        'quarter_op': _g('NUMBER', nullable=True), 'quarter_eps': _g('NUMBER', nullable=True),
        'year_op': _g('NUMBER', nullable=True), 'year_eps': _g('NUMBER', nullable=True)},
        required=['quarter_label', 'quarter_op', 'quarter_eps', 'year_op', 'year_eps']),
}, required=['report_scope', 'company', 'code', 'opinion', 'tp', 'earnings', 'conviction',
             'tone_label', 'strong_phrases', 'hedge_phrases', 'negative_phrases',
             'investment_points', 'one_line', 'estimates'])


def _openai_schema(node):
    """Gemini 스키마 → OpenAI json_schema 변환."""
    t = node['type'].lower()
    out = {'type': t}
    if node.get('nullable'): out['type'] = [t, 'null']
    if 'enum' in node: out['enum'] = node['enum']
    if t == 'object':
        out['properties'] = {k: _openai_schema(v) for k, v in node['properties'].items()}
        out['required'] = list(node['properties'])  # strict 모드는 전 필드 required
        out['additionalProperties'] = False
    if t == 'array': out['items'] = _openai_schema(node['items'])
    return out


class QuotaExhaustedError(RuntimeError):
    """일일 쿼터/크레딧 소진 — 이번 런에서는 회복 불가."""


def ai_provider():
    if os.getenv('GEMINI_API_KEY', '').strip(): return 'gemini'
    if os.getenv('OPENAI_API_KEY', '').strip(): return 'openai'
    return None


def _post_json(url, body, headers, timeout=90):
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers={'Content-Type': 'application/json', **headers}, method='POST')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


GEMINI_CANDIDATES = ['gemini-flash-lite-latest', 'gemini-3.6-flash', 'gemini-3.5-flash',
                     'gemini-2.5-flash', 'gemini-2.0-flash']


def gemini_model_queue():
    env = os.getenv('GEMINI_MODEL', '').strip()
    return ([env] if env else []) + [m for m in GEMINI_CANDIDATES if m != env]


def gemini_analyze(user_text, model=None):
    model = model or gemini_model_queue()[0]
    url = GEMINI_API_TEMPLATE.format(model=model) + f"?key={os.environ['GEMINI_API_KEY'].strip()}"
    body = {'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_text}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': GEMINI_SCHEMA,
                                 'temperature': 0.1}}
    for attempt in range(3):
        try:
            payload = _post_json(url, body, {})
            text = payload['candidates'][0]['content']['parts'][0]['text']
            return json.loads(text), model
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            # 무료 티어 일일 한도(RPD) 소진은 기다려도 회복 안 됨 — 즉시 중단해 캐시를 저장한다.
            if exc.code == 429 and ('PerDay' in detail or 'RESOURCE_EXHAUSTED' in detail and 'daily' in detail.lower()):
                raise QuotaExhaustedError(f'Gemini daily quota exhausted: {detail[:1200]}') from exc
            if exc.code == 429 and attempt < 2:
                time.sleep(30); continue
            raise RuntimeError(f'Gemini API error {exc.code}: {detail[:300]}') from exc
    raise RuntimeError('Gemini retry loop exited unexpectedly')


def openai_analyze(user_text, model=None):
    model = os.getenv('OPENAI_MODEL', 'gpt-5-mini')
    body = {'model': model, 'instructions': SYSTEM_PROMPT, 'input': user_text,
            'text': {'format': {'type': 'json_schema', 'name': 'report_tone',
                                'schema': _openai_schema(GEMINI_SCHEMA), 'strict': True}}}
    for attempt in range(3):
        try:
            payload = _post_json(OPENAI_RESPONSES_API, body,
                                 {'Authorization': f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"})
            text = payload.get('output_text') or '\n'.join(
                c.get('text', '') for item in payload.get('output', []) if item.get('type') == 'message'
                for c in item.get('content', []))
            return json.loads(text), model
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            if exc.code == 429 and 'insufficient_quota' in detail:
                raise QuotaExhaustedError('OpenAI quota exhausted (check billing)') from exc
            if exc.code == 429 and attempt < 2:
                time.sleep(20); continue
            raise RuntimeError(f'OpenAI API error {exc.code}: {detail[:300]}') from exc
    raise RuntimeError('OpenAI retry loop exited unexpectedly')


def normalize_result(raw):
    """모델 출력의 형 안정화 — 이후 diff 엔진이 방어 코드 없이 쓰게 한다."""
    tp = raw.get('tp') or {}
    def _num(x):
        try: return None if x is None else float(x)
        except (TypeError, ValueError): return None
    def _phrases(key): return [str(x)[:120] for x in (raw.get(key) or []) if str(x).strip()][:3]
    conviction = raw.get('conviction')
    conviction = min(5, max(1, int(conviction))) if isinstance(conviction, (int, float)) else 3
    return {
        'report_scope': raw.get('report_scope') if raw.get('report_scope') in ('기업', '산업') else '산업',
        'company': str(raw.get('company') or '').strip()[:40],
        'code': raw.get('code') if re.fullmatch(r'[0-9]{6}', str(raw.get('code') or '')) else '',
        'opinion': str(raw.get('opinion') or '없음').strip()[:20],
        'tp': {'direction': tp.get('direction') if tp.get('direction') in DIRECTIONS else '없음',
               'value': _num(tp.get('value')), 'prior': _num(tp.get('prior')),
               'reasons': [x for x in (tp.get('reasons') or []) if x in TP_REASONS],
               'evidence': str(tp.get('evidence') or '')[:240]},
        'earnings': {'direction': (raw.get('earnings') or {}).get('direction')
                     if (raw.get('earnings') or {}).get('direction') in DIRECTIONS else '없음',
                     'evidence': str((raw.get('earnings') or {}).get('evidence') or '')[:240]},
        'conviction': conviction,
        'tone_label': raw.get('tone_label') if raw.get('tone_label') in TONE_LABELS else '중립·건조',
        'strong_phrases': _phrases('strong_phrases'),
        'hedge_phrases': _phrases('hedge_phrases'),
        'negative_phrases': _phrases('negative_phrases'),
        'investment_points': [{'label': str(p.get('label') or '')[:20], 'summary': str(p.get('summary') or '')[:100]}
                              for p in (raw.get('investment_points') or []) if str(p.get('label') or '').strip()][:4],
        'one_line': str(raw.get('one_line') or '')[:100],
        'estimates': {
            'quarter_label': str((raw.get('estimates') or {}).get('quarter_label') or '')[:8],
            'quarter_op': _num((raw.get('estimates') or {}).get('quarter_op')),
            'quarter_eps': _num((raw.get('estimates') or {}).get('quarter_eps')),
            'year_op': _num((raw.get('estimates') or {}).get('year_op')),
            'year_eps': _num((raw.get('estimates') or {}).get('year_eps')),
        },
    }


def build_input_text(report, messages_by_id, pdf_cache):
    """텔레그램 전문 + PDF 원문 발췌를 합쳐 분석 입력을 만든다."""
    msg = messages_by_id.get(str(report['id'])) or messages_by_id.get(int(report['id']) if str(report['id']).isdigit() else -1)
    telegram_text = (msg or {}).get('text') or report.get('summary') or ''
    pdf_entry = pdf_cache.get(report.get('source_url') or '') or {}
    pdf_body = pdf_entry.get('text', '') if pdf_entry.get('status') == 'pdf' else ''
    parts = [f"[발간일] {report['date']}  [애널리스트] {report['analyst']} ({report['sector']})",
             f"[텔레그램 요약]\n{telegram_text[:6000]}"]
    if len(pdf_body) > 400:
        parts.append(f"[PDF 원문 발췌]\n{pdf_body[:16000]}")
    return '\n\n'.join(parts)


def flat_reports(history):
    seen, out = set(), []
    for month in history.get('months', []):
        for analyst in month.get('analysts', []):
            for report in analyst.get('reports', []):
                if report['id'] in seen: continue
                seen.add(report['id']); out.append(report)
    return sorted(out, key=lambda x: (x['date'], str(x['id'])), reverse=True)


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def run(max_reports=100, since='', budget_seconds=1500):
    provider = ai_provider()
    history = load_json(HISTORY, {'months': []})
    cache = load_json(AI_CACHE, {})
    reports = flat_reports(history)
    def needs_analysis(rid):
        entry = cache.get(rid)
        # 스키마 확장(estimates) 이전에 분석된 항목은 재분석 대상
        return not entry or 'estimates' not in (entry.get('result') or {})
    pending = [r for r in reports if needs_analysis(str(r['id'])) and (not since or r['month'] >= since)]
    if not provider:
        (DATA_DIR / 'ai_run_status.json').write_text(json.dumps(
            {'ran_at': datetime.now(timezone.utc).isoformat(), 'provider': None,
             'analyzed': 0, 'pending_left': len(pending),
             'last_error': 'GEMINI_API_KEY/OPENAI_API_KEY 시크릿 없음'}, ensure_ascii=False, indent=1), encoding='utf-8')
        print(json.dumps({'provider': None, 'analyzed': 0, 'pending': len(pending),
                          'note': 'GEMINI_API_KEY/OPENAI_API_KEY 없음 — AI 분석 건너뜀'}, ensure_ascii=False))
        return cache
    messages_by_id = {str(m['id']): m for m in load_json(MSG_FILE, [])}
    pdf_cache = load_json(PDF_CACHE, {})
    analyze = gemini_analyze if provider == 'gemini' else openai_analyze
    recent_cut = (datetime.now(timezone.utc) - timedelta(days=45)).date().isoformat()
    naver_cache = load_json(NAVER_CACHE, {})
    naver_changed = False

    def get_consensus(code):
        nonlocal naver_changed
        day_key = f"{code}:{datetime.now(timezone.utc).date().isoformat()}"
        if day_key not in naver_cache:
            try:
                import naver_consensus
                naver_cache[day_key] = naver_consensus.fetch_consensus(code)
                naver_changed = True
            except Exception as exc:  # 컨센 실패는 분석을 막지 않는다
                print(f'::warning::컨센 조회 실패 {code}: {str(exc)[:120]}')
                return None
        return naver_cache[day_key]

    # 무료 티어 분당 한도를 지키는 호출 간격(Gemini flash 10 RPM)
    delay = float(os.getenv('AI_CALL_DELAY', '6.5' if provider == 'gemini' else '0.5'))
    model_queue = gemini_model_queue() if provider == 'gemini' else [None]
    model_idx = 0
    done, errors, started = 0, 0, time.monotonic()
    last_error = ''

    def next_gemini_model():
        # 아직 한 건도 성공 못 했을 때만 후보 모델을 순차 시도한다(무료 쿼터가 세대별로 다름).
        nonlocal model_idx
        if analyze is gemini_analyze and done == 0 and model_idx + 1 < len(model_queue):
            model_idx += 1
            print(f'::warning::모델 전환 시도 → {model_queue[model_idx]}')
            return True
        return False

    try:
        for report in pending[:max_reports]:
            if time.monotonic() - started > budget_seconds: break
            try:
                raw, model = analyze(build_input_text(report, messages_by_id, pdf_cache),
                                     model_queue[model_idx])
                result = normalize_result(raw)
                entry = {'analyzed_at': datetime.now(timezone.utc).isoformat(),
                         'model': model, 'result': result}
                # 인제스트 시점의 시장 컨센 스냅샷(네이버). 과거 리포트에 지금 컨센을 붙이면
                # 왜곡이라 최근 45일 내 리포트에만 저장한다. 코드별 하루 1회 조회.
                code = result['code'] or report.get('code') or ''
                if code and report['date'] >= recent_cut:
                    entry['consensus'] = get_consensus(code)
                cache[str(report['id'])] = entry
                done += 1
            except QuotaExhaustedError as exc:
                last_error = str(exc)[:1400]
                if next_gemini_model(): continue
                # Gemini 일일 한도 소진 시 OpenAI 키가 있으면 남은 물량을 폴백으로 계속 처리한다.
                if analyze is gemini_analyze and os.getenv('OPENAI_API_KEY', '').strip():
                    print('::warning::Gemini 한도 소진 — OpenAI 폴백으로 전환')
                    analyze, delay, provider = openai_analyze, float(os.getenv('AI_CALL_DELAY', '0.5')), 'gemini→openai'
                    continue
                print(f'::warning::{exc}'); break
            except Exception as exc:  # 개별 실패는 건너뛰고 다음 런에서 재시도
                if '404' in str(exc) and next_gemini_model(): continue
                errors += 1
                last_error = str(exc)[:300]
                print(f"::warning::report {report['id']} 분석 실패: {str(exc)[:200]}")
                if errors >= 8: break
            time.sleep(delay)
    finally:
        if done:
            AI_CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        if naver_changed:
            NAVER_CACHE.write_text(json.dumps(naver_cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        status = {'ran_at': datetime.now(timezone.utc).isoformat(), 'provider': provider,
                  'analyzed': done, 'errors': errors, 'cached_total': len(cache),
                  'pending_left': len(pending) - done, 'model_tried': model_queue[model_idx] if provider != 'openai' else 'openai', 'last_error': last_error}
        (DATA_DIR / 'ai_run_status.json').write_text(
            json.dumps(status, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'provider': provider, 'analyzed': done, 'errors': errors,
                      'cached_total': len(cache), 'pending_left': len(pending) - done}, ensure_ascii=False))
    return cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max', type=int, default=int(os.getenv('AI_MAX_REPORTS', '100')))
    ap.add_argument('--since', default=os.getenv('AI_SINCE', ''))
    ap.add_argument('--budget-seconds', type=int, default=int(os.getenv('AI_BUDGET_SECONDS', '1500')))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if args.dry_run:
        cache = load_json(AI_CACHE, {})
        pending = [r for r in flat_reports(load_json(HISTORY, {'months': []}))
                   if (str(r['id']) not in cache or 'estimates' not in (cache.get(str(r['id']), {}).get('result') or {}))
                   and (not args.since or r['month'] >= args.since)]
        print(json.dumps({'provider': ai_provider(), 'cached': len(cache), 'pending': len(pending)}, ensure_ascii=False))
        return
    run(args.max, args.since, args.budget_seconds)


if __name__ == '__main__':
    main()
