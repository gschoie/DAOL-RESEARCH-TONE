"""부실 PDF의 Gemini 직접 판독(AI 레스큐) — 텍스트 추출·OCR 둘 다 실패한 캐시 구제.

폰트가 통째로 깨진 PDF(예: 2026-07-06 자동차 프리뷰)는 백필 OCR로도 글자가 뭉개져
규칙 기반 TP 추출(extract_bundled_tp)이 불가능하다. Gemini는 PDF를 inline_data로
받아 이미지처럼 직접 읽을 수 있으므로, 한글<200자 부실 캐시를 대상으로 원본 PDF를
통째로 판독시켜 (a) 검색용 전사 텍스트, (b) 종목별 적정주가 표를 구조화 추출한다.
결과는 캐시 엔트리에 text(전사)·ai_read(재시도 방지)·ai_tp(합성 레코드 재료)로 저장되고,
build_daol_tone_v2.build_bundled_records가 ai_tp를 소비한다.

저녁 런/수동 실행에서 배치로 돈다(무료 티어 쿼터 보호: 런당 AI_RESCUE_MAX건).
"""
import base64
import json
import os
import re
import time
import urllib.error
from pathlib import Path

from ai_report_analyzer import (GEMINI_API_TEMPLATE, HISTORY, QuotaExhaustedError, _g, _post_json,
                                flat_reports, gemini_model_queue, load_json)
from backfill_pdf_text import GARBAGE_KR, report_dates
from build_daol_tone_dashboard import _fetch_pdf_bytes, _kr
from build_daol_tone_v2 import extract_bundled_tp

DATA = Path(__file__).resolve().parent / 'data'
CACHE_FILE = DATA / 'daol_pdf_text_cache.json'
MAX_ITEMS = int(os.getenv('AI_RESCUE_MAX', '15'))
BUDGET = int(os.getenv('AI_RESCUE_BUDGET_SECONDS', '420'))
DIRECTIONS = ['상향', '하향', '유지', '신규']

PROMPT = '''이 PDF는 한국 증권사(다올투자증권) 리서치 보고서인데, 폰트 문제로 텍스트 추출이 실패해
원문이 비어 있다. PDF를 직접 읽고 JSON만 출력한다.

- transcript: 검색용 핵심 전사(한국어, 6000자 이내). 표지의 자료 제목·발간일·애널리스트,
  산업/기업 투자의견, 각 종목 섹션의 제목·핵심 논거, 주요 표(실적 추정·밸류에이션)의 숫자를
  일반 텍스트로 담는다. 그림 캡션만 나열하지 말 것.
- tp_changes: 이 자료에 나오는 "종목별 적정주가(목표주가)" 전부(표지·종목 페이지의
  적정주가 박스 기준). company=기업명, code=6자리 종목코드(확실치 않으면 빈 문자열),
  value=새 적정주가(원 단위 숫자), prior=직전 적정주가(원, 본문에 금액이 명시된 경우만 —
  비율로 역산하지 말 것), direction=[상향, 하향, 유지, 신규] 중 하나.
  종목 TP가 없는 산업 전망 자료면 빈 배열.'''

RESCUE_SCHEMA = _g('OBJECT', properties={
    'transcript': _g('STRING'),
    'tp_changes': _g('ARRAY', items=_g('OBJECT', properties={
        'company': _g('STRING'), 'code': _g('STRING'),
        'value': _g('NUMBER'), 'prior': _g('NUMBER', nullable=True),
        'direction': _g('STRING', enum=DIRECTIONS)},
        required=['company', 'code', 'value', 'direction'])),
}, required=['transcript', 'tp_changes'])


