# -*- coding: utf-8 -*-
"""
Kevin Kataria Stock Intelligence - Production Version
"""
from flask import Flask, jsonify, send_from_directory, request as freq
from flask_cors import CORS
import requests, re, os, time, json, threading, datetime, difflib, uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "YOUR_GROQ_KEY_HERE")
ALERT_THRESHOLD = 500_000
_pool = ThreadPoolExecutor(max_workers=10)

UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
YFH = {"User-Agent": UA, "Accept": "application/json,*/*", "Accept-Language": "en-US,en;q=0.9"}
SCH = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
MCH = {"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "en-IN,en;q=0.9", "Referer": "https://www.moneycontrol.com"}
GNH = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
NSE_H = {"User-Agent": UA, "Accept": "application/json,*/*", "Referer": "https://www.nseindia.com", "Accept-Language": "en-US,en;q=0.9"}

_http = requests.Session()
_retry = Retry(total=2, connect=2, read=2, backoff_factor=0.25, status_forcelist=[429,500,502,503,504], allowed_methods=["GET","POST"])
_adapter = HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=_retry)
_http.mount("https://", _adapter)
_http.mount("http://", _adapter)

def http_get(url, headers=None, timeout=8, **kwargs):
    return _http.get(url, headers=headers, timeout=timeout, **kwargs)

def http_post(url, headers=None, timeout=15, **kwargs):
    return _http.post(url, headers=headers, timeout=timeout, **kwargs)

# ============================================================
# SMART CACHE
# ============================================================
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and time.time() < item["exp"]:
            return item["data"]
    return None

def cache_set(key, data, ttl=300):
    with _cache_lock:
        _cache[key] = {"data": data, "exp": time.time() + ttl, "ts": time.strftime("%H:%M")}

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_watchlist.json")
_watchlist_lock = threading.Lock()

def _load_user_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list): return data
    except Exception: pass
    return []

def _save_user_watchlist(items):
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
    except Exception: pass

USER_WATCHLIST = _load_user_watchlist()

def clear_cache_key(key):
    with _cache_lock:
        _cache.pop(key, None)

def get_combined_watchlist():
    seen = set()
    combined = []
    for item in WATCHLIST:
        sym = str(item.get("symbol", "")).upper().strip()
        if not sym or sym in seen: continue
        seen.add(sym)
        combined.append(item)
    with _watchlist_lock:
        for item in USER_WATCHLIST:
            sym = str(item.get("symbol", "")).upper().strip()
            if not sym or sym in seen: continue
            seen.add(sym)
            combined.append(item)
    return combined

# ============================================================
# HELPERS
# ============================================================
def safe_float(v):
    try: return float(v) if v and str(v).strip() not in ("","Nil","nil","-","NA","--") else 0.0
    except: return 0.0

def ema(d, p):
    k, e = 2/(p+1), d[0]
    for x in d[1:]: e = x*k + e*(1-k)
    return e

def sma(d, p):
    return sum(d[-p:])/p if len(d) >= p else None

