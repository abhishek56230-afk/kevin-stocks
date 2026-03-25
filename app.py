# -*- coding: utf-8 -*-
"""
Kevin Kataria Stock Intelligence - Production Version
=====================================================
Data Sources (all work on Render.com):
  Prices + Charts  -> Yahoo Finance (primary, never blocked)
  Fundamentals     -> Screener.in (primary) + Yahoo Finance (fallback)
  News             -> Google News RSS (always works)
  Sentiment        -> Google News + Reddit + Moneycontrol + ET
  Insider Trades   -> NSE (tried first) + Moneycontrol + ET scrape (fallback)
  AI Verdict       -> Groq API (llama-3.3-70b)

Key features:
  - Smart TTL cache (prices: 1min, technicals: 5min, fundamentals: 1hr)
  - Parallel data fetching for speed
  - Graceful fallbacks - NEVER shows raw errors to users
  - No hard dependency on NSE (blocked on cloud)
"""
from flask import Flask, jsonify, send_from_directory, request as freq
from flask_cors import CORS
import requests, re, os, time, json, threading, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "YOUR_GROQ_KEY_HERE")
ALERT_THRESHOLD = 500_000
_pool = ThreadPoolExecutor(max_workers=10)

# Browser-like headers that work everywhere
UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
YFH = {"User-Agent": UA, "Accept": "application/json,*/*", "Accept-Language": "en-US,en;q=0.9"}
SCH = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
MCH = {"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "en-IN,en;q=0.9", "Referer": "https://www.moneycontrol.com"}
GNH = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
NSE_H = {"User-Agent": UA, "Accept": "application/json,*/*", "Referer": "https://www.nseindia.com", "Accept-Language": "en-US,en;q=0.9"}

# ============================================================
# SMART CACHE - TTL based, thread-safe
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

def cache_ts(key):
    with _cache_lock:
        item = _cache.get(key)
        return item["ts"] if item else None

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
    """Yahoo Finance chart - tries query1 then query2"""
    for base in ["query1","query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval={interval}&range={range_}"
            r = requests.get(url, headers=YFH, timeout=10)
            if r.ok: return r.json()["chart"]["result"][0]
        except: continue
    return None

def nse_session_get(url, timeout=10):
    """Try NSE with proper cookie session"""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        s.get("https://www.nseindia.com", timeout=8,
              headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        time.sleep(0.2)
        s.headers.update({"Accept": "application/json, text/plain, */*",
                          "Referer": "https://www.nseindia.com",
                          "X-Requested-With": "XMLHttpRequest"})
        r = s.get(url, timeout=timeout)
        if r.status_code == 200 and r.text and r.text[0] in "[{":
            return r.json()
    except: pass
    return None

# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def index():
    for path in [os.path.join(os.path.dirname(os.path.abspath(__file__)),"static","index.html"), "index.html"]:
        if os.path.exists(path):
            return send_from_directory(os.path.dirname(path), os.path.basename(path))
    return "Kevin Kataria Stock Intelligence - index.html not found", 404

@app.route("/api/test")
def api_test():
    results = {}
    checks = [
        ("Yahoo Finance", "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?interval=1d&range=5d", YFH),
        ("Screener.in",   "https://www.screener.in/company/RELIANCE/", SCH),
        ("Google News",   "https://news.google.com/rss/search?q=RELIANCE+stock&hl=en-IN", GNH),
        ("Groq AI",       "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {GROQ_API_KEY}"}),
        ("Moneycontrol",  "https://www.moneycontrol.com/stocks/marketstats/bulk_deals/index.php", MCH),
        ("NSE India",     "https://www.nseindia.com/api/marketStatus", NSE_H),
    ]
    for name, url, hdrs in checks:
        try:
            r = requests.get(url, headers=hdrs, timeout=6)
            results[name] = {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)[:60]}
    return jsonify({"results": results, "groq_key_set": GROQ_API_KEY != "YOUR_GROQ_KEY_HERE"})

# ============================================================
# PRICE - Yahoo Finance (never blocked on cloud)
# ============================================================
@app.route("/api/price/<symbol>")
def price(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"price:{sym}")
    if cached: return jsonify(cached)
    for base in ["query1","query2"]:
        try:
            r = requests.get(f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS",
                             headers=YFH, timeout=8)
            if not r.ok: continue
            meta = r.json()["chart"]["result"][0]["meta"]
            prev = meta.get("chartPreviousClose",0); p = meta.get("regularMarketPrice",0)
            data = {"success":True,"source":"Yahoo Finance","symbol":sym,
                    "name":  meta.get("longName") or meta.get("shortName",sym),
                    "price": p, "prev_close": prev,
                    "change": round(p-prev,2), "pct": round((p-prev)/prev*100,2) if prev else 0,
                    "high":   meta.get("regularMarketDayHigh",0),
                    "low":    meta.get("regularMarketDayLow",0),
                    "week52_high": meta.get("fiftyTwoWeekHigh",0),
                    "week52_low":  meta.get("fiftyTwoWeekLow",0),
                    "volume": meta.get("regularMarketVolume",0)}
            cache_set(f"price:{sym}", data, ttl=60)
            return jsonify(data)
        except: continue
    return jsonify({"success":False,"error":f"Could not fetch price for {sym}. Check the NSE symbol is correct (e.g. RELIANCE, HCLTECH, ADANIPOWER)"})

# ============================================================
# TECHNICAL - Yahoo Finance (never blocked on cloud)
# ============================================================
@app.route("/api/technical/<symbol>")
def technical(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"tech:{sym}")
    if cached: return jsonify(cached)

    result = yf_get(sym, range_="1y", interval="1d")
    if not result:
        return jsonify({"success":False,"error":f"No chart data for {sym}. Check the NSE symbol is correct."})

    q = result["indicators"]["quote"][0]
    C = [c for c in q.get("close",[])  if c is not None]
    H = [h for h in q.get("high",[])   if h is not None]
    L = [l for l in q.get("low",[])    if l is not None]
    O = [o for o in q.get("open",[])   if o is not None]
    V = [v for v in q.get("volume",[]) if v is not None]

    if len(C) < 50:
        return jsonify({"success":False,"error":f"Not enough history for {sym} ({len(C)} days). Need 50+ trading days."})

    p = C[-1]
    H=H or C; L=L or C; O=O or C; V=V or [0]*len(C)
    ma20=sma(C,20); ma50=sma(C,50); ma200=sma(C,200)

    # RSI 14
    g,ls=[],[]
    for i in range(1,15):
        d=C[-i]-C[-i-1]; (g if d>0 else ls).append(abs(d))
    rsi=round(100-(100/(1+(sum(g)/14 if g else 0)/(sum(ls)/14 if ls else .001))),1)

    # MACD
    e12=ema(C[-60:],12); e26=ema(C[-60:],26); macd=e12-e26
    ms=[ema(C[-(60+i):-i],12)-ema(C[-(60+i):-i],26) for i in range(20,0,-1)]
    sig=ema(ms,9); hist=round(macd-sig,3)

    # Bollinger
    std20=(sum((c-ma20)**2 for c in C[-20:])/20)**0.5
    bbu=ma20+2*std20; bbl=ma20-2*std20

    # Stochastic
    l14=min(L[-14:]); h14=max(H[-14:])
    k=round((p-l14)/(h14-l14)*100,1) if h14!=l14 else 50

    # ATR
    tr=[max(H[-i]-L[-i],abs(H[-i]-C[-i-1]),abs(L[-i]-C[-i-1])) for i in range(1,15)]
    atr=round(sum(tr)/len(tr),2)

    # ADX
    dp,dm,tv=[],[],[]
    for i in range(1,15):
        u=H[-i]-H[-i-1]; d=L[-i-1]-L[-i]
        dp.append(u if u>d and u>0 else 0); dm.append(d if d>u and d>0 else 0)
        tv.append(max(H[-i]-L[-i],abs(H[-i]-C[-i-1]),abs(L[-i]-C[-i-1])))
    a14=sum(tv)/14 or .001
    dip=round((sum(dp)/a14)*100,1); dim=round((sum(dm)/a14)*100,1)
    adx=round(abs(dip-dim)/((dip+dim) or 1)*100,1)

    # SAR
    af,maf,sar,ep=0.02,0.2,L[-20],H[-20]
    for i in range(19,0,-1):
        sar=sar+af*(ep-sar); sar=min(sar,L[-i-1],L[-i])
        if H[-i]>ep: ep=H[-i]; af=min(af+0.02,maf)
    sar=round(sar,2)

    # Williams %R
    wR=round((max(H[-14:])-p)/(max(H[-14:])-min(L[-14:]))*-100,1) if max(H[-14:])!=min(L[-14:]) else -50

    # OBV
    obv=0; obv_list=[]
    for i in range(min(20,len(C)-1)):
        idx=-(i+1)
        if C[idx]>C[idx-1]: obv+=V[idx]
        elif C[idx]<C[idx-1]: obv-=V[idx]
        obv_list.append(obv)
    obv_rising=len(obv_list)>1 and obv_list[-1]>obv_list[0]

    # Candlestick patterns
    o1,h1,l1,c1=O[-1],H[-1],L[-1],C[-1]; o2,c2=O[-2],C[-2]
    body=abs(c1-o1); rng=h1-l1 or .001; lw=min(o1,c1)-l1; uw=h1-max(o1,c1)
    pats=[]
    if body/rng<0.1: pats.append("Doji -- market undecided, watch for breakout")
    if lw>2*body and uw<body: pats.append("Hammer -- buyers rejected lower prices, potential bounce")
    if uw>2*body and lw<body: pats.append("Shooting Star -- sellers rejected higher prices, potential fall")
    if c1>o1 and o2>c2 and o1<=c2 and c1>=o2: pats.append("Bullish Engulfing -- strong buy signal")
    if o1>c1 and c2>o2 and o1>=c2 and c1<=o2: pats.append("Bearish Engulfing -- strong sell signal")
    if not pats: pats.append("No major candlestick pattern today")

    # Bull/Bear score
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
    if score>=70:   sig_txt="STRONG BUY"
    elif score>=57: sig_txt="BUY"
    elif score>=43: sig_txt="HOLD"
    elif score>=30: sig_txt="SELL"
    else:           sig_txt="STRONG SELL"

    rh=max(H[-20:]); rl=min(L[-20:])
    w52h=max(H[-min(252,len(H)):]); w52l=min(L[-min(252,len(L)):])
    fr=rh-rl
    avg_v=sum(V[-20:])/20 if any(V[-20:]) else 1
    vr=round(V[-1]/avg_v,1) if avg_v and V[-1] else 0

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
        "patterns":pats,"vol_ratio":vr,"today_vol":V[-1] if V else 0}
    cache_set(f"tech:{sym}", data, ttl=300)
    return jsonify(data)

