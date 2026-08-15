"""오늘의 브리핑 생성 — tone_summary를 Gemini로 요약해 daily_brief.json으로 저장.

아침 08:30(KST) 크론이 이 스크립트를 돌려 웹 챗봇 첫 화면 카드에 띄운다.
키가 없거나 호출이 실패하면 데이터만으로 만든 폴백 브리핑을 쓴다(배포는 계속).
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
KST = timezone(timedelta(hours=9))
MODEL = os.getenv('BRIEF_MODEL', 'gemini-flash-lite-latest')
RECENT_DAYS = 3  # 브리핑에 담을 최근 발간·이벤트 범위


def _fmt_report(r: dict) -> str:
    bits = [r.get('date', ''), r.get('analyst', ''), r.get('sector', ''), r.get('title', '')]
    if r.get('conviction'):
        bits.append(f"확신도{r['conviction']}")
    if r.get('post_url'):
        bits.append(f"링크 {r['post_url']}")
    return ' | '.join(str(b) for b in bits if b)


def _fmt_event(e: dict) -> str:
    bits = [e.get('date', ''), e.get('company', ''), e.get('analyst', ''),
            e.get('type', ''), e.get('detail', '')]
    if e.get('source'):
        bits.append(f"링크 {e['source']}")
    return ' | '.join(str(b) for b in bits if b)


def build_fallback(reports: list, events: list) -> str:
    lines = ['**오늘의 다올 변화 (자동 요약)**', '']
    if events:
        lines.append('주요 이벤트:')
        for e in events[:8]:
            link = f" [원문]({e['source']})" if e.get('source') else ''
            lines.append(f"- {e.get('date','')} **{e.get('company','')}** — {e.get('type','')}: "
                         f"{str(e.get('detail',''))[:120]}{link}")
    if reports:
        lines.append('')
        lines.append('최신 리포트:')
        for r in reports[:6]:
            link = f" [원문]({r['post_url']})" if r.get('post_url') else ''
            lines.append(f"- {r.get('date','')} {r.get('analyst','')} — {str(r.get('title',''))[:90]}{link}")
    return '\n'.join(lines)


def call_gemini(prompt: str) -> str:
    key = os.getenv('GEMINI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('GEMINI_API_KEY 없음')
    body = json.dumps({
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'thinkingConfig': {'thinkingLevel': 'low'}},
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent',
        data=body, headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'ignore')[:200]
        # 이 모델이 thinkingLevel을 모르면 설정 빼고 1회 재시도
        if exc.code == 400 and ('thinking' in detail.lower() or 'Unknown name' in detail):
            body2 = json.dumps({'contents': [{'role': 'user', 'parts': [{'text': prompt}]}]}).encode('utf-8')
            req2 = urllib.request.Request(req.full_url, data=body2, headers=dict(req.headers))
            resp = json.load(urllib.request.urlopen(req2, timeout=120))
        else:
            raise
    parts = (resp.get('candidates') or [{}])[0].get('content', {}).get('parts', [])
    text = ''.join(p.get('text', '') for p in parts).strip()
    if not text:
        raise RuntimeError('빈 응답')
    return text


def main() -> None:
    summary = json.loads((DATA / 'tone_summary.json').read_text(encoding='utf-8'))
    today = datetime.now(KST)
    cutoff = (today - timedelta(days=RECENT_DAYS)).strftime('%Y-%m-%d')
    reports = [r for r in summary.get('latest', []) if str(r.get('date', '')) >= cutoff]
    events = [e for e in summary.get('events', []) if str(e.get('date', '')) >= cutoff]

    ai_used = False
    if not reports and not events:
        text = '최근 발간·변화 이벤트가 없습니다. (주말/휴일이거나 데이터 수집 전일 수 있습니다)'
    else:
        prompt = f"""당신은 다올투자증권 리서치를 추적하는 애널리스트 비서입니다. 아래 최근 {RECENT_DAYS}일의
다올 리포트 발간 목록과 변화 이벤트를 바탕으로, 아침 브리핑을 한국어 마크다운으로 작성하세요.

규칙:
- 6~12줄, 두괄식. 가장 중요한 변화(TP 변경, 의견 전환, 실적 서프라이즈, 톤 급변)부터.
- 수치(TP, 방향)를 명시하고, 항목마다 제공된 링크가 있으면 [원문](URL) 형태로 붙일 것.
- 데이터에 없는 내용은 쓰지 말 것. 과장 금지. 인사말·서론 없이 본문만.
- 형식: 소제목(###) 1~2개 + 불릿.

[변화 이벤트]
{chr(10).join(_fmt_event(e) for e in events[:20]) or '(없음)'}

[최신 리포트]
{chr(10).join(_fmt_report(r) for r in reports[:20]) or '(없음)'}"""
        try:
            text = call_gemini(prompt)
            ai_used = True
        except Exception as exc:
            print(f'::warning::브리핑 AI 생성 실패({exc!r}) — 폴백 사용')
            text = build_fallback(reports, events)

    out = {
        'date': today.strftime('%Y-%m-%d'),
        'generated_at': today.isoformat(),
        'ai': ai_used,
        'model': MODEL if ai_used else None,
        'report_count': len(reports),
        'event_count': len(events),
        'text': text,
    }
    (DATA / 'daily_brief.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"daily_brief: {out['date']} ai={ai_used} reports={len(reports)} events={len(events)} chars={len(text)}")


if __name__ == '__main__':
    main()
