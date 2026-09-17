"""가비지 PDF 캐시 백필 배치 — status='pdf'인데 한글이 거의 없는 껍데기 텍스트 재수집.

원인: 일부 PDF는 본문 폰트가 텍스트 추출기에 안 읽혀 그림 캡션만 남은 채
'성공'으로 캐시됐다(2026-09 조사: 758건 중 372건). 이 엔트리들은 단축링크
차단(buly.kr)과 무관한 직링크(final_url)를 갖고 있어 재수집이 가능하다.

저녁 런/수동 실행에서 배치로 돈다: 런당 BACKFILL_MAX건·BACKFILL_BUDGET_SECONDS 안에서
직링크로 다시 받아 개선된 추출기(build_daol_tone_dashboard.extract_text_from_pdf,
OCR 3페이지 폴백 포함)로 교체한다. 최근 리포트 우선(메타 인덱스의 날짜 역순).
"""
import json
import os
import re
import shutil
import time
from pathlib import Path

from build_daol_tone_dashboard import _fetch_pdf_bytes, _kr, extract_text_from_pdf

DATA = Path(__file__).resolve().parent / 'data'
CACHE_FILE = DATA / 'daol_pdf_text_cache.json'
MSG_FILE = DATA / 'daol_messages.json'
GARBAGE_KR = 200      # 이 미만이면 껍데기로 간주
MAX_ITEMS = int(os.getenv('BACKFILL_MAX', '40'))
BUDGET = int(os.getenv('BACKFILL_BUDGET_SECONDS', '600'))


def report_dates():
    """source_url → 리포트 날짜(메시지 날짜) — 최근 자료 우선 처리용."""
    try:
        msgs = json.loads(MSG_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}
    items = msgs if isinstance(msgs, list) else msgs.get('messages') or []
    out = {}
    for m in items:
        if not isinstance(m, dict):
            continue
        d = str(m.get('date', ''))[:10]
        for url in re.findall(r'https?://\S+', str(m.get('text', ''))):
            out.setdefault(url.rstrip(').,'), d)
    return out


def main():
    # 폰트 깨진 PDF 복구는 사실상 OCR 의존 — tesseract가 없는 런(FAST 모드)에서 돌면
    # 전부 '개선불가'로 오판정하고 표식이 붙는 사고가 난다(2026-09-17 실측). 없으면 그냥 건너뛴다.
    if not shutil.which('tesseract'):
        print('backfill: tesseract 미설치(FAST 런?) — 생략')
        return
    cache = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
    # 과거 OCR 없이 돌았던 런이 잘못 붙인 표식은 지워 재시도 대상으로 되돌린다(1회성 마이그레이션).
    for v in cache.values():
        if isinstance(v, dict) and v.get('error') == 'backfill: no better text':
            v['error'] = ''
    dates = report_dates()
    targets = []
    for url, v in cache.items():
        if not isinstance(v, dict) or v.get('status') != 'pdf':
            continue
        if _kr(v.get('text') or '') >= GARBAGE_KR:
            continue
        if str(v.get('error') or '').startswith('backfill:'):
            continue  # 지난 백필에서 OCR로도 개선 불가 판정 — 반복 시도 방지
        direct = v.get('final_url')
        if not direct or direct == url:
            continue  # 직링크가 없으면 단축링크 재시도라 차단에 걸린다 — 건너뜀
        targets.append((dates.get(url, ''), url, direct))
    targets.sort(reverse=True)  # 최근 자료 우선
    print(f'backfill: 대상 {len(targets)}건 (한글<{GARBAGE_KR}·직링크 보유) — 이번 런 최대 {MAX_ITEMS}건/{BUDGET}s')

    started = time.monotonic()
    fixed = still = fail = 0
    for date, url, direct in targets[:MAX_ITEMS]:
        if time.monotonic() - started > BUDGET:
            print('예산 소진 — 다음 런에서 계속')
            break
        try:
            content, final, code = _fetch_pdf_bytes(direct)
            if code >= 400:
                raise RuntimeError(f'HTTP {code}')
            text = extract_text_from_pdf(content)
            old_kr = _kr(cache[url].get('text') or '')
            if _kr(text) > old_kr:
                cache[url] = {'status': 'pdf', 'final_url': final, 'text': text, 'error': ''}
                fixed += 1
                print(f'  복구 {date} {url[:40]} 한글 {old_kr}→{_kr(text)}')
            else:
                still += 1  # OCR까지 시도해도 개선 안 됨 — 재시도해도 같으니 표식을 남겨 다음 런에서 제외
                cache[url]['error'] = 'backfill: no better text (ocr)'
        except Exception as exc:
            fail += 1
            print(f'  실패 {date} {url[:40]} {type(exc).__name__}: {str(exc)[:80]}')
        time.sleep(0.3)

    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'backfill 결과: 복구 {fixed} / 개선불가 {still} / 실패 {fail}')


if __name__ == '__main__':
    main()
