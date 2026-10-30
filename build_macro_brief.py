"""매크로.다올.브리핑 — 조병현·허정인 전략/경제 자료의 낭독용 MD 생성(트레일링 1주).

목적: 리서치톤이 잡은 다올 전략·매크로 자료(조병현·허정인)의 본문 전체를 오디오북 제작용
낭독 원고(MD)로 만들어 대시보드에서 내려받고(건별 + 1주 합본), Notion에도 쌓는다.

왜 PDF를 다시 읽나: 조병현 자료는 PDF 폰트가 깨져 텍스트 추출이 뭉개지는 경우가 많다
(10/22 금통위 리뷰 — 4,186자 중 한글 1,733자가 전부 깨진 글자). 텔레그램 요약은 400자
안팎이라 낭독감이 없다. 그래서 ai_pdf_rescue와 같은 방식으로 PDF 원본을 Gemini에 inline으로
넘겨 '본문 전체를 순서대로, 표·각주·컴플라이언스는 빼고' 낭독용으로 전사한다.

산출물
  data/daol_macro_brief.json               건별 캐시(id → md 등). 재생성 방지, 머지 캐시에 포함.
  site/macro_daol/<date>_<id>.md           건별 낭독 원고(아카이브 — 창을 벗어나도 남는다)
  site/macro_daol/macro_daol_1w.md         트레일링 1주 합본(최신 먼저)
  site/macro_daol/index.json               대시보드 페이지·Notion 적재용 목록(creation/sync 상태)
창 밖의 옛 자료는 삭제하지 않는다. 윈도 안에서 원문 확보 실패분은 다음 런에 다시 시도(최대 4회).

환경변수: GEMINI_API_KEY(필수), MACRO_BRIEF_DAYS(기본 7), MACRO_BRIEF_MAX(런당 AI 호출 상한,
기본 6), MACRO_BRIEF_BUDGET(초, 기본 480), MACRO_BRIEF_ANALYSTS(쉼표, 기본 조병현,허정인).
"""
import base64
import json
import os
import re
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_report_analyzer import (GEMINI_API_TEMPLATE, HISTORY, QuotaExhaustedError, _post_json,
                                flat_reports, gemini_model_queue, load_json)
from build_daol_tone_dashboard import _fetch_pdf_bytes, _kr

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
CACHE_FILE = DATA / 'daol_macro_brief.json'
PDF_CACHE = DATA / 'daol_pdf_text_cache.json'
MSG_FILE = DATA / 'daol_messages.json'
OUT_DIR = ROOT / 'site' / 'macro_daol'
SITE_BASE = 'https://gschoie.github.io/DAOL-RESEARCH-TONE/'

KST = timezone(timedelta(hours=9))
DAYS = int(os.getenv('MACRO_BRIEF_DAYS', '7'))
MAX_AI = int(os.getenv('MACRO_BRIEF_MAX', '6'))
BUDGET = int(os.getenv('MACRO_BRIEF_BUDGET', '480'))
ANALYSTS = {a.strip() for a in os.getenv('MACRO_BRIEF_ANALYSTS', '조병현,허정인').split(',') if a.strip()}
MAX_ATTEMPTS = 4
MIN_KR_OK = 1200          # 낭독 원고로 인정하는 최소 한글 자수(요약 수준이면 다음 런에 재시도)
WEEKDAY = '월화수목금토일'

PROMPT_PDF = '''이 PDF는 다올투자증권 매크로·투자전략 리서치 자료다. 오디오북(TTS 낭독)용 원고를 만든다.
텍스트 추출이 깨지는 자료라 PDF를 직접 읽어야 한다. 아래 규칙을 지켜 마크다운만 출력한다.

[포함]
- 본문 전체를 원문 순서대로 빠짐없이 옮긴다. 요약·축약 금지. 단락 구조와 소제목(##)을 살린다.
- 표는 그 표가 말하려는 핵심 수치를 문장으로 풀어 쓴다(행·열 나열 금지).
- 그림·차트는 본문에 없는 정보를 담고 있을 때만 그 메시지를 한 문장으로 적고, 단순 추이 차트는
  생략한다. "Fig 1: ~ 차트다" 식의 목록 나열은 하지 않는다.
- 첫 줄은 "# 자료 제목", 둘째 줄은 "발간일 YYYY년 M월 D일 · 애널리스트 이름" 한 줄.

[제외]
- 표지의 연락처·이메일·전화, 각주·자료 출처 표기("자료: Bloomberg, 다올투자증권" 류), 페이지 번호,
  컴플라이언스 고지·투자의견 분포·면책 문구, 목차.

[낭독 친화 표기]
- 수치는 원문 그대로 두되 읽기 쉽게: bp→베이시스포인트, YoY→전년 대비, QoQ→전분기 대비,
  MoM→전월 대비, Fed→연준, FOMC→에프오엠씨, CPI→소비자물가, PCE→개인소비지출물가, GDP→지디피,
  KOSPI→코스피, 12MF→12개월 선행, PER/PBR→피이알/피비알. %는 그대로 둔다.
- 영문 약어가 처음 나오면 한글 풀이를 붙인다. 괄호 안 영문 원어 병기는 생략한다.
- 한 문장이 너무 길면 둘로 나눈다. 구어체로 바꾸지 말고 보고서 문체를 유지한다.'''