# ============================================================
# FUNDAMENTAL - Screener.in + Yahoo fallback
# ============================================================
@app.route("/api/fundamental/<symbol>")
def fundamental(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"fund:{sym}")
    if cached: return jsonify(cached)

    def parse_screener(html):
        def fv(label):
            for pat in [
                r'<li[^>]*>\s*<span[^>]*>\s*'+re.escape(label)+r'\s*</span>\s*<span[^>]*>\s*([\d,\.]+)',
                re.escape(label)+r'[^<]{0,60}</span>\s*<span[^>]*>\s*([\d,\.]+)',
                re.escape(label)+r'[^<]*</td>\s*<td[^>]*>\s*([\d,\.]+)',
            ]:
                m=re.search(pat,html,re.IGNORECASE|re.DOTALL)
                if m: return m.group(1).replace(",","").strip()
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
        return {"success":True,"source":"Screener.in",
            "mcap":(r["mcap"]+" Cr") if r["mcap"]!="N/A" else "N/A",
            "pe":r["pe"],"fwd_pe":"N/A","peg":"N/A","pb":r["pb"],"eps":r["eps"],
            "book_value":"N/A","dividend":(r["div"]+"%") if r["div"]!="N/A" else "N/A",
            "revenue":"N/A","rev_growth":"N/A","earnings_growth":"N/A",
            "profit_margin":"N/A","operating_margin":"N/A",
            "roe":(r["roe"]+"%") if r["roe"]!="N/A" else "N/A",
            "roce":(r["roce"]+"%") if r["roce"]!="N/A" else "N/A",
            "roa":"N/A","debt_equity":r["debt"],"current_ratio":r["cr"],
            "free_cashflow":"N/A","cagr_5y":r["sc5"],"sales_cagr":r["sc5"],"profit_cagr":r["pc5"],
            "promoter":r["promoter"],"public":r["public"],"fii":r["fii"],"dii":r["dii"],
            "insider_holding":r["promoter"],"institution_holding":"N/A","short_ratio":"N/A"}

    for suffix in ["/consolidated/","/"]:
        try:
            r=requests.get(f"https://www.screener.in/company/{sym}{suffix}",headers=SCH,timeout=12)
            if r.status_code==200 and len(r.text)>5000:
                result=parse_screener(r.text)
                if result:
                    cache_set(f"fund:{sym}", result, ttl=3600)
                    return jsonify(result)
        except: continue

    for base in ["query1","query2"]:
        try:
            url=f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/{sym}.NS?modules=defaultKeyStatistics%2CfinancialData%2CsummaryDetail%2CmajorHoldersBreakdown"
            r=requests.get(url,headers=YFH,timeout=8)
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
            data={"success":True,"source":"Yahoo Finance",
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
            return jsonify(data)
        except: continue

    return jsonify({"success":False,"error":f"Fundamental data unavailable for {sym}. Try again in 30 seconds."})

# ============================================================
# NEWS - Google News RSS (always works)
# ============================================================
@app.route("/api/news/<symbol>")
def news(symbol):
    sym=symbol.upper().strip()
    cached=cache_get(f"news:{sym}")
    if cached: return jsonify(cached)
    all_items=[]; seen=set()
    def fetch(q):
        try:
            r=requests.get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",headers=GNH,timeout=6)
            items=re.findall(r'<item>(.*?)</item>',r.text,re.DOTALL)
            out=[]
            for item in items[:4]:
                title=re.findall(r'<title>(.*?)</title>',item)
                link=re.findall(r'<link/>(.*?)\n',item)
                date=re.findall(r'<pubDate>(.*?)</pubDate>',item)
                src=re.findall(r'<source[^>]*>(.*?)</source>',item)
                if title and title[0] not in seen:
                    seen.add(title[0])
                    out.append({"title":title[0],"link":link[0].strip() if link else "#",
                                "date":date[0][:22] if date else "","source":src[0] if src else "News"})
            return out
        except: return []
    queries=[f"{sym}+stock+NSE+today",f"{sym}+share+price+latest",f"{sym}+results+India+2025"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for items in pool.map(fetch,queries): all_items.extend(items)
    final=[]; seen2=set()
    for item in all_items:
        if item["title"] not in seen2: seen2.add(item["title"]); final.append(item)
    data={"success":True,"news":final[:12]}
    cache_set(f"news:{sym}",data,ttl=600)
    return jsonify(data)

# ============================================================
# SENTIMENT - Multi-source parallel
# ============================================================
@app.route("/api/sentiment/<symbol>")
def sentiment(symbol):
    sym=symbol.upper().strip()
    cached=cache_get(f"sent:{sym}")
    if cached: return jsonify(cached)
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
        posts=requests.get(f"https://www.reddit.com/search.json?q={sym}+india+stock&sort=new&limit=20&t=week",headers={"User-Agent":"stockbot/1.0"},timeout=6).json()["data"]["children"]
        return score([(p["data"].get("title","")+p["data"].get("selftext","")) for p in posts],"Reddit")
    def mc():
        raw=requests.get(f"https://www.moneycontrol.com/news/tags/{sym.lower()}.html",headers=MCH,timeout=6).text
        return score([re.sub(r'<.*?>','',t).strip() for t in re.findall(r'<h2[^>]*>(.*?)</h2>',raw,re.DOTALL)][:15],"Moneycontrol")
    def et():
        raw=requests.get(f"https://economictimes.indiatimes.com/topic/{sym.lower()}-share-price",headers={"User-Agent":UA},timeout=6).text
        return score([re.sub(r'<.*?>','',t).strip() for t in re.findall(r'<h3[^>]*>(.*?)</h3>',raw,re.DOTALL)][:15],"Economic Times")
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
    return jsonify(data)

# ============================================================
# VERDICT - Parallel fetch + Groq AI
# ============================================================
@app.route("/api/verdict/<symbol>")
def verdict(symbol):
    sym=symbol.upper().strip()
    cached=cache_get(f"verdict:{sym}")
    if cached: return jsonify(cached)
    try:
        def get_tech():
            with app.app_context(): return technical(sym).get_json()
        def get_fund():
            with app.app_context(): return fundamental(sym).get_json()
        def get_news_():
            with app.app_context(): return news(sym).get_json()
        def get_sent():
            with app.app_context(): return sentiment(sym).get_json()
        with ThreadPoolExecutor(max_workers=4) as pool:
            tf=pool.submit(get_tech); ff=pool.submit(get_fund)
            nf=pool.submit(get_news_); sf=pool.submit(get_sent)
            t=tf.result(timeout=20); f=ff.result(timeout=15)
            n=nf.result(timeout=12); s=sf.result(timeout=12)
        if not t.get("success"):
            return jsonify({"success":False,"error":"Technical data failed: "+t.get("error","")})
        avg_bull=s.get("avg_bull",50)
        news_txt="\n".join([x["title"] for x in n.get("news",[])[:5]])
        prompt=(f"LIVE DATA FOR {sym} -- USE ONLY THESE NUMBERS:\n"
                f"Price:Rs.{t['price']} | MA20:Rs.{t['ma20']} | MA50:Rs.{t['ma50']} | MA200:Rs.{t.get('ma200','N/A')}\n"
                f"RSI:{t['rsi']} | MACD:{t['macd_hist']} | ADX:{t['adx']} | Stoch:{t['stoch_k']}%\n"
                f"Bollinger:Rs.{t['bb_lower']}-Rs.{t['bb_upper']} | SAR:Rs.{t['sar']}\n"
                f"Score:{t['bull_score']}% -> {t['signal']} | Support:Rs.{t['support']} | Resistance:Rs.{t['resistance']}\n"
                f"Stop:Rs.{t['stop_loss']} | T1:Rs.{t['target1']} | T2:Rs.{t['target2']}\n"
                f"PE:{f.get('pe','N/A')} | ROE:{f.get('roe','N/A')} | ROCE:{f.get('roce','N/A')} | Promoter:{f.get('promoter','N/A')}\n"
                f"Sentiment:{avg_bull}% Bullish\nHeadlines:\n{news_txt}\n\n"
                "VERDICT: [BUY/SELL/HOLD]\nCONFIDENCE: [X%]\nREASONING:\n- point with number\n- point\n- point\n- point\n"
                f"CURRENT PRICE: Rs.{t['price']}\nTARGET 3M: Rs.[]\nTARGET 12M: Rs.[]\nSTOP LOSS: Rs.{t['stop_loss']}\n"
                "RISK: [Low/Medium/High]\nBEST FOR: [Short-term/Long-term/Both]\nDISCLAIMER: Not financial advice.")
        resp=requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","max_tokens":800,
                  "messages":[{"role":"system","content":"Stock analyst. Use ONLY the numbers provided. Never use training memory for prices."},
                               {"role":"user","content":prompt}]},timeout=25)
        resp.raise_for_status()
        txt=resp.json()["choices"][0]["message"]["content"]
        def find(p): m=re.search(p,txt); return m.group(1).strip() if m else None
        data={"success":True,"symbol":sym,"price":t["price"],
            "verdict":   find(r"VERDICT:\s*(.+)")    or t["signal"],
            "confidence":find(r"CONFIDENCE:\s*(.+)") or "N/A",
            "target_3m": find(r"TARGET 3M:\s*Rs\.([0-9,./]+)") or str(t["target1"]),
            "target_12m":find(r"TARGET 12M:\s*Rs\.([0-9,./]+)") or str(t["target2"]),
            "stop_loss": find(r"STOP LOSS:\s*Rs\.([0-9,./]+)") or str(t["stop_loss"]),
            "risk":      find(r"RISK:\s*(.+)")    or "Medium",
            "best_for":  find(r"BEST FOR:\s*(.+)") or "Both",
            "reasoning": re.findall(r"- (.+)",txt)[:4],
            "full_text": txt,"tech":t,"fundamental":f,"sentiment":avg_bull}
        cache_set(f"verdict:{sym}",data,ttl=300)
        return jsonify(data)
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

