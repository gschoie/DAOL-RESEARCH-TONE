"""매일 밸류에이션 스냅샷 축적 → data/valuation_history.json

파이프라인이 하루 여러 번 돌아도 날짜당 1행(같은 날은 최신값으로 교체)씩,
커버 종목별 [date, per, pbr, psr, roe, opm, close, tp]를 쌓는다.
챗봇의 밸류 추세 질문("PER이 최근 저점인가?")과 이후 시계열 분석의 원천 DB.
per~opm은 네이버 '올해(E)' 컨센서스, close는 종가 캐시 최신값, tp는 다올 목표주가.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
OUT = DATA / 'valuation_history.json'
FIELDS = ['date', 'per', 'pbr', 'psr', 'roe', 'opm', 'close', 'tp']
MAX_ROWS = 500  # 종목당 약 2년치


def latest_tp(entry):
    """다올 TP: 타임라인의 마지막 tp_event 우선, 없으면 스트리트 패널의 다올 값."""
    for it in reversed(entry.get('timeline') or []):
        te = isinstance(it, dict) and it.get('tp_event')
        if te and te.get('value'):
            return te['value']
    st = (entry.get('street') or {}).get('stats') or {}
    return st.get('daol')


def main():
    funda = json.loads((DATA / 'naver_fundamentals.json').read_text(encoding='utf-8'))
    prices = json.loads((DATA / 'price_close_cache.json').read_text(encoding='utf-8'))
    tone = json.loads((DATA / 'daol_tone_v2.json').read_text(encoding='utf-8'))

    kst = datetime.now(timezone(timedelta(hours=9)))
    today, year = kst.date().isoformat(), str(kst.year)

    hist = {'fields': FIELDS, 'companies': {}}
    if OUT.is_file():
        try:
            hist = json.loads(OUT.read_text(encoding='utf-8'))
        except Exception:
            pass
    comps = hist.setdefault('companies', {})
    hist['fields'] = FIELDS
    hist['note'] = ('종목별 rows = [date, per, pbr, psr, roe, opm, close, tp]. per~opm은 네이버 올해(E) 컨센서스'
                    '(per·pbr·psr 배, roe·opm %), close 원, tp 다올 목표주가 원. 날짜당 1행, 값 없으면 null.')

    added = 0
    for code, f in (funda.get('companies') or {}).items():
        rec = (f.get('years') or {}).get(year) or {}
        closes = (prices.get(code) or {}).get('closes') or []
        close = closes[-1][1] if closes else None
        entry = (tone.get('companies') or {}).get(code) or {}
        row = [today, rec.get('per'), rec.get('pbr'), rec.get('psr'), rec.get('roe'), rec.get('opm'),
               close, latest_tp(entry)]
        if all(v is None for v in row[1:]):
            continue
        c = comps.setdefault(code, {'name': f.get('name') or code, 'rows': []})
        c['name'] = f.get('name') or c.get('name') or code
        rows = c.setdefault('rows', [])
        if rows and rows[-1] and rows[-1][0] == today:
            rows[-1] = row  # 같은 날 재실행이면 최신값으로 교체
        else:
            rows.append(row)
            added += 1
        del rows[:-MAX_ROWS]

    hist['updated'] = today
    OUT.write_text(json.dumps(hist, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'valuation snapshot: {today} 기준 {len(comps)}종목 (신규 행 {added})')


if __name__ == '__main__':
    main()
