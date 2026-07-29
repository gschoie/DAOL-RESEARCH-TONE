"""다올 리서치톤 데이터에서 섹터 간 콜라보 후보 테마를 찾아낸다.

daol_tone_history.json(리서치톤 빌더 산출물)을 입력으로 받아
매크로·미국공략·원자재·산업테마 사전을 리포트 본문에 매칭하고,
같은 테마를 언급한 서로 다른 섹터 애널리스트(=콜라보 후보)와
수혜/피해 방향(=대립 페어)을 정리해 정적 HTML 레이더를 만든다.
네트워크 접근 없음 — 리서치톤 갱신 후에 돌리면 된다.
"""
import json, re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / 'data' / 'daol_tone_history.json'
OUT_HTML = ROOT / 'site' / 'daol_collab_radar.html'
OUT_JSON = ROOT / 'data' / 'daol_collab_radar.json'

# ── 테마 사전: (테마명, 카테고리, 매칭 정규식) ───────────────────────────────
# 새 테마는 여기 한 줄만 추가하면 된다. 카테고리는 화면 필터로 쓰인다.
THEMES = [
    # 매크로 / 시장환경
    ('금리 사이클', '매크로', r'금리\s*(인하|인상|동결)|기준금리|연준|\bFed\b|FOMC|파월|피벗'),
    ('환율/원화', '매크로', r'환율|원화\s*(강세|약세)|원/?달러|달러\s*(강세|약세)|외환'),
    ('관세/무역분쟁', '매크로', r'관세|무역\s*(분쟁|협상|장벽)|통상\s*(압박|협상)|USTR|232조|301조'),
    ('중국 경기/부양', '매크로', r'중국\s*(경기|부양|소비|수요|시장)|중국발|양회|이구환신'),
    ('국내 정책/재정', '매크로', r'추경|재정\s*(정책|확대)|정부\s*(정책|지원|예산)|소비\s*쿠폰|부양책|新정부|새\s*정부'),
    ('밸류업/주주환원', '매크로', r'밸류업|주주\s*환원|자사주\s*(매입|소각)|배당\s*(확대|성향|정책)|상법\s*개정'),
    ('중동/호르무즈 리스크', '매크로', r'이란|호르무즈|중동\s*(리스크|긴장|분쟁|전쟁)|해협\s*(봉쇄|통항|통과)|유조선\s*피격|이스라엘|지정학\s*리스크'),
    # 미국 시장 공략
    ('AI/데이터센터 투자', '미국공략', r'AI\s*(투자|수요|인프라|모멘텀|산업|데이터\s*센터|서버)|데이터\s*센터|하이퍼스케일|CAPEX|캐펙스|엔비디아|NVIDIA'),
    ('전력기기/전력망', '미국공략', r'전력\s*(기기|망|수요|인프라|설비)|변압기|초고압|송배전|ESS'),
    ('미국 현지생산/리쇼어링', '미국공략', r'리쇼어링|온쇼어링|현지\s*(생산|공장)|미국\s*(공장|증설|진출|투자|시장\s*공략)|IRA'),
    ('미 함정 MRO/조선협력', '미국공략', r'\bMRO\b|미\s*해군|미국\s*조선|마스가|MASGA|함정\s*(수출|정비|시장)|필리\s*조선소'),
    ('K뷰티 수출', '미국공략', r'(미국|북미|아마존|글로벌)[^.▶]{0,40}(화장품|뷰티|스킨케어)|(화장품|뷰티)[^.▶]{0,40}(미국|북미|아마존|수출)'),
    ('K푸드 수출', '미국공략', r'K[- ]?푸드|라면\s*수출|(미국|북미|글로벌)[^.▶]{0,30}(라면|김밥|식품\s*수출)'),
    # 원자재
    ('유가/정유', '원자재', r'유가|WTI|두바이유|브렌트|정제\s*마진|OPEC'),
    ('구리/비철금속', '원자재', r'구리|전기동|비철|아연|알루미늄'),
    ('철강/철광석', '원자재', r'철광석|철강\s*(가격|시황|업황)|열연|후판'),
    ('이차전지 메탈', '원자재', r'리튬|니켈|코발트|양극재\s*가격|메탈\s*가격'),
    ('금/귀금속', '원자재', r'금\s*가격|금값|귀금속|은\s*가격|골드'),
    ('원전/우라늄', '원자재', r'원전|원자력|우라늄|SMR|체코\s*원전'),
    ('해운 운임', '원자재', r'\bSCFI\b|\bBDI\b|컨테이너\s*운임|해상\s*운임|탱커\s*(운임|시황)|운임\s*(상승|하락|급등|급락)'),
    # 산업 공통 테마
    ('HBM/AI반도체', '산업테마', r'HBM|고대역폭|AI\s*반도체|파운드리|D램|DRAM|낸드'),
    ('방산 수출', '산업테마', r'방산|방위\s*산업|무기\s*수출|K[- ]?방산|폴란드\s*(수출|계약)|NATO|국방\s*(예산|비)'),
    ('조선 사이클', '산업테마', r'조선\s*(업황|사이클|주)|신조선가|수주\s*(잔고|모멘텀|사이클)|LNG\s*선|친환경\s*선박'),
    ('비만/GLP-1', '산업테마', r'GLP-?1|비만\s*치료|위고비|마운자로|삭센다'),
    ('K콘텐츠', '산업테마', r'K[- ]?(콘텐츠|팝|POP)|웹툰|드라마\s*(수출|판매)|음반|공연|콘서트|팬덤'),
    ('로봇/자동화', '산업테마', r'로봇|휴머노이드|자동화\s*(설비|투자)'),
    ('가상자산/스테이블코인', '산업테마', r'가상\s*자산|스테이블\s*코인|비트코인|암호화폐|토큰\s*증권|STO'),
]