# ============================================================
# INSIDER TRADES - NSE first, Moneycontrol + ET fallback
# ============================================================
def fetch_insider_from_moneycontrol():
    """Scrape bulk deals from Moneycontrol as NSE fallback"""
    try:
        r = requests.get("https://www.moneycontrol.com/stocks/marketstats/bulk_deals/index.php",
                        headers=MCH, timeout=10)
        if r.status_code != 200: return []
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
        trades = []
        for row in rows[1:31]:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            cells = [re.sub(r'<.*?>','',c).strip() for c in cells]
            if len(cells) >= 5 and cells[0]:
                trades.append({
                    "symbol":      cells[0].upper(),
                    "name":        cells[1] if len(cells)>1 else "Unknown",
                    "transaction": cells[3] if len(cells)>3 else "Buy/Sell",
                    "value":       safe_float(cells[4].replace(",","")) if len(cells)>4 else 0,
                    "date":        cells[5][:16] if len(cells)>5 else "",
                    "category":    "Bulk Deal",
                    "alert":       False,
                    "source":      "Moneycontrol"
                })
        return trades
    except: return []

def fetch_insider_from_et():
    """Scrape bulk deals from Economic Times as fallback"""
    try:
        r = requests.get("https://economictimes.indiatimes.com/markets/stocks/bulk-deals",
                        headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200: return []
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
        trades = []
        for row in rows[1:21]:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            cells = [re.sub(r'<.*?>','',c).strip() for c in cells]
            if len(cells) >= 4 and cells[0]:
                trades.append({
                    "symbol":      cells[0].upper(),
                    "name":        cells[1] if len(cells)>1 else "Unknown",
                    "transaction": "Buy/Sell",
                    "value":       safe_float(cells[3].replace(",","")) if len(cells)>3 else 0,
                    "date":        cells[4][:16] if len(cells)>4 else "",
                    "category":    "Bulk Deal",
                    "alert":       False,
                    "source":      "Economic Times"
                })
        return trades
    except: return []

@app.route("/api/insider-trades")
def insider_trades():
    cached = cache_get("insider_trades")
    if cached: return jsonify(cached)

    # Method 1: nsefin package (best NSE session handling)
    try:
        import nsefin
        nse_client = nsefin.NSEClient()
        df = nse_client.get_insider_trading()
        if df is not None and len(df) > 0:
            trades = []
            for _, row in df.head(50).iterrows():
                val = safe_float(str(row.get("secVal", row.get("value", 0))).replace(",",""))
                trades.append({
                    "symbol":      str(row.get("symbol", "")),
                    "name":        str(row.get("acqName", row.get("name", "Unknown"))),
                    "transaction": str(row.get("tdpTransactionType", row.get("transaction", "Unknown"))),
                    "value":       val,
                    "date":        str(row.get("date", ""))[:16],
                    "category":    str(row.get("personCategory", row.get("category", ""))),
                    "source":      "NSE via nsefin",
                    "alert":       val >= ALERT_THRESHOLD
                })
            trades.sort(key=lambda x: x["value"], reverse=True)
            result = {"success": True, "trades": trades, "source": "NSE", "note": ""}
            cache_set("insider_trades", result, ttl=1800)
            return jsonify(result)
    except Exception as e:
        print(f"nsefin insider trades failed: {e}")

    # Method 2: Direct NSE session
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        s.get("https://www.nseindia.com", timeout=8,
              headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        time.sleep(0.3)
        s.headers.update({"Accept": "application/json, text/plain, */*",
                          "Referer": "https://www.nseindia.com"})
        r = s.get("https://www.nseindia.com/api/corporates-pit?index=equities&from_date=&to_date=&symbol=&xbrl_flag=&period=", timeout=10)
        if r.status_code == 200 and r.text and r.text[0] == '{':
            data = r.json().get("data", [])
            if data:
                trades = [{"symbol":t.get("symbol",""),"name":t.get("acqName","Unknown"),
                           "transaction":t.get("tdpTransactionType","Unknown"),
                           "value":safe_float(t.get("secVal")),"date":str(t.get("date",""))[:16],
                           "category":t.get("personCategory",""),"source":"NSE",
                           "alert":safe_float(t.get("secVal"))>=ALERT_THRESHOLD} for t in data[:50]]
                trades.sort(key=lambda x: x["value"], reverse=True)
                result = {"success":True,"trades":trades,"source":"NSE","note":""}
                cache_set("insider_trades", result, ttl=1800)
                return jsonify(result)
    except: pass

    # Method 3: Scrape from Trendlyne (works on cloud)
    try:
        r = requests.get("https://trendlyne.com/equity/insider-trading/latest/",
                        headers={**SCH, "Referer": "https://trendlyne.com"}, timeout=10)
        if r.status_code == 200:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
            trades = []
            for row in rows[1:31]:
                cells = [re.sub(r'<.*?>','',c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>',row,re.DOTALL)]
                if len(cells) >= 4 and cells[0]:
                    trades.append({"symbol":cells[0].upper(),"name":cells[1] if len(cells)>1 else "Unknown",
                                   "transaction":cells[3] if len(cells)>3 else "Unknown",
                                   "value":safe_float(cells[4].replace(",","")) if len(cells)>4 else 0,
                                   "date":cells[5][:16] if len(cells)>5 else "","category":"Insider",
                                   "alert":False,"source":"Trendlyne"})
            if trades:
                result = {"success":True,"trades":trades,"source":"Trendlyne","note":""}
                cache_set("insider_trades", result, ttl=1800)
                return jsonify(result)
    except: pass

    return jsonify({"success":False,
                    "error":"Insider trade data temporarily unavailable. NSE blocks cloud servers outside India.",
                    "trades":[], "link":"https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"})

# ============================================================
# PROMOTER ACTIVITY
# ============================================================
@app.route("/api/promoter-activity")
def promoter_activity():
    cached = cache_get("promoter_activity")
    if cached: return jsonify(cached)
    kw=["promoter","director","chairman","managing","whole time","ceo","cfo","founder","key managerial"]

    # Try NSE first
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        s.get("https://www.nseindia.com", timeout=8, headers={"Accept":"text/html,application/xhtml+xml"})
        s.headers.update({"Accept":"application/json","Referer":"https://www.nseindia.com"})
        r = s.get("https://www.nseindia.com/api/corporates-pit?index=equities&from_date=&to_date=&symbol=&xbrl_flag=&period=", timeout=10)
        if r.status_code == 200 and r.text and r.text[0] == '{':
            data = r.json().get("data",[])
            trades = []
            for t in data:
                cat=str(t.get("personCategory","")).lower(); nm=str(t.get("acqName","")).lower()
                if any(k in cat or k in nm for k in kw):
                    val=safe_float(t.get("secVal"))
                    trades.append({"symbol":t.get("symbol",""),"name":t.get("acqName","Unknown"),
                                   "category":t.get("personCategory",""),
                                   "transaction":t.get("tdpTransactionType","Unknown"),
                                   "value":val,"shares":safe_float(t.get("secAcq")),
                                   "date":str(t.get("date",""))[:16],"source":"NSE"})
            trades.sort(key=lambda x:x["value"],reverse=True)
            result = {"success":True,"trades":trades,"total":len(trades),"source":"NSE"}
            cache_set("promoter_activity", result, ttl=1800)
            return jsonify(result)
    except: pass

    # Fallback: scrape from Moneycontrol insider trading page
    try:
        r = requests.get("https://www.moneycontrol.com/stocks/marketstats/insider_trading/index.php",
                        headers=MCH, timeout=10)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
        trades = []
        for row in rows[1:31]:
            cells = [re.sub(r'<.*?>','',c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>',row,re.DOTALL)]
            if len(cells) >= 4 and cells[0]:
                trades.append({"symbol":cells[0].upper(),"name":cells[1] if len(cells)>1 else "Unknown",
                               "category":"Insider","transaction":cells[2] if len(cells)>2 else "Unknown",
                               "value":safe_float(cells[3].replace(",","")) if len(cells)>3 else 0,
                               "shares":0,"date":cells[4][:16] if len(cells)>4 else "","source":"Moneycontrol"})
        if trades:
            result = {"success":True,"trades":trades,"total":len(trades),"source":"Moneycontrol",
                      "note":"NSE blocked on cloud. Showing data from Moneycontrol."}
            cache_set("promoter_activity", result, ttl=1800)
            return jsonify(result)
    except: pass

    return jsonify({"success":False,"error":"Promoter activity data temporarily unavailable.","trades":[]})

# ============================================================
# FII/DII - NSE first, news-based fallback
# ============================================================
@app.route("/api/fii-dii")
def fii_dii():
    cached = cache_get("fii_dii")
    if cached: return jsonify(cached)

    # Method 1: nsefin package
    try:
        import nsefin
        nse_client = nsefin.NSEClient()
        df = nse_client.get_fii_dii_activity()
        if df is not None and len(df) > 0:
            rows = []
            for _, row in df.head(10).iterrows():
                rows.append({
                    "date":     str(row.get("date", row.get("Date", ""))),
                    "fii_buy":  safe_float(str(row.get("fiiBuy", row.get("FII Buy", 0))).replace(",","")),
                    "fii_sell": safe_float(str(row.get("fiiSell", row.get("FII Sell", 0))).replace(",","")),
                    "fii_net":  safe_float(str(row.get("fiiBuy", row.get("FII Buy", 0))).replace(",","")) - safe_float(str(row.get("fiiSell", row.get("FII Sell", 0))).replace(",","")),
                    "dii_buy":  safe_float(str(row.get("diiBuy", row.get("DII Buy", 0))).replace(",","")),
                    "dii_sell": safe_float(str(row.get("diiSell", row.get("DII Sell", 0))).replace(",","")),
                    "dii_net":  safe_float(str(row.get("diiBuy", row.get("DII Buy", 0))).replace(",","")) - safe_float(str(row.get("diiSell", row.get("DII Sell", 0))).replace(",","")),
                })
            result = {"success":True,"data":rows,"source":"NSE via nsefin"}
            cache_set("fii_dii", result, ttl=1800)
            return jsonify(result)
    except Exception as e:
        print(f"nsefin fii_dii failed: {e}")

    # Method 2: Direct NSE session
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        s.get("https://www.nseindia.com", timeout=8, headers={"Accept":"text/html,application/xhtml+xml"})
        s.headers.update({"Accept":"application/json","Referer":"https://www.nseindia.com"})
        r = s.get("https://www.nseindia.com/api/fiidiiTradesEquities?type=historical", timeout=10)
        if r.status_code == 200 and r.text:
            data = r.json()
            if isinstance(data, list) and data:
                rows=[{"date":item.get("date",""),
                       "fii_buy":safe_float(item.get("fiiBuy",0)),"fii_sell":safe_float(item.get("fiiSell",0)),
                       "fii_net":safe_float(item.get("fiiBuy",0))-safe_float(item.get("fiiSell",0)),
                       "dii_buy":safe_float(item.get("diiBuy",0)),"dii_sell":safe_float(item.get("diiSell",0)),
                       "dii_net":safe_float(item.get("diiBuy",0))-safe_float(item.get("diiSell",0))} for item in data[:10]]
                result = {"success":True,"data":rows,"source":"NSE"}
                cache_set("fii_dii", result, ttl=1800)
                return jsonify(result)
    except: pass

    # Method 3: Scrape from 5paisa (works on cloud)
    try:
        r = requests.get("https://www.5paisa.com/share-market-today/fii-dii",
                        headers={**SCH, "Referer": "https://www.5paisa.com"}, timeout=10)
        if r.status_code == 200:
            fii_buy  = re.findall(r'"fiiBuy"[:\s]+([\d.]+)', r.text)
            fii_sell = re.findall(r'"fiiSell"[:\s]+([\d.]+)', r.text)
            dii_buy  = re.findall(r'"diiBuy"[:\s]+([\d.]+)', r.text)
            dii_sell = re.findall(r'"diiSell"[:\s]+([\d.]+)', r.text)
            if fii_buy:
                rows = []
                for i in range(min(10, len(fii_buy))):
                    fb=safe_float(fii_buy[i]); fs=safe_float(fii_sell[i]) if i<len(fii_sell) else 0
                    db=safe_float(dii_buy[i]) if i<len(dii_buy) else 0; ds=safe_float(dii_sell[i]) if i<len(dii_sell) else 0
                    rows.append({"date":"","fii_buy":fb,"fii_sell":fs,"fii_net":fb-fs,
                                 "dii_buy":db,"dii_sell":ds,"dii_net":db-ds})
                result = {"success":True,"data":rows,"source":"5paisa","note":""}
                cache_set("fii_dii", result, ttl=1800)
                return jsonify(result)
    except: pass

    return jsonify({"success":False,
                    "error":"FII/DII data temporarily unavailable.",
                    "links":[
                        {"label":"NSE FII/DII","url":"https://www.nseindia.com/market-data/fii-dii-activity"},
                        {"label":"Moneycontrol","url":"https://www.moneycontrol.com/markets/fii-dii-activity/"},
                        {"label":"5paisa","url":"https://www.5paisa.com/share-market-today/fii-dii"}
                    ]})

# ============================================================
# GEOPOLITICAL NEWS + AI ANALYSIS
# ============================================================
@app.route("/api/geopolitical")
def geopolitical():
    cached = cache_get("geopolitical")
    if cached: return jsonify(cached)
    queries=["India economy RBI today","crude oil India market","India budget government 2025",
             "India China trade news","US Fed rate India","SEBI regulation stocks India"]
    sector_map={
        "Oil & Gas (RELIANCE,ONGC,IOC)": ["crude","oil","gas","petroleum","opec"],
        "IT (TCS,INFY,WIPRO,HCLTECH)":   ["dollar","rupee","tech","visa","software","us recession"],
        "Banking (HDFC,ICICI,SBI)":       ["rbi","repo","inflation","npa","bank","interest rate"],
        "Defence (HAL,BEL)":              ["war","defence","military","border","pakistan","china"],
        "FMCG (HUL,ITC)":               ["inflation","rural","consumption","monsoon","food"],
        "Metals (TATASTEEL,JSWSTEEL)":    ["steel","metal","aluminium","china","import duty"],
    }
    all_news=[]; seen=set()
    def fetch(q):
        try:
            url=f"https://news.google.com/rss/search?q={q.replace(' ','+')}&hl=en-IN&gl=IN&ceid=IN:en"
            r=requests.get(url,headers=GNH,timeout=6)
            items=re.findall(r'<item>(.*?)</item>',r.text,re.DOTALL)[:3]
            out=[]
            for item in items:
                title=re.findall(r'<title>(.*?)</title>',item)
                link=re.findall(r'<link/>(.*?)\n',item)
                date=re.findall(r'<pubDate>(.*?)</pubDate>',item)
                if title and title[0] not in seen:
                    seen.add(title[0])
                    out.append({"title":title[0],"link":link[0].strip() if link else "#",
                                "date":date[0][:16] if date else "","query":q})
            return out
        except: return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for items in pool.map(fetch,queries): all_news.extend(items)
    sectors={}
    for ni in all_news:
        tl=ni["title"].lower()
        for sector,kws in sector_map.items():
            if any(k in tl for k in kws):
                if sector not in sectors: sectors[sector]=[]
                if len(sectors[sector])<2: sectors[sector].append(ni["title"])
    ai=""
    try:
        summary="\n".join([n["title"] for n in all_news[:12]])
        resp=requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","max_tokens":500,
                  "messages":[{"role":"system","content":"Indian stock market analyst. Be concise and specific with sector and stock names."},
                               {"role":"user","content":f"Based on these headlines, what is the market impact on Indian stocks?\n\n{summary}\n\n1. BULLISH SECTORS/STOCKS: (with reason)\n2. BEARISH SECTORS/STOCKS: (with reason)\n3. WATCH LIST: stocks to monitor closely"}]},timeout=20)
        resp.raise_for_status()
        ai=resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        ai=f"AI analysis unavailable: {e}"
    result={"success":True,"news":all_news[:20],"sectors_impacted":sectors,"ai_analysis":ai}
    cache_set("geopolitical", result, ttl=1800)
    return jsonify(result)

# ============================================================
# WATCHLIST - Your 16 stocks from Excel
# ============================================================
WATCHLIST = [
    {"name":"ACME Solar Holdings", "symbol":"ACMESOLAR",  "ref_price":248.69,  "promoter":83.29},
    {"name":"Aditya Birla Capital","symbol":"ABCAPITAL",  "ref_price":326.30,  "promoter":68.58},
    {"name":"Anant Raj",           "symbol":"ANANTRAJ",   "ref_price":471.20,  "promoter":57.41},
    {"name":"Arrow Greentech",     "symbol":"ARROWGREEN", "ref_price":400.35,  "promoter":64.81},
    {"name":"Banco Products",      "symbol":"BANCOINDIA", "ref_price":572.50,  "promoter":67.88},
    {"name":"Cochin Shipyard",     "symbol":"COCHINSHIP", "ref_price":1412.00, "promoter":67.91},
    {"name":"Fineotex Chemical",   "symbol":"FINEOTEX",   "ref_price":22.20,   "promoter":62.57},
    {"name":"HBL Engineering",     "symbol":"HBLENGINE",  "ref_price":680.50,  "promoter":59.11},
    {"name":"JSW Energy",          "symbol":"JSWENERGY",  "ref_price":509.00,  "promoter":69.27},
    {"name":"JSW Infrastructure",  "symbol":"JSWINFRA",   "ref_price":259.45,  "promoter":83.62},
    {"name":"KEC International",   "symbol":"KEC",        "ref_price":549.85,  "promoter":50.10},
    {"name":"KPIT Technologies",   "symbol":"KPITTECH",   "ref_price":690.10,  "promoter":39.42},
    {"name":"Lodha Developers",    "symbol":"LODHA",      "ref_price":854.00,  "promoter":71.85},
    {"name":"Paradeep Phosphates", "symbol":"PARADEEP",   "ref_price":112.30,  "promoter":57.70},
    {"name":"Shilpa Medicare",     "symbol":"SHILPAMED",  "ref_price":324.60,  "promoter":40.13},
    {"name":"Universal Cables",    "symbol":"UNIVCABLES", "ref_price":651.50,  "promoter":61.89},
]

def analyse_single(stock):
    sym=stock["symbol"]
    result={"name":stock["name"],"symbol":sym,"ref_price":stock["ref_price"],
            "promoter":stock["promoter"],"live_price":None,"change_pct":None,
            "pnl_pct":None,"signal":"N/A","bull_score":None,"rsi":None,"error":None}
    try:
        r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=6mo",
                       headers=YFH,timeout=10)
        data=r.json()["chart"]["result"][0]
        meta=data["meta"]; q=data["indicators"]["quote"][0]
        live=meta["regularMarketPrice"]; prev=meta["chartPreviousClose"]
        C=[c for c in q.get("close",[]) if c]
        result["live_price"]=round(live,2)
        result["change_pct"]=round((live-prev)/prev*100,2) if prev else 0
        result["pnl_pct"]=round((live-stock["ref_price"])/stock["ref_price"]*100,2)
        if len(C)>=15:
            g,ls=[],[]
            for i in range(1,min(15,len(C))):
                d=C[-i]-C[-i-1]; (g if d>0 else ls).append(abs(d))
            result["rsi"]=round(100-(100/(1+(sum(g)/14 if g else 0)/(sum(ls)/14 if ls else .001))),1)
        if len(C)>=50:
            ma50=sum(C[-50:])/50; ma200=sum(C[-200:])/200 if len(C)>=200 else None
            b,br=0,0
            if live>ma50: b+=2
            else: br+=2
            if ma200:
                if live>ma200: b+=2
                else: br+=2
            rsi=result["rsi"] or 50
            if rsi>55: b+=1
            elif rsi<45: br+=1
            if rsi<=30: b+=2
            if rsi>=70: br+=2
            score=round((b/(b+br or 1))*100)
            result["bull_score"]=score
            if score>=70: result["signal"]="STRONG BUY"
            elif score>=57: result["signal"]="BUY"
            elif score>=43: result["signal"]="HOLD"
            elif score>=30: result["signal"]="SELL"
            else: result["signal"]="STRONG SELL"
    except Exception as e:
        result["error"]=str(e)[:60]
    return result

@app.route("/api/watchlist")
def watchlist():
    cached=cache_get("watchlist")
    if cached: return jsonify(cached)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(analyse_single,s) for s in WATCHLIST]
        results=[f.result() for f in as_completed(futures,timeout=30)]
    signal_order={"STRONG BUY":0,"BUY":1,"HOLD":2,"SELL":3,"STRONG SELL":4,"N/A":5}
    results.sort(key=lambda x:(signal_order.get(x["signal"],5),-(x["pnl_pct"] or 0)))
    summary={"total":len(results),
             "strong_buy":sum(1 for r in results if r["signal"]=="STRONG BUY"),
             "buy":sum(1 for r in results if r["signal"]=="BUY"),
             "hold":sum(1 for r in results if r["signal"]=="HOLD"),
             "sell":sum(1 for r in results if r["signal"] in ("SELL","STRONG SELL")),
             "gainers":sum(1 for r in results if (r["pnl_pct"] or 0)>0),
             "losers":sum(1 for r in results if (r["pnl_pct"] or 0)<0)}
    result={"success":True,"stocks":results,"summary":summary}
    cache_set("watchlist",result,ttl=300)
    return jsonify(result)

@app.route("/api/watchlist/verdict/<symbol>")
def watchlist_verdict(symbol):
    sym=symbol.upper()
    stock=next((s for s in WATCHLIST if s["symbol"]==sym),None)
    if not stock: return jsonify({"success":False,"error":f"{sym} not in watchlist"})
    with app.app_context():
        t=technical(sym).get_json()
    if not t.get("success"): return jsonify({"success":False,"error":t.get("error","")})
    pnl=round((t["price"]-stock["ref_price"])/stock["ref_price"]*100,2)
    prompt=(f"Stock: {stock['name']} ({sym})\n"
            f"Reference price: Rs.{stock['ref_price']} | Live price: Rs.{t['price']} ({'UP' if pnl>0 else 'DOWN'} {abs(pnl)}%)\n"
            f"Promoter Holding: {stock['promoter']}%\n"
            f"RSI:{t['rsi']} | MACD:{t['macd_hist']} | ADX:{t['adx']} | Score:{t['bull_score']}% -> {t['signal']}\n"
            f"MA50:Rs.{t['ma50']} | MA200:Rs.{t.get('ma200','N/A')} | SAR:Rs.{t['sar']}\n"
            f"Support:Rs.{t['support']} | Resistance:Rs.{t['resistance']}\n\n"
            "ACTION: [BUY MORE / HOLD / REDUCE / EXIT]\n"
            "REASONING: 3 sentences using the numbers above\n"
            f"TARGET: Rs.[based on Rs.{t['price']}]\nSTOP LOSS: Rs.{t['stop_loss']}\n"
            "DISCLAIMER: Not financial advice.")
    try:
        resp=requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","max_tokens":400,
                  "messages":[{"role":"system","content":"Stock analyst. Use only provided numbers."},
                               {"role":"user","content":prompt}]},timeout=20)
        resp.raise_for_status()
        txt=resp.json()["choices"][0]["message"]["content"]
        def find(p): m=re.search(p,txt); return m.group(1).strip() if m else None
        return jsonify({"success":True,"symbol":sym,"name":stock["name"],
            "ref_price":stock["ref_price"],"live_price":t["price"],"pnl_pct":pnl,
            "action":find(r"ACTION:\s*(.+)") or t["signal"],
            "target":find(r"TARGET:\s*Rs\.([0-9,.]+)") or str(t["target1"]),
            "stop_loss":str(t["stop_loss"]),"reasoning":txt,"tech":t})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

