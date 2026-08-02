"""한경 에이셀(t.me/ked_epic_ai) 리서치 요약 수집 — 타사 TP·의견 비교용.

포맷이 정형이라 정규식만으로 파싱한다:
    [✨ 리서치 요약] 팬오션 목표주가 상향 리포트 요약
    목표주가 상향 | 팬오션 | "VLCC 투자는 장기 이익 기반을 위한 것"
    목표주가 5,300원 >> 5,900원 | 투자의견 중립-유지 | KB증권
    ✅ ... 요약 불릿 ...   🟥 리스크 요인 ...

산출: data/ked_street.json — [{id,date,company,brokerage,tp_old,tp_new,direction,opinion,
quote,val_methods,post_url}] (최근 120일 보존, 증분 수집)
"""
import argparse
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'ked_street.json'
CHANNEL = 'ked_epic_ai'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'}
KEEP_DAYS = 120

HEAD_RE = re.compile(r'목표주가\s*(상향|하향|유지|신규)[^|]*\|\s*([^|]+?)\s*\|')
TP_RE = re.compile(r'목표주가\s*([\d,]+)\s*원?\s*(?:>>\s*([\d,]+)\s*원?)?\s*\|\s*투자의견\s*([^|]+?)\s*\|\s*([^\s|]+)')
QUOTE_RE = re.compile(r'[“"]([^”"]{4,80})[”"]')
VAL_RE = re.compile(r'P/?E|PER|P/?B|PBR|EV/EBITDA|DCF|SOTP|RIM|멀티플|밸류에이션|WACC', re.I)


def _num(x):
    try:
        return float(str(x).replace(',', ''))
    except (TypeError, ValueError):
        return None


def parse_post(mid, date, text, post_url):
    if '리서치 요약' not in text or '목표주가' not in text:
        return None
    flat = re.sub(r'\s+', ' ', text)
    head = HEAD_RE.search(flat)
    tp = TP_RE.search(flat)
    if not head or not tp:
        return None
    old, new = _num(tp.group(1)), _num(tp.group(2))
    if new is None:  # '>>'가 없으면 단일 TP 표기(유지·신규 등)
        old, new = None, old
    # 불릿 중 밸류에이션 방법 언급 라인(최대 2개)
    bullets = [b.strip('- ').strip() for b in text.split('\n') if b.strip().startswith('-')]
    vals = [b[:90] for b in bullets if VAL_RE.search(b)][:2]
    quote = QUOTE_RE.search(flat)
    return {
        'id': mid, 'date': date[:10], 'company': head.group(2).strip()[:30],
        'direction': head.group(1), 'tp_old': old, 'tp_new': new,
        'opinion': tp.group(3).strip()[:20], 'brokerage': tp.group(4).strip()[:20],
        'quote': quote.group(1).strip() if quote else '', 'val_methods': vals,
        'post_url': post_url,
    }


def fetch_page(session, param=''):
    url = f'https://t.me/s/{CHANNEL}{param}'
    soup = BeautifulSoup(session.get(url, headers=UA, timeout=20).text, 'lxml')
    out = []
    for node in soup.select('.tgme_widget_message'):
        mid = int(node['data-post'].rsplit('/', 1)[-1])
        t = node.select_one('time')
        body = node.select_one('.tgme_widget_message_text')
        if not t:
            continue
        out.append((mid, t.get('datetime'), body.get_text('\n') if body else '',
                    f'https://t.me/{CHANNEL}/{mid}'))
    return out


def collect(backfill_days=0):
    session = requests.Session()
    existing = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else []
    by_id = {int(x['id']): x for x in existing}
    if backfill_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=backfill_days)).isoformat()
        before = None
        while True:
            rows = fetch_page(session, f'?before={before}' if before else '')
            if not rows:
                break
            for mid, dt, text, url in rows:
                parsed = parse_post(mid, dt, text, url)
                if parsed:
                    by_id[mid] = parsed
            oldest = min(r[0] for r in rows)
            if min(r[1] for r in rows) < cutoff or oldest == before:
                break
            before = oldest
            time.sleep(0.25)
    else:  # 증분: 마지막 id 이후만
        after = max(by_id, default=0)
        while True:
            rows = fetch_page(session, f'?after={after}' if after else '')
            new_count, newest = 0, after
            for mid, dt, text, url in rows:
                newest = max(newest, mid)
                if mid in by_id:
                    continue
                parsed = parse_post(mid, dt, text, url)
                if parsed:
                    by_id[mid] = parsed
                new_count += 1
            if newest == after or new_count == 0:
                break
            after = newest
            time.sleep(0.25)
    floor = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).date().isoformat()
    entries = sorted((x for x in by_id.values() if x['date'] >= floor), key=lambda x: int(x['id']))
    OUT.write_text(json.dumps(entries, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({'entries': len(entries), 'first': entries[0]['date'] if entries else None,
                      'last': entries[-1]['date'] if entries else None}, ensure_ascii=False))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill-days', type=int, default=0)
    collect(ap.parse_args().backfill_days)