def gemini_read_pdf(pdf_bytes, model):
    """PDF 원본을 inline_data로 넘겨 전사+TP 구조화 추출. ai_report_analyzer의 재시도 규칙과 동일."""
    url = GEMINI_API_TEMPLATE.format(model=model) + f"?key={os.environ['GEMINI_API_KEY'].strip()}"
    body = {'contents': [{'role': 'user', 'parts': [
                {'text': PROMPT},
                {'inline_data': {'mime_type': 'application/pdf',
                                 'data': base64.b64encode(pdf_bytes).decode('ascii')}}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': RESCUE_SCHEMA,
                                 'temperature': 0.1}}
    for attempt in range(3):
        try:
            payload = _post_json(url, body, {}, timeout=180)
            return json.loads(payload['candidates'][0]['content']['parts'][0]['text'])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            if exc.code == 429 and ('PerDay' in detail or 'RESOURCE_EXHAUSTED' in detail and 'daily' in detail.lower()):
                raise QuotaExhaustedError(f'Gemini daily quota exhausted: {detail[:1200]}') from exc
            if exc.code == 429 and attempt < 2:
                time.sleep(30); continue
            raise RuntimeError(f'Gemini API error {exc.code}: {detail[:300]}') from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < 2:
                time.sleep(10); continue
            raise RuntimeError(f'Gemini network error: {exc}') from exc
    raise RuntimeError('Gemini retry loop exited unexpectedly')


def clean_tp_changes(raw):
    """모델 출력 검증: value>0, direction 화이트리스트, code는 6자리 숫자만 유지(아니면 빈값)."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict): continue
        try:
            value = float(item.get('value') or 0)
        except (TypeError, ValueError):
            continue
        direction = str(item.get('direction') or '')
        company = str(item.get('company') or '').strip()
        if value <= 0 or direction not in DIRECTIONS or not company: continue
        code = str(item.get('code') or '').strip()
        if not (len(code) == 6 and code.isdigit()): code = ''
        prior = item.get('prior')
        try:
            prior = float(prior) if prior else None
        except (TypeError, ValueError):
            prior = None
        if prior is not None and prior <= 0: prior = None
        # 방향과 값이 모순이면 방향 판독 오류 — prior를 버리고 단독값으로 강등하느니 통째로 스킵
        if prior is not None:
            if direction == '상향' and not value > prior: continue
            if direction == '하향' and not value < prior: continue
            if direction == '유지' and value != prior: continue
        out.append({'company': company, 'code': code, 'value': value,
                    'prior': prior, 'direction': direction})
    return out


def main():
    if not os.getenv('GEMINI_API_KEY', '').strip():
        print('ai-rescue: GEMINI_API_KEY 없음 — 생략')
        return
    cache = json.loads(CACHE_FILE.read_text(encoding='utf-8'))

    def direct_of(url, v):
        """판독 가능 조건: pdf 캐시·미판독·직링크(단축링크뿐이면 다운로드가 차단에 걸린다)."""
        if not isinstance(v, dict) or v.get('status') != 'pdf': return None
        if v.get('ai_read'): return None  # 이미 판독함 — 재시도 금지(쿼터 보호)
        direct = v.get('final_url')
        return direct if direct and direct != url else None

    # 1순위: 인뎁스·프리뷰 산업자료인데 규칙 TP 추출이 빈손인 자료 — 글자 수가 충분해도
    # OCR 글자가 뭉개져 표를 못 읽는 유형(현대차 7/6 프리뷰)이 여기 잡힌다. 최신순(flat_reports).
    prime, seen = [], set()
    for r in flat_reports(load_json(HISTORY, {'months': []})):
        if r.get('report_type') != '산업자료' or not r.get('source_url'): continue
        if not re.search(r'In-?Depth|인뎁스|Preview|프리뷰', r.get('title') or '', re.I): continue
        url = r['source_url']
        direct = direct_of(url, cache.get(url) or {})
        if not direct or url in seen: continue
        if extract_bundled_tp((cache.get(url) or {}).get('text') or ''): continue
        seen.add(url)
        prime.append((r.get('date', ''), url, direct))
    # 2순위: 껍데기 캐시(한글<GARBAGE_KR) 전반 — 검색용 전사 확보. 최근 자료 우선.
    dates = report_dates()
    rest = []
    for url, v in cache.items():
        direct = direct_of(url, v)
        if not direct or url in seen: continue
        if _kr(v.get('text') or '') >= GARBAGE_KR: continue
        rest.append((dates.get(url, ''), url, direct))
    rest.sort(reverse=True)
    targets = prime + rest
    print(f'ai-rescue: 대상 {len(targets)}건 (TP미검출 인뎁스 {len(prime)} + 껍데기 {len(rest)}) — 이번 런 최대 {MAX_ITEMS}건/{BUDGET}s')

    model_queue = gemini_model_queue()
    model_idx = 0
    started = time.monotonic()
    done = fail = 0
    changed = False
    for date, url, direct in targets[:MAX_ITEMS]:
        if time.monotonic() - started > BUDGET:
            print('예산 소진 — 다음 런에서 계속')
            break
        try:
            content, final, code = _fetch_pdf_bytes(direct)
            if code >= 400 or not content:
                raise RuntimeError(f'HTTP {code}')
            while True:
                try:
                    raw = gemini_read_pdf(content, model_queue[model_idx])
                    break
                except (QuotaExhaustedError, RuntimeError) as exc:
                    # 첫 성공 전에는 후보 모델을 순차 시도(무료 쿼터가 세대별로 다름)
                    if done == 0 and model_idx + 1 < len(model_queue):
                        model_idx += 1
                        print(f'::warning::모델 전환 시도 → {model_queue[model_idx]}')
                        continue
                    raise
            transcript = str(raw.get('transcript') or '').strip()[:8000]
            if _kr(transcript) < 150:
                raise RuntimeError(f'전사 부실(한글 {_kr(transcript)}자) — 캐시 미갱신')
            tp = clean_tp_changes(raw.get('tp_changes'))
            v = cache[url]
            v['text'] = transcript + '\n\n[AI판독: 텍스트 추출 실패 PDF를 Gemini가 직접 판독한 전사본]'
            v['ai_read'] = True
            v['ai_tp'] = tp
            v['error'] = ''
            if final: v['final_url'] = final
            changed = True
            done += 1
            print(f'  판독 {date} {url[:40]} 한글 {_kr(transcript)}자 / TP {len(tp)}건 ({model_queue[model_idx]})')
        except QuotaExhaustedError as exc:
            print(f'::warning::{exc}')
            break
        except Exception as exc:  # 개별 실패는 다음 런에서 재시도
            fail += 1
            print(f'  실패 {date} {url[:40]} {type(exc).__name__}: {str(exc)[:120]}')
            if fail >= 5: break
        time.sleep(float(os.getenv('AI_CALL_DELAY', '2')))

    if changed:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'ai-rescue 결과: 판독 {done} / 실패 {fail}')


if __name__ == '__main__':
    main()