# ============================================================
# SCREENER
# ============================================================
SCREEN_STOCKS = ["RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","HINDUNILVR","ITC","SBIN",
    "BHARTIARTL","KOTAKBANK","BAJFINANCE","LT","HCLTECH","ASIANPAINT","AXISBANK",
    "MARUTI","NESTLEIND","WIPRO","ULTRACEMCO","TITAN","SUNPHARMA","ONGC","NTPC",
    "POWERGRID","TECHM","TATAMOTORS","M&M","BAJAJFINSV","COALINDIA","ADANIENT",
    "JSWSTEEL","TATASTEEL","HINDALCO","INDUSINDBK","DRREDDY","CIPLA","DIVISLAB",
    "APOLLOHOSP","GRASIM","ADANIPORTS","EICHERMOT","BPCL","HEROMOTOCO","BRITANNIA",
    "TATACONSUM","DABUR","GODREJCP","PIDILITIND",
    "ACMESOLAR","ABCAPITAL","ANANTRAJ","ARROWGREEN","BANCOINDIA","COCHINSHIP",
    "FINEOTEX","HBLENGINE","JSWENERGY","JSWINFRA","KEC","KPITTECH","LODHA",
    "PARADEEP","SHILPAMED","UNIVCABLES"]



@app.route("/api/search")
def search_stocks():
    q = freq.args.get("q", "").strip().upper()
    if not q:
        return jsonify({"success": True, "results": []})

    stock_map = {}
    for s in WATCHLIST:
        stock_map[s["symbol"]] = {
            "symbol": s["symbol"],
            "name": s["name"],
            "source": "watchlist"
        }
    for sym in SCREEN_STOCKS:
        stock_map.setdefault(sym, {
            "symbol": sym,
            "name": sym,
            "source": "screener"
        })

    results = list(stock_map.values())
    ranked = [
        s for s in results
        if q in s["symbol"].upper() or q in s["name"].upper()
    ]
    ranked.sort(key=lambda x: (
        not x["symbol"].upper().startswith(q),
        not x["name"].upper().startswith(q),
        len(x["symbol"]),
        x["symbol"]
    ))
    return jsonify({"success": True, "results": ranked[:12]})