def yf_get(sym, range_="1y", interval="1d"):
    for base in ["query1","query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval={interval}&range={range_}"
            r = requests.get(url, headers=YFH, timeout=10)
            if r.ok: return r.json()["chart"]["result"][0]
        except: continue
    return None

# ============================================================
# CORE DATA FETCHERS (Thread-Safe)
# ============================================================
def get_price_data(sym):
    sym = sym.upper().strip()
    cached = cache_get(f"price:{sym}")
    if cached: return cached
    for base in ["query1","query2"]:
        try:
            r = http_get(f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS", headers=YFH, timeout=8)
            if not r.ok: continue
            meta = r.json()["chart"]["result"][0]["meta"]
            prev = meta.get("chartPreviousClose",0); p = meta.get("regularMarketPrice",0)
            data = {"success":True,"source":"Yahoo Finance","symbol":sym,
                    "name":  meta.get("longName") or meta.get("shortName",sym),
                    "price": p, "prev_close": prev,
                    "change": round(p-prev,2), "pct": round((p-prev)/prev*100,2) if prev else 0,
                    "high":   meta.get("regularMarketDayHigh",0), "low": meta.get("regularMarketDayLow",0),
                    "week52_high": meta.get("fiftyTwoWeekHigh",0), "week52_low":  meta.get("fiftyTwoWeekLow",0),
                    "volume": meta.get("regularMarketVolume",0)}
            cache_set(f"price:{sym}", data, ttl=60)
            return data
        except: continue
    return {"success":False,"error":f"Could not fetch price for {sym}."}

def get_technical_data(sym):
    sym = sym.upper().strip()
    cached = cache_get(f"tech:{sym}")
    if cached: return cached

    result = yf_get(sym, range_="1y", interval="1d")
    if not result:
        return {"success":False,"error":f"No chart data for {sym}."}

    q = result["indicators"]["quote"][0]
    C = [c for c in q.get("close",[]) if c is not None]
    if len(C) < 50: return {"success":False,"error":f"Not enough history for {sym}."}

    H = [h for h in q.get("high",[]) if h is not None] or C
    L = [l for l in q.get("low",[]) if l is not None] or C
    O = [o for o in q.get("open",[]) if o is not None] or C
    V = [v for v in q.get("volume",[]) if v is not None] or [0]*len(C)
    p = C[-1]
    ma20=sma(C,20); ma50=sma(C,50); ma200=sma(C,200)

    g,ls=[],[]
    for i in range(1,15):
        d=C[-i]-C[-i-1]; (g if d>0 else ls).append(abs(d))
    rsi=round(100-(100/(1+(sum(g)/14 if g else 0)/(sum(ls)/14 if ls else .001))),1)

    e12=ema(C[-60:],12); e26=ema(C[-60:],26); macd=e12-e26
    ms=[ema(C[-(60+i):-i],12)-ema(C[-(60+i):-i],26) for i in range(20,0,-1)]
    sig=ema(ms,9); hist=round(macd-sig,3)

    std20=(sum((c-ma20)**2 for c in C[-20:])/20)**0.5
    bbu=ma20+2*std20; bbl=ma20-2*std20

    l14=min(L[-14:]); h14=max(H[-14:])
    k=round((p-l14)/(h14-l14)*100,1) if h14!=l14 else 50

    tr=[max(H[-i]-L[-i],abs(H[-i]-C[-i-1]),abs(L[-i]-C[-i-1])) for i in range(1,15)]
    atr=round(sum(tr)/len(tr),2)

    dp,dm,tv=[],[],[]
    for i in range(1,15):
        u=H[-i]-H[-i-1]; d=L[-i-1]-L[-i]
        dp.append(u if u>d and u>0 else 0); dm.append(d if d>u and d>0 else 0)
        tv.append(max(H[-i]-L[-i],abs(H[-i]-C[-i-1]),abs(L[-i]-C[-i-1])))
    a14=sum(tv)/14 or .001
    dip=round((sum(dp)/a14)*100,1); dim=round((sum(dm)/a14)*100,1)
    adx=round(abs(dip-dim)/((dip+dim) or 1)*100,1)

    af,maf,sar,ep=0.02,0.2,L[-20],H[-20]
    for i in range(19,0,-1):
        sar=sar+af*(ep-sar); sar=min(sar,L[-i-1],L[-i])
        if H[-i]>ep: ep=H[-i]; af=min(af+0.02,maf)
    sar=round(sar,2)

    wR=round((max(H[-14:])-p)/(max(H[-14:])-min(L[-14:]))*-100,1) if max(H[-14:])!=min(L[-14:]) else -50

    obv=0; obv_list=[]
    for i in range(min(20,len(C)-1)):
        idx=-(i+1)
        if C[idx]>C[idx-1]: obv+=V[idx]
        elif C[idx]<C[idx-1]: obv-=V[idx]
        obv_list.append(obv)
    obv_rising=len(obv_list)>1 and obv_list[-1]>obv_list[0]

    o1,h1,l1,c1=O[-1],H[-1],L[-1],C[-1]; o2,c2=O[-2],C[-2]
    body=abs(c1-o1); rng=h1-l1 or .001; lw=min(o1,c1)-l1; uw=h1-max(o1,c1)
    pats=[]
    if body/rng<0.1: pats.append("Doji -- market undecided")
    if lw>2*body and uw<body: pats.append("Hammer -- potential bounce")
    if uw>2*body and lw<body: pats.append("Shooting Star -- potential fall")
    if c1>o1 and o2>c2 and o1<=c2 and c1>=o2: pats.append("Bullish Engulfing -- strong buy signal")
    if o1>c1 and c2>o2 and o1>=c2 and c1<=o2: pats.append("Bearish Engulfing -- strong sell signal")
    if not pats: pats.append("No major candlestick pattern today")

    b,br=0,0
    if p>ma20: b+=1
    else: br+=1
    if p>ma50: b+=1
    else: br+=1
    if ma200:
        if p>ma200: b+=2
        else: br+=2
        if ma50 and ma50>ma200: b+=2
        else: br+=2
    if rsi>50: b+=1
    else: br+=1
    if rsi<=30: b+=2
    if rsi>=70: br+=2
    if hist>0: b+=2
    else: br+=2
    if p<bbl: b+=2
    elif p>bbu: br+=2
    if k<=20: b+=1
    elif k>=80: br+=1
    if obv_rising: b+=1
    else: br+=1
    if p>sar: b+=1
    else: br+=1
    total=b+br or 1; score=round((b/total)*100)
    
    if score>=70: sig_txt="STRONG BUY"
    elif score>=57: sig_txt="BUY"
    elif score>=43: sig_txt="HOLD"
    elif score>=30: sig_txt="SELL"
    else: sig_txt="STRONG SELL"

    rh=max(H[-20:]); rl=min(L[-20:])
    w52h=max(H[-min(252,len(H)):]); w52l=min(L[-min(252,len(L)):])
    fr=rh-rl
    avg_v=sum(V[-20:])/20 if any(V[-20:]) else 1

    data={"success":True,"symbol":sym,"price":round(p,2),
        "ma20":round(ma20,2),"ma50":round(ma50,2),"ma200":round(ma200,2) if ma200 else None,
        "rsi":rsi,"macd":round(macd,3),"macd_signal":round(sig,3),"macd_hist":hist,
        "bb_upper":round(bbu,2),"bb_lower":round(bbl,2),"bb_mid":round(ma20,2),
        "stoch_k":k,"atr":atr,"adx":adx,"di_plus":dip,"di_minus":dim,
        "sar":sar,"williams":wR,"obv_rising":obv_rising,
        "bull_score":score,"signal":sig_txt,
        "target1":round(p+3*atr,2),"target2":round(p+6*atr,2),"stop_loss":round(p-2*atr,2),
        "support":round(rl,2),"resistance":round(rh,2),
        "week52_high":round(w52h,2),"week52_low":round(w52l,2),
        "fib_382":round(rh-0.382*fr,2),"fib_500":round(rh-0.5*fr,2),"fib_618":round(rh-0.618*fr,2),
        "golden_cross":bool(ma50 and ma200 and ma50>ma200),
        "patterns":pats,"vol_ratio":round(V[-1]/avg_v,1) if avg_v and V[-1] else 0,"today_vol":V[-1] if V else 0}
    cache_set(f"tech:{sym}", data, ttl=300)
    return data

def get_fundamental_data(sym):
    sym = sym.upper().strip()
    cached = cache_get(f"fund:{sym}")
    if cached: return cached

    def parse_screener(html):
        def fv(label):
            patterns = [
                r'<li[^>]*>\s*<span[^>]*>\s*' + re.escape(label) + r'\s*</span>\s*<span[^>]*>\s*([\d,\.]+)',
                re.escape(label) + r'[^<]{0,80}</span>\s*<span[^>]*>\s*([\d,\.]+)',
                re.escape(label) + r'[^<]*</td>\s*<td[^>]*>\s*([\d,\.]+)',
                re.escape(label) + r'[^<]{0,20}:\s*([\d,\.]+)',
                re.escape(label) + r'[\s\S]{0,100}?([\d]+(?:[,\.]\d+)+)',
            ]
            for pat in patterns:
                try:
                    m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
                    if m:
                        val = m.group(1).replace(",","").strip()
                        if val and float(val) != 0: return val
                except: pass
            return "N/A"
        def fc(metric):
            for period in ["5 Years","5 Yrs"]:
                m=re.search(metric+r'.*?'+period+r'.*?([\d.]+)%',html,re.IGNORECASE|re.DOTALL)
                if m: return m.group(1)+"%"
            return "N/A"
        def fh(label):
            m=re.search(label+r'[^%<]{0,80}([\d.]+)\s*%',html,re.IGNORECASE)
            return m.group(1)+"%" if m else "N/A"
        r={"pe":fv("Stock P/E"),"mcap":fv("Market Cap"),"pb":fv("Price to Book"),
           "div":fv("Dividend Yield"),"roe":fv("Return on equity"),"roce":fv("ROCE"),
           "debt":fv("Debt to equity"),"cr":fv("Current ratio"),"eps":fv("EPS in Rs"),
           "sc5":fc("Sales"),"pc5":fc("Profit"),
           "promoter":fh("Promoter"),"public":fh("Public"),"fii":fh("FII"),"dii":fh("DII")}
        if all(v=="N/A" for v in r.values()): return None
        def pf(v): return (v+"%") if v!="N/A" else "N/A"
        live_price = None
        try:
            pr = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=2d", headers=YFH, timeout=5)
            if pr.ok: live_price = pr.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
        except: pass
        return {"success":True,"source":"Screener.in", "price": live_price,
            "mcap":(r["mcap"]+" Cr") if r["mcap"]!="N/A" else "N/A", "pe":r["pe"],"fwd_pe":"N/A","peg":"N/A","pb":r["pb"],"eps":r["eps"],
            "book_value":"N/A","dividend":pf(r["div"]), "revenue":"N/A","rev_growth":"N/A","earnings_growth":"N/A",
            "profit_margin":"N/A","operating_margin":"N/A", "roe":pf(r["roe"]),"roce":pf(r["roce"]),
            "roa":"N/A","debt_equity":r["debt"],"current_ratio":r["cr"], "free_cashflow":"N/A","cagr_5y":r["sc5"],
            "sales_cagr":r["sc5"],"profit_cagr":r["pc5"], "promoter":r["promoter"],"public":r["public"],"fii":r["fii"],
            "dii":r["dii"], "insider_holding":r["promoter"],"institution_holding":"N/A","short_ratio":"N/A"}

    for suffix in ["/consolidated/","/"]:
        try:
            r=http_get(f"https://www.screener.in/company/{sym}{suffix}",headers=SCH,timeout=12)
            if r.status_code==200 and len(r.text)>5000:
                result=parse_screener(r.text)
                if result:
                    cache_set(f"fund:{sym}", result, ttl=3600)
                    return result
        except: continue

    for base in ["query1","query2"]:
        try:
            url=f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/{sym}.NS?modules=defaultKeyStatistics%2CfinancialData%2CsummaryDetail%2CmajorHoldersBreakdown"
            r=http_get(url,headers=YFH,timeout=8)
            if not r.ok: continue
            res=r.json()["quoteSummary"]["result"][0]
            fd=res.get("financialData",{}); sd=res.get("summaryDetail",{})
            ks=res.get("defaultKeyStatistics",{}); mh=res.get("majorHoldersBreakdown",{})
            def fv2(d,k):
                v=d.get(k)
                if isinstance(v,dict): return v.get("fmt",str(v.get("raw","N/A")))
                return str(v) if v not in (None,"") else "N/A"
            def pv(d,k):
                v=d.get(k)
                if isinstance(v,dict):
                    raw=v.get("raw")
                    if raw is not None: return str(round(float(raw)*100,2))+"%"
                return "N/A"
            yf_price = (sd.get("regularMarketPrice") or {}).get("raw") or (fd.get("currentPrice") or {}).get("raw")
            data={"success":True,"source":"Yahoo Finance", "price": yf_price,
                "mcap":fv2(sd,"marketCap"),"pe":fv2(sd,"trailingPE"),"fwd_pe":fv2(sd,"forwardPE"),
                "peg":fv2(ks,"pegRatio"),"pb":fv2(ks,"priceToBook"),"eps":fv2(ks,"trailingEps"),
                "book_value":fv2(ks,"bookValue"),"dividend":pv(sd,"dividendYield"),
                "revenue":fv2(fd,"totalRevenue"),"rev_growth":pv(fd,"revenueGrowth"),
                "earnings_growth":pv(fd,"earningsGrowth"),"profit_margin":pv(fd,"profitMargins"),
                "operating_margin":pv(fd,"operatingMargins"),"roe":pv(fd,"returnOnEquity"),
                "roce":"N/A","roa":pv(fd,"returnOnAssets"),"debt_equity":fv2(fd,"debtToEquity"),
                "current_ratio":fv2(fd,"currentRatio"),"free_cashflow":fv2(fd,"freeCashflow"),
                "cagr_5y":"N/A","sales_cagr":"N/A","profit_cagr":"N/A",
                "promoter":pv(mh,"insidersPercentHeld"),"public":"N/A","fii":"N/A","dii":"N/A",
                "insider_holding":pv(mh,"insidersPercentHeld"),
                "institution_holding":pv(mh,"institutionsPercentHeld"),"short_ratio":fv2(ks,"shortRatio")}
            cache_set(f"fund:{sym}", data, ttl=3600)
            return data
        except: continue

    return {"success":False,"error":f"Fundamental data unavailable for {sym}."}

def get_news_data(sym, refresh=False):
    sym = sym.upper().strip()
    cache_key = f"news:{sym}"
    if not refresh:
        cached = cache_get(cache_key)
        if cached: return cached

    all_items = []
    seen = set()

    def parse_pubdate(raw):
        try:
            dt = parsedate_to_datetime(raw)
            if not dt: return None
            if dt.tzinfo is None: dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except: return None

    def fetch(q):
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"
            r = http_get(url, headers=GNH, timeout=8)
            root = ET.fromstring(r.text)
            out = []
            for item in root.findall('.//item')[:8]:
                title = (item.findtext('title') or '').strip()
                link = (item.findtext('link') or '#').strip()
                raw_date = (item.findtext('pubDate') or '').strip()
                source_el = item.find('source')
                source = (source_el.text or 'News').strip() if source_el is not None else 'News'
                if not title: continue
                norm_title = re.sub(r'\s+', ' ', title)
                if norm_title in seen: continue
                seen.add(norm_title)
                dt = parse_pubdate(raw_date)
                out.append({"title": norm_title, "link": link, "date": dt.strftime("%d %b %Y %H:%M UTC") if dt else raw_date[:25],
                            "published_at": dt.isoformat() if dt else "", "source": source, "query": q})
            return out
        except: return []

    queries = [f'"{sym}" NSE stock latest today', f'"{sym}" share price today India', f'"{sym}" results OR order OR target today']
    with ThreadPoolExecutor(max_workers=3) as pool:
        for items in pool.map(fetch, queries): all_items.extend(items)

    def sort_key(item):
        raw = item.get("published_at") or ""
        try: return datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except: return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    final = sorted(all_items, key=sort_key, reverse=True)
    data = {"success": True, "news": final[:12], "fetched_at": datetime.datetime.utcnow().isoformat() + "Z"}
    cache_set(cache_key, data, ttl=120)
    return data

def get_sentiment_data(sym):
    sym=sym.upper().strip()
    cached=cache_get(f"sent:{sym}")
    if cached: return cached
    BULL=["buy","bullish","surge","rally","gain","up","strong","rise","upgrade","outperform","breakout","profit","record","high","positive","beat"]
    BEAR=["sell","bearish","crash","drop","fall","loss","weak","decline","downgrade","caution","risk","miss","negative","concern","debt","fraud"]
    def score(titles,src):
        bc=sum(1 for t in titles for w in BULL if w in t.lower())
        sc_=sum(1 for t in titles for w in BEAR if w in t.lower()); total=bc+sc_
        if not total: return {"source":src,"bull":0,"bear":0,"score":"Neutral","count":len(titles)}
        bp=round((bc/total)*100)
        return {"source":src,"bull":bp,"bear":100-bp,"score":"Bullish" if bp>60 else ("Bearish" if (100-bp)>60 else "Neutral"),"count":len(titles)}
    def g_news():
        t=re.findall(r'<title>(.*?)</title>',requests.get(f"https://news.google.com/rss/search?q={sym}+stock+NSE&hl=en-IN&gl=IN&ceid=IN:en",headers=GNH,timeout=6).text)[2:15]
        return score(t,"Google News")
    def reddit():
        try:
            posts=requests.get(f"https://www.reddit.com/search.json?q={sym}+india+stock&sort=new&limit=20&t=week",headers={"User-Agent":"stockbot/1.0"},timeout=6).json()["data"]["children"]
            return score([(p["data"].get("title","")+p["data"].get("selftext","")) for p in posts],"Reddit")
        except: return {"source":"Reddit","bull":0,"bear":0,"score":"Neutral","count":0}
    def mc():
        try:
            raw=requests.get(f"https://www.moneycontrol.com/news/tags/{sym.lower()}.html",headers=MCH,timeout=6).text
            return score([re.sub(r'<.*?>','',t).strip() for t in re.findall(r'<h2[^>]*>(.*?)</h2>',raw,re.DOTALL)][:15],"Moneycontrol")
        except: return {"source":"Moneycontrol","bull":0,"bear":0,"score":"Neutral","count":0}
    def et():
        try:
            raw=requests.get(f"https://economictimes.indiatimes.com/topic/{sym.lower()}-share-price",headers={"User-Agent":UA},timeout=6).text
            return score([re.sub(r'<.*?>','',t).strip() for t in re.findall(r'<h3[^>]*>(.*?)</h3>',raw,re.DOTALL)][:15],"Economic Times")
        except: return {"source":"Economic Times","bull":0,"bear":0,"score":"Neutral","count":0}
    
    results=[]
    futs={_pool.submit(f):f.__name__ for f in [g_news,reddit,mc,et]}
    for fut in as_completed(futs,timeout=12):
        try: results.append(fut.result())
        except: pass
    valid=[r for r in results if r["bull"]+r["bear"]>0]
    avg_bull=round(sum(r["bull"] for r in valid)/len(valid)) if valid else 50
    data={"success":True,"symbol":sym,"sources":results,"avg_bull":avg_bull,
          "overall":"Bullish" if avg_bull>60 else ("Bearish" if (100-avg_bull)>60 else "Neutral")}
    cache_set(f"sent:{sym}",data,ttl=600)
    return data

# ============================================================
# VERDICT & CONTEXT BUILDERS
# ============================================================
def build_verdict_data(sym):
    sym = sym.upper().strip()
    cached = cache_get(f"verdict:{sym}")
    if cached: return cached

    with ThreadPoolExecutor(max_workers=4) as pool:
        tf = pool.submit(get_technical_data, sym)
        ff = pool.submit(get_fundamental_data, sym)
        nf = pool.submit(get_news_data, sym, False)
        sf = pool.submit(get_sentiment_data, sym)
        t = tf.result(timeout=20)
        f = ff.result(timeout=15)
        n = nf.result(timeout=12)
        s = sf.result(timeout=12)

    if not t.get("success"): return {"success": False, "error": "Technical data failed."}

    avg_bull = s.get("avg_bull", 50)
    news_txt = "\n".join([x["title"] for x in n.get("news", [])[:5]])
    prompt = (
        f"LIVE DATA FOR {sym} -- USE ONLY THESE NUMBERS:\n"
        f"Price:Rs.{t.get('price','N/A')} | MA20:Rs.{t.get('ma20','N/A')} | MA50:Rs.{t.get('ma50','N/A')}\n"
        f"RSI:{t.get('rsi','N/A')} | MACD:{t.get('macd_hist','N/A')} | ADX:{t.get('adx','N/A')} | Stoch:{t.get('stoch_k','N/A')}%\n"
        f"Bollinger:Rs.{t.get('bb_lower','N/A')}-Rs.{t.get('bb_upper','N/A')} | SAR:Rs.{t.get('sar','N/A')}\n"
        f"Score:{t.get('bull_score','N/A')}% -> {t.get('signal','N/A')} | Support:Rs.{t.get('support','N/A')} | Resistance:Rs.{t.get('resistance','N/A')}\n"
        f"Stop:Rs.{t.get('stop_loss','N/A')} | T1:Rs.{t.get('target1','N/A')} | T2:Rs.{t.get('target2','N/A')}\n"
        f"PE:{f.get('pe','N/A')} | ROE:{f.get('roe','N/A')} | ROCE:{f.get('roce','N/A')} | Promoter:{f.get('promoter','N/A')}\n"
        f"Sentiment:{avg_bull}% Bullish\nHeadlines:\n{news_txt}\n\n"
        "VERDICT: [BUY/SELL/HOLD]\nCONFIDENCE: [X%]\nREASONING:\n- point with number\n- point\n"
        f"CURRENT PRICE: Rs.{t.get('price','N/A')}\nTARGET 3M: Rs.[]\nTARGET 12M: Rs.[]\nSTOP LOSS: Rs.{t.get('stop_loss','N/A')}\n"
        "RISK: [Low/Medium/High]\nBEST FOR: [Short-term/Long-term/Both]\nDISCLAIMER: Not financial advice."
    )
    try:
        resp = http_post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "max_tokens": 800,
                  "messages": [{"role": "system", "content": "Stock analyst. Use ONLY the numbers provided."},
                               {"role": "user", "content": prompt}]}, timeout=25)
        resp.raise_for_status()
        js = resp.json()
        if "choices" in js and len(js["choices"]) > 0:
            txt = js["choices"][0]["message"]["content"]
        else:
            raise Exception("Invalid Groq API response format")
    except Exception as e:
        return {"success": False, "error": f"AI unavailable: {str(e)}"}

    def find(p):
        m = re.search(p, txt)
        return m.group(1).strip() if m else None

    data = {
        "success": True, "symbol": sym, "price": t.get("price"),
        "verdict": find(r"VERDICT:\s*(.+)") or t.get("signal"),
        "confidence": find(r"CONFIDENCE:\s*(.+)") or "N/A",
        "target_3m": find(r"TARGET 3M:\s*Rs\.([0-9,./]+)") or str(t.get("target1")),
        "target_12m": find(r"TARGET 12M:\s*Rs\.([0-9,./]+)") or str(t.get("target2")),
        "stop_loss": find(r"STOP LOSS:\s*Rs\.([0-9,./]+)") or str(t.get("stop_loss")),
        "risk": find(r"RISK:\s*(.+)") or "Medium",
        "best_for": find(r"BEST FOR:\s*(.+)") or "Both",
        "reasoning": re.findall(r"- (.+)", txt)[:4],
        "full_text": txt, "tech": t, "fundamental": f, "sentiment": avg_bull,
    }
    cache_set(f"verdict:{sym}", data, ttl=300)
    return data

