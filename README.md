# DAOL 리서치 톤 트래커

다올투자증권 리서치(텔레그램 [@daolresearch](https://t.me/s/daolresearch))를 수집해
**"애널리스트가 그 기업/그 산업을 보는 톤이 어떻게 변해왔는가"**를 추적하는 독립 프로그램.
[GS Research Desk 대시보드](https://gschoie.github.io/GS-output-dashboard/)에서 분리된 별도 프로젝트다.

**사이트**: https://gschoie.github.io/DAOL-RESEARCH-TONE/

## 무엇을 잡아내나

- **TP 변경 + 배경 4분류** — 실적추정 / 멀티플 / 방법론 / 시점롤포워드 (근거 문장 인용)
- **산업의견 변화** — 비중확대·중립·비중축소 전환 이벤트
- **실적 추정 상향/하향** — 영업이익·EPS 방향과 근거
- **AI 톤 분석** — 확신도 1~5점, 톤 라벨, 강한/헤징/부정 문구 원문 인용, 급변 이벤트
- **투자포인트 변화** — 직전 보고서 대비 신규 등장/소멸

## 파이프라인

```
build_daol_tone_dashboard.py --refresh   # t.me/s/daolresearch 수집 + 정규식 1차 추출 + PDF 원문
ai_report_analyzer.py                    # 리포트 1건→LLM 구조화 JSON (Gemini 우선/OpenAI 폴백, 증분 캐시)
build_daol_tone_v2.py                    # 타임라인·섹터-애널 표·변화 이벤트·요약 (순수 파이썬)
build_daol_collab_radar.py               # 섹터 콜라보 레이더 HTML
```

산출물은 `data/`에 커밋백되고 `site/` + JSON이 GitHub Pages로 배포된다.
갱신은 `refresh-tone.yml`이 3시간 간격(하루 7회)으로 자동 실행.

## 필요한 시크릿 (Settings → Secrets → Actions)

| 이름 | 용도 |
|---|---|
| `GEMINI_API_KEY` | AI 톤 분석 (무료 티어, 필수) |
| `OPENAI_API_KEY` | Gemini 한도 소진 시 폴백 (선택) |

키가 없어도 정규식 추출만으로 사이트는 정상 동작한다(톤 칸은 "AI 대기").

## 테스트

```
python -m unittest discover -s tests -p "test_*.py"
```