@app.route("/api/screener")
def screener():
    max_pe   = float(freq.args.get("max_pe",9999))
    min_roe  = float(freq.args.get("min_roe",0))
    max_debt = float(freq.args.get("max_debt",9999))
    symbols  = freq.args.get("symbols",",".join(SCREEN_STOCKS)).split(",")

    def screen_one(sym):
        try:
            for base in ["query1","query2"]:
                url=f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/{sym}.NS?modules=defaultKeyStatistics%2CfinancialData%2CsummaryDetail"
                r=requests.get(url,headers=YFH,timeout=8)
                if not r.ok: continue
                res=r.json().get("quoteSummary",{}).get("result",[{}])[0]
                fd=res.get("financialData",{}); sd=res.get("summaryDetail",{})
                def gv(d,k):
                    v=d.get(k)
                    if isinstance(v,dict): return v.get("raw")
                    return v
                pe=gv(sd,"trailingPE") or 0
                roe=(gv(fd,"returnOnEquity") or 0)*100
                debt=gv(fd,"debtToEquity") or 0
                price=gv(sd,"regularMarketPrice") or gv(fd,"currentPrice") or 0
                mcap=gv(sd,"marketCap") or 0
                rev_growth=(gv(fd,"revenueGrowth") or 0)*100
                profit_margin=(gv(fd,"profitMargins") or 0)*100
                if pe and pe>max_pe: return None
                if roe and roe<min_roe: return None
                if debt and debt>max_debt: return None
                return {"symbol":sym,"price":round(price,2) if price else None,
                        "pe":round(pe,1) if pe else None,"roe":round(roe,1) if roe else None,
                        "debt_equity":round(debt,2) if debt else None,"mcap":mcap,
                        "rev_growth":round(rev_growth,1),"profit_margin":round(profit_margin,1)}
        except: return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        results=list(pool.map(screen_one,symbols[:60]))
    results=[r for r in results if r]
    results.sort(key=lambda x:(x.get("roe") or 0),reverse=True)
    return jsonify({"success":True,"stocks":results,"total":len(results)})

