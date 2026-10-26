"""전략·매크로·채권 자료 → LLM 구조화(코스피 전망·섹터 콜·금리·환율·변수·멀티플).

기업/산업 톤 분석(ai_report_analyzer)은 TP·투자포인트 중심이라 전략 자료가 담는 정보
(지수 밴드, 주도·선호 섹터, 미국·한국 금리 경로, 시장 변수, 시장 멀티플)를 담지 못한다.
전략 자료만 따로 골라 전용 스키마로 뽑고 data/daol_strategy_ai.json 에 리포트 id 기준
캐시한다(증분). build_daol_tone_v2가 이 캐시를 읽어 v2 JSON의 'strategy' 블록을 만든다.

단독 실행:
  python ai_strategy_analyzer.py --max 40
  python ai_strategy_analyzer.py --dry-run
"""
import argparse, json, os, re, time, urllib.error
from datetime import datetime, timezone
from pathlib import Path

from ai_report_analyzer import (GEMINI_API_TEMPLATE, QuotaExhaustedError, _g, _post_json,
                                build_input_text, flat_reports, gemini_model_queue, load_json)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
HISTORY = DATA_DIR / 'daol_tone_history.json'
MSG_FILE = DATA_DIR / 'daol_messages.json'
PDF_CACHE = DATA_DIR / 'daol_pdf_text_cache.json'
V2_FILE = DATA_DIR / 'daol_tone_v2.json'
OUT = DATA_DIR / 'daol_strategy_ai.json'

SCHEMA_VERSION = 1
STRATEGY_ANALYSTS = {'투자전략팀', '조병현', '허정인'}
KINDS = ['시장전망', '주간시황', '매크로이슈', '통화정책리뷰', '채권·금리', '경제지표리뷰', '기타']
VIEWS = ['강세', '중립', '약세', '언급없음']
KOSPI_DIRS = ['상향', '하향', '유지', '신규', '언급없음']
POLICY = ['인상', '동결', '인하', '언급없음']
FX_DIRS = ['원화강세', '원화약세', '보합', '언급없음']
EARN_DIRS = ['상향', '하향', '유지', '언급없음']
EFFECTS = ['긍정', '부정', '중립']
# 변수 라벨을 고정 어휘로 모아야 '시장 변수 트래커'에서 자료 간 히스토리가 이어진다.
VAR_LABELS = ['Fed 통화정책', '한국은행', '미국 금리', '인플레이션', '미국 경기', '고용',
              '환율', '유가', '관세·통상', '중국 경기', '반도체 사이클', 'AI 투자', '기업이익',
              '외국인 수급', '유동성', '지정학', '정치·선거', '재정', '신용 리스크', '밸류업·정책']

SYSTEM_PROMPT = f'''당신은 한국 증권사(다올투자증권) 투자전략·매크로·채권 리서치를 구조화하는 전문가다.
보고서(텔레그램 요약 + PDF 원문 발췌)를 읽고 JSON만 출력한다. 본문에 없는 내용은 만들지 말고
"언급없음"/null/빈 배열로 둔다. 모든 텍스트는 한국어, 숫자는 숫자형.

- kind: {KINDS} 중 하나. 주간 시황(Market Weekly 등)=주간시황, FOMC·금통위 리뷰=통화정책리뷰,
  CPI·고용 등 지표 리뷰=경제지표리뷰, 반기/연간 전망·전략=시장전망.
- market_view: 주식시장에 대한 이 자료의 전반적 시각 {VIEWS}. one_line: 핵심 결론 한 문장(70자 이내).
- kospi: 코스피 지수 전망. low/high=제시한 밴드 하단/상단(pt), target=목표·적정 지수(pt).
  '지수 하단 5,210pt'처럼 한쪽만 있으면 그쪽만. basis=산출 근거(예: "TTM PBR 1.3배", "12M Fwd PER 11배"),
  horizon=기간(예: "4분기", "26년 하반기", "단기"). direction=직전 전망 대비 {KOSPI_DIRS}
  (본문에 상향/하향 표현이 없으면 첫 제시는 "신규", 판단 불가면 "언급없음"). quote=근거 원문(120자 이내).
- week: 주간 시황일 때만. kospi_pct/kosdaq_pct=금주 등락률(%), up/down=금주 상승·하락 상위 업종(최대 3).
- sectors: 애널리스트의 "판단"만. leading=시장 주도 섹터로 본 것, prefer=비중확대·선호·단기 매수 권고,
  avoid=비선호·축소 권고. 단순히 그 주에 오르내린 업종 나열은 넣지 말 것. quote=근거 원문(120자 이내).
- rates.us / rates.kr: policy=향후 정책금리 방향 {POLICY}, path=경로 서술(예: "연내 2회 인상, 최종 3.50%"),
  yield=국채금리 전망 서술(예: "10년물 5.0~5.3% 등락"). quote=근거 원문(120자 이내).
- fx: usdkrw=원/달러 전망 서술(레벨·밴드), direction={FX_DIRS}.
- earnings: 코스피(시장) 이익 전망 방향 {EARN_DIRS}, note=요지(80자 이내).
- multiple: 시장 밸류에이션. per/pbr=본문에 제시된 코스피 PER/PBR 수치(배), note=맥락(예: "12M Fwd PER 역사적 상단").
- variables: 시장에 영향을 주는 변수 최대 6개. label은 가급적 다음 중에서 고른다:
  {VAR_LABELS} (해당 없으면 10자 이내 새 라벨). effect=주식시장에 {EFFECTS}, note=요지(70자 이내).
- flows: 수급(외국인·기관·개인) 서술 한 문장, 없으면 빈 문자열.'''