def build_stock_context(sym):
    sym = (sym or "").upper().strip()
    if not sym: return {"success": False, "error": "Symbol is required."}

    cache_key = f"context:{sym}"
    cached = cache_get(cache_key)
    if cached: return cached

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            "price": pool.submit(get_price_data, sym),
            "technical": pool.submit(get_technical_data, sym),
            "fundamental": pool.submit(get_fundamental_data, sym),
            "news": pool.submit(get_news_data, sym, False),
            "sentiment": pool.submit(get_sentiment_data, sym),
            "verdict": pool.submit(build_verdict_data, sym),
        }
        data = {k: f.result(timeout=30) for k, f in futures.items()}

    context = {"success": True, "symbol": sym, "data_used": ["price", "technicals", "fundamentals", "news", "sentiment", "verdict"]}
    context.update(data)
    cache_set(cache_key, context, ttl=120)
    return context

# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def index():
    for path in [os.path.join(os.path.dirname(os.path.abspath(__file__)),"static","index.html"), "index.html"]:
        if os.path.exists(path): return send_from_directory(os.path.dirname(path), os.path.basename(path))
    return "Stock Intelligence Pro - index.html not found", 404

@app.route("/api/health")
def api_health():
    return jsonify({"success": True, "status": "ok", "cache_keys": len(_cache)})

