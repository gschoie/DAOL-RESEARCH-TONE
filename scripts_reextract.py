# -*- coding: utf-8 -*-
"""1회성: pypdf가 본문을 못 읽은 캐시(적정주가 마커 없음)를 pypdfium2로 재추출."""
import json, re, sys, time
from io import BytesIO
from pathlib import Path

import requests
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'data' / 'daol_pdf_text_cache.json'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126'}

cache = json.loads(CACHE.read_text(encoding='utf-8'))
targets = [k for k, v in cache.items() if v.get('status') == 'pdf' and '적정주가' not in v.get('text', '')]
print(f'targets: {len(targets)}', flush=True)
fixed = fail = 0
for i, url in enumerate(targets):
    try:
        r = requests.get(url, headers=UA, timeout=(8, 30), allow_redirects=True)
        r.raise_for_status()
        doc = pdfium.PdfDocument(r.content)
        alt = '\n'.join(doc[p].get_textpage().get_text_range() for p in range(len(doc)))
        doc.close()
        alt = re.sub(r'[ \t]+', ' ', alt)
        alt = re.sub(r'\n{3,}', '\n\n', alt).strip()
        if len(alt) > len(cache[url].get('text', '')) or '적정주가' in alt:
            cache[url]['text'] = alt
            cache[url]['final_url'] = r.url
            fixed += 1
    except Exception as exc:
        fail += 1
        print(f'  skip {url[:40]}: {str(exc)[:60]}', flush=True)
    if (i + 1) % 25 == 0:
        print(f'  {i+1}/{len(targets)} fixed={fixed} fail={fail}', flush=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    time.sleep(0.4)
CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
now_with = sum(1 for v in cache.values() if v.get('status') == 'pdf' and '적정주가' in v.get('text', ''))
print(f'done fixed={fixed} fail={fail} | 적정주가 보유 캐시: {now_with}', flush=True)
