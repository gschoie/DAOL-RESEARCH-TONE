"""챗봇 도구의 순수 조회 로직 (단위 테스트 대상).

daol_tone_v2.json 구조를 조회하되, 필드 구성이 런마다 조금씩 달라질 수 있으므로
방어적으로 접근한다. 반환값은 항상 JSON 직렬화 가능한 파이썬 객체이며,
호출부(chatbot.py)에서 문자열로 직렬화해 모델에 전달한다.
"""
from __future__ import annotations

import json
from typing import Any

# 모델 컨텍스트 낭비를 막기 위한 상한
MAX_TIMELINE_ITEMS = 12
MAX_STREET_ITEMS = 20
MAX_REPORT_RESULTS = 10
MAX_RESULT_CHARS = 12000

# 리포트 카드로 추릴 필드 — 원문급 대용량 필드(points_detail 등)는 제외
REPORT_CARD_FIELDS = (
    "id", "date", "analyst", "sector", "company", "code", "report_type", "title",
    "opinion", "conviction", "tone_label", "one_line", "points",
    "post_url", "source_url",
)


def _timeline_of(entry: Any) -> list:
    """companies 항목에서 타임라인 배열을 꺼낸다. 항목이 배열이면 그 자체가 타임라인."""
    if isinstance(entry, list):
        return entry
    if isinstance(entry, dict):
        for key in ("timeline", "reports", "items", "history"):
            value = entry.get(key)
            if isinstance(value, list):
                return value
    return []