def _q(): return _g('STRING')
STRATEGY_SCHEMA = _g('OBJECT', properties={
    'kind': _g('STRING', enum=KINDS),
    'market_view': _g('STRING', enum=VIEWS),
    'one_line': _g('STRING'),
    'kospi': _g('OBJECT', properties={
        'low': _g('NUMBER', nullable=True), 'high': _g('NUMBER', nullable=True),
        'target': _g('NUMBER', nullable=True), 'basis': _g('STRING'), 'horizon': _g('STRING'),
        'direction': _g('STRING', enum=KOSPI_DIRS), 'quote': _q()},
        required=['low', 'high', 'target', 'basis', 'horizon', 'direction', 'quote']),
    'week': _g('OBJECT', properties={
        'kospi_pct': _g('NUMBER', nullable=True), 'kosdaq_pct': _g('NUMBER', nullable=True),
        'up': _g('ARRAY', items=_g('STRING')), 'down': _g('ARRAY', items=_g('STRING'))},
        required=['kospi_pct', 'kosdaq_pct', 'up', 'down']),
    'sectors': _g('OBJECT', properties={
        'leading': _g('ARRAY', items=_g('STRING')), 'prefer': _g('ARRAY', items=_g('STRING')),
        'avoid': _g('ARRAY', items=_g('STRING')), 'quote': _q()},
        required=['leading', 'prefer', 'avoid', 'quote']),
    'rates': _g('OBJECT', properties={
        'us': _g('OBJECT', properties={'policy': _g('STRING', enum=POLICY), 'path': _q(), 'yield': _q(), 'quote': _q()},
                 required=['policy', 'path', 'yield', 'quote']),
        'kr': _g('OBJECT', properties={'policy': _g('STRING', enum=POLICY), 'path': _q(), 'yield': _q(), 'quote': _q()},
                 required=['policy', 'path', 'yield', 'quote'])},
        required=['us', 'kr']),
    'fx': _g('OBJECT', properties={'usdkrw': _q(), 'direction': _g('STRING', enum=FX_DIRS), 'quote': _q()},
             required=['usdkrw', 'direction', 'quote']),
    'earnings': _g('OBJECT', properties={'direction': _g('STRING', enum=EARN_DIRS), 'note': _q()},
                   required=['direction', 'note']),
    'multiple': _g('OBJECT', properties={'per': _g('NUMBER', nullable=True), 'pbr': _g('NUMBER', nullable=True),
                                         'note': _q()}, required=['per', 'pbr', 'note']),
    'variables': _g('ARRAY', items=_g('OBJECT', properties={
        'label': _q(), 'effect': _g('STRING', enum=EFFECTS), 'note': _q()}, required=['label', 'effect', 'note'])),
    'flows': _q(),
}, required=['kind', 'market_view', 'one_line', 'kospi', 'week', 'sectors', 'rates', 'fx',
             'earnings', 'multiple', 'variables', 'flows'])


def _num(x, lo=None, hi=None):
    try:
        v = None if x is None else float(x)
    except (TypeError, ValueError):
        return None
    if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