POS = re.compile(r'수혜|긍정|기회|호재|호조|상향|개선|성장|확대|모멘텀|강세|반등|증가|호실적|최대\s*실적|성수기|낙수', re.I)
NEG = re.compile(r'부담|우려|피해|리스크|하향|둔화|악재|약세|축소|감소|불확실|부진|역풍|경계', re.I)

# 섹터 표기 흔들림(·/, 구명칭)을 하나로 모은다. 전략·시황·채권은 '전략/시황'으로 묶어
# 콜라보 후보 판정(서로 다른 '산업' 섹터 2개 이상)에서 제외할 수 있게 한다.
SECTOR_CANON = [
    ('투자전략', '전략/시황'), ('시황', '전략/시황'), ('채권', '전략/시황'), ('매크로', '전략/시황'),
    ('건설', '건설/부동산'), ('자동차', '자동차/이차전지'), ('철강', '철강/비철금속'),
    ('반도체', '반도체/소부장'), ('테크노', '반도체/소부장'), ('전기전자', '전기전자'),
    ('인터넷', '인터넷/게임/레저'), ('게임', '인터넷/게임/레저'), ('의료', '의료기기/화장품'),
    ('화장품', '의료기기/화장품'), ('엔터', '엔터'), ('조선', '조선/기계/방산'),
    ('제약', '제약/바이오'), ('음식료', '음식료'), ('금융', '금융'), ('은행', '금융'), ('보험', '금융'),
    ('운송', '운송/로봇'), ('로봇', '운송/로봇'),
]

def norm_sector(s):
    s = (s or '').replace('·', '/').strip()
    for key, canon in SECTOR_CANON:
        if s.startswith(key):
            return canon
    return s or '미분류'

def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()

def flatten_reports(history):
    seen = {}
    for month in history.get('months', []):
        for a in month.get('analysts', []):
            for r in a.get('reports', []):
                seen[r['id']] = r
    return sorted(seen.values(), key=lambda x: (x['date'], x['id']))

def report_blob(r):
    parts = [r.get('title', ''), r.get('summary', ''), r.get('pitch', ''), r.get('report_conclusion', '')]
    parts += r.get('valuation', []) + r.get('earnings_changes', []) + r.get('top_picks', [])
    return ' ▶ '.join(dict.fromkeys(clean(p) for p in parts if clean(p)))

def chunks(blob):
    out = [clean(x) for x in re.split(r'(?=[▶★■●☑💡])|(?<=[.!?])\s+', blob)]
    return [x.lstrip('▶★■●☑💡 ') for x in out if 15 <= len(x) <= 420]

