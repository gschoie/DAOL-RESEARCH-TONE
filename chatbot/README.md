# 다올 리서치 톤 챗봇 (2단계)

다올리서치 텔레그램 리포트 1,200여 건의 구조화 데이터 + **PDF 원문**에 근거해 답하는
챗봇. 1단계(GS-output-dashboard `daol_chatbot`)를 데이터 생산지인 이 리포로 옮기고,
리포트 메타 검색·PDF 원문 검색/열람 도구를 추가했다.

데이터는 리포의 `data/`가 있으면 그것을 직접 읽고(로컬 우선), 없으면
GitHub Pages JSON을 TTL 캐시(기본 10분)로 받아온다 — 리포 밖 단독 실행도 가능.

## 구조

| 파일 | 역할 |
|---|---|
| `data_source.py` | 로컬 `data/` 우선 + Pages 폴백 로더 (tone_v2/summary/ked_street/pdf원문/메시지) |
| `tools.py` | 순수 조회 로직(기업 뷰, 섹터 톤, 오늘 브리핑, 리포트 메타 검색, 전시장 검색) |
| `fulltext.py` | PDF 원문 키워드 검색(스니펫 추출) + 원문 열람, source_url↔메타 조인 |
| `chatbot.py` | Anthropic API tool runner + 다올 톤 시스템 프롬프트 (도구 7종) |
| `server.py` | 127.0.0.1 전용 로컬 서버 (`POST /api/chat` + 채팅 UI) |
| `static/index.html` | 단일 파일 채팅 UI |

## 도구 7종

| 도구 | 담당 질문 |
|---|---|
| `get_today_briefing` | "오늘 뭐 바뀌었어?" |
| `get_company_view` | "삼양식품 다올 뷰?" |
| `get_sector_tone` | "조선 톤 요즘 어때?" |
| `search_reports` | "HBM 언급한 리포트?", "박영도 최근 리포트?" (애널리스트/섹터/기간 필터) |
| `search_report_fulltext` | "원문에서 뭐라고 했어?" — PDF 원문 키워드 검색 + 문장 인용 |
| `get_report_fulltext` | 리포트 한 건 원문 길게 보기 (`around` 키워드 주변 구간) |
| `search_street` | 다올 미커버 종목 — 전시장 타사 리포트 120일치 |

## 실행

```powershell
cd chatbot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:ANTHROPIC_API_KEY = "sk-ant-..."
python server.py
```

브라우저에서 <http://127.0.0.1:8788>을 엽니다.

CLI로 한 번만 물어보려면:

```powershell
python chatbot.py "최광식 조선 톤 요즘 어때?"
```

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_API_KEY` | (필수) | Anthropic API 키 |
| `DAOL_CHATBOT_MODEL` | `claude-opus-5` | 사용할 Claude 모델 |
| `DAOL_TONE_DATA_DIR` | `../data` | 로컬 데이터 디렉토리 |
| `DAOL_TONE_BASE_URL` | `https://gschoie.github.io/DAOL-RESEARCH-TONE/` | 폴백 데이터 URL |
| `DAOL_CHATBOT_CACHE_TTL` | `600` | 원격 JSON 캐시 TTL(초) |
| `PORT` | `8788` | 서버 포트 |

## 챗봇이 따르는 신뢰 원칙

- 데이터에 없는 값은 지어내지 않음
- 위클리 자료에는 투자의견 없음 — 의견을 추정하지 않음
- 근거 문구는 리포트 원문 표현 인용, 인용 시 날짜·애널리스트·제목 명시
- 의견 어휘: 기업 BUY/HOLD/REDUCE · 산업 비중확대/중립/비중축소
- 구조화 요약과 원문이 다르면 원문 우선

## 테스트

```powershell
python -m unittest discover -s tests -v
```

네트워크·API 키 없이 실행됩니다(조회 로직만 검증).

## 다음 단계 후보

- 답변 스트리밍(SSE)으로 체감 속도 개선
- 사이트(`site/index.html`)에 챗봇 진입점 링크
- 원문 검색 고도화(형태소 분리, 동의어) — 현재는 공백 구분 AND 부분문자열 매칭