def _name_of(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("name", "company", "company_name"):
            value = entry.get(key)
            if isinstance(value, str):
                return value
    return ""


def _entry_matches(query: str, code: str, entry: Any) -> bool:
    q = query.strip().lower()
    if not q:
        return False
    if q in code.lower():
        return True
    if q in _name_of(entry).lower():
        return True
    # 이름 필드가 없으면 타임라인 제목에서 매칭 시도
    for item in _timeline_of(entry)[:5]:
        if isinstance(item, dict) and q in str(item.get("title", "")).lower():
            return True
    return False


def resolve_company(query: str, tone: dict) -> tuple[str, Any] | None:
    """종목명 또는 종목코드로 companies 항목을 찾는다. 산업(IND:) 키는 제외."""
    companies = tone.get("companies", {})
    if not isinstance(companies, dict):
        return None
    # 정확한 코드 일치 우선
    if query in companies:
        return query, companies[query]
    for code, entry in companies.items():
        if code.startswith("IND:"):
            continue
        if _entry_matches(query, code, entry):
            return code, entry
    return None


def company_view(query: str, tone: dict) -> dict:
    """기업 뷰: 타임라인 최근 카드 + 타사 대비(street) + 컨센 대비."""
    resolved = resolve_company(query, tone)
    if resolved is None:
        return {"error": f"'{query}'에 해당하는 다올 커버 종목을 찾지 못했습니다. "
                         "미커버 종목이면 search_street 도구를 사용하세요."}
    code, entry = resolved
    timeline = _timeline_of(entry)
    street = tone.get("street", {})
    street_row = street.get(code) if isinstance(street, dict) else None
    if street_row is None and isinstance(entry, dict):
        street_row = entry.get("street")
    return {
        "code": code,
        "name": _name_of(entry) or None,
        "timeline_recent": timeline[-MAX_TIMELINE_ITEMS:],
        "street_position": street_row,
    }


def sector_tone(sector_query: str, tone: dict) -> dict:
    """섹터 톤: sectors 행(최근 톤·평소 베이스라인) + IND:섹터 타임라인 최근 카드."""
    q = sector_query.strip().lower()
    result: dict[str, Any] = {"sector_rows": [], "industry_timeline_recent": []}

    sectors = tone.get("sectors")
    rows = sectors.values() if isinstance(sectors, dict) else (sectors if isinstance(sectors, list) else [])
    for row in rows:
        if q in json.dumps(row, ensure_ascii=False).lower():
            result["sector_rows"].append(row)

    companies = tone.get("companies", {})
    if isinstance(companies, dict):
        for code, entry in companies.items():
            if code.startswith("IND:") and q in code.lower():
                result["industry_timeline_recent"] = _timeline_of(entry)[-MAX_TIMELINE_ITEMS:]
                result["industry_key"] = code
                break

    if not result["sector_rows"] and not result["industry_timeline_recent"]:
        available = [c for c in companies if c.startswith("IND:")] if isinstance(companies, dict) else []
        result["error"] = f"'{sector_query}' 섹터를 찾지 못했습니다."
        result["available_industries"] = available
    return result


def today_briefing(summary: dict) -> dict:
    """오늘 브리핑: tone_summary.json 그대로(최신 리포트 20건 + 이벤트 20건 + 카운트)."""
    return summary


def street_search(query: str, ked: Any) -> dict:
    """전시장(타사) 리포트에서 종목명으로 검색 — 다올 미커버 종목 질의용."""
    q = query.strip().lower()
    rows: list = []
    if isinstance(ked, dict):
        for value in ked.values():
            if isinstance(value, list):
                rows.extend(value)
            elif isinstance(value, dict):
                rows.append(value)
    elif isinstance(ked, list):
        rows = ked

    matches = [r for r in rows if isinstance(r, dict) and q in json.dumps(r, ensure_ascii=False).lower()]
    if not matches:
        return {"error": f"타사 리포트(120일)에서 '{query}'를 찾지 못했습니다."}
    return {"matches": matches[:MAX_STREET_ITEMS], "total_matches": len(matches)}


def _report_card(item: dict) -> dict:
    return {k: item[k] for k in REPORT_CARD_FIELDS if k in item}


def iter_all_reports(tone: dict):
    """companies(산업 IND: 포함) 타임라인의 모든 리포트 카드를 중복 없이 돈다."""
    seen: set = set()
    companies = tone.get("companies", {})
    if not isinstance(companies, dict):
        return
    for entry in companies.values():
        for item in _timeline_of(entry):
            if not isinstance(item, dict):
                continue
            key = item.get("id") or item.get("post_url") or id(item)
            if key in seen:
                continue
            seen.add(key)
            yield item


def search_reports(query: str, tone: dict, analyst: str = "", sector: str = "",
                   date_from: str = "", date_to: str = "") -> dict:
    """리포트 메타 검색: 제목·한줄요약·투자포인트·기업명에 대해 AND 키워드 매칭.

    query가 비어 있어도 analyst/sector/기간 필터만으로 최신순 목록을 돌려준다.
    """
    terms = [t.lower() for t in query.split() if t.strip()]
    matches: list[dict] = []
    for item in iter_all_reports(tone):
        if analyst and analyst.strip() not in str(item.get("analyst", "")):
            continue
        if sector and sector.strip() not in str(item.get("sector", "")):
            continue
        date = str(item.get("date", ""))
        if date_from and date and date < date_from:
            continue
        if date_to and date and date > date_to:
            continue
        if terms:
            haystack = " ".join([
                str(item.get("title", "")), str(item.get("one_line", "")),
                " ".join(map(str, item.get("points", []) or [])),
                str(item.get("company", "")), str(item.get("analyst", "")),
                str(item.get("sector", "")),
            ]).lower()
            if not all(t in haystack for t in terms):
                continue
        matches.append(item)

    if not matches:
        return {"error": f"조건(query='{query}', analyst='{analyst}', sector='{sector}')에 맞는 리포트가 없습니다."}

    matches.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    return {"total_matches": len(matches),
            "results": [_report_card(r) for r in matches[:MAX_REPORT_RESULTS]]}


def serialize(result: Any) -> str:
    """도구 결과를 모델에 넘길 문자열로 직렬화. 과도하게 길면 잘라낸다."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '… (이하 생략: 결과가 잘렸습니다. 더 구체적으로 조회하세요.)'
    return text