# 모델이 "없음"을 빈 문자열 대신 말로 적는 경우가 많다("언급없음 · 언급없음" 줄이 화면에 남는다)
NOISE = {'언급없음', '없음', '해당없음', '해당 없음', '언급 없음', 'n/a', 'na', 'null', 'none', '-', '—'}
def _s(x, n):
    t = str(x or '').strip()
    return '' if t.lower() in NOISE else t[:n]
def _enum(x, allowed, default): return x if x in allowed else default
def _list(xs, n=4, w=16): return [_s(x, w) for x in (xs or []) if str(x or '').strip()][:n]


def normalize(raw):
    """형 안정화 + 범위 방어(코스피 1,000~20,000pt, 멀티플 0~60배, 주간 등락 ±40%)."""
    k = raw.get('kospi') or {}
    low, high, target = (_num(k.get('low'), 1000, 20000), _num(k.get('high'), 1000, 20000),
                         _num(k.get('target'), 1000, 20000))
    if low and high and low > high:
        low, high = high, low
    w = raw.get('week') or {}
    sec = raw.get('sectors') or {}
    rates = raw.get('rates') or {}
    def rate(side):
        r = rates.get(side) or {}
        return {'policy': _enum(r.get('policy'), POLICY, '언급없음'), 'path': _s(r.get('path'), 80),
                'yield': _s(r.get('yield'), 80), 'quote': _s(r.get('quote'), 160)}
    fx = raw.get('fx') or {}
    earn = raw.get('earnings') or {}
    mult = raw.get('multiple') or {}
    return {
        'kind': _enum(raw.get('kind'), KINDS, '기타'),
        'market_view': _enum(raw.get('market_view'), VIEWS, '언급없음'),
        'one_line': _s(raw.get('one_line'), 120),
        'kospi': {'low': low, 'high': high, 'target': target, 'basis': _s(k.get('basis'), 40),
                  'horizon': _s(k.get('horizon'), 20),
                  'direction': _enum(k.get('direction'), KOSPI_DIRS, '언급없음'), 'quote': _s(k.get('quote'), 160)},
        'week': {'kospi_pct': _num(w.get('kospi_pct'), -40, 40), 'kosdaq_pct': _num(w.get('kosdaq_pct'), -40, 40),
                 'up': _list(w.get('up'), 3), 'down': _list(w.get('down'), 3)},
        'sectors': {'leading': _list(sec.get('leading')), 'prefer': _list(sec.get('prefer'), 5),
                    'avoid': _list(sec.get('avoid')), 'quote': _s(sec.get('quote'), 160)},
        'rates': {'us': rate('us'), 'kr': rate('kr')},
        'fx': {'usdkrw': _s(fx.get('usdkrw'), 60), 'direction': _enum(fx.get('direction'), FX_DIRS, '언급없음'),
               'quote': _s(fx.get('quote'), 160)},
        'earnings': {'direction': _enum(earn.get('direction'), EARN_DIRS, '언급없음'), 'note': _s(earn.get('note'), 100)},
        'multiple': {'per': _num(mult.get('per'), 0, 60), 'pbr': _num(mult.get('pbr'), 0, 10), 'note': _s(mult.get('note'), 80)},
        'variables': [{'label': _s(v.get('label'), 12), 'effect': _enum(v.get('effect'), EFFECTS, '중립'),
                       'note': _s(v.get('note'), 90)}
                      for v in (raw.get('variables') or []) if isinstance(v, dict) and str(v.get('label') or '').strip()][:6],
        'flows': _s(raw.get('flows'), 120),
    }


def gemini_strategy(user_text, model):
    url = GEMINI_API_TEMPLATE.format(model=model) + f"?key={os.environ['GEMINI_API_KEY'].strip()}"
    body = {'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_text}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': STRATEGY_SCHEMA,
                                 'temperature': 0.1}}
    for attempt in range(3):
        try:
            payload = _post_json(url, body, {}, timeout=120)
            return json.loads(payload['candidates'][0]['content']['parts'][0]['text'])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            if exc.code == 429 and ('PerDay' in detail or 'RESOURCE_EXHAUSTED' in detail and 'daily' in detail.lower()):
                raise QuotaExhaustedError(f'Gemini daily quota exhausted: {detail[:600]}') from exc
            if exc.code == 429 and attempt < 2:
                time.sleep(30); continue
            # 500/503(혼잡)은 일시적 — 실패로 세지 말고 잠시 쉬었다 재시도(첫 백필 런에서 5건을 날렸다)
            if exc.code in (500, 502, 503) and attempt < 2:
                time.sleep(20); continue
            raise RuntimeError(f'Gemini API error {exc.code}: {detail[:300]}') from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < 2:
                time.sleep(10); continue
            raise RuntimeError(f'Gemini network error: {exc}') from exc
    raise RuntimeError('Gemini retry loop exited unexpectedly')