def extract_hits(reports):
    compiled = [(name, cat, re.compile(pat, re.I)) for name, cat, pat in THEMES]
    hits = []
    for r in reports:
        blob = report_blob(r)
        if not blob:
            continue
        parts = chunks(blob)
        sector = norm_sector(r.get('sector'))
        for name, cat, pat in compiled:
            matched = [c for c in parts if pat.search(c)]
            if not matched:
                continue
            score = sum(len(POS.findall(c)) - len(NEG.findall(c)) for c in matched)
            direction = '수혜' if score > 0 else ('피해' if score < 0 else '중립')
            # 방향 단서가 있는 문장을 우선 근거로 남긴다.
            matched.sort(key=lambda c: -(len(POS.findall(c)) + len(NEG.findall(c))))
            hits.append({
                'theme': name, 'category': cat, 'date': r['date'],
                'analyst': r.get('analyst', '미분류'), 'sector': sector,
                'company': r.get('company', ''), 'code': r.get('code', ''),
                'direction': direction, 'score': score,
                'evidences': [c[:240] for c in dict.fromkeys(matched)][:2],
                'title': clean(r.get('title', ''))[:140],
                'post_url': r.get('post_url', ''), 'source_url': r.get('source_url', ''),
            })
    return hits

def coverage_map(reports):
    """종목 → 전담 애널(리포트 최다 작성자) 맵. 다올은 1종목 1애널이라 이게 '주인'이다."""
    counts = defaultdict(lambda: defaultdict(int))
    codes = {}
    for r in reports:
        company, code = clean(r.get('company', '')), r.get('code', '')
        if not code or len(company) < 2 or re.match(r'^\dQ\d\d', company) or company in ('다올', '산업/기타'):
            continue
        counts[company][(r.get('analyst', ''), norm_sector(r.get('sector')))] += 1
        codes[company] = code
    owners = {}
    for company, by_owner in counts.items():
        (analyst, sector), _ = max(by_owner.items(), key=lambda x: x[1])
        owners[company] = {'code': codes[company], 'analyst': analyst, 'sector': sector}
    return owners

def extract_cross_mentions(reports, owners):
    """다른 섹터 애널 리포트 본문에 등장한 '남의 커버리지 종목'을 찾는다.

    같은 종목을 두 명이 정식 커버하는 일은 없으므로(1종목 1애널),
    콜라보 신호는 '언급'에서 나온다 — 예: 전략팀이 조선주를 탑픽으로 집거나,
    전력기기 리포트에 반도체 종목이 수혜주로 등장하는 경우.
    """
    # 영문 종목명(NC, HMM 등)은 NCT·NCA·MNC 같은 약어에 오탐되므로 단어 경계를 강제한다.
    # 한글 혼동어는 개별 예외: 하이브↔하이브리드, 카카오↔코코아(음식료 원가) 등.
    overrides = {'하이브': r'하이브(?!리드|로자임)', '카카오': r'카카오(?!게임즈|뱅크|페이|엔터|모빌리티)'}
    chunk_exclude = {'카카오': re.compile(r'카카오\s*(가격|원가|시세|콩|버터|투입단가)|카카오[/,]\s*유제품|코코아|초콜릿')}
    def name_pattern(name):
        if name in overrides:
            return re.compile(overrides[name])
        if re.fullmatch(r'[A-Za-z0-9&. ]+', name):
            return re.compile(r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])')
        return re.compile(re.escape(name))
    compiled = {c: name_pattern(c) for c in owners}
    out = []
    for r in reports:
        blob = report_blob(r)
        if not blob:
            continue
        analyst, sector = r.get('analyst', ''), norm_sector(r.get('sector'))
        parts = chunks(blob)
        matched = {}
        for company, owner in owners.items():
            if owner['analyst'] == analyst or company in (r.get('company') or ''):
                continue
            exclude = chunk_exclude.get(company)
            hit = next((c for c in parts if compiled[company].search(c) and not (exclude and exclude.search(c))), None)
            if hit:
                matched[company] = hit
        for company, hit in matched.items():
            # '한화' vs '한화에어로스페이스'처럼 더 긴 종목명이 같이 잡히면 짧은 쪽은 버린다.
            if any(company != other and company in other and hit == matched[other] for other in matched):
                continue
            owner = owners[company]
            out.append({'company': company, 'code': owner['code'],
                        'owner_analyst': owner['analyst'], 'owner_sector': owner['sector'],
                        'analyst': analyst, 'sector': sector, 'date': r['date'],
                        'evidence': hit[:240], 'title': clean(r.get('title', ''))[:140],
                        'post_url': r.get('post_url', '')})
    return out

