import argparse, html, json, re, time
from io import BytesIO
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
# requests / bs4 / pypdf 는 네트워크·PDF 처리에만 필요하므로 각 함수 안에서 지연 임포트한다.
# (is_report 등 순수 로직은 표준 라이브러리만으로 임포트되어 테스트가 가볍다.)

ROOT=Path(__file__).resolve().parent
DATA_DIR=ROOT/'data'
CACHE=DATA_DIR
MSG_FILE=DATA_DIR/'daol_messages.json'
PDF_CACHE=DATA_DIR/'daol_pdf_text_cache.json'
HISTORY=ROOT/'data'/'daol_tone_history.json'
OUT=ROOT/'daol_research_tone.html'
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'}

def clean(s): return re.sub(r'\s+',' ',s or '').strip()

def pdf_text(source_url, cache):
    import requests
    from pypdf import PdfReader
    cached=cache.get(source_url)
    if cached:return cached
    result={'status':'failed','final_url':source_url,'text':'','error':''}
    for attempt in range(2):
        try:
            r=requests.get(source_url,headers=UA,timeout=(8,20),allow_redirects=True)
            r.raise_for_status()
            if len(r.content)>20_000_000:raise ValueError('PDF exceeds 20MB')
            text='\n'.join(page.extract_text() or '' for page in PdfReader(BytesIO(r.content)).pages)
            # 일부 인뎁스 PDF는 본문 폰트가 pypdf로 안 읽힌다(ToUnicode 누락) — 핵심 마커가 없으면
            # pypdfium2로 재추출해 더 나은 쪽을 쓴다(GS건설 인뎁스 TP 누락 사고 재발 방지).
            if '적정주가' not in text:
                try:
                    import pypdfium2 as _pdfium
                    _doc=_pdfium.PdfDocument(r.content)
                    alt='\n'.join(_doc[i].get_textpage().get_text_range() for i in range(len(_doc)))
                    if '적정주가' in alt or len(alt)>len(text):text=alt
                except Exception:pass
            text=re.sub(r'[ \t]+',' ',text);text=re.sub(r'\n{3,}','\n\n',text).strip()
            if len(text)<300:raise ValueError('PDF text is empty or scanned')
            result={'status':'pdf','final_url':r.url,'text':text,'error':''};break
        except ValueError as e:
            result['error']=f'{type(e).__name__}: {e}'[:240]
            break
        except Exception as e:
            result['error']=f'{type(e).__name__}: {e}'[:240]
            if attempt<1:time.sleep(1.5)
    cache[source_url]=result
    return result

def detail_lines(t, pattern, limit=6):
    chunks=re.split(r'\n+|(?<=[.!?])\s+|(?=[▶■●◆])',t or '')
    out=[]
    for chunk in chunks:
        x=clean(chunk)
        if 18<=len(x)<=700 and re.search(pattern,x,re.I):out.append(x[:650])
    return list(dict.fromkeys(out))[:limit]

def heading_excerpt(t, heading_pattern, max_chars=1400):
    m=re.search(heading_pattern,t or '',re.I)
    if not m:return ''
    excerpt=(t or '')[m.end():m.end()+max_chars*2]
    excerpt=re.split(r'\n\s*(?:II|III|IV|V|[2-9])\s*[.]|\n\s*Focus Chart',excerpt,maxsplit=1,flags=re.I)[0]
    return clean(excerpt)[:max_chars]

def pdf_details(t, is_industry):
    pitch=extract_section(t,r'(?:Pitch|투자\s*포인트|Investment\s*Point)')
    if not pitch:
        xs=detail_lines(t,r'Pitch|투자\s*포인트|매수\s*근거',4);pitch=' '.join(xs)[:1200]
    if not pitch:pitch=heading_excerpt(t,r'(?:I[.]\s*)?(?:Summary\s*&\s*Focus Chart|Summary|핵심\s*요약)',1200)
    if not pitch:
        xs=detail_lines(t,r'전망|판단|예상|매수|투자\s*기회|모멘텀',4);pitch=' '.join(xs)[:1200]
    conclusions=detail_lines(t,r'결론|투자전략|투자의견|전망|핵심\s*요약',5)
    valuation=detail_lines(t,r'Valuation|밸류에이션|적정주가\s*산출|목표주가\s*산출|PER|PBR|EV/EBITDA|WACC|DCF|적용\s*(?:배수|멀티플)',6)
    earnings=detail_lines(t,r'(?:실적|이익|매출|영업이익|EPS)\s*(?:추정|전망|예상|상향|하향)|추정치\s*(?:상향|하향)',6)
    preferred=detail_lines(t,r'최선호주|차선호주|Top[ -]?Pick|탑픽|선호주|선호\s*의견',5)
    conclusion=' '.join(conclusions)[:1400]
    if not conclusion and pitch:conclusion=pitch[:900]
    return {'pitch':pitch,'conclusion':conclusion,
            'valuation':valuation,'earnings_changes':earnings,'preferred_stocks':preferred}