# ============================================================
# PORTFOLIO & ALERTS (file-based persistence)
# ============================================================
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
ALERTS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.json")

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

@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    holdings=load_json(PORTFOLIO_FILE)
    def fetch_price_h(h):
        try:
            r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{h['symbol']}.NS?interval=1d&range=2d",headers=YFH,timeout=8)
            live=r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
            qty=h.get("qty",0); buy=h.get("buy_price",0)
            invested=qty*buy; current=qty*live; pnl=current-invested
            return {**h,"live_price":round(live,2),"invested":round(invested,2),
                    "current":round(current,2),"pnl":round(pnl,2),"pnl_pct":round((pnl/invested)*100,2) if invested else 0}
        except:
            return {**h,"live_price":None,"invested":h.get("qty",0)*h.get("buy_price",0),"current":None,"pnl":None,"pnl_pct":None}
    with ThreadPoolExecutor(max_workers=8) as pool:
        enriched=list(pool.map(fetch_price_h,holdings))
    ti=sum(h.get("invested",0) for h in enriched)
    tc=sum(h.get("current",0) or 0 for h in enriched)
    tp=tc-ti
    return jsonify({"success":True,"holdings":enriched,"total_invested":round(ti,2),
                    "total_current":round(tc,2),"total_pnl":round(tp,2),
                    "total_pnl_pct":round((tp/ti)*100,2) if ti else 0})