# ── 미등록 테마 후보 자동 발굴 ──────────────────────────────────────────────
# 최근 90일 언급 비중이 과거 대비 급증(burst)했는데 THEMES에 없는 단어를 찾는다.
# 상용구·조사 어미가 섞여 나오므로 자동 승격은 하지 않고 화면에 후보로만 노출한다.
# 좋은 후보를 발견하면 THEMES에 한 줄 추가하면 된다.
CAND_STOP = re.compile(
    r'^(다올|유지|보고서|전망|기대|실적|매출|영업|이익|억원|조원|만원|십억원|원문|컴플|채널|텔레|투자|의견|적정|주가'
    r'|시장|대비|상향|하향|확대|축소|예상|상승|하락|성장|증가|감소|기록|지속|판단|추정|컨센|상회|하회|예정|연결|업종'
    r'|각각|추가|국내|글로벌|확인|기존|다만|기준|개선|분기|신규|가능|향후|주요|통해|공개|따른|대한|이후|따라|전년'
    r'|당사|동사|현재|최근|올해|내년|하반기|상반기|부문|위주|수준|규모|중심|관련|반영|발표|출시|진행|계획|목표'
    r'|긍정|부정|모두|경우|자료|리포트|미국|중국|일본|유럽|한국|산업|기업|종목|섹터|주식|증시|환경|기반|효과'
    r'|영향|핵심|필요|모멘텀|사이클|심리|밸류|체력|이제|단순|잠정|상장|그룹|개별|종전|갈수록|회사|아래|종가'
    r'|초과수익률|조사분석|추천기준일|추천종목|근거|제시|누적|구조|흐름|사업|기여|대응|잔고|전환'
    r'|특정|모든|당장|파악|추이|적극|고성장|본격|여부|전반|일부|이상|이하|기존과|비롯)')
CAND_STOP_EN = re.compile(r'^(YoY|QoQ|BUY|TP|OPM|GPM|EPS|PER|PBR|ROE|EV|EBITDA|Review|Preview|Complian\w*|Notice'
                          r'|Overweig\w*|Neutral|DAOL|Valuation|Performan\w*|BEP|ETF|IR|CEO|CFO|OP|NP|YTD|Q\d+\w*|H\d+\w*)$', re.I)
JOSA_END = re.compile(r'[가-힣]*(을|를|은|는|의|에|로|와|과|도|만|서|된|한|간|며|까지|부터|면서)$')

def discover_candidates(reports, days=90, min_recent=6, min_sectors=3, min_burst=3.0):
    compiled = [re.compile(p, re.I) for _, _, p in THEMES]
    latest = max(r['date'] for r in reports)
    pivot = (datetime.fromisoformat(latest) - timedelta(days=days)).date().isoformat()
    total, recent, sectors = defaultdict(set), defaultdict(set), defaultdict(set)
    n_new = sum(1 for r in reports if r['date'] >= pivot) or 1
    n_old = sum(1 for r in reports if r['date'] < pivot) or 1
    for r in reports:
        blob = report_blob(r)
        if not blob:
            continue
        s = norm_sector(r.get('sector'))
        for t in set(re.findall(r'[가-힣]{2,8}|[A-Z][A-Za-z0-9]{2,8}', blob)):
            if CAND_STOP.match(t) or CAND_STOP_EN.match(t) or JOSA_END.match(t):
                continue
            if any(p.search(t) for p in compiled):
                continue
            total[t].add(r['id']); sectors[t].add(s)
            if r['date'] >= pivot:
                recent[t].add(r['id'])
    out = []
    for t, ids in total.items():
        rc, old = len(recent[t]), len(ids) - len(recent[t])
        if rc < min_recent or len(sectors[t]) < min_sectors:
            continue
        burst = (rc / n_new) / ((old + 1) / n_old)
        if burst >= min_burst:
            out.append({'term': t, 'recent': rc, 'total': len(ids), 'sectors': len(sectors[t]),
                        'burst': round(burst, 1)})
    return sorted(out, key=lambda x: -x['burst'])[:15]

