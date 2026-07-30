"""네이버 모바일 금융 API에서 시장 컨센서스(당분기·당해 영업이익/EPS)를 읽는다.

코스피200 컨센 트래커(kospi_consensus/scrape.py)와 같은 무토큰 엔드포인트를 쓰되,
여기서는 리포트 인제스트 시점의 스냅샷 1회 조회 용도라 헤드리스 렌더링 없이
모바일 JSON API만 사용한다(연간 E도 이 API에 있음 — 실측 확인).

단위: 영업이익 억원, EPS 원.
"""
import json
from datetime import datetime, timezone, timedelta

import requests

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
      'Referer': 'https://m.stock.naver.com/'}
API = 'https://m.stock.naver.com/api/stock/{code}/finance/{kind}'


def _num(x):
    s = str(x if x is not None else '').replace(',', '').strip()
    if s in ('', '-', 'N/A', 'nan', 'None'): return None
    try: return float(s)
    except ValueError: return None


def _first_estimate(payload):
    """financeInfo에서 가장 가까운 추정(E) 기간의 (period, 영업이익, EPS)를 뽑는다."""
    fi = (payload or {}).get('financeInfo') or {}
    titles = fi.get('trTitleList') or []
    rows = fi.get('rowList') or []
    target = next((t for t in titles if t.get('isConsensus') == 'Y'), None)
    if not target: return None
    key, period = target['key'], target['title'].rstrip('.')

    def val(name):
        row = next((r for r in rows if r.get('title', '').strip() == name), None)
        return _num(row['columns'].get(key, {}).get('value')) if row else None

    return {'period': period, 'op': val('영업이익'), 'eps': val('EPS')}


def quarter_label(period):
    """'2026.06' → '2Q26'."""
    try:
        y, m = period.split('.')[:2]
        return f"{int(m) // 3}Q{y[2:]}"
    except (ValueError, IndexError):
        return period


def fetch_consensus(code, session=None):
    """당분기(가장 가까운 E 분기)·당해(가장 가까운 E 연도) 컨센. 실패 항목은 None."""
    s = session or requests
    out = {'fetched_at': datetime.now(timezone.utc).isoformat(), 'quarter': None, 'annual': None}
    for kind, slot in (('quarter', 'quarter'), ('annual', 'annual')):
        try:
            payload = s.get(API.format(code=code, kind=kind), headers=UA, timeout=10).json()
            out[slot] = _first_estimate(payload)
        except Exception:
            out[slot] = None
    if out['quarter']:
        out['quarter']['label'] = quarter_label(out['quarter']['period'])
    if out['annual']:
        out['annual']['label'] = out['annual']['period'].split('.')[0] + 'E'
    return out


if __name__ == '__main__':
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    code = sys.argv[1] if len(sys.argv) > 1 else '329180'
    print(json.dumps(fetch_consensus(code), ensure_ascii=False, indent=1))