PROMPT_TEXT = PROMPT_PDF.replace('텍스트 추출이 깨지는 자료라 PDF를 직접 읽어야 한다.',
                                 '아래에 추출된 원문 텍스트를 준다(순서·줄바꿈이 다소 흐트러져 있을 수 있다).')


def now_kst():
    return datetime.now(KST)


def gemini_transcribe(model, text=None, pdf_bytes=None):
    """낭독용 MD 생성. PDF는 inline_data, 추출 텍스트는 본문 그대로. ai_pdf_rescue와 같은 재시도 규칙."""
    url = GEMINI_API_TEMPLATE.format(model=model) + f"?key={os.environ['GEMINI_API_KEY'].strip()}"
    if pdf_bytes is not None:
        parts = [{'text': PROMPT_PDF},
                 {'inline_data': {'mime_type': 'application/pdf',
                                  'data': base64.b64encode(pdf_bytes).decode('ascii')}}]
    else:
        parts = [{'text': PROMPT_TEXT + '\n\n[원문 텍스트]\n' + text[:90000]}]
    body = {'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'temperature': 0.15, 'maxOutputTokens': 32768}}
    for attempt in range(3):
        try:
            payload = _post_json(url, body, {}, timeout=240)
            cand = payload['candidates'][0]
            out = ''.join(p.get('text', '') for p in cand['content']['parts'])
            if cand.get('finishReason') == 'MAX_TOKENS':
                out += '\n\n(원고가 길어 여기서 잘렸습니다.)'
            return out.strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            if exc.code == 429 and ('PerDay' in detail or 'RESOURCE_EXHAUSTED' in detail and 'daily' in detail.lower()):
                raise QuotaExhaustedError(f'Gemini daily quota exhausted: {detail[:600]}') from exc
            if exc.code == 429 and attempt < 2:
                time.sleep(30); continue
            if exc.code in (500, 502, 503) and attempt < 2:
                time.sleep(20); continue
            raise RuntimeError(f'Gemini API error {exc.code}: {detail[:300]}') from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < 2:
                time.sleep(10); continue
            raise RuntimeError(f'Gemini network error: {exc}') from exc
    raise RuntimeError('Gemini retry loop exited unexpectedly')


def fetch_pdf(report):
    """PDF 바이트 확보 — 직링크(pdf_url) 우선, 없으면 단축링크(source_url). PDF 아니면 None."""
    for url in (report.get('pdf_url'), report.get('source_url')):
        if not url:
            continue
        try:
            data, final, status = _fetch_pdf_bytes(url)
        except Exception as exc:
            print(f'    PDF 다운로드 실패 {url[:50]}: {type(exc).__name__}')
            continue
        if status == 200 and data[:5] == b'%PDF-' and len(data) < 18_000_000:
            return data
        print(f'    PDF 아님/상태 {status} {final[:60]}')
    return None


def source_text(report, pdf_cache, messages):
    """PDF 폴백용 추출 텍스트(깨지지 않은 경우만)와 텔레그램 전문."""
    entry = pdf_cache.get(report.get('source_url') or '') or {}
    txt = entry.get('text') or '' if entry.get('status') == 'pdf' else ''
    kr = _kr(txt)
    garbled = txt.count('•') >= 150 or (kr and kr / max(len(txt), 1) < 0.2)
    good = txt if kr >= 1500 and not garbled else ''
    msg = messages.get(str(report['id'])) or {}
    return good, (msg.get('text') or report.get('summary') or '')


def telegram_md(report, tg):
    """원문을 못 구했을 때의 임시 원고 — 텔레그램 요약본. 다음 런에 다시 시도한다."""
    d = datetime.fromisoformat(report['date'])
    body = re.sub(r'^\[[^\]]*\]\s*', '', tg.strip())
    body = re.sub(r'\n{3,}', '\n\n', body)
    return (f"# {clean_title(report['title'])}\n발간일 {d.year}년 {d.month}월 {d.day}일 · {report['analyst']}\n\n"
            f"(원문 PDF를 아직 확보하지 못해 텔레그램 요약본으로 대신합니다.)\n\n{body}\n")


def clean_title(t):
    """채널 머리말([Macro Issue / …]) 제거, 본문이 딸려온 긴 제목은 첫 절(로마숫자 소제목 앞)에서 끊는다."""
    t = re.sub(r'^\[[^\]]*\]\s*', '', t or '')
    t = re.sub(r'\s+', ' ', t.replace('★', '')).strip()
    t = re.sub(r'\s+(?:[IVX]+|\d+)\.\s.*$', '', t)
    if len(t) > 80:
        t = t[:80].rsplit(' ', 1)[0] + '…'
    return t


def select_reports(history):
    cutoff = (now_kst().date() - timedelta(days=DAYS)).isoformat()
    out = []
    for r in flat_reports(history):
        if r.get('analyst') in ANALYSTS and r['date'] >= cutoff and not r.get('bundled'):
            out.append(r)
    return out


def md_filename(report):
    return f"{report['date']}_{report['id']}.md"


def write_outputs(cache, selected):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (now_kst().date() - timedelta(days=DAYS)).isoformat()
    items = []
    for rid, e in cache.items():
        if not e.get('md'):
            continue
        fn = e['file']
        (OUT_DIR / fn).write_text(e['md'], encoding='utf-8')
        items.append({'id': rid, 'date': e['date'], 'weekday': e.get('weekday', ''), 'analyst': e['analyst'],
                      'title': e['title'], 'file': f'macro_daol/{fn}', 'url': SITE_BASE + f'macro_daol/{fn}',
                      'src': e['src'], 'kr': e.get('kr', 0), 'chars': len(e['md']), 'model': e.get('model', ''),
                      'post_url': e.get('post_url', ''), 'pdf_url': e.get('pdf_url', ''),
                      'generated': e.get('ts', ''), 'in_window': e['date'] >= cutoff,
                      'final': e['src'] != 'telegram'})
    items.sort(key=lambda x: (x['date'], str(x['id'])), reverse=True)
    window = [i for i in items if i['in_window']]
    stamp = now_kst().strftime('%Y-%m-%d %H:%M')
    parts = [f"# 매크로.다올.브리핑 — 최근 {DAYS}일 합본\n갱신 {stamp} KST · {len(window)}건 · "
             f"애널리스트 {' · '.join(sorted(ANALYSTS))}\n\n"
             "이 문서는 리서치톤 파이프라인이 자동으로 다시 씁니다. 건별 원고는 같은 폴더의 날짜별 파일에 있습니다.\n"]
    for i in window:
        md = cache[i['id']]['md']
        parts.append('\n\n---\n\n' + md.strip() + '\n')
    (OUT_DIR / 'macro_daol_1w.md').write_text(''.join(parts), encoding='utf-8')
    (OUT_DIR / 'macro_daol_2w.md').unlink(missing_ok=True)   # 1주로 바꾸기 전 합본 파일명
    pending = [{'id': str(r['id']), 'date': r['date'], 'analyst': r['analyst'], 'title': clean_title(r['title'])}
               for r in selected if not (cache.get(str(r['id'])) or {}).get('md')]
    index = {'generated': stamp, 'window_days': DAYS, 'analysts': sorted(ANALYSTS),
             'combined': {'file': 'macro_daol/macro_daol_1w.md', 'url': SITE_BASE + 'macro_daol/macro_daol_1w.md',
                          'count': len(window), 'chars': sum(len(p) for p in parts)},
             'items': items, 'pending': pending}
    (OUT_DIR / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding='utf-8')
    return len(window), len(items)


def main():
    history = load_json(HISTORY, {'months': []})
    pdf_cache = load_json(PDF_CACHE, {})
    messages = {str(m['id']): m for m in load_json(MSG_FILE, [])}
    cache = load_json(CACHE_FILE, {})
    selected = select_reports(history)
    def needs_work(e):
        if e.get('src') in (None, 'telegram'):
            return e.get('attempts', 0) < MAX_ATTEMPTS
        return 'lite' in (e.get('model') or '') and not e.get('upgraded')
    todo = [r for r in selected if needs_work(cache.get(str(r['id'])) or {})]
    print(f'매크로 브리핑: 창 {DAYS}일 대상 {len(selected)}건, 이번 런 생성 필요 {len(todo)}건 (상한 {MAX_AI})')
    has_key = bool(os.getenv('GEMINI_API_KEY', '').strip())
    # 낭독 원고는 오독이 그대로 귀에 들어오므로 품질 우선 — flash-lite는 맨 뒤(첫 런의 lite 원고에
    # '통상문'(통방문)·'급리'(금리) 류 오독). lite로 만든 원고는 상위 모델로 한 번 다시 전사한다.
    queue = gemini_model_queue()
    models = [m for m in queue if 'lite' not in m] + [m for m in queue if 'lite' in m]
    strong = [m for m in models if 'lite' not in m]
    started = time.time()
    done = 0
    for r in todo:
        rid = str(r['id'])
        if done >= MAX_AI or time.time() - started > BUDGET:
            print('  런 상한/예산 도달 — 나머지는 다음 런'); break
        prev = cache.get(rid) or {}
        entry = {'date': r['date'], 'weekday': WEEKDAY[datetime.fromisoformat(r['date']).weekday()],
                 'analyst': r['analyst'], 'title': clean_title(r['title']), 'post_url': r.get('post_url', ''),
                 'pdf_url': r.get('pdf_url') or r.get('source_url') or '', 'file': md_filename(r),
                 'attempts': prev.get('attempts', 0) + 1, 'md': prev.get('md', ''), 'src': prev.get('src', ''),
                 'kr': prev.get('kr', 0), 'model': prev.get('model', '')}
        print(f'  [{rid}] {r["date"]} {r["analyst"]} {entry["title"][:50]}')
        good_text, tg = source_text(r, pdf_cache, messages)
        md, src, model_used = '', '', ''
        upgrading = bool(prev.get('md')) and prev.get('src') != 'telegram'
        if upgrading:
            entry['upgraded'] = True   # 상위 모델 재전사는 1회만 시도(실패하면 lite 원고 유지)
        if has_key and (strong or not upgrading):
            pdf = fetch_pdf(r)
            for model in (strong if upgrading else models):
                if not pdf and not good_text:
                    break
                try:
                    md = gemini_transcribe(model, text=None if pdf else good_text, pdf_bytes=pdf)
                    src = 'pdf' if pdf else 'text'
                    model_used = model
                    if _kr(md) < MIN_KR_OK:
                        print(f'    {model}: 원고 짧음(한글 {_kr(md)}) — 다른 모델로')
                        md, src = '', ''
                        continue
                    break
                except QuotaExhaustedError as exc:
                    print(f'    {model} 일일 쿼터 소진 — 다음 모델: {str(exc)[:80]}')
                    continue
                except RuntimeError as exc:
                    msg = str(exc)
                    print(f'    {model} 실패: {msg[:120]}')
                    if 'API error 404' in msg or 'API error 400' in msg:
                        continue
                    break
            done += 1
        if md:
            entry.update(md=md, src=src, kr=_kr(md), model=model_used, ts=now_kst().strftime('%Y-%m-%d %H:%M'))
            print(f'    완료 {src} {model_used} 한글 {entry["kr"]}자')
        elif not entry['md'] and tg:
            entry.update(md=telegram_md(r, tg), src='telegram', kr=_kr(tg), ts=now_kst().strftime('%Y-%m-%d %H:%M'))
            print('    임시: 텔레그램 요약본')
        cache[rid] = entry
    # 캐시에만 있고 창 밖인 건도 그대로 둔다(아카이브). 창 안 신규 레코드의 메타(제목 등)는 갱신.
    for r in selected:
        e = cache.get(str(r['id']))
        if e:
            e['post_url'] = r.get('post_url', '') or e.get('post_url', '')
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    n_window, n_all = write_outputs(cache, selected)
    print(json.dumps({'window': n_window, 'archive': n_all, 'generated_this_run': done,
                      'pending': sum(1 for r in selected if not (cache.get(str(r['id'])) or {}).get('md')),
                      'telegram_only': sum(1 for r in selected if (cache.get(str(r['id'])) or {}).get('src') == 'telegram')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