def strategy_reports():
    """전략 자료 선별: 전략 애널(투자전략팀·조병현·허정인) 또는 직전 v2 빌드가 '전략·시황'으로 귀속한 자료."""
    prev_sector = {}
    for c in (load_json(V2_FILE, {}).get('companies') or {}).values():
        for r in c.get('timeline') or []:
            if not r.get('bundled'):
                prev_sector[str(r.get('id'))] = r.get('sector')
    out = []
    for r in flat_reports(load_json(HISTORY, {'months': []})):
        if r['analyst'] in STRATEGY_ANALYSTS or prev_sector.get(str(r['id'])) == '전략·시황':
            out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max', type=int, default=int(os.getenv('AI_STRATEGY_MAX', '40')))
    ap.add_argument('--budget-seconds', type=int, default=int(os.getenv('AI_STRATEGY_BUDGET', '420')))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    cache = load_json(OUT, {})
    reports = strategy_reports()
    pending = [r for r in reports
               if (cache.get(str(r['id'])) or {}).get('v') != SCHEMA_VERSION]
    print(f'전략 자료 {len(reports)}건 · 구조화 대기 {len(pending)}건 (이번 런 최대 {args.max})')
    if args.dry_run or not pending:
        return
    if not os.getenv('GEMINI_API_KEY', '').strip():
        print('GEMINI_API_KEY 없음 — 전략 구조화 생략')
        return

    messages_by_id = {str(m['id']): m for m in load_json(MSG_FILE, [])}
    pdf_cache = load_json(PDF_CACHE, {})
    # 지수 밴드·금리 수치를 정확히 읽는 게 핵심이라 lite 계열은 후순위
    base = gemini_model_queue()
    queue = [m for m in base if 'lite' not in m] + [m for m in base if 'lite' in m]
    model_idx, done, errors, started = 0, 0, 0, time.monotonic()
    delay = float(os.getenv('AI_CALL_DELAY', '6.5'))
    try:
        todo = pending[:args.max]; i = 0
        while i < len(todo):
            r = todo[i]
            if time.monotonic() - started > args.budget_seconds:
                print('예산 소진 — 다음 런에서 계속'); break
            try:
                raw = gemini_strategy(build_input_text(r, messages_by_id, pdf_cache), queue[model_idx])
                cache[str(r['id'])] = {'v': SCHEMA_VERSION, 'model': queue[model_idx],
                                       'analyzed_at': datetime.now(timezone.utc).isoformat(),
                                       'result': normalize(raw)}
                done += 1; i += 1
            except QuotaExhaustedError as exc:
                # 일일 한도는 모델별로 따로다(3.6-flash는 무료 티어 RPD가 작아 런당 1~13건에서
                # 끊겼다) — 성공 여부와 무관하게 다음 모델로 내려가 같은 자료를 다시 시도한다.
                if model_idx + 1 < len(queue):
                    model_idx += 1; print(f'::warning::모델 한도 소진 → {queue[model_idx]}로 전환'); continue
                print(f'::warning::{str(exc)[:200]}'); break
            except Exception as exc:
                # 폐기된 모델(404)·요청 거부(400)는 자료가 아니라 모델 문제 — 성공 여부와 무관하게 다음 모델로
                if ('404' in str(exc) or '400' in str(exc)) and model_idx + 1 < len(queue):
                    model_idx += 1; print(f'::warning::모델 전환 → {queue[model_idx]}'); continue
                errors += 1; i += 1
                print(f"::warning::전략 {r['id']} 구조화 실패: {str(exc)[:160]}")
                if errors >= 6: break
            time.sleep(delay)
    finally:
        if done:
            OUT.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({'strategy_analyzed': done, 'errors': errors, 'cached': len(cache),
                      'pending_left': len(pending) - done, 'model': queue[model_idx]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