@app.route("/api/price/<symbol>")
def route_price(symbol): return jsonify(get_price_data(symbol))

@app.route("/api/technical/<symbol>")
def route_technical(symbol): return jsonify(get_technical_data(symbol))

@app.route("/api/fundamental/<symbol>")
def route_fundamental(symbol): return jsonify(get_fundamental_data(symbol))

@app.route("/api/sentiment/<symbol>")
def route_sentiment(symbol): return jsonify(get_sentiment_data(symbol))

@app.route("/api/news/<symbol>")
def route_news(symbol): 
    refresh = str(freq.args.get("refresh", "")).lower() in ("1", "true", "yes")
    return jsonify(get_news_data(symbol, refresh))

@app.route("/api/verdict/<symbol>")
def route_verdict(symbol): return jsonify(build_verdict_data(symbol))

# ============================================================
# SCREENER ROUTE (Now Thread-Safe)
# ============================================================
@app.route("/api/screener")
def screener():
    def fp(k, default):
        v = freq.args.get(k, "")
        try: return float(v) if str(v).strip() else default
        except: return default

    def to_num(v):
        try:
            if v is None: return None
            if isinstance(v, (int, float)): return float(v)
            s = str(v).strip().replace(",", "").replace("%", "").replace("Cr", "").replace("cr", "")
            if s in ("", "N/A", "NA", "--", "-", "Nil", "nil", "None"): return None
            return float(s)
        except: return None

    def passes(val, min_val=None, max_val=None, required=False):
        if val is None: return not required
        if min_val is not None and val < min_val: return False
        if max_val is not None and val > max_val: return False
        return True

    min_pe = fp("min_pe", 0); max_pe = fp("max_pe", 9999)
    max_peg = fp("max_peg", 9999); min_roe = fp("min_roe", -9999)
    max_debt = fp("max_debt", 9999)
    min_mcap = fp("min_mcap", 0); max_mcap = fp("max_mcap", 9999999999)
    min_promoter = fp("min_promoter", 0); max_promoter = fp("max_promoter", 100)
    min_fii = fp("min_fii", 0); max_fii = fp("max_fii", 100)
    min_dii = fp("min_dii", 0); max_dii = fp("max_dii", 100)
    min_public = fp("min_public", 0); max_public = fp("max_public", 100)

    pe_required = min_pe > 0 or max_pe < 9999
    peg_required = max_peg < 9999; roe_required = min_roe > -9999
    debt_required = max_debt < 9999
    mcap_required = min_mcap > 0 or max_mcap < 9999999999
    promoter_required = min_promoter > 0 or max_promoter < 100
    fii_required = min_fii > 0 or max_fii < 100
    dii_required = min_dii > 0 or max_dii < 100
    public_required = min_public > 0 or max_public < 100

    raw_symbols = [s.strip().upper() for s in freq.args.get("symbols", "").split(",") if s.strip()]
    base_universe = ["RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","HINDUNILVR","ITC","SBIN", "BHARTIARTL","KOTAKBANK","BAJFINANCE","LT","HCLTECH","ASIANPAINT","AXISBANK", "MARUTI","NESTLEIND","WIPRO","ULTRACEMCO","TITAN","SUNPHARMA","ONGC","NTPC"]
    
    symbols = []
    seen = set()
    for sym in raw_symbols + base_universe:
        if sym and sym not in seen:
            seen.add(sym); symbols.append(sym)

    symbols_to_scan = symbols[:40] # Capped for cloud response times

    started = time.time()
    results, errors = [], []

    def fetch_symbol(sym):
        try:
            data = get_fundamental_data(sym)
            if not data or not data.get("success"): return None

            pe = to_num(data.get("pe")); peg = to_num(data.get("peg"))
            roe = to_num(data.get("roe")); debt = to_num(data.get("debt_equity"))
            mcap_cr = to_num(data.get("mcap"))
            promoter = to_num(data.get("promoter"))
            fii = to_num(data.get("fii")); dii = to_num(data.get("dii")); public = to_num(data.get("public"))
            price = to_num(data.get("price"))

            if not passes(pe, min_pe if min_pe > 0 else None, max_pe if max_pe < 9999 else None, pe_required): return None
            if not passes(peg, None, max_peg if max_peg < 9999 else None, peg_required): return None
            if not passes(roe, min_roe if min_roe > -9999 else None, None, roe_required): return None
            if not passes(debt, None, max_debt if max_debt < 9999 else None, debt_required): return None
            if not passes(mcap_cr, min_mcap if min_mcap > 0 else None, max_mcap if max_mcap < 9999999999 else None, mcap_required): return None
            if not passes(promoter, min_promoter if min_promoter > 0 else None, max_promoter if max_promoter < 100 else None, promoter_required): return None
            if not passes(fii, min_fii if min_fii > 0 else None, max_fii if max_fii < 100 else None, fii_required): return None
            if not passes(dii, min_dii if min_dii > 0 else None, max_dii if max_dii < 100 else None, dii_required): return None
            if not passes(public, min_public if min_public > 0 else None, max_public if max_public < 100 else None, public_required): return None

            return {
                "symbol": sym, "price": round(price, 2) if price else None,
                "pe": round(pe, 1) if pe else None, "peg": round(peg, 2) if peg else None,
                "roe": round(roe, 1) if roe else None, "debt_equity": round(debt, 2) if debt else None,
                "mcap_cr": round(mcap_cr, 1) if mcap_cr else None, "promoter": round(promoter, 1) if promoter else None,
                "fii": round(fii, 1) if fii else None, "dii": round(dii, 1) if dii else None,
                "public": round(public, 1) if public else None,
            }
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:80]}")
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_symbol, sym): sym for sym in symbols_to_scan}
        for fut in as_completed(futures, timeout=42):
            try:
                row = fut.result(timeout=1)
                if row: results.append(row)
            except: pass

    results.sort(key=lambda x: (-(x.get("roe") or -9999), x.get("pe") or 999999))

    return jsonify({
        "success": True, "stocks": results, "total": len(results),
        "scanned": len(symbols_to_scan), "elapsed_sec": round(time.time() - started, 1), "errors": errors[:5]
    })

