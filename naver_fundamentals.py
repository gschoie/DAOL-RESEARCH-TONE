"""다올 커버 종목의 연간 재무·밸류 지표(네이버 금융) 수집 → data/naver_fundamentals.json

챗봇 기업 카드(차트 아래 표)와 get_fundamentals 도구가 쓴다.
naver_consensus.py와 같은 무토큰 모바일 API를 사용:
  - /api/stock/{code}/finance/annual : 매출액·영업이익·순이익·영업이익률·ROE·PER·PBR (연도별, E 포함)
  - /api/stock/{code}/basic          : 시가총액(PSR 계산용)
EV/EBITDA는 이 API에 없으므로 행이 발견될 때만 담는다(없으면 null).
하루 1회면 충분하므로 캐시가 20시간 이내면 수집을 건너뛴다(FORCE_FUNDA=1로 강제).
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent / 'data'
OUT = DATA / 'naver_fundamentals.json'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
      'Referer': 'https://m.stock.naver.com/'}
API = 'https://m.stock.naver.com/api/stock/{code}/{path}'
YEARS = ('2025', '2026')
FRESH_HOURS = 20


def _num(x):
    s = str(x if x is not None else '').replace(',', '').strip()
    if s in ('', '-', 'N/A', 'nan', 'None'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_annual(payload):
    """financeInfo에서 연도(2025/2026)별 지표를 뽑는다. 반환: {year: {필드: 값, 'est': bool}}"""
    fi = (payload or {}).get('financeInfo') or {}
    titles = fi.get('trTitleList') or []
    rows = fi.get('rowList') or []

    def row_val(key, *names, exact=True):
        for r in rows:
            t = str(r.get('title', '')).strip()
            for name in names:
                if (exact and t == name) or (not exact and t.startswith(name)):
                    return _num((r.get('columns') or {}).get(key, {}).get('value'))
        return None

    out = {}
    for t in titles:
        year = str(t.get('title', ''))[:4]
        if year not in YEARS:
            continue
        key = t.get('key')
        est = t.get('isConsensus') == 'Y'
        rec = {
            'revenue': row_val(key, '매출액'),
            'op': row_val(key, '영업이익'),
            'opm': row_val(key, '영업이익률', exact=False),
            'ni': row_val(key, '당기순이익'),
            'roe': row_val(key, 'ROE', exact=False),
            'per': row_val(key, 'PER', exact=False),
            'pbr': row_val(key, 'PBR', exact=False),
            'ev_ebitda': row_val(key, 'EV/EBITDA', exact=False),
            'est': est,
        }
        if rec['opm'] is None and rec['op'] is not None and rec['revenue']:
            rec['opm'] = round(rec['op'] / rec['revenue'] * 100, 1)
        # 같은 연도가 확정·추정으로 중복되면 추정(E)이 아닌 쪽을 우선하되, 값이 빈 쪽은 버린다
        if year in out and rec['revenue'] is None:
            continue
        out[year] = rec
    return out


def find_market_cap(obj):
    """basic 응답에서 시가총액(억원)을 키 이름으로 탐색 — 응답 구조 변화에 방어적."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in ('marketvalue', 'marketsum', 'marketcap'):
                    n = _num(v)
                    if n:
                        return n
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def main():
    if OUT.is_file() and os.getenv('FORCE_FUNDA', '') != '1':
        try:
            prev_meta = json.loads(OUT.read_text(encoding='utf-8'))
            fetched = datetime.fromisoformat(prev_meta.get('fetched_at'))
            age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
            if age_h < FRESH_HOURS:
                print(f'fundamentals: 캐시 신선({age_h:.1f}h) — 수집 생략')
                return
        except Exception:
            pass

    tone = json.loads((DATA / 'daol_tone_v2.json').read_text(encoding='utf-8'))
    codes = [(c, (e or {}).get('name') or c) for c, e in (tone.get('companies') or {}).items()
             if isinstance(e, dict) and len(c) == 6 and c.isdigit()]
    prev = {}
    if OUT.is_file():
        try:
            prev = json.loads(OUT.read_text(encoding='utf-8')).get('companies') or {}
        except Exception:
            prev = {}

    s = requests.Session()
    companies, ok, fail = {}, 0, 0
    for code, name in codes:
        try:
            annual = s.get(API.format(code=code, path='finance/annual'), headers=UA, timeout=10).json()
            years = parse_annual(annual)
            if not years:
                raise ValueError('연도 데이터 없음')
            mcap = None
            try:
                basic = s.get(API.format(code=code, path='basic'), headers=UA, timeout=10).json()
                mcap = find_market_cap(basic)
            except Exception:
                pass
            for y, rec in years.items():
                rec['psr'] = round(mcap / rec['revenue'], 2) if mcap and rec.get('revenue') else None
            companies[code] = {'name': name, 'market_cap': mcap, 'years': years}
            ok += 1
        except Exception as exc:
            fail += 1
            if code in prev:  # 이번에 실패해도 지난 값 유지
                companies[code] = prev[code]
            if fail <= 5:
                print(f'::warning::{name}({code}) 수집 실패: {type(exc).__name__}')
        time.sleep(0.25)

    if ok < max(5, len(codes) // 4):  # 대량 실패 시 기존 파일을 덮어쓰지 않는다
        print(f'::warning::수집 성공 {ok}/{len(codes)} — 너무 적어 기존 캐시 유지')
        return
    OUT.write_text(json.dumps({
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'source': 'NAVER FINANCE (m.stock.naver.com)',
        'unit_note': '매출액·영업이익·순이익·시가총액: 억원 / 영업이익률·ROE: % / PER·PBR·PSR·EV/EBITDA: 배',
        'companies': companies,
    }, ensure_ascii=False), encoding='utf-8')
    print(f'fundamentals: {ok}건 수집, {fail}건 실패(이전값 유지 포함), 총 {len(companies)}건 저장')


if __name__ == '__main__':
    main()