def bootstrap_messages(days=130):
    import requests
    from bs4 import BeautifulSoup
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    session=requests.Session();session.headers.update(UA)
    cutoff=datetime.now(timezone.utc)-timedelta(days=days); before=None; by_id={}
    while True:
        url='https://t.me/s/daolresearch'+(f'?before={before}' if before else '')
        soup=BeautifulSoup(session.get(url,timeout=20).text,'lxml'); nodes=soup.select('.tgme_widget_message')
        if not nodes:break
        dates=[];ids=[]
        for node in nodes:
            mid=int(node['data-post'].rsplit('/',1)[-1]);ids.append(mid)
            t=node.select_one('time');body=node.select_one('.tgme_widget_message_text')
            if not t:continue
            dt=datetime.fromisoformat(t.get('datetime'));dates.append(dt)
            links=[a.get('href') for a in node.select('.tgme_widget_message_text a[href]') if (a.get('href') or '').startswith('http')]
            by_id[mid]={'id':mid,'date':t.get('datetime'),'text':clean(body.get_text(' ') if body else ''),'links':links,'post_url':f'https://t.me/daolresearch/{mid}'}
        if dates and min(dates)<cutoff:break
        nb=min(ids)
        if before==nb:break
        before=nb;time.sleep(.15)
    out=[by_id[k] for k in sorted(by_id) if datetime.fromisoformat(by_id[k]['date'])>=cutoff]
    MSG_FILE.write_text(json.dumps(out,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    return out

def refresh_messages(messages):
    import requests
    from bs4 import BeautifulSoup
    session=requests.Session();session.headers.update(UA)
    by_id={int(x['id']):x for x in messages}; after=max(by_id,default=0)
    while after:
        soup=BeautifulSoup(session.get(f'https://t.me/s/daolresearch?after={after}',timeout=20).text,'lxml')
        nodes=soup.select('.tgme_widget_message'); new=0; newest=after
        for node in nodes:
            mid=int(node['data-post'].rsplit('/',1)[-1]); newest=max(newest,mid)
            if mid in by_id: continue
            t=node.select_one('time'); body=node.select_one('.tgme_widget_message_text')
            if not t: continue
            links=[a.get('href') for a in node.select('.tgme_widget_message_text a[href]') if (a.get('href') or '').startswith('http')]
            by_id[mid]={'id':mid,'date':t.get('datetime'),'text':clean(body.get_text(' ') if body else ''),'links':links,'post_url':f'https://t.me/daolresearch/{mid}'};new+=1
        if newest==after or new==0:break
        after=newest;time.sleep(.2)
    out=sorted(by_id.values(),key=lambda x:int(x['id']))
    MSG_FILE.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    return out

REPORT_LINK_HINTS=('buly.kr','bit.ly','daolfn','daolsecurities')

def has_report_link(m):
    return any(any(h in (u or '') for h in REPORT_LINK_HINTS) for u in m.get('links',[]))

def is_report(m):
    t=m['text']
    # 데일리뉴스·모닝브리프·관세업데이트·증시캘린더는 리포트가 아니다.
    if re.search(r'데일리\s*(NEWS|뉴스)|Daily\s*Morning\s*Brief|Morning\s*Brief|관세\s*업데이트|증시\s*캘린더|DAOL\s*Daily',t[:200],re.I):return False
    has_report_original=bool(re.search(r'보고서\s*원문',t,re.I))
    has_compliance=bool(re.search(r'컴플라이언스|Compliance(?:\s*Notice)?',t,re.I))
    if has_report_original and has_compliance:return True
    # 주간 시황(위클리/주시뉴스)은 컴플라이언스 문구 없이 리포트 링크만 붙는다.
    is_weekly=bool(re.search(r'주시\s*뉴스|Weekly',t,re.I))
    if is_weekly and has_report_link(m):return True
    # 애널 리포트: 리포트 PDF 링크 + 애널 귀속 헤더 + 분석 신호(의견/목표가/종목코드/★)
    analyst_hdr=re.search(r'\[다올[^\]]*\]|담당\s*[가-힣]{2,4}',t[:200])
    signal=re.search(r'Overweight|Underweight|비중\s*(확대|축소)|목표주가|투자의견|\([0-9]{6}\)|★',t,re.I)
    return bool(has_report_link(m) and analyst_hdr and signal)

def analyst_sector(t):
    head=t[:350]
    roster={'고영민':'반도체/소부장','김연미':'전기전자/반도체','유지웅':'자동차/이차전지','오정하':'운송/로봇','박종현':'의료기기/화장품','이정우':'의료기기/화장품','이지수':'제약/바이오','김혜영':'인터넷·게임·레저','임도영':'엔터테인먼트','최광식':'조선/기계/방산','김지원':'금융','이다연':'음식료','조병현':'투자전략','박영도':'건설/부동산'}
    patterns=[
        r'다올(?:투자증권)?\s+([가-힣A-Za-z·/& ]{1,35}?)\s+담당\s+([가-힣]{2,4})',
        r'다올(?:투자증권)?\s+([가-힣A-Za-z·/& ]{1,35}?)\s+([가-힣]{2,4})(?:\s|\]|☎|/|\|)',
        r'([가-힣A-Za-z·/& ]{1,35}?)\s*[|/]\s*([가-힣]{2,4})\s*[|/]\s*DAOL',
    ]
    if '투자전략팀' in head:return '투자전략팀','투자전략'
    for p in patterns:
        m=re.search(p,head)
        if m:
            sector=clean(m.group(1)).strip('/| '); analyst=m.group(2)
            sector=re.sub(r'^(투자증권\s+)?','',sector)
            return analyst,sector
    for name,sector in roster.items():
        if name in head:return name,sector
    company_roster={'오리온':('이다연','음식료'),'오리온홀딩스':('이다연','음식료'),'농심':('이다연','음식료'),'삼양식품':('이다연','음식료')}
    for company,(name,sector) in company_roster.items():
        if company in head:return name,sector
    # 이름 태그 없는 산업자료 폴백: 서두 '[담배/음식료(Overweight)' 식 섹터 선언을 로스터 담당 섹터와
    # 대조해 유일 매칭일 때만 귀속. '반도체'처럼 복수 담당 섹터는 그대로 미분류(오귀속 방지).
    m=re.search(r'\[\s*([가-힣A-Za-z·/&, ]{2,30}?)\s*\((?:Overweight|Neutral|Underweight)',head,re.I)
    if m:
        segs={s.strip() for s in re.split(r'[/·,]',m.group(1)) if s.strip()}
        hits={name for name,rsec in roster.items() if segs & {x.strip() for x in re.split(r'[/·]',rsec)}}
        if len(hits)==1:return hits.pop(),m.group(1)
    return '미분류','미분류'

def report_title(t):
    base=clean(t.split('▶')[0])
    # 표준 서식('[헤드라인] ★ …')이 아닌 인사말형 포스트: 인사말 제거 후 첫 문장만 제목으로.
    if not base.startswith('['):
        base=re.sub(r'^안녕(?:하세요|하십니까)[,.!?]?\s*[^.!?]{0,60}?입니다[.!?]?\s*','',base)
        parts=re.split(r'(?<=[.!?])\s+',base,maxsplit=1)
        if parts and len(parts[0])>=10:base=parts[0]
    # ▶ 없이 ☑️/➡️/✅/💡 불릿이나 구분선으로 본문을 잇는 포스트: 첫 불릿부터는 제목이 아님
    cut=re.split(r'\s*(?:[☑✅➡💡]|-{4,})',base,maxsplit=1)[0]
    if len(cut)>=6:base=cut
    return base[:240]

def company_name(t):
    m=re.search(r'(?:\[|\]\s*|★\s*|#\s*|^)([가-힣A-Za-z0-9&. ]{1,40})\(([0-9]{6})\)',t)
    if m:return clean(m.group(1)),m.group(2)
    m=re.search(r'[★#]\s*([가-힣A-Za-z0-9&.]{2,30})',t)
    return (m.group(1),'') if m else ('산업/기타','')

def opinion(t):
    if re.search(r'\bOverweight\b|비중\s*확대',t,re.I):return '비중확대'
    if re.search(r'\bUnderweight\b|비중\s*축소',t,re.I):return '비중축소'
    if re.search(r'\bNeutral\b|중립',t,re.I):return '중립'
    return ''

def sentences(t):
    return [clean(x) for x in re.split(r'(?=[▶★■●]|(?<=[.!?])\s+)',t) if clean(x)]

def top_pick_lines(t):
    out=[]
    for s in sentences(t):
        if re.search(r'최선호주|차선호주|Top[ -]?Pick|탑픽|선호주|선호\s*의견',s,re.I): out.append(s[:420])
    return list(dict.fromkeys(out))

def amount_value(raw):
    x=raw.replace(',','').replace(' ','')
    m=re.search(r'([0-9]+(?:\.[0-9]+)?)',x)
    if not m:return None
    v=float(m.group(1))*(10000 if '만' in x else 1)
    return int(v)

def fmt_amount(v): return f'{v:,}원' if v is not None else '기존 TP 미표기'

def extract_section(t, label):
    """Extract a compact Telegram section such as Pitch/Conclusion/Issue."""
    m=re.search(rf'(?:▶\s*)?{label}\s*:\s*(.+?)(?=(?:▶\s*)?(?:Issue|Pitch|Rationale|Conclusion|결론|보고서\s*원문|Compliance|컴플라이언스)\s*:|(?:★|♣)\s*보고서|$)',t,re.I|re.S)
    return clean(m.group(1))[:900] if m else ''

def report_pitch(t):
    pitch=extract_section(t,'Pitch')
    if pitch:return pitch
    m=re.search(r'(?:핵심\s*주제\s*&\s*아이디어\s*요약|핵심\s*아이디어)\s*["“]?\s*(.+?)["”](?=\s*▶|$)',t,re.I|re.S)
    return clean(m.group(1))[:900] if m else ''

def industry_conclusion(t):
    explicit=extract_section(t,'(?:Conclusion|결론)')
    if explicit:return explicit
    pitch=report_pitch(t)
    if pitch:return pitch
    bullets=[s for s in sentences(t) if s.startswith('▶')]
    return clean(bullets[-1])[:900] if bullets else ''

def tp_changes(t,company):
    results=[]
    # Explicit old -> new amounts, for both raises and cuts.
    pats=[
      r'(?:적정주가|TP)[^▶★]{0,120}?([0-9][0-9,.]*\s*만?원)[^▶★]{0,50}?(?:에서|→|->)[^▶★]{0,30}?([0-9][0-9,.]*\s*만?원)[^▶★]{0,30}?(상향|하향)',
      r'([0-9][0-9,.]*\s*만?원)[^▶★]{0,20}?(?:에서|→|->)[^▶★]{0,20}?([0-9][0-9,.]*\s*만?원)[^▶★]{0,40}?(?:적정주가|TP)?[^▶★]{0,20}?(상향|하향)'
    ]
    spans=[]
    for pat in pats:
        for m in re.finditer(pat,t,re.I):
            old,new=amount_value(m.group(1)),amount_value(m.group(2))
            if old and new:spans.append((m.start(),m.end(),old,new,m.group(3)))
    # New TP only.
    for m in re.finditer(r'(?:적정주가|TP)(?:는|를|를\s*)?\s*([0-9][0-9,.]*\s*만?원)(?:으로)?\s*(상향|하향)',t,re.I):
        new=amount_value(m.group(1))
        if new and not any(a<=m.start()<=b for a,b,*_ in spans):spans.append((m.start(),m.end(),None,new,m.group(2)))
    # Header notation: TP 61,000원(상향/하향/유지).
    for m in re.finditer(r'(?:적정주가|TP)\s*([0-9][0-9,.]*\s*만?원)\s*\((상향|하향|유지)\)',t[:500],re.I):
        new=amount_value(m.group(1)); direction=m.group(2)
        if new and not any(a<=m.start()<=b for a,b,*_ in spans):spans.append((m.start(),m.end(),None,new,direction))
    # Industry notes often list several companies after declaring estimate/TP raises.
    if re.search(r'적정주가.{0,30}상향|적정주가를\s*상향',t,re.I):
        for m in re.finditer(r'([가-힣A-Za-z0-9&.]{2,30})\s*:\s*[^▶\n]{0,100}?적정주가\s*([0-9][0-9,.]*\s*만?원)',t,re.I):
            new=amount_value(m.group(2))
            if new:spans.append((m.start(),m.end(),None,new,'상향',m.group(1)))
    for span in sorted(spans,key=lambda x:x[0]):
        a,b,old,new,direction=span[:5];change_company=span[5] if len(span)>5 else company
        ctx=t[max(0,a-260):min(len(t),b+420)]
        reasons=[]
        if re.search(r'실적|이익\s*추정|EPS|어닝|매출\s*추정|추정치\s*상향',ctx,re.I):reasons.append('어닝/실적 추정 상향')
        if re.search(r'멀티플|PER|PBR|EV/EBITDA|WACC|할인율|목표배수|적용\s*배수',ctx,re.I):reasons.append('적용 멀티플 조정')
        if re.search(r'기준\s*연도|기준년도|롤[ -]?포워드|12개월\s*선행|12M|202[5-9]년\s*(?:EPS|BPS|실적)',ctx,re.I):reasons.append('밸류에이션 기준연도/방법 변경')
        if not reasons:reasons=['본문상 명시 배경 추가 확인 필요']
        results.append({'company':change_company,'direction':direction,'old':old,'new':new,'display':f'{fmt_amount(old)} → {fmt_amount(new)}' if old else f'{fmt_amount(new)} ({direction})','reasons':reasons,'evidence':clean(ctx)[:520]})
    # deduplicate identical changes
    return list({(x['company'],x['direction'],x['old'],x['new']):x for x in results}.values())

def source_link(m):
    preferred=[u for u in m.get('links',[]) if any(x in u for x in ['buly.kr','bit.ly','daolfn','daolsecurities'])]
    return preferred[-1] if preferred else m['post_url']

def analyze(messages, pdf_since='2025-05'):
    cache=json.loads(PDF_CACHE.read_text(encoding='utf-8')) if PDF_CACHE.exists() else {}
    cache_changed=False
    reports=[]
    pdf_started=time.monotonic();pdf_budget_seconds=900
    for m in sorted((x for x in messages if is_report(x)),key=lambda x:x['date'],reverse=True):
        t=m['text']; analyst,sector=analyst_sector(t); company,code=company_name(t); dt=datetime.fromisoformat(m['date'])
        month=dt.strftime('%Y-%m');is_industry=company in ('산업/기타',sector) or bool(re.search(r'\((?:Overweight|Neutral|Underweight)\)',t[:300],re.I))
        source=source_link(m);analysis_text=t;analysis_source='텔레그램 요약';final_url=source;pdf_error=''
        details={'pitch':report_pitch(t),'conclusion':industry_conclusion(t) if is_industry else '',
                 'valuation':detail_lines(t,r'Valuation|밸류에이션|적정주가\s*산출|PER|PBR|EV/EBITDA|WACC|DCF|적용\s*(?:배수|멀티플)',4),
                 'earnings_changes':detail_lines(t,r'(?:실적|이익|매출|영업이익|EPS)\s*(?:추정|전망|상향|하향)|추정치\s*(?:상향|하향)',4),
                 'preferred_stocks':top_pick_lines(t)}
        if month>=pdf_since and (source in cache or time.monotonic()-pdf_started<pdf_budget_seconds):
            was_cached=source in cache;p=pdf_text(source,cache);cache_changed=cache_changed or not was_cached
            final_url=p['final_url'];pdf_error=p['error']
            if p['status']=='pdf':
                analysis_text=p['text'];analysis_source='PDF 원문 분석';details=pdf_details(analysis_text,is_industry)
        elif month>=pdf_since:
            pdf_error='PDF 처리 시간 한도 초과 — 텔레그램 요약 사용'
        changes=tp_changes(analysis_text,company)
        reports.append({'id':m['id'],'date':dt.date().isoformat(),'month':month,'analyst':analyst,'sector':sector,'company':company,'code':code,'report_type':'산업자료' if is_industry else '기업자료','opinion':opinion(analysis_text) or opinion(t),'top_picks':details['preferred_stocks'],'preferred_stocks':details['preferred_stocks'],'tp_changes':changes,'tp_raises':[x for x in changes if x['direction']=='상향'],'pitch':details['pitch'],'industry_conclusion':details['conclusion'] if is_industry else '','report_conclusion':details['conclusion'],'valuation':details['valuation'],'earnings_changes':details['earnings_changes'],'analysis_source':analysis_source,'pdf_url':final_url,'pdf_error':pdf_error,'title':report_title(t),'summary':clean(t)[:900],'post_url':m['post_url'],'source_url':source})
    if cache_changed:
        PDF_CACHE.write_text(json.dumps(cache,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    return sorted(reports,key=lambda x:(x['date'],x['id']))

def monthly_summary(reports):
    grouped=defaultdict(lambda:defaultdict(lambda:{'sectors':set(),'opinions':[],'top_picks':[],'tp_changes':[],'reports':[]}))
    for r in reports:
        g=grouped[r['month']][r['analyst']];g['sectors'].add(r['sector']);g['reports'].append(r)
        if r['opinion']:g['opinions'].append({'value':r['opinion'],'date':r['date'],'source':r['post_url']})
        for x in r['top_picks']:g['top_picks'].append({'text':x,'date':r['date'],'source':r['post_url']})
        g['tp_changes'].extend([{**x,'date':r['date'],'source':r['post_url']} for x in r['tp_changes']])
    result=[]; previous={}
    # Compare preferences chronologically, then present newest month first.
    for month in sorted(grouped):
        analysts=[]
        for name,g in sorted(grouped[month].items()):
            current=[x['text'] for x in g['top_picks']]
            prior=previous.get((name,','.join(sorted(g['sectors']))),[])
            if current and not prior:change='해당 기간 최초 명시'
            elif current==prior:change='전월 대비 유지'
            elif current and prior:change='전월과 문구/구성 변경'
            else:change='해당 월 명시 없음'
            if current:previous[(name,','.join(sorted(g['sectors'])))]=current
            reports_latest=sorted(g['reports'],key=lambda x:(x['date'],x['id']),reverse=True)
            analysts.append({'analyst':name,'sectors':sorted(g['sectors']),'opinion':g['opinions'][-1]['value'] if g['opinions'] else '명시 없음','opinion_history':g['opinions'],'top_picks':g['top_picks'],'top_pick_change':change,'tp_changes':g['tp_changes'],'tp_raises':[x for x in g['tp_changes'] if x['direction']=='상향'],'report_count':len(reports_latest),'reports':reports_latest})
        result.append({'month':month,'analysts':analysts,'report_count':sum(x['report_count'] for x in analysts)})
    return list(reversed(result))

def render(data):
    payload=json.dumps(data,ensure_ascii=False).replace('</','<\\/')
    template='''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DAOL 리서치 톤 트래커</title>
<style>:root{--ink:#172033;--muted:#687386;--line:#dce2ea;--navy:#17365d;--blue:#eaf2fb;--green:#dff3e6;--red:#fde7e7;--gold:#fff3d6}*{box-sizing:border-box}body{margin:0;background:#f4f6f9;color:var(--ink);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif}.wrap{max-width:1500px;margin:auto;padding:28px}.hero{background:linear-gradient(120deg,#132d4f,#245a87);color:white;padding:28px;border-radius:18px}.hero h1{margin:0 0 8px;font-size:27px}.controls{display:grid;grid-template-columns:1fr 220px 220px;gap:10px;margin:18px 0}.controls input,.controls select{padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:white}.month{margin:22px 0}.month h2{display:flex;justify-content:space-between}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}.card{background:white;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 3px 13px #20304a0c}.card header{padding:16px 18px;background:#fff;border-bottom:1px solid var(--line)}.card h3{margin:0;font-size:18px}.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.chip{padding:3px 8px;border-radius:999px;background:var(--blue);font-size:12px}.opinion{font-weight:700}.비중확대{background:var(--green)}.비중축소{background:var(--red)}.중립{background:var(--gold)}.section{padding:13px 18px;border-bottom:1px solid #edf0f4}.section h4{margin:0 0 7px;font-size:13px;color:#43516a}.item{margin:6px 0}.change{color:#825b00;font-weight:650}.tp{border-left:3px solid #e27b35;padding-left:10px;margin:9px 0}.tp strong{font-size:15px}.evidence{color:var(--muted);font-size:12px;margin-top:4px}details{padding:11px 18px}details summary{cursor:pointer;font-weight:650}a{color:#1767aa;text-decoration:none}.report{padding:8px 0;border-top:1px dashed var(--line)}.empty{color:var(--muted)}@media(max-width:800px){.wrap{padding:12px}.controls{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}</style></head><body><div class="wrap"><section class="hero"><h1>DAOL 리서치 톤 트래커</h1><div>오래된 월부터 누적 · 섹터 의견 · 최선호주 변화 · TP 상향과 배경</div></section><div class="controls"><input id="q" placeholder="애널리스트·섹터·종목 검색"><select id="month"><option value="">전체 월</option></select><select id="analyst"><option value="">전체 애널리스트</option></select></div><main id="app"></main></div><script>const DATA=__PAYLOAD__;const $=s=>document.querySelector(s);const esc=s=>(s??'').toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const months=DATA.months.map(x=>x.month), analysts=[...new Set(DATA.months.flatMap(m=>m.analysts.map(a=>a.analyst)))].sort();months.forEach(x=>$('#month').insertAdjacentHTML('beforeend',`<option>${x}</option>`));analysts.forEach(x=>$('#analyst').insertAdjacentHTML('beforeend',`<option>${esc(x)}</option>`));
function render(){const q=$('#q').value.toLowerCase(),mo=$('#month').value,an=$('#analyst').value;let h='';for(const m of DATA.months){if(mo&&m.month!==mo)continue;let cards='';for(const a of m.analysts){const blob=JSON.stringify(a).toLowerCase();if(an&&a.analyst!==an||q&&!blob.includes(q))continue;const picks=a.top_picks.length?a.top_picks.map(x=>`<div class="item">${esc(x.text)} <a href="${x.source}" target="_blank">원문</a></div>`).join(''):'<div class="empty">명시 없음</div>';const tps=a.tp_raises.length?a.tp_raises.map(x=>`<div class="tp"><strong>${esc(x.company)} · ${esc(x.display)}</strong><div>${x.reasons.map(esc).join(' · ')}</div><div class="evidence">${esc(x.evidence)}</div><a href="${x.source}" target="_blank">근거 원문</a></div>`).join(''):'<div class="empty">TP 상향 명시 없음</div>';const reps=a.reports.map(r=>`<div class="report"><b>${r.date}</b> ${esc(r.company)} — ${esc(r.title)} <a href="${r.source_url}" target="_blank">보고서</a> · <a href="${r.post_url}" target="_blank">게시물</a></div>`).join('');cards+=`<article class="card"><header><h3>${esc(a.analyst)}</h3><div class="chips">${a.sectors.map(s=>`<span class="chip">${esc(s)}</span>`).join('')}<span class="chip opinion ${a.opinion}">${esc(a.opinion)}</span><span class="chip">보고서 ${a.report_count}건</span></div></header><section class="section"><h4>최선호주 / Top Pick</h4><div class="change">${esc(a.top_pick_change)}</div>${picks}</section><section class="section"><h4>TP 상향</h4>${tps}</section><details><summary>발간 보고서 펼치기</summary>${reps}</details></article>`}if(cards)h+=`<section class="month"><h2><span>${m.month}</span><small>${m.report_count} reports</small></h2><div class="grid">${cards}</div></section>`}$('#app').innerHTML=h||'<p class="empty">검색 결과가 없습니다.</p>'}['q','month','analyst'].forEach(id=>$('#'+id).addEventListener(id==='q'?'input':'change',render));render();</script></body></html>'''
    OUT.write_text(template.replace('__PAYLOAD__',payload),encoding='utf-8')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--refresh',action='store_true');ap.add_argument('--backfill-days',type=int,default=0);ap.add_argument('--pdf-since',default='2025-05');args=ap.parse_args()
    messages=bootstrap_messages(args.backfill_days) if args.backfill_days else (json.loads(MSG_FILE.read_text(encoding='utf-8')) if MSG_FILE.exists() else bootstrap_messages())
    if args.refresh:messages=refresh_messages(messages)
    reports=analyze(messages,args.pdf_since);months=monthly_summary(reports)
    HISTORY.parent.mkdir(exist_ok=True);data={'generated_at':datetime.now(timezone.utc).isoformat(),'source':'https://t.me/daolresearch','report_count':len(reports),'months':months}
    HISTORY.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');render(data)
    print(json.dumps({'html':str(OUT),'history':str(HISTORY),'reports':len(reports),'months':[x['month'] for x in months]},ensure_ascii=False))
if __name__=='__main__':main()