def render(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    template = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DAOL 섹터 콜라보 레이더</title>
<style>
:root{--ink:#172033;--muted:#687386;--line:#dce2ea;--blue:#eaf2fb;--green:#dff3e6;--red:#fde7e7;--gold:#fff3d6;--vs:#fbeaf3}
*{box-sizing:border-box}body{margin:0;background:#f4f6f9;color:var(--ink);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif}
.wrap{max-width:1500px;margin:auto;padding:24px}
.hero{background:linear-gradient(120deg,#2b1d4f,#5b3a87);color:#fff;padding:26px;border-radius:18px}
.hero h1{margin:0 0 6px;font-size:26px}.hero small{opacity:.85}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.stat{background:#ffffff22;border-radius:10px;padding:8px 14px}.stat b{font-size:19px;display:block}
.controls{display:grid;grid-template-columns:1fr 170px 190px;gap:10px;margin:18px 0 6px}
.controls input,.controls select{padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;font:inherit}
.cats{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0 18px}
.cat{padding:6px 13px;border-radius:999px;border:1px solid var(--line);background:#fff;cursor:pointer;font:inherit;font-size:13px}
.cat.on{background:#2b1d4f;color:#fff;border-color:#2b1d4f}
h2.sec{margin:26px 0 12px;font-size:19px}h2.sec small{color:var(--muted);font-weight:400;margin-left:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 3px 13px #20304a0c}
.card header{padding:15px 18px;border-bottom:1px solid var(--line)}
.card h3{margin:0;font-size:17px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.chip{padding:3px 9px;border-radius:999px;background:var(--blue);font-size:12px;font-weight:500}
.chip.수혜{background:var(--green)}.chip.피해{background:var(--red)}.chip.중립{background:#eef1f5}
.chip.collab{background:var(--gold);font-weight:700}.chip.vs{background:var(--vs);font-weight:700}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.idea{margin:0;padding:11px 18px;background:#faf7ef;border-bottom:1px solid var(--line);font-size:13px}
.idea b{color:#7a5b00}
.section{padding:12px 18px}.section h4{margin:0 0 7px;font-size:12.5px;color:#43516a;text-transform:uppercase;letter-spacing:.4px}
.ev{border-left:3px solid #b9a5e0;padding:2px 0 2px 10px;margin:8px 0}
.ev .meta{font-size:12px;color:var(--muted)}.ev .meta b{color:var(--ink)}
.ev .quote{margin-top:2px}
.vslane{display:grid;grid-template-columns:1fr 1fr;gap:0}
.vslane>div{padding:12px 16px}.vslane>div:first-child{border-right:1px dashed var(--line)}
.vslane h5{margin:0 0 6px;font-size:13px}
a{color:#1767aa;text-decoration:none}a:hover{text-decoration:underline}
.matrix-wrap{overflow-x:auto;background:#fff;border:1px solid var(--line);border-radius:14px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:7px 10px;border-bottom:1px solid #edf0f4;text-align:center;white-space:nowrap}
th:first-child,td:first-child{text-align:left;background:#fff}
thead th{background:#f7f9fc}
td.hot{background:var(--gold);font-weight:700}td.warm{background:var(--blue)}td.zero{color:#c6ccd6}
.empty{color:var(--muted);padding:16px}
.cand-wrap{display:flex;gap:8px;flex-wrap:wrap;background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px}
.cand{padding:7px 13px;border-radius:999px;background:#f0fbf2;border:1px solid #bfe3c8;font-size:13px}
.cand b{font-size:14px}.cand small{color:var(--muted);margin-left:5px}
details.more{padding:0 18px 12px}details.more summary{cursor:pointer;font-size:12.5px;color:var(--muted)}
@media(max-width:800px){.wrap{padding:12px}.controls{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.vslane{grid-template-columns:1fr}.vslane>div:first-child{border-right:0;border-bottom:1px dashed var(--line)}}
</style></head><body><div class="wrap">
<section class="hero"><h1>🤝 DAOL 섹터 콜라보 레이더</h1><small>같은 테마를 서로 다른 섹터가 동시에 다루면 = 콜라보 보고서 후보 · 방향이 갈리면 = 수혜 vs 피해 페어</small><div class="stats" id="stats"></div></section>
<div class="controls"><input id="q" placeholder="테마·애널리스트·섹터·종목 검색"><select id="period"><option value="30">최근 30일</option><option value="60">최근 60일</option><option value="90" selected>최근 90일</option><option value="180">최근 180일</option><option value="0">전체 기간</option></select><select id="minsec"><option value="2" selected>산업섹터 2개 이상</option><option value="3">산업섹터 3개 이상</option><option value="1">전체 테마 보기</option></select></div>
<div class="cats" id="cats"></div>
<h2 class="sec">⚔️ 수혜 vs 피해 — 방향이 갈리는 테마<small>대립 페어 보고서 아이디어</small></h2><div class="grid" id="vs"></div>
<h2 class="sec">🤝 콜라보 후보 테마<small>서로 다른 산업 섹터가 함께 언급</small></h2><div class="grid" id="cards"></div>
<h2 class="sec">🧩 종목 크로스 멘션<small>다른 섹터 리포트에 등장한 커버리지 종목 — 이미 일어나고 있는 콜라보</small></h2><div class="grid" id="cross"></div>
<h2 class="sec">🗺️ 테마 × 섹터 매트릭스<small>언급 리포트 수</small></h2><div class="matrix-wrap"><table id="matrix"></table></div>
<h2 class="sec">🌱 미등록 테마 후보<small>최근 90일 언급 급증 · 테마 사전에 없는 키워드 — 자동 발굴이라 노이즈 섞임</small></h2><div class="cand-wrap" id="cands"></div>
<p class="empty">키워드 사전 기반 자동 추출 결과입니다. 근거 문장의 원문 링크로 반드시 교차 확인하세요. · 생성 <span id="gen"></span></p>
</div>
<script>
const DATA=__PAYLOAD__;const $=s=>document.querySelector(s);
const esc=s=>(s??'').toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STRATEGY='전략/시황';let cat='';
$('#gen').textContent=(DATA.generated_at||'').slice(0,10);
const cats=['전체',...new Set(DATA.hits.map(h=>h.category))];
$('#cats').innerHTML=cats.map(c=>`<button class="cat${(c==='전체'?'':c)===cat?' on':''}" data-cat="${c==='전체'?'':c}">${c}</button>`).join('');
$('#cats').addEventListener('click',e=>{const b=e.target.closest('.cat');if(!b)return;cat=b.dataset.cat;[...document.querySelectorAll('.cat')].forEach(x=>x.classList.toggle('on',x.dataset.cat===cat));render()});
function cutoffDate(){const d=+$('#period').value;return d?new Date(Date.now()-d*864e5).toISOString().slice(0,10):''}
function dirOf(p){return p.pos>p.neg?'수혜':(p.neg>p.pos?'피해':'중립')}
function evHtml(e){return `<div class="ev"><div class="meta"><b>${esc(e.analyst)}</b> · ${esc(e.sector)} · ${e.date} · <span class="chip ${e.direction}" style="font-size:11px">${e.direction}</span></div><div class="quote">${esc(e.evidences[0]||e.title)}</div><div class="meta"><a href="${e.source_url}" target="_blank">보고서</a> · <a href="${e.post_url}" target="_blank">게시물</a></div></div>`}
function aggregate(){
 const cut=cutoffDate(),q=$('#q').value.toLowerCase();
 const hits=DATA.hits.filter(h=>(!cut||h.date>=cut)&&(!cat||h.category===cat)&&(!q||(h.theme+h.analyst+h.sector+h.company+h.title+(h.evidences||[]).join(' ')).toLowerCase().includes(q)));
 const byTheme=new Map();
 for(const h of hits){
  let t=byTheme.get(h.theme);if(!t){t={theme:h.theme,category:h.category,hits:[],parts:new Map()};byTheme.set(h.theme,t)}
  t.hits.push(h);
  let p=t.parts.get(h.analyst);if(!p){p={analyst:h.analyst,sector:h.sector,pos:0,neg:0,n:0,latest:''};t.parts.set(h.analyst,p)}
  p.n++;if(h.direction==='수혜')p.pos++;if(h.direction==='피해')p.neg++;if(h.date>p.latest)p.latest=h.date;
 }
 const themes=[...byTheme.values()].map(t=>{
  const parts=[...t.parts.values()].sort((a,b)=>b.n-a.n);
  const sectors=[...new Set(parts.filter(p=>p.sector!==STRATEGY).map(p=>p.sector))];
  const dirs=new Set(parts.map(dirOf));
  t.hits.sort((a,b)=>b.date.localeCompare(a.date));
  return {...t,parts,sectors,opposed:dirs.has('수혜')&&dirs.has('피해'),score:sectors.length*3+parts.length+t.hits.filter(h=>h.date>=new Date(Date.now()-45*864e5).toISOString().slice(0,10)).length*0.5};
 }).sort((a,b)=>b.score-a.score);
 return {hits,themes};
}
function render(){
 const {hits,themes}=aggregate();const minsec=+$('#minsec').value;
 const collab=themes.filter(t=>t.sectors.length>=minsec);
 const vs=themes.filter(t=>t.opposed&&t.sectors.length>=2);
 $('#stats').innerHTML=`<div class="stat"><b>${themes.length}</b>활성 테마</div><div class="stat"><b>${collab.length}</b>콜라보 후보</div><div class="stat"><b>${vs.length}</b>수혜vs피해</div><div class="stat"><b>${hits.length}</b>테마 언급</div>`;
 $('#cards').innerHTML=collab.map(t=>{
  const bySec=new Map();for(const p of t.parts){if(p.sector===STRATEGY)continue;if(!bySec.has(p.sector))bySec.set(p.sector,[]);bySec.get(p.sector).push(p.analyst)}
  const idea=[...bySec.entries()].slice(0,3).map(([s,as])=>`${s}(${as.join('·')})`).join(' × ');
  const chips=t.parts.map(p=>`<span class="chip ${dirOf(p)}">${esc(p.analyst)} · ${esc(p.sector)} · ${p.n}건</span>`).join('');
  const evs=t.hits.slice(0,4).map(evHtml).join('');
  const more=t.hits.length>4?`<details class="more"><summary>근거 ${t.hits.length-4}건 더 보기</summary>${t.hits.slice(4,16).map(evHtml).join('')}</details>`:'';
  return `<article class="card"><header><h3>${esc(t.theme)}<span class="chip">${esc(t.category)}</span><span class="chip collab">🤝 섹터 ${t.sectors.length}개</span>${t.opposed?'<span class="chip vs">⚔️ 방향 갈림</span>':''}</h3><div class="chips">${chips}</div></header><p class="idea"><b>콜라보 아이디어</b> — ${esc(idea)} 공동 「${esc(t.theme)}」 기획 보고서</p><section class="section"><h4>최근 근거</h4>${evs}</section>${more}</article>`;
 }).join('')||'<p class="empty">조건에 맞는 테마가 없습니다. 기간을 늘리거나 필터를 풀어보세요.</p>';
 $('#vs').innerHTML=vs.map(t=>{
  const good=t.parts.filter(p=>dirOf(p)==='수혜'),bad=t.parts.filter(p=>dirOf(p)==='피해');
  const pick=(ps,dir)=>ps.map(p=>{const h=t.hits.find(x=>x.analyst===p.analyst&&x.direction===dir);return h?evHtml(h):`<div class="ev"><div class="meta"><b>${esc(p.analyst)}</b> · ${esc(p.sector)}</div></div>`}).join('');
  return `<article class="card"><header><h3>${esc(t.theme)}<span class="chip">${esc(t.category)}</span><span class="chip vs">⚔️ 수혜 vs 피해</span></h3></header><div class="vslane"><div><h5>😊 수혜 시각</h5>${pick(good,'수혜')||'<p class="empty">—</p>'}</div><div><h5>😨 피해/부담 시각</h5>${pick(bad,'피해')||'<p class="empty">—</p>'}</div></div></article>`;
 }).join('')||'<p class="empty">현재 필터에서 방향이 갈리는 테마가 없습니다.</p>';
 renderCross();renderMatrix(themes);
}
function renderCross(){
 const cut=cutoffDate(),q=$('#q').value.toLowerCase();
 const rows=DATA.cross_mentions.filter(r=>(!cut||r.date>=cut)&&(!q||(r.company+r.analyst+r.owner_analyst+r.sector+r.evidence).toLowerCase().includes(q)));
 const byCo=new Map();
 for(const r of rows){const k=r.code||r.company;if(!byCo.has(k))byCo.set(k,[]);byCo.get(k).push(r)}
 const cross=[...byCo.values()].map(list=>({company:list[0].company,code:list[0].code,owner:list[0].owner_analyst,ownerSector:list[0].owner_sector,list:list.sort((a,b)=>b.date.localeCompare(a.date)),mentioners:[...new Set(list.map(x=>x.analyst))]})).sort((a,b)=>b.mentioners.length-a.mentioners.length||b.list.length-a.list.length);
 $('#cross').innerHTML=cross.slice(0,15).map(c=>`<article class="card"><header><h3>${esc(c.company)} <span class="chip">${c.code}</span><span class="chip collab">언급 ${c.mentioners.length}명</span></h3><div class="chips"><span class="chip 수혜">전담 ${esc(c.owner)} · ${esc(c.ownerSector)}</span>${c.mentioners.map(a=>`<span class="chip">${esc(a)}</span>`).join('')}</div></header><section class="section">${c.list.slice(0,4).map(r=>`<div class="ev"><div class="meta"><b>${esc(r.analyst)}</b> · ${esc(r.sector)} · ${r.date} · <a href="${r.post_url}" target="_blank">게시물</a></div><div class="quote">${esc(r.evidence)}</div></div>`).join('')}</section></article>`).join('')||'<p class="empty">해당 기간에 크로스 멘션이 없습니다.</p>';
}
function renderMatrix(themes){
 const secs=[...new Set(themes.flatMap(t=>t.parts.map(p=>p.sector)))].filter(s=>s!==STRATEGY).sort();
 if(!themes.length||!secs.length){$('#matrix').innerHTML='';return}
 const head=`<thead><tr><th>테마</th>${secs.map(s=>`<th>${esc(s)}</th>`).join('')}</tr></thead>`;
 const body=themes.map(t=>{
  const bySec={};for(const p of t.parts)bySec[p.sector]=(bySec[p.sector]||0)+p.n;
  return `<tr><td>${esc(t.theme)}</td>${secs.map(s=>{const n=bySec[s]||0;return `<td class="${n>=5?'hot':n?'warm':'zero'}">${n||'·'}</td>`}).join('')}</tr>`;
 }).join('');
 $('#matrix').innerHTML=head+`<tbody>${body}</tbody>`;
}
$('#cands').innerHTML=(DATA.candidates||[]).map(c=>`<span class="cand"><b>${esc(c.term)}</b><small>최근 ${c.recent}건 · 섹터 ${c.sectors}개 · ×${c.burst}</small></span>`).join('')||'<p class="empty">급증 후보가 없습니다.</p>';
['q','period','minsec'].forEach(id=>$('#'+id).addEventListener(id==='q'?'input':'change',render));
render();
</script></body></html>'''
    OUT_HTML.write_text(template.replace('__PAYLOAD__', data), encoding='utf-8')

def main():
    history = json.loads(HISTORY.read_text(encoding='utf-8'))
    reports = flatten_reports(history)
    hits = extract_hits(reports)
    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'daol_tone_history.json',
        'report_count': len(reports),
        'hits': hits,
        'cross_mentions': extract_cross_mentions(reports, coverage_map(reports)),
        'candidates': discover_candidates(reports),
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    render(payload)
    themes = sorted({h['theme'] for h in hits})
    print(json.dumps({'html': str(OUT_HTML), 'json': str(OUT_JSON), 'reports': len(reports),
                      'hits': len(hits), 'themes': len(themes)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