# Add Watchlist, Alerts & JSON Handlers
def load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f: return json.load(f)
    except: pass
    return []

def save_json(path, data):
    try:
        with open(path,"w") as f: json.dump(data,f)
        return True
    except: return False

WATCHLIST = [
    {"name":"ACME Solar Holdings", "symbol":"ACMESOLAR",  "ref_price":248.69,  "promoter":83.29},
    {"name":"Aditya Birla Capital","symbol":"ABCAPITAL",  "ref_price":326.30,  "promoter":68.58},
]

@app.route("/api/watchlist")
def watchlist():
    cached=cache_get("watchlist")
    if cached: return jsonify(cached)
    live_watchlist = get_combined_watchlist()
    results = []
    
    def analyse_single(stock):
        sym=stock["symbol"]
        result={"name":stock["name"],"symbol":sym,"ref_price":stock["ref_price"],"live_price":None,"change_pct":None,"pnl_pct":None,"signal":"N/A"}
        try:
            d = get_technical_data(sym)
            if d.get("success"):
                result["live_price"] = d["price"]
                result["change_pct"] = 0 # Not tracking daily change here for brevity
                ref_price = safe_float(stock.get("ref_price", 0))
                result["pnl_pct"] = round((d["price"]-ref_price)/ref_price*100,2) if ref_price else None
                result["signal"] = d["signal"]
        except: pass
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(analyse_single, s) for s in live_watchlist]
        for f in as_completed(futures, timeout=30):
            try: results.append(f.result(timeout=5))
            except: pass
            
    summary={"total":len(results)}
    result={"success":True,"stocks":results,"summary":summary}
    cache_set("watchlist",result,ttl=120)
    return jsonify(result)

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