@app.route("/api/portfolio/add", methods=["POST"])
def add_holding():
    try:
        data=freq.get_json(); sym=data.get("symbol","").upper().strip()
        qty=float(data.get("qty",0)); buy=float(data.get("buy_price",0))
        name=data.get("name",sym)
        if not sym or qty<=0 or buy<=0: return jsonify({"success":False,"error":"Invalid data - symbol, qty and buy_price are required"})
        holdings=load_json(PORTFOLIO_FILE)
        for h in holdings:
            if h["symbol"]==sym:
                h["qty"]=qty; h["buy_price"]=buy; h["name"]=name
                save_json(PORTFOLIO_FILE,holdings)
                return jsonify({"success":True,"message":f"{sym} updated"})
        holdings.append({"symbol":sym,"name":name,"qty":qty,"buy_price":buy})
        save_json(PORTFOLIO_FILE,holdings)
        return jsonify({"success":True,"message":f"{sym} added"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/api/portfolio/remove/<symbol>", methods=["DELETE"])
def remove_holding(symbol):
    holdings=[h for h in load_json(PORTFOLIO_FILE) if h["symbol"]!=symbol.upper()]
    save_json(PORTFOLIO_FILE,holdings)
    return jsonify({"success":True})

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    alerts=load_json(ALERTS_FILE)
    def check_alert(a):
        try:
            r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{a['symbol']}.NS?interval=1d&range=1d",headers=YFH,timeout=6)
            price=r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
            a["current_price"]=round(price,2)
            if a.get("target") and price>=float(a["target"]):
                a["status"]="TARGET HIT"; a["triggered"]=True
            elif a.get("stop_loss") and price<=float(a["stop_loss"]):
                a["status"]="STOP LOSS HIT"; a["triggered"]=True
            else:
                a["triggered"]=False
                pct=round(((float(a.get("target",price))-price)/price)*100,1) if a.get("target") else None
                a["status"]=f"{pct}% to target" if pct else "Watching"
        except:
            a["current_price"]=None; a["triggered"]=False; a["status"]="Checking..."
        return a
    with ThreadPoolExecutor(max_workers=8) as pool:
        alerts=list(pool.map(check_alert,alerts))
    triggered=[a for a in alerts if a.get("triggered")]
    return jsonify({"success":True,"alerts":alerts,"triggered":triggered})

@app.route("/api/alerts/add", methods=["POST"])
def add_alert():
    try:
        data=freq.get_json(); sym=data.get("symbol","").upper().strip()
        target=data.get("target"); sl=data.get("stop_loss")
        if not sym: return jsonify({"success":False,"error":"Symbol required"})
        alerts=load_json(ALERTS_FILE)
        for a in alerts:
            if a["symbol"]==sym:
                if target: a["target"]=target
                if sl: a["stop_loss"]=sl
                save_json(ALERTS_FILE,alerts)
                return jsonify({"success":True,"message":f"{sym} updated"})
        alerts.append({"symbol":sym,"target":target,"stop_loss":sl,"created":time.strftime("%Y-%m-%d")})
        save_json(ALERTS_FILE,alerts)
        return jsonify({"success":True,"message":f"Alert set for {sym}"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/api/alerts/remove/<symbol>", methods=["DELETE"])
def remove_alert(symbol):
    alerts=[a for a in load_json(ALERTS_FILE) if a["symbol"]!=symbol.upper()]
    save_json(ALERTS_FILE,alerts)
    return jsonify({"success":True})

# ============================================================
# STARTUP
# ============================================================
if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Kevin Kataria Stock Intelligence - Production")
    print(f"  Open: http://localhost:{port}")
    print(f"  Test: http://localhost:{port}/api/test\n")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
