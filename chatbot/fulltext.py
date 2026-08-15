"""리포트 PDF 원문 검색 — 순수 로직 (단위 테스트 대상).

daol_pdf_text_cache.json(source_url -> {status, text, ...})을 대상으로
키워드 검색과 스니펫 추출을 한다. 리포트 메타(제목/애널리스트/날짜)는
daol_tone_v2.json 타임라인의 source_url로 조인하고, 타임라인에 없는
리포트(위클리 등)는 텔레그램 메시지의 links로 폴백 조인한다.
"""
from __future__ import annotations

from typing import Any

MAX_FULLTEXT_RESULTS = 8
SNIPPET_RADIUS = 140
MAX_SNIPPETS_PER_REPORT = 3
MAX_FULLTEXT_CHARS = 9000


def _terms(query: str) -> list[str]:
    return [t.lower() for t in query.split() if t.strip()]


def _entry_text(entry: Any) -> str:
    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
        return entry["text"]
    return ""


def build_meta_index(tone: dict, messages: list | None = None) -> dict[str, dict]:
    """source_url -> 리포트 메타 카드. tone_v2 타임라인 우선, 메시지 폴백."""
    index: dict[str, dict] = {}
    companies = tone.get("companies", {})
    if isinstance(companies, dict):
        for entry in companies.values():
            timeline = entry.get("timeline", []) if isinstance(entry, dict) else (entry if isinstance(entry, list) else [])
            for item in timeline:
                if not isinstance(item, dict):
                    continue
                src = item.get("source_url")
                if not src or src in index:
                    continue
                index[src] = {
                    "id": item.get("id"),
                    "date": item.get("date"),
                    "title": item.get("title"),
                    "analyst": item.get("analyst"),
                    "sector": item.get("sector"),
                    "company": item.get("company"),
                    "code": item.get("code"),
                    "post_url": item.get("post_url"),
                }
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for link in msg.get("links", []) or []:
            if link and link not in index:
                index[link] = {
                    "id": str(msg.get("id", "")),
                    "date": (msg.get("date") or "")[:10],
                    "title": (msg.get("text") or "")[:90],
                    "post_url": msg.get("post_url"),
                }
    return index


def _snippets(text: str, terms: list[str]) -> list[str]:
    lower = text.lower()
    spans: list[tuple[int, int]] = []
    for term in terms:
        start = 0
        while len(spans) < MAX_SNIPPETS_PER_REPORT:
            pos = lower.find(term, start)
            if pos < 0:
                break
            lo, hi = max(0, pos - SNIPPET_RADIUS), min(len(text), pos + len(term) + SNIPPET_RADIUS)
            # 기존 스니펫과 겹치면 건너뜀
            if any(lo < s_hi and hi > s_lo for s_lo, s_hi in spans):
                start = pos + len(term)
                continue
            spans.append((lo, hi))
            start = pos + len(term)
        if len(spans) >= MAX_SNIPPETS_PER_REPORT:
            break
    spans.sort()
    return ["…" + " ".join(text[lo:hi].split()) + "…" for lo, hi in spans]


def search_fulltext(query: str, pdf_cache: dict, meta_index: dict[str, dict],
                    analyst: str = "", sector: str = "") -> dict:
    """키워드(공백 구분, AND)로 PDF 원문을 검색해 스니펫과 메타를 돌려준다."""
    terms = _terms(query)
    if not terms:
        return {"error": "검색어가 비어 있습니다."}

    scored: list[tuple[int, str, dict, str]] = []
    for src, entry in pdf_cache.items():
        text = _entry_text(entry)
        if not text:
            continue
        meta = meta_index.get(src, {})
        if analyst and analyst.strip() not in str(meta.get("analyst", "")):
            continue
        if sector and sector.strip() not in str(meta.get("sector", "")):
            continue
        lower = text.lower()
        counts = [lower.count(t) for t in terms]
        if not all(counts):
            continue
        scored.append((sum(counts), src, meta, text))

    if not scored:
        return {"error": f"원문 1,200여 건에서 '{query}'를 찾지 못했습니다. 검색어를 줄이거나 바꿔 보세요."}

    # 점수 우선, 동점이면 최신 날짜 우선
    scored.sort(key=lambda r: (r[0], str(r[2].get("date") or "")), reverse=True)

    results = []
    for score, src, meta, text in scored[:MAX_FULLTEXT_RESULTS]:
        results.append({**meta, "source_url": src, "match_count": score,
                        "snippets": _snippets(text, terms)})
    return {"total_matches": len(scored), "results": results}


def get_fulltext(ref: str, pdf_cache: dict, meta_index: dict[str, dict],
                 around: str = "") -> dict:
    """리포트 하나의 원문을 가져온다. ref는 source_url 또는 리포트 id.

    around 키워드를 주면 그 주변 구간을, 없으면 앞부분을 돌려준다.
    """
    src, meta = None, {}
    if ref in pdf_cache:
        src, meta = ref, meta_index.get(ref, {})
    else:
        for url, m in meta_index.items():
            if str(m.get("id")) == str(ref) and url in pdf_cache:
                src, meta = url, m
                break
    if src is None:
        return {"error": f"'{ref}'에 해당하는 원문을 찾지 못했습니다. search_report_fulltext로 먼저 검색하세요."}

    text = _entry_text(pdf_cache.get(src))
    if not text:
        return {"error": "이 리포트는 원문 텍스트가 없습니다(수집 실패 또는 이미지 PDF)."}

    if around:
        pos = text.lower().find(around.lower())
        if pos >= 0:
            lo = max(0, pos - MAX_FULLTEXT_CHARS // 2)
            excerpt = text[lo:lo + MAX_FULLTEXT_CHARS]
            return {**meta, "source_url": src, "around": around, "text": excerpt,
                    "truncated": len(text) > len(excerpt)}

    return {**meta, "source_url": src, "text": text[:MAX_FULLTEXT_CHARS],
            "truncated": len(text) > MAX_FULLTEXT_CHARS}
