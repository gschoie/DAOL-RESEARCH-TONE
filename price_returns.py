"""이벤트 사후 주가 검증용 일별 종가 캐시.

최근 150일 내 기업 리포트가 있는 종목의 일별 종가를 네이버 시세 API로 받아
data/price_close_cache.json 에 저장한다(증분 — 마지막 캐시 일자 이후만 재조회).
v2 빌더는 이 캐시만 읽어 네트워크 없이 +5/+20영업일 수익률을 계산한다.

캐시 구조: {code: {"updated": "YYYY-MM-DD", "closes": [["YYYY-MM-DD", close], ...(날짜 오름차순)]}}
"""
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
HISTORY = DATA_DIR / 'daol_tone_history.json'
CACHE = DATA_DIR / 'price_close_cache.json'

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
      'Referer': 'https://finance.naver.com/'}
PRICE_API = ('https://api.finance.naver.com/siseJson.naver?symbol={code}'
             '&requestType=1&startTime={s}&endTime={e}&timeframe=day')
LOOKBACK_DAYS = 150


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def recent_codes(history, cutoff):
    codes = {}
    for month in history.get('months', []):
        for group in month.get('analysts', []):
            for r in group.get('reports', []):
                code = r.get('code') or ''
                if re.fullmatch(r'[0-9]{6}', code) and r['date'] >= cutoff:
                    codes[code] = max(codes.get(code, ''), r['date'])
    return codes


def bundled_codes(cutoff):
    """직전 빌드 v2 산출물의 합성(bundled) 레코드 종목코드 — 인뎁스 종목 페이지로만
    커버되는 종목(예: 은행주)은 history에 기업자료가 없어 recent_codes에 안 잡히고,
    그러면 상세 모달의 종가 오버레이 차트가 비게 된다. 이 스크립트가 v2 빌드보다
    먼저 돌아 전 런 산출물을 읽으므로 신규 종목은 최악 1런 지연으로 채워진다."""
    try:
        v2 = json.loads((DATA_DIR / 'daol_tone_v2.json').read_text(encoding='utf-8'))
    except Exception:
        return {}
    out = {}
    stack = [v2]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            code = o.get('code') or ''
            if o.get('bundled') and re.fullmatch(r'[0-9]{6}', code) and o.get('date', '') >= cutoff:
                out[code] = max(out.get(code, ''), o['date'])
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return out


def fetch_closes(code, start, end, session):
    url = PRICE_API.format(code=code, s=start.strftime('%Y%m%d'), e=end.strftime('%Y%m%d'))
    txt = session.get(url, headers=UA, timeout=10).text.strip()
    rows = json.loads(txt.replace("'", '"'))
    out = []
    for row in rows[1:]:
        if not row or row[4] in (None, '', 0):
            continue
        d = str(row[0])
        out.append([f'{d[:4]}-{d[4:6]}-{d[6:8]}', float(row[4])])
    return out


def refresh(max_codes=200):
    today = datetime.now(timezone(timedelta(hours=9))).date()  # 국내 증시 기준 KST
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    history = load_json(HISTORY, {'months': []})
    cache = load_json(CACHE, {})
    codes = recent_codes(history, cutoff)
    for code, d in bundled_codes(cutoff).items():
        codes[code] = max(codes.get(code, ''), d)
    session = requests.Session()
    fetched = errors = 0
    for code in sorted(codes)[:max_codes]:
        entry = cache.get(code) or {}
        if entry.get('updated') == today.isoformat():
            continue
        # 증분: 캐시 마지막 일자 이후만 받아 이어붙인다(중복 일자는 새 값으로 교체)
        have = {d: c for d, c in entry.get('closes', [])}
        start = today - timedelta(days=LOOKBACK_DAYS + 30)
        if have:
            start = max(start, date.fromisoformat(max(have)) - timedelta(days=3))
        try:
            for d, c in fetch_closes(code, start, today, session):
                have[d] = c
            floor = (today - timedelta(days=LOOKBACK_DAYS + 40)).isoformat()
            cache[code] = {'updated': today.isoformat(),
                           'closes': sorted([d, c] for d, c in have.items() if d >= floor)}
            fetched += 1
        except Exception as exc:
            errors += 1
            print(f'::warning::시세 조회 실패 {code}: {str(exc)[:100]}')
            if errors >= 10:
                break
        time.sleep(0.25)
    # 코스피 지수 — 전략 보드 '지수 전망 vs 실제' 차트용. 전략 자료가 2025-05부터라 종목보다
    # 길게(약 1.5년) 보관한다. 실패해도 차트에 실제 지수 선만 빠진다.
    try:
        entry = cache.get('KOSPI') or {}
        if entry.get('updated') != today.isoformat():
            have = {d: c for d, c in entry.get('closes', [])}
            start = date(2025, 5, 1) if not have else date.fromisoformat(max(have)) - timedelta(days=3)
            for d, c in fetch_closes('KOSPI', start, today, session):
                have[d] = c
            floor = (today - timedelta(days=560)).isoformat()
            cache['KOSPI'] = {'updated': today.isoformat(),
                              'closes': sorted([d, c] for d, c in have.items() if d >= floor)}
    except Exception as exc:
        print(f'::warning::코스피 지수 조회 실패: {str(exc)[:100]}')
    # 커버가 끝난 종목의 캐시는 자연 유지(무해) — 파일 크기만 정리 목적이면 codes 밖 항목 제거
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({'codes': len(codes), 'fetched': fetched, 'errors': errors,
                      'cached_total': len(cache)}, ensure_ascii=False))


if __name__ == '__main__':
    refresh()
