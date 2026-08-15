"""다올 리서치 톤 챗봇 코어 (2단계).

Anthropic SDK의 tool runner(베타)로 도구 호출 루프를 돌린다. 데이터는 이 리포의
data/(로컬) 또는 GitHub Pages JSON에서 조회한다. 1단계(GS-output-dashboard의
daol_chatbot) 대비 리포트 메타 검색과 PDF 원문 검색·열람 도구가 추가되었다.
"""
from __future__ import annotations

import os

import anthropic
from anthropic import beta_tool

import data_source
import fulltext
import tools

MODEL = os.getenv("DAOL_CHATBOT_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.getenv("DAOL_CHATBOT_MAX_TOKENS", "16000"))

SYSTEM_PROMPT = """당신은 "다올 리서치 톤" 챗봇입니다. 다올투자증권 리서치(텔레그램 채널, 2025-05-28~)의 \
리포트 1,200여 건을 구조화한 데이터와 리포트 PDF 원문에 근거해, 애널리스트가 기업/산업을 보는 뷰·톤과 \
그 변화를 답합니다.

[신뢰 원칙 — 반드시 준수]
1. 도구로 조회한 데이터에 있는 사실만 말합니다. 데이터에 없는 값은 절대 지어내지 않고 "데이터에 없음"이라고 답합니다.
2. 근거는 리포트 원문 표현(one_line, 강한/헤징/부정 문구, 투자포인트, PDF 원문 스니펫)을 인용합니다.
3. 의견 어휘는 통일되어 있습니다 — 기업: BUY/HOLD/REDUCE, 산업: 비중확대/중립/비중축소. \
위클리 자료에는 투자의견이 없으므로 의견을 추정하지 않습니다.
4. 목표주가(TP)는 tp_event의 value/prior/direction/reasons(실적추정·멀티플·방법론·시점롤포워드)를 그대로 사용합니다.
5. 리포트를 인용할 때는 날짜·애널리스트·제목을 함께 밝힙니다.

[서술 톤 — 다올 리서치 스타일]
- 두괄식: 결론(뷰/의견/TP)을 첫 문장에 제시합니다.
- 숫자 중심: TP, 컨센 대비 %(est_compare), 이벤트 사후수익률(ret5/ret20) 등 수치를 명시합니다.
- "~로 판단", "~를 반영해 TP를 XX원으로 상향" 같은 리서치 관용 표현을 사용합니다.
- 간결하게, 질문에 필요한 범위만 답합니다.

[도구 선택 가이드]
- 오늘/최근 변화("오늘 뭐 바뀌었어?") → get_today_briefing
- 특정 기업의 다올 뷰("삼양식품 다올 뷰?") → get_company_view (종목명 또는 6자리 종목코드)
- 섹터/애널리스트 톤("조선 톤 요즘 어때?") → get_sector_tone
- 주제/키워드로 리포트 찾기("HBM 언급한 리포트?", "박영도 최근 리포트") → search_reports
- 리포트 본문 표현/수치 확인("원문에서 뭐라고 했어?") → search_report_fulltext, 더 길게 보려면 get_report_fulltext
- 다올 미커버 종목 → search_street (전시장 타사 리포트 120일치)
답하기 전에 반드시 관련 도구를 호출해 데이터를 확인하세요. 구조화 필드(요약)와 원문이 다르면 원문을 우선합니다."""


def _meta_index() -> dict:
    tone = data_source.load_tone_v2()
    try:
        messages = data_source.load_messages()
    except Exception:
        messages = []
    return fulltext.build_meta_index(tone, messages)


@beta_tool
def get_today_briefing() -> str:
    """오늘의 브리핑을 가져온다. 최신 다올 리포트 20건, 최근 변화 이벤트 20건(TP/의견/실적/톤이례,
    사후수익률 ret5/ret20 포함), 전체 카운트를 담고 있다. "오늘 뭐 바뀌었어?" 류 질문에 사용한다."""
    return tools.serialize(tools.today_briefing(data_source.load_summary()))


@beta_tool
def get_company_view(company: str) -> str:
    """다올 커버 종목 하나의 뷰를 가져온다: 리포트 타임라인 최근 카드(의견/확신도/톤/TP 이벤트/투자포인트/
    컨센 대비 추정치)와 타사 대비 포지션(street). 종목을 찾지 못하면 search_street를 안내한다.

    Args:
        company: 종목명(예: "삼양식품") 또는 6자리 종목코드(예: "003230").
    """
    return tools.serialize(tools.company_view(company, data_source.load_tone_v2()))


@beta_tool
def get_sector_tone(sector: str) -> str:
    """섹터(산업)의 다올 톤을 가져온다: 애널리스트별 최근 톤·평소 베이스라인·6개월 카운트(sectors)와
    해당 산업(IND:섹터) 리포트 타임라인 최근 카드. "조선 톤 요즘 어때?" 류 질문에 사용한다.

    Args:
        sector: 섹터명(예: "조선", "방산", "음식료").
    """
    return tools.serialize(tools.sector_tone(sector, data_source.load_tone_v2()))


@beta_tool
def search_reports(query: str = "", analyst: str = "", sector: str = "",
                   date_from: str = "", date_to: str = "") -> str:
    """다올 리포트 1,200여 건의 메타데이터(제목/한줄요약/투자포인트/기업/애널리스트)를 검색한다.
    "HBM 언급한 리포트 있어?", "박영도 최근 리포트 뭐 냈어?" 류 질문에 사용한다.
    결과 카드의 id/source_url은 원문 도구(get_report_fulltext)에 그대로 넘길 수 있다.

    Args:
        query: 공백 구분 키워드(AND 매칭). 비워두면 필터만으로 최신순 목록.
        analyst: 애널리스트 이름 필터(예: "최광식").
        sector: 섹터 필터(예: "조선").
        date_from: 시작일 YYYY-MM-DD (포함).
        date_to: 종료일 YYYY-MM-DD (포함).
    """
    return tools.serialize(tools.search_reports(query, data_source.load_tone_v2(),
                                                analyst=analyst, sector=sector,
                                                date_from=date_from, date_to=date_to))


@beta_tool
def search_report_fulltext(query: str, analyst: str = "", sector: str = "") -> str:
    """리포트 PDF 원문 1,200여 건에서 키워드를 검색해, 매칭 리포트의 메타와 원문 스니펫(문장 인용)을
    돌려준다. 요약 필드에 없는 구체 수치·표현·근거를 원문에서 확인할 때 사용한다.

    Args:
        query: 공백 구분 키워드(AND 매칭). 예: "수주잔고 슬롯".
        analyst: 애널리스트 이름 필터(선택).
        sector: 섹터 필터(선택).
    """
    return tools.serialize(fulltext.search_fulltext(query, data_source.load_pdf_text_cache(),
                                                    _meta_index(), analyst=analyst, sector=sector))


@beta_tool
def get_report_fulltext(ref: str, around: str = "") -> str:
    """리포트 하나의 PDF 원문을 길게 가져온다(최대 9,000자). ref는 search_reports/search_report_fulltext
    결과의 id 또는 source_url. around에 키워드를 주면 그 주변 구간을 돌려준다.

    Args:
        ref: 리포트 id(예: "14439") 또는 source_url(단축링크).
        around: 이 키워드 주변 구간을 볼 때 지정(선택).
    """
    return tools.serialize(fulltext.get_fulltext(ref, data_source.load_pdf_text_cache(),
                                                 _meta_index(), around=around))


@beta_tool
def search_street(company: str) -> str:
    """전시장 타사 증권사 리포트(최근 120일, 2,100여 건)에서 종목을 검색한다. 다올이 커버하지 않는
    종목에 대한 질문에 사용한다. 결과에는 증권사/TP 변경/의견/제목/요약 불릿이 담긴다.

    Args:
        company: 종목명(예: "에코프로").
    """
    return tools.serialize(tools.street_search(company, data_source.load_ked_street()))


TOOLS = [get_today_briefing, get_company_view, get_sector_tone,
         search_reports, search_report_fulltext, get_report_fulltext, search_street]


def answer(messages: list[dict]) -> str:
    """대화 이력(user/assistant 텍스트 턴)을 받아 최종 답변 텍스트를 반환한다."""
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )
    final = runner.until_done()

    if final.stop_reason == "refusal":
        return "요청을 처리할 수 없습니다. 질문을 바꿔서 다시 시도해 주세요."

    text = "".join(block.text for block in final.content if block.type == "text")
    return text or "답변을 생성하지 못했습니다. 다시 시도해 주세요."


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "오늘 뭐 바뀌었어?"
    print(answer([{"role": "user", "content": question}]))
