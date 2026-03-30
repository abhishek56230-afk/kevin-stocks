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
import requests, re, os, time, json, threading, datetime, difflib
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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


# Shared HTTP session for better connection reuse on Render
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


WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_watchlist.json")
_watchlist_lock = threading.Lock()

def _load_user_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []

def _save_user_watchlist(items):
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

USER_WATCHLIST = _load_user_watchlist()

def clear_cache_key(key):
    with _cache_lock:
        _cache.pop(key, None)

def normalize_mobile_number(number):
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) == 10:
        return "91" + digits
    if digits.startswith("0") and len(digits) > 10:
        digits = digits.lstrip("0")
    return digits

def get_combined_watchlist():
    seen = set()
    combined = []
    for item in WATCHLIST:
        sym = str(item.get("symbol", "")).upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        combined.append(item)
    with _watchlist_lock:
        for item in USER_WATCHLIST:
            sym = str(item.get("symbol", "")).upper().strip()
            if not sym or sym in seen:
                continue
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

@app.route("/api/health")
def api_health():
    return jsonify({
        "success": True,
        "status": "ok",
        "service": "Kevin Kataria Stock Intelligence",
        "cache_keys": len(_cache),
        "search_universe": len(_search_universe_cached()) if "_search_universe_cached" in globals() else None
    })

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
            r = http_get(f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS",
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
            # Try multiple patterns — Screener.in HTML structure varies
            patterns = [
                # li/span pattern (most common on Screener.in)
                r'<li[^>]*>\s*<span[^>]*>\s*' + re.escape(label) + r'\s*</span>\s*<span[^>]*>\s*([\d,\.]+)',
                # span followed by another span
                re.escape(label) + r'[^<]{0,80}</span>\s*<span[^>]*>\s*([\d,\.]+)',
                # table cell pattern
                re.escape(label) + r'[^<]*</td>\s*<td[^>]*>\s*([\d,\.]+)',
                # data attribute or value after colon
                re.escape(label) + r'[^<]{0,20}:\s*([\d,\.]+)',
                # number within 100 chars (tighter to avoid wrong matches)
                re.escape(label) + r'[\s\S]{0,100}?([\d]+(?:[,\.]\d+)+)',
            ]
            for pat in patterns:
                try:
                    m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
                    if m:
                        val = m.group(1).replace(",","").strip()
                        if val and float(val) != 0:
                            return val
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
        # Fetch live price via Yahoo for the price field
        live_price = None
        try:
            pr = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=2d", headers=YFH, timeout=5)
            if pr.ok:
                live_price = pr.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
        except: pass
        return {"success":True,"source":"Screener.in",
            "price": live_price,
            "mcap":(r["mcap"]+" Cr") if r["mcap"]!="N/A" else "N/A",
            "pe":r["pe"],"fwd_pe":"N/A","peg":"N/A","pb":r["pb"],"eps":r["eps"],
            "book_value":"N/A","dividend":pf(r["div"]),
            "revenue":"N/A","rev_growth":"N/A","earnings_growth":"N/A",
            "profit_margin":"N/A","operating_margin":"N/A",
            "roe":pf(r["roe"]),"roce":pf(r["roce"]),
            "roa":"N/A","debt_equity":r["debt"],"current_ratio":r["cr"],
            "free_cashflow":"N/A","cagr_5y":r["sc5"],"sales_cagr":r["sc5"],"profit_cagr":r["pc5"],
            "promoter":r["promoter"],"public":r["public"],"fii":r["fii"],"dii":r["dii"],
            "insider_holding":r["promoter"],"institution_holding":"N/A","short_ratio":"N/A"}

    for suffix in ["/consolidated/","/"]:
        try:
            r=http_get(f"https://www.screener.in/company/{sym}{suffix}",headers=SCH,timeout=12)
            if r.status_code==200 and len(r.text)>5000:
                result=parse_screener(r.text)
                if result:
                    cache_set(f"fund:{sym}", result, ttl=3600)
                    return jsonify(result)
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
            # Get live price for the header
            yf_price = None
            try:
                yf_price = (sd.get("regularMarketPrice") or {}).get("raw") or                            (fd.get("currentPrice") or {}).get("raw")
            except: pass
            data={"success":True,"source":"Yahoo Finance",
                "price": yf_price,
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
            r=http_get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",headers=GNH,timeout=6)
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
        resp=http_post("https://api.groq.com/openai/v1/chat/completions",
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
        resp=http_post("https://api.groq.com/openai/v1/chat/completions",
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
        ref_price = safe_float(stock.get("ref_price", 0))
        result["pnl_pct"]=round((live-ref_price)/ref_price*100,2) if ref_price else None
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
    live_watchlist = get_combined_watchlist()
    results = []
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(analyse_single, s) for s in live_watchlist]
            for f in as_completed(futures, timeout=30):
                try:
                    results.append(f.result(timeout=5))
                except Exception:
                    pass
    except Exception:
        pass  # TimeoutError — return partial results
    signal_order={"STRONG BUY":0,"BUY":1,"HOLD":2,"SELL":3,"STRONG SELL":4,"N/A":5}
    results.sort(key=lambda x:(signal_order.get(x["signal"],5),-(x["pnl_pct"] or 0)))
    summary={"total":len(results),
             "strong_buy": sum(1 for r in results if r["signal"]=="STRONG BUY"),
             "buy":        sum(1 for r in results if r["signal"]=="BUY"),
             "hold":       sum(1 for r in results if r["signal"]=="HOLD"),
             "sell":       sum(1 for r in results if r["signal"]=="SELL"),
             "strong_sell":sum(1 for r in results if r["signal"]=="STRONG SELL"),
             "gainers":    sum(1 for r in results if (r["pnl_pct"] or 0)>0),
             "losers":     sum(1 for r in results if (r["pnl_pct"] or 0)<0)}
    result={"success":True,"stocks":results,"summary":summary}
    cache_set("watchlist",result,ttl=120)
    return jsonify(result)

@app.route("/api/watchlist/add", methods=["POST"])
def add_to_watchlist():
    data = freq.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).upper().strip()
    name = str(data.get("name", symbol)).strip() or symbol

    if not symbol:
        return jsonify({"success":False,"error":"Symbol is required"}), 400

    existing = get_combined_watchlist()
    if any(str(x.get("symbol", "")).upper().strip() == symbol for x in existing):
        return jsonify({"success":True,"message":f"{symbol} is already in watchlist"})

    item = {
        "name": name,
        "symbol": symbol,
        "ref_price": 0,
        "promoter": 0
    }

    with _watchlist_lock:
        USER_WATCHLIST.append(item)
        _save_user_watchlist(USER_WATCHLIST)

    clear_cache_key("watchlist")
    return jsonify({"success":True,"message":f"{symbol} added to watchlist"})

@app.route("/api/watchlist/remove/<symbol>", methods=["DELETE"])
def remove_from_watchlist(symbol):
    sym = str(symbol or "").upper().strip()
    removed = False

    with _watchlist_lock:
        before = len(USER_WATCHLIST)
        USER_WATCHLIST[:] = [x for x in USER_WATCHLIST if str(x.get("symbol", "")).upper().strip() != sym]
        removed = len(USER_WATCHLIST) != before
        _save_user_watchlist(USER_WATCHLIST)

    clear_cache_key("watchlist")
    return jsonify({"success":True,"removed":removed,"symbol":sym})

@app.route("/api/verdict-share/<symbol>")
def verdict_share(symbol):
    sym = symbol.upper().strip()
    mobile = normalize_mobile_number(freq.args.get("mobile", ""))
    channel = str(freq.args.get("channel", "sms")).lower().strip()

    with app.app_context():
        verdict_data = verdict(sym).get_json()

    if not verdict_data.get("success"):
        return jsonify({"success":False,"error":verdict_data.get("error", f"Could not build verdict for {sym}")})

    reason_lines = verdict_data.get("reasoning") or []
    compact_reasons = "; ".join(reason_lines[:3]) if reason_lines else "AI verdict generated from technical, fundamental and sentiment inputs."
    share_text = (
        f"Stock Verdict: {sym}\n"
        f"Verdict: {verdict_data.get('verdict', 'HOLD')}\n"
        f"Current Price: Rs.{verdict_data.get('price', 'N/A')}\n"
        f"3M Target: Rs.{verdict_data.get('target_3m', 'N/A')}\n"
        f"12M Target: Rs.{verdict_data.get('target_12m', 'N/A')}\n"
        f"Stop Loss: Rs.{verdict_data.get('stop_loss', 'N/A')}\n"
        f"Risk: {verdict_data.get('risk', 'N/A')} | Best For: {verdict_data.get('best_for', 'N/A')}\n"
        f"Why: {compact_reasons}\n"
        f"Disclaimer: Not financial advice."
    )

    encoded = requests.utils.quote(share_text)
    sms_link = f"sms:{mobile}?body={encoded}" if mobile else f"sms:?body={encoded}"
    whatsapp_link = f"https://wa.me/{mobile}?text={encoded}" if mobile else f"https://wa.me/?text={encoded}"

    return jsonify({
        "success":True,
        "symbol":sym,
        "mobile":mobile,
        "channel":channel,
        "share_text":share_text,
        "sms_link":sms_link,
        "whatsapp_link":whatsapp_link,
        "note":"This prepares the AI verdict message for SMS or WhatsApp. Actual sending happens from the user's device/app."
    })

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
        resp=http_post("https://api.groq.com/openai/v1/chat/completions",
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



SEARCH_STOCKS_EXTENDED = [
    # ── A ──
    {"symbol": "AARTIIND",     "name": "Aarti Industries"},
    {"symbol": "AARTIDRUGS",   "name": "Aarti Drugs"},
    {"symbol": "AAVAS",        "name": "AAVAS Financiers"},
    {"symbol": "ABB",          "name": "ABB India"},
    {"symbol": "ABBOTINDIA",   "name": "Abbott India"},
    {"symbol": "ABCAPITAL",    "name": "Aditya Birla Capital"},
    {"symbol": "ABFRL",        "name": "Aditya Birla Fashion & Retail"},
    {"symbol": "ACC",          "name": "ACC Cement"},
    {"symbol": "ACCELYA",      "name": "Accelya Solutions"},
    {"symbol": "ACE",          "name": "Action Construction Equipment"},
    {"symbol": "ACMESOLAR",    "name": "ACME Solar Holdings"},
    {"symbol": "ADANIENT",     "name": "Adani Enterprises"},
    {"symbol": "ADANIENSOL",   "name": "Adani Energy Solutions"},
    {"symbol": "ADANIGAS",     "name": "Adani Total Gas"},
    {"symbol": "ADANIGREEN",   "name": "Adani Green Energy"},
    {"symbol": "ADANIPORTS",   "name": "Adani Ports & SEZ"},
    {"symbol": "ADANIPOWER",   "name": "Adani Power"},
    {"symbol": "ADANITRANS",   "name": "Adani Transmission"},
    {"symbol": "ADHUNIKIND",   "name": "Adhunik Industries"},
    {"symbol": "ADVENZYMES",   "name": "Advanced Enzyme Technologies"},
    {"symbol": "AFFLE",        "name": "Affle India"},
    {"symbol": "AGRO",         "name": "Dhanuka Agritech"},
    {"symbol": "AHLUCONT",     "name": "Ahluwalia Contracts"},
    {"symbol": "AIAENG",       "name": "AIA Engineering"},
    {"symbol": "AJANTPHARM",   "name": "Ajanta Pharma"},
    {"symbol": "AJMERA",       "name": "Ajmera Realty"},
    {"symbol": "AKZOINDIA",    "name": "Akzo Nobel India"},
    {"symbol": "ALEMBICLTD",   "name": "Alembic Ltd"},
    {"symbol": "ALKEM",        "name": "Alkem Laboratories"},
    {"symbol": "ALKYLAMINE",   "name": "Alkyl Amines Chemicals"},
    {"symbol": "ALLCARGO",     "name": "Allcargo Logistics"},
    {"symbol": "AMARAJABAT",   "name": "Amara Raja Energy & Mobility"},
    {"symbol": "AMARAJAELE",   "name": "Amara Raja Electronics"},
    {"symbol": "AMBER",        "name": "Amber Enterprises India"},
    {"symbol": "AMBUJACEM",    "name": "Ambuja Cements"},
    {"symbol": "AMIORG",       "name": "Ami Organics"},
    {"symbol": "ANANTRAJ",     "name": "Anant Raj"},
    {"symbol": "ANGELONE",     "name": "Angel One"},
    {"symbol": "APCOTEXIND",   "name": "Apcotex Industries"},
    {"symbol": "APLAPOLLO",    "name": "APL Apollo Tubes"},
    {"symbol": "APLLTD",       "name": "Alembic Pharma"},
    {"symbol": "APOLLOHOSP",   "name": "Apollo Hospitals"},
    {"symbol": "APOLLOTYRE",   "name": "Apollo Tyres"},
    {"symbol": "APTUS",        "name": "Aptus Value Housing Finance"},
    {"symbol": "ARMANFIN",     "name": "Arman Financial Services"},
    {"symbol": "ARROWGREEN",   "name": "Arrow Greentech"},
    {"symbol": "ASAHIINDIA",   "name": "Asahi India Glass"},
    {"symbol": "ASHOKLEY",     "name": "Ashok Leyland"},
    {"symbol": "ASIANPAINT",   "name": "Asian Paints"},
    {"symbol": "ASKAUTOLTD",   "name": "ASK Automotive"},
    {"symbol": "ASTERDM",      "name": "Aster DM Healthcare"},
    {"symbol": "ASTRAL",       "name": "Astral Ltd"},
    {"symbol": "ASTRAZEN",     "name": "AstraZeneca Pharma India"},
    {"symbol": "ATFL",         "name": "Agro Tech Foods"},
    {"symbol": "ATGL",         "name": "Adani Total Gas"},
    {"symbol": "ATUL",         "name": "Atul Ltd"},
    {"symbol": "AUBANK",       "name": "AU Small Finance Bank"},
    {"symbol": "AUROPHARMA",   "name": "Aurobindo Pharma"},
    {"symbol": "AUTOAXLES",    "name": "Automotive Axles"},
    {"symbol": "AVANTIFEED",   "name": "Avanti Feeds"},
    {"symbol": "AVTNPL",       "name": "AVT Natural Products"},
    {"symbol": "AWL",          "name": "Adani Wilmar"},
    {"symbol": "AXISCADES",    "name": "Axis CADES"},
    {"symbol": "AXISBANK",     "name": "Axis Bank"},
    # ── B ──
    {"symbol": "BAJAJ-AUTO",   "name": "Bajaj Auto"},
    {"symbol": "BAJAJCON",     "name": "Bajaj Consumer Care"},
    {"symbol": "BAJAJFINSV",   "name": "Bajaj Finserv"},
    {"symbol": "BAJAJHFL",     "name": "Bajaj Housing Finance"},
    {"symbol": "BAJAJHLDNG",   "name": "Bajaj Holdings & Investment"},
    {"symbol": "BAJFINANCE",   "name": "Bajaj Finance"},
    {"symbol": "BALAMINES",    "name": "Balaji Amines"},
    {"symbol": "BALKRISIND",   "name": "Balkrishna Industries"},
    {"symbol": "BALMLAWRIE",   "name": "Balmer Lawrie & Co"},
    {"symbol": "BALRAMCHIN",   "name": "Balrampur Chini Mills"},
    {"symbol": "BANCOINDIA",   "name": "Banco Products India"},
    {"symbol": "BANDHANBNK",   "name": "Bandhan Bank"},
    {"symbol": "BANKBARODA",   "name": "Bank of Baroda"},
    {"symbol": "BANKINDIA",    "name": "Bank of India"},
    {"symbol": "BANSWRAS",     "name": "Banswara Syntex"},
    {"symbol": "BATAINDIA",    "name": "Bata India"},
    {"symbol": "BAYERCROP",    "name": "Bayer Cropscience"},
    {"symbol": "BDL",          "name": "Bharat Dynamics"},
    {"symbol": "BEL",          "name": "Bharat Electronics"},
    {"symbol": "BEML",         "name": "BEML Ltd"},
    {"symbol": "BERGEPAINT",   "name": "Berger Paints India"},
    {"symbol": "BFINVEST",     "name": "BF Investment"},
    {"symbol": "BHARATFORG",   "name": "Bharat Forge"},
    {"symbol": "BHARTIARTL",   "name": "Bharti Airtel"},
    {"symbol": "BHEL",         "name": "Bharat Heavy Electricals"},
    {"symbol": "BIKAJI",       "name": "Bikaji Foods International"},
    {"symbol": "BIOCON",       "name": "Biocon"},
    {"symbol": "BIRLACORPN",   "name": "Birla Corporation"},
    {"symbol": "BLISSGVS",     "name": "Bliss GVS Pharma"},
    {"symbol": "BLUESTARCO",   "name": "Blue Star"},
    {"symbol": "BPCL",         "name": "Bharat Petroleum"},
    {"symbol": "BRIGADE",      "name": "Brigade Enterprises"},
    {"symbol": "BRITANNIA",    "name": "Britannia Industries"},
    {"symbol": "BSOFT",        "name": "Birlasoft"},
    {"symbol": "BSE",          "name": "BSE Ltd"},
    # ── C ──
    {"symbol": "CAMS",         "name": "CAMS Services"},
    {"symbol": "CAMPUS",       "name": "Campus Activewear"},
    {"symbol": "CANBK",        "name": "Canara Bank"},
    {"symbol": "CANFINHOME",   "name": "Can Fin Homes"},
    {"symbol": "CAPLIPOINT",   "name": "Caplin Point Laboratories"},
    {"symbol": "CARBORUNIV",   "name": "Carborundum Universal"},
    {"symbol": "CASTROLIND",   "name": "Castrol India"},
    {"symbol": "CEATLTD",      "name": "CEAT"},
    {"symbol": "CENTENKA",     "name": "Century Enka"},
    {"symbol": "CENTEXT",      "name": "Century Textiles & Industries"},
    {"symbol": "CENTRALBK",    "name": "Central Bank of India"},
    {"symbol": "CESC",         "name": "CESC Ltd"},
    {"symbol": "CGPOWER",      "name": "CG Power & Industrial Solutions"},
    {"symbol": "CHAMBLFERT",   "name": "Chambal Fertilisers"},
    {"symbol": "CHEMCON",      "name": "Chemcon Specialty Chemicals"},
    {"symbol": "CHENNPETRO",   "name": "Chennai Petroleum"},
    {"symbol": "CHOLAFIN",     "name": "Cholamandalam Investment & Finance"},
    {"symbol": "CHOLAHLDNG",   "name": "Cholamandalam Financial Holdings"},
    {"symbol": "CIPLA",        "name": "Cipla"},
    {"symbol": "CLEAN",        "name": "Clean Science & Technology"},
    {"symbol": "CMSINFO",      "name": "CMS Info Systems"},
    {"symbol": "COALINDIA",    "name": "Coal India"},
    {"symbol": "COCHINSHIP",   "name": "Cochin Shipyard"},
    {"symbol": "COFORGE",      "name": "Coforge"},
    {"symbol": "COLPAL",       "name": "Colgate-Palmolive India"},
    {"symbol": "CONCOR",       "name": "Container Corporation of India"},
    {"symbol": "CONTROLPR",    "name": "Control Print"},
    {"symbol": "COROMANDEL",   "name": "Coromandel International"},
    {"symbol": "CRAFTSMAN",    "name": "Craftsman Automation"},
    {"symbol": "CROMPTON",     "name": "Crompton Greaves Consumer Electricals"},
    {"symbol": "CSBBANK",      "name": "CSB Bank"},
    {"symbol": "CUB",          "name": "City Union Bank"},
    {"symbol": "CUMMINSIND",   "name": "Cummins India"},
    # ── D ──
    {"symbol": "DABUR",        "name": "Dabur India"},
    {"symbol": "DALBHARAT",    "name": "Dalmia Bharat"},
    {"symbol": "DATAPATTNS",   "name": "Data Patterns India"},
    {"symbol": "DBCORP",       "name": "DB Corp"},
    {"symbol": "DCMSHRIRAM",   "name": "DCM Shriram"},
    {"symbol": "DCXSYS",       "name": "DCX Systems"},
    {"symbol": "DEEPAKFERT",   "name": "Deepak Fertilisers"},
    {"symbol": "DEEPAKNTR",    "name": "Deepak Nitrite"},
    {"symbol": "DELTACORP",    "name": "Delta Corp"},
    {"symbol": "DHANI",        "name": "Dhani Services"},
    {"symbol": "DHARMAJ",      "name": "Dharmaj Crop Guard"},
    {"symbol": "DHANUKA",      "name": "Dhanuka Agritech"},
    {"symbol": "DHUNSERI",     "name": "Dhunseri Ventures"},
    {"symbol": "DISHTV",       "name": "Dish TV India"},
    {"symbol": "DIVI",         "name": "Divi's Laboratories"},
    {"symbol": "DIXON",        "name": "Dixon Technologies India"},
    {"symbol": "DLF",          "name": "DLF"},
    {"symbol": "DMART",        "name": "Avenue Supermarts (DMart)"},
    {"symbol": "DODLA",        "name": "Dodla Dairy"},
    {"symbol": "DRREDDY",      "name": "Dr Reddy's Laboratories"},
    {"symbol": "DWARKESH",     "name": "Dwarikesh Sugar Industries"},
    # ── E ──
    {"symbol": "EASEMYTRIP",   "name": "Easy Trip Planners"},
    {"symbol": "ECLERX",       "name": "eClerx Services"},
    {"symbol": "EDELWEISS",    "name": "Edelweiss Financial Services"},
    {"symbol": "EICHERMOT",    "name": "Eicher Motors"},
    {"symbol": "EIHOTEL",      "name": "EIH Ltd (Oberoi Hotels)"},
    {"symbol": "ELGIEQUIP",    "name": "Elgi Equipments"},
    {"symbol": "EMAMILTD",     "name": "Emami"},
    {"symbol": "EMAMIREALTY",  "name": "Emami Realty"},
    {"symbol": "EMCURE",       "name": "Emcure Pharmaceuticals"},
    {"symbol": "ENDURANCE",    "name": "Endurance Technologies"},
    {"symbol": "ENGINERSIN",   "name": "Engineers India"},
    {"symbol": "ENIL",         "name": "Entertainment Network India (Radio Mirchi)"},
    {"symbol": "EQUITASBNK",   "name": "Equitas Small Finance Bank"},
    {"symbol": "ESABINDIA",    "name": "ESAB India"},
    {"symbol": "ESCORTS",      "name": "Escorts Kubota"},
    {"symbol": "ESTER",        "name": "Ester Industries"},
    {"symbol": "EXIDEIND",     "name": "Exide Industries"},
    # ── F ──
    {"symbol": "FACT",         "name": "Fertilisers and Chemicals Travancore"},
    {"symbol": "FAZE3Q",       "name": "Faze Three"},
    {"symbol": "FEDERALBNK",   "name": "Federal Bank"},
    {"symbol": "FIEMIND",      "name": "Fiem Industries"},
    {"symbol": "FINCABLES",    "name": "Finolex Cables"},
    {"symbol": "FINEORG",      "name": "Fine Organics"},
    {"symbol": "FINEOTEX",     "name": "Fineotex Chemical"},
    {"symbol": "FINPIPE",      "name": "Finolex Industries"},
    {"symbol": "FLUOROCHEM",   "name": "Gujarat Fluorochemicals"},
    {"symbol": "FMGOETZE",     "name": "Federal-Mogul Goetze India"},
    {"symbol": "FORTIS",       "name": "Fortis Healthcare"},
    {"symbol": "FSL",          "name": "Firstsource Solutions"},
    # ── G ──
    {"symbol": "GAIL",         "name": "GAIL India"},
    {"symbol": "GALAXYSURF",   "name": "Galaxy Surfactants"},
    {"symbol": "GARFIBRES",    "name": "Garware Technical Fibres"},
    {"symbol": "GAYAPROJ",     "name": "Gayatri Projects"},
    {"symbol": "GDL",          "name": "Gateway Distriparks"},
    {"symbol": "GESHIP",       "name": "Great Eastern Shipping"},
    {"symbol": "GHCL",         "name": "GHCL"},
    {"symbol": "GICRE",        "name": "General Insurance Corporation"},
    {"symbol": "GILLETTE",     "name": "Gillette India"},
    {"symbol": "GLAXO",        "name": "GSK Pharmaceuticals India"},
    {"symbol": "GLENMARK",     "name": "Glenmark Pharmaceuticals"},
    {"symbol": "GMMPFAUDLR",   "name": "GMM Pfaudler"},
    {"symbol": "GMRINFRA",     "name": "GMR Airports Infrastructure"},
    {"symbol": "GMBREW",       "name": "GM Breweries"},
    {"symbol": "GNFC",         "name": "GNFC"},
    {"symbol": "GODREJAGRO",   "name": "Godrej Agrovet"},
    {"symbol": "GODREJCP",     "name": "Godrej Consumer Products"},
    {"symbol": "GODREJIND",    "name": "Godrej Industries"},
    {"symbol": "GODREJPROP",   "name": "Godrej Properties"},
    {"symbol": "GODFRYPHLP",   "name": "Godfrey Phillips India"},
    {"symbol": "GRANULES",     "name": "Granules India"},
    {"symbol": "GRAPHITE",     "name": "Graphite India"},
    {"symbol": "GRASIM",       "name": "Grasim Industries"},
    {"symbol": "GRAVITA",      "name": "Gravita India"},
    {"symbol": "GREENPANEL",   "name": "Greenpanel Industries"},
    {"symbol": "GRINDWELL",    "name": "Grindwell Norton"},
    {"symbol": "GRMOVER",      "name": "GRM Overseas"},
    {"symbol": "GSFC",         "name": "Gujarat State Fertilizers & Chemicals"},
    {"symbol": "GSPL",         "name": "Gujarat State Petronet"},
    {"symbol": "GTPL",         "name": "GTPL Hathway"},
    {"symbol": "GUJALKALI",    "name": "Gujarat Alkalies & Chemicals"},
    {"symbol": "GUJGASLTD",    "name": "Gujarat Gas"},
    {"symbol": "GULFOILLUB",   "name": "Gulf Oil Lubricants India"},
    # ── H ──
    {"symbol": "HAL",          "name": "Hindustan Aeronautics"},
    {"symbol": "HAPPSTMNDS",   "name": "Happiest Minds Technologies"},
    {"symbol": "HARDWYN",      "name": "Hardwyn India"},
    {"symbol": "HARIOMPIPE",   "name": "Hariom Pipe Industries"},
    {"symbol": "HAVELLS",      "name": "Havells India"},
    {"symbol": "HBLENGINE",    "name": "HBL Engineering"},
    {"symbol": "HCLTECH",      "name": "HCL Technologies"},
    {"symbol": "HDFCAMC",      "name": "HDFC AMC"},
    {"symbol": "HDFCBANK",     "name": "HDFC Bank"},
    {"symbol": "HDFCLIFE",     "name": "HDFC Life Insurance"},
    {"symbol": "HERITGFOOD",   "name": "Heritage Foods"},
    {"symbol": "HEROMOTOCO",   "name": "Hero MotoCorp"},
    {"symbol": "HFCL",         "name": "HFCL"},
    {"symbol": "HGS",          "name": "Hinduja Global Solutions"},
    {"symbol": "HIKAL",        "name": "Hikal"},
    {"symbol": "HILTON",       "name": "Hilton Metal Forging"},
    {"symbol": "HINDCOPPER",   "name": "Hindustan Copper"},
    {"symbol": "HINDALCO",     "name": "Hindalco Industries"},
    {"symbol": "HINDPETRO",    "name": "Hindustan Petroleum"},
    {"symbol": "HINDUNILVR",   "name": "Hindustan Unilever"},
    {"symbol": "HINDWREX",     "name": "Hindware Home Innovation"},
    {"symbol": "HONAUT",       "name": "Honeywell Automation India"},
    {"symbol": "HUDCO",        "name": "HUDCO"},
    {"symbol": "HYUNDAI",      "name": "Hyundai Motor India"},
    # ── I ──
    {"symbol": "ICICIGI",      "name": "ICICI Lombard General Insurance"},
    {"symbol": "ICICIBANK",    "name": "ICICI Bank"},
    {"symbol": "ICICIPRULI",   "name": "ICICI Prudential Life Insurance"},
    {"symbol": "ICRA",         "name": "ICRA"},
    {"symbol": "IDEA",         "name": "Vodafone Idea"},
    {"symbol": "IDFC",         "name": "IDFC"},
    {"symbol": "IDFCFIRSTB",   "name": "IDFC First Bank"},
    {"symbol": "IEX",          "name": "Indian Energy Exchange"},
    {"symbol": "IFBIND",       "name": "IFB Industries"},
    {"symbol": "IGPL",         "name": "IG Petrochemicals"},
    {"symbol": "IGL",          "name": "Indraprastha Gas"},
    {"symbol": "IIFL",         "name": "IIFL Finance"},
    {"symbol": "IIFLFIN",      "name": "IIFL Finance"},
    {"symbol": "IIFLSEC",      "name": "IIFL Securities"},
    {"symbol": "INDHOTEL",     "name": "Indian Hotels (Taj)"},
    {"symbol": "INDIACEM",     "name": "India Cements"},
    {"symbol": "INDIAMART",    "name": "IndiaMART InterMESH"},
    {"symbol": "INDIANB",      "name": "Indian Bank"},
    {"symbol": "INDIGO",       "name": "InterGlobe Aviation (IndiGo)"},
    {"symbol": "INDSINDBK",    "name": "IndusInd Bank"},
    {"symbol": "INDUSINDBK",   "name": "IndusInd Bank"},
    {"symbol": "INDUSTOWER",   "name": "Indus Towers"},
    {"symbol": "INFIBEAM",     "name": "Infibeam Avenues"},
    {"symbol": "INFOBEAN",     "name": "InfoBeans Technologies"},
    {"symbol": "INFY",         "name": "Infosys"},
    {"symbol": "INOXGREEN",    "name": "INOX Green Energy"},
    {"symbol": "INOXWIND",     "name": "Inox Wind"},
    {"symbol": "INTELLECT",    "name": "Intellect Design Arena"},
    {"symbol": "IOB",          "name": "Indian Overseas Bank"},
    {"symbol": "IOC",          "name": "Indian Oil Corporation"},
    {"symbol": "IPCALAB",      "name": "Ipca Laboratories"},
    {"symbol": "IRB",          "name": "IRB Infrastructure"},
    {"symbol": "IRCTC",        "name": "IRCTC"},
    {"symbol": "IREDA",        "name": "Indian Renewable Energy Dev Agency"},
    {"symbol": "IRFC",         "name": "Indian Railway Finance Corporation"},
    {"symbol": "ISEC",         "name": "ICICI Securities"},
    {"symbol": "ITC",          "name": "ITC"},
    {"symbol": "ITI",          "name": "ITI Ltd"},
    # ── J ──
    {"symbol": "JAIBALAJI",    "name": "Jai Balaji Industries"},
    {"symbol": "JAICORPLTD",   "name": "Jai Corp"},
    {"symbol": "JAGRAN",       "name": "Jagran Prakashan"},
    {"symbol": "JBCHEPHARM",   "name": "JB Chemicals & Pharma"},
    {"symbol": "JBMA",         "name": "JBM Auto"},
    {"symbol": "JINDALPOLY",   "name": "Jindal Poly Films"},
    {"symbol": "JINDALSAW",    "name": "Jindal Saw"},
    {"symbol": "JINDALSTEL",   "name": "Jindal Steel & Power"},
    {"symbol": "JKCEMENT",     "name": "JK Cement"},
    {"symbol": "JKIL",         "name": "J Kumar Infraprojects"},
    {"symbol": "JKLAKSHMI",    "name": "JK Lakshmi Cement"},
    {"symbol": "JKPAPER",      "name": "JK Paper"},
    {"symbol": "JKTYRE",       "name": "JK Tyre & Industries"},
    {"symbol": "JLHL",         "name": "Jupiter Life Line Hospitals"},
    {"symbol": "JMFINANCIL",   "name": "JM Financial"},
    {"symbol": "JSL",          "name": "Jindal Stainless"},
    {"symbol": "JSWENERGY",    "name": "JSW Energy"},
    {"symbol": "JSWINFRA",     "name": "JSW Infrastructure"},
    {"symbol": "JSWSTEEL",     "name": "JSW Steel"},
    {"symbol": "JUBLFOOD",     "name": "Jubilant FoodWorks"},
    {"symbol": "JUBLINGREA",   "name": "Jubilant Ingrevia"},
    # ── K ──
    {"symbol": "KAJARIACER",   "name": "Kajaria Ceramics"},
    {"symbol": "KALYANKJIL",   "name": "Kalyan Jewellers India"},
    {"symbol": "KANSAINER",    "name": "Kansai Nerolac Paints"},
    {"symbol": "KARURVYSYA",   "name": "Karur Vysya Bank"},
    {"symbol": "KAYNES",       "name": "Kaynes Technology India"},
    {"symbol": "KDDL",         "name": "KDDL"},
    {"symbol": "KEC",          "name": "KEC International"},
    {"symbol": "KECL",         "name": "KEC International"},
    {"symbol": "KFINTECH",     "name": "KFin Technologies"},
    {"symbol": "KHADIM",       "name": "Khadim India"},
    {"symbol": "KIOCL",        "name": "KIOCL"},
    {"symbol": "KIRIINDUS",    "name": "Kiri Industries"},
    {"symbol": "KIRLOSBROS",   "name": "Kirloskar Brothers"},
    {"symbol": "KIRLOSENG",    "name": "Kirloskar Electric"},
    {"symbol": "KIRLOSIND",    "name": "Kirloskar Industries"},
    {"symbol": "KNRCON",       "name": "KNR Constructions"},
    {"symbol": "KOKUYOCMLN",   "name": "Kokuyo Camlin"},
    {"symbol": "KOTAKBANK",    "name": "Kotak Mahindra Bank"},
    {"symbol": "KPITTECH",     "name": "KPIT Technologies"},
    {"symbol": "KSCL",         "name": "Kaveri Seed Company"},
    {"symbol": "KSOLVES",      "name": "Ksolves India"},
    # ── L ──
    {"symbol": "LALPATHLAB",   "name": "Dr Lal PathLabs"},
    {"symbol": "LATENTVIEW",   "name": "Latent View Analytics"},
    {"symbol": "LAURUSLABS",   "name": "Laurus Labs"},
    {"symbol": "LAXMIMACH",    "name": "Lakshmi Machine Works"},
    {"symbol": "LT",           "name": "Larsen & Toubro"},
    {"symbol": "LTIM",         "name": "LTIMindtree"},
    {"symbol": "LTTS",         "name": "L&T Technology Services"},
    {"symbol": "LLOYDSENGG",   "name": "Lloyds Engineering Works"},
    {"symbol": "LODHA",        "name": "Macrotech Developers (Lodha)"},
    {"symbol": "LSIL",         "name": "Lumax Alternate Energy"},
    {"symbol": "LUPIN",        "name": "Lupin"},
    # ── M ──
    {"symbol": "MAHINDCIE",    "name": "Mahindra CIE Automotive"},
    {"symbol": "MAHINDRALT",   "name": "Mahindra Logistics"},
    {"symbol": "MAHLIFE",      "name": "Mahindra Lifespace Developers"},
    {"symbol": "MANAKSTEEL",   "name": "Maanak Steel & Power"},
    {"symbol": "MARICO",       "name": "Marico"},
    {"symbol": "MARUTI",       "name": "Maruti Suzuki India"},
    {"symbol": "MASKINVEST",   "name": "Mask Investments"},
    {"symbol": "MCX",          "name": "Multi Commodity Exchange of India"},
    {"symbol": "MEDPLUSLTD",   "name": "MedPlus Health Services"},
    {"symbol": "METROPOLIS",   "name": "Metropolis Healthcare"},
    {"symbol": "MIRZAINT",     "name": "Mirza International"},
    {"symbol": "MLDL",         "name": "Mahindra Lifespace Developers"},
    {"symbol": "MOLDTKPAC",    "name": "Mold-Tek Packaging"},
    {"symbol": "MOTHERSON",    "name": "Samvardhana Motherson International"},
    {"symbol": "MPHASIS",      "name": "Mphasis"},
    {"symbol": "MPSLTD",       "name": "MPS Ltd"},
    {"symbol": "MRF",          "name": "MRF"},
    {"symbol": "MSUMI",        "name": "Motherson Sumi Wiring India"},
    {"symbol": "MUTHOOTFIN",   "name": "Muthoot Finance"},
    {"symbol": "M&M",          "name": "Mahindra & Mahindra"},
    {"symbol": "M&MFIN",       "name": "Mahindra & Mahindra Financial Services"},
    # ── N ──
    {"symbol": "NATIONALUM",   "name": "National Aluminium Company"},
    {"symbol": "NAUKRI",       "name": "Info Edge India (Naukri)"},
    {"symbol": "NAVINFLUOR",   "name": "Navin Fluorine International"},
    {"symbol": "NESTLEIND",    "name": "Nestle India"},
    {"symbol": "NILKAMAL",     "name": "Nilkamal"},
    {"symbol": "NMDC",         "name": "NMDC"},
    {"symbol": "NMSEZ",        "name": "NMSEZ"},
    {"symbol": "NTPC",         "name": "NTPC"},
    # ── O ──
    {"symbol": "OFSS",         "name": "Oracle Financial Services Software"},
    {"symbol": "OIL",          "name": "Oil India"},
    {"symbol": "OMAXE",        "name": "Omaxe"},
    {"symbol": "ONGC",         "name": "Oil & Natural Gas Corporation"},
    {"symbol": "OPTIEMUS",     "name": "Optiemus Infracom"},
    {"symbol": "ORCHPHARMA",   "name": "Orchid Pharma"},
    {"symbol": "ORIENTELEC",   "name": "Orient Electric"},
    {"symbol": "ORIENTPAPER",  "name": "Orient Paper & Industries"},
    # ── P ──
    {"symbol": "PARADEEP",     "name": "Paradeep Phosphates"},
    {"symbol": "PAGEIND",      "name": "Page Industries"},
    {"symbol": "PATANJALI",    "name": "Patanjali Foods"},
    {"symbol": "PCBL",         "name": "PCBL"},
    {"symbol": "PCINDIA",      "name": "PC Jeweller"},
    {"symbol": "PERSISTENT",   "name": "Persistent Systems"},
    {"symbol": "PETRONET",     "name": "Petronet LNG"},
    {"symbol": "PFIZER",       "name": "Pfizer India"},
    {"symbol": "PHOENIXLTD",   "name": "Phoenix Mills"},
    {"symbol": "PIDILITIND",   "name": "Pidilite Industries"},
    {"symbol": "PIIND",        "name": "PI Industries"},
    {"symbol": "PLASTIBLND",   "name": "Plastiblends India"},
    {"symbol": "POLYCAB",      "name": "Polycab India"},
    {"symbol": "POLYMED",      "name": "Poly Medicure"},
    {"symbol": "POWERGRID",    "name": "Power Grid Corporation of India"},
    {"symbol": "PRIVISCL",     "name": "Privi Speciality Chemicals"},
    {"symbol": "PRSMJOHNSN",   "name": "Prism Johnson"},
    {"symbol": "PUNJABCHEM",   "name": "Punjab Chemicals & Crop Protection"},
    {"symbol": "PURVA",        "name": "Puravankara"},
    # ── R ──
    {"symbol": "RAJRATAN",     "name": "Rajratan Global Wire"},
    {"symbol": "RANE",         "name": "Rane Holdings"},
    {"symbol": "RATNAMANI",    "name": "Ratnamani Metals & Tubes"},
    {"symbol": "RBA",          "name": "Restaurant Brands Asia"},
    {"symbol": "RECLTD",       "name": "REC Ltd"},
    {"symbol": "RELAXO",       "name": "Relaxo Footwears"},
    {"symbol": "RELIANCE",     "name": "Reliance Industries"},
    {"symbol": "RELIGARE",     "name": "Religare Enterprises"},
    {"symbol": "ROHLTD",       "name": "Royal Orchid Hotels"},
    {"symbol": "ROUTE",        "name": "Route Mobile"},
    # ── S ──
    {"symbol": "SAIL",         "name": "Steel Authority of India"},
    {"symbol": "SANOFI",       "name": "Sanofi India"},
    {"symbol": "SBIN",         "name": "State Bank of India"},
    {"symbol": "SCHAEFFLER",   "name": "Schaeffler India"},
    {"symbol": "SHANKARA",     "name": "Shankara Building Products"},
    {"symbol": "SHILPAMED",    "name": "Shilpa Medicare"},
    {"symbol": "SHREECEM",     "name": "Shree Cement"},
    {"symbol": "SHREDIGCEM",   "name": "Shree Digvijay Cement"},
    {"symbol": "SIEMENS",      "name": "Siemens India"},
    {"symbol": "SJVN",         "name": "SJVN"},
    {"symbol": "SMSPHARMA",    "name": "SMS Pharmaceuticals"},
    {"symbol": "SOLARA",       "name": "Solara Active Pharma Sciences"},
    {"symbol": "SONAMCLOCK",   "name": "Sona BLW Precision Forgings"},
    {"symbol": "SRF",          "name": "SRF"},
    {"symbol": "STAR",         "name": "Star Health and Allied Insurance"},
    {"symbol": "STYRENIX",     "name": "Styrenix Performance Materials"},
    {"symbol": "SUBEXLTD",     "name": "Subex"},
    {"symbol": "SUNPHARMA",    "name": "Sun Pharmaceutical Industries"},
    {"symbol": "SUNTV",        "name": "Sun TV Network"},
    {"symbol": "SUPRIYA",      "name": "Supriya Lifescience"},
    {"symbol": "SUPREMEIND",   "name": "Supreme Industries"},
    {"symbol": "SURYAROSNI",   "name": "Surya Roshni"},
    {"symbol": "SUZLON",       "name": "Suzlon Energy"},
    {"symbol": "SWSOLAR",      "name": "Sterling and Wilson Renewable Energy"},
    # ── T ──
    {"symbol": "TAKE",         "name": "Take Solutions"},
    {"symbol": "TANLA",        "name": "Tanla Platforms"},
    {"symbol": "TATACHEM",     "name": "Tata Chemicals"},
    {"symbol": "TATACOMM",     "name": "Tata Communications"},
    {"symbol": "TATACONSUM",   "name": "Tata Consumer Products"},
    {"symbol": "TATAELXSI",    "name": "Tata Elxsi"},
    {"symbol": "TATAMOTORS",   "name": "Tata Motors"},
    {"symbol": "TATAPOWER",    "name": "Tata Power"},
    {"symbol": "TATASTEEL",    "name": "Tata Steel"},
    {"symbol": "TATATECH",     "name": "Tata Technologies"},
    {"symbol": "TCS",          "name": "Tata Consultancy Services"},
    {"symbol": "TECHM",        "name": "Tech Mahindra"},
    {"symbol": "THERMAX",      "name": "Thermax"},
    {"symbol": "TIMKEN",       "name": "Timken India"},
    {"symbol": "TITAN",        "name": "Titan Company"},
    {"symbol": "TNPETRO",      "name": "Tamil Nadu Petroproducts"},
    {"symbol": "TORNTPHARM",   "name": "Torrent Pharmaceuticals"},
    {"symbol": "TORNTPOWER",   "name": "Torrent Power"},
    {"symbol": "TTML",         "name": "Tata Teleservices Maharashtra"},
    {"symbol": "TVSMOTOR",     "name": "TVS Motor Company"},
    {"symbol": "TVSHLTD",      "name": "TVS Holdings"},
    {"symbol": "TVS-E",        "name": "TVS Electronics"},
    # ── U ──
    {"symbol": "UCOBANK",      "name": "UCO Bank"},
    {"symbol": "UJJIVANSFB",   "name": "Ujjivan Small Finance Bank"},
    {"symbol": "UNICHEMLAB",   "name": "Unichem Laboratories"},
    {"symbol": "UNITEDPOLY",   "name": "United Polyfab Gujarat"},
    {"symbol": "UNIVCABLES",   "name": "Universal Cables"},
    {"symbol": "UPL",          "name": "UPL"},
    {"symbol": "UTIAMC",       "name": "UTI Asset Management Company"},
    {"symbol": "ULTRACEMCO",   "name": "UltraTech Cement"},
    # ── V ──
    {"symbol": "VARDHACRLC",   "name": "Vardhman Acrylics"},
    {"symbol": "VARROC",       "name": "Varroc Engineering"},
    {"symbol": "VBL",          "name": "Varun Beverages"},
    {"symbol": "VENKEYS",      "name": "Venkys India"},
    {"symbol": "VESUVIUS",     "name": "Vesuvius India"},
    {"symbol": "VIRINCHI",     "name": "Virinchi"},
    {"symbol": "VOLTAMP",      "name": "Voltamp Transformers"},
    {"symbol": "VOLTAS",       "name": "Voltas"},
    {"symbol": "VTL",          "name": "Vardhman Special Steels"},
    # ── W ──
    {"symbol": "WABAG",        "name": "VA Tech Wabag"},
    {"symbol": "WEBELSOLAR",   "name": "Webel Solar"},
    {"symbol": "WELCORP",      "name": "Welspun Corp"},
    {"symbol": "WELENT",       "name": "Welspun Enterprises"},
    {"symbol": "WELSPUNLIV",   "name": "Welspun Living"},
    {"symbol": "WHIRLPOOL",    "name": "Whirlpool of India"},
    {"symbol": "WIPL",         "name": "Western India Plywoods"},
    {"symbol": "WIPRO",        "name": "Wipro"},
    {"symbol": "WOCKPHARMA",   "name": "Wockhardt"},
    # ── Y / Z ──
    {"symbol": "YESBANK",      "name": "Yes Bank"},
    {"symbol": "ZEEL",         "name": "Zee Entertainment Enterprises"},
    {"symbol": "ZENTEC",       "name": "Zen Technologies"},
    {"symbol": "ZOMATO",       "name": "Zomato"},
    # ── Commonly searched by company name (not symbol) ──
    {"symbol": "FRACTALINK",    "name": "Fractal Analytics"},
    {"symbol": "DELHIVERY",     "name": "Delhivery"},
    {"symbol": "NYKAA",         "name": "FSN E-Commerce Ventures (Nykaa)"},
    {"symbol": "PAYTM",         "name": "One97 Communications (Paytm)"},
    {"symbol": "POLICYBZR",     "name": "PB Fintech (PolicyBazaar)"},
    {"symbol": "CARTRADE",      "name": "CarTrade Tech"},
    {"symbol": "EASEMYTRIP",    "name": "Easy Trip Planners"},
    {"symbol": "ZOMATO",        "name": "Zomato"},
    {"symbol": "NAUKRI",        "name": "Info Edge India (Naukri)"},
    {"symbol": "JUSTDIAL",      "name": "Just Dial"},
    {"symbol": "INDIAMART",     "name": "IndiaMART InterMESH"},
    {"symbol": "MATRIMONY",     "name": "Matrimony.com"},
    {"symbol": "INDIGOPNTS",    "name": "Indigo Paints"},
    {"symbol": "LATENTVIEW",    "name": "Latent View Analytics"},
    {"symbol": "HAPPSTMNDS",    "name": "Happiest Minds Technologies"},
    {"symbol": "TANLA",         "name": "Tanla Platforms"},
    {"symbol": "ROUTE",         "name": "Route Mobile"},
    {"symbol": "RATEGAIN",      "name": "RateGain Travel Technologies"},
    {"symbol": "MTAR",          "name": "MTAR Technologies"},
    {"symbol": "NAZARA",        "name": "Nazara Technologies"},
    {"symbol": "HFCL",          "name": "HFCL"},
    {"symbol": "RAILTEL",       "name": "RailTel Corporation of India"},
    {"symbol": "IRCTC",         "name": "Indian Railway Catering & Tourism (IRCTC)"},
    {"symbol": "MAZDOCK",       "name": "Mazagon Dock Shipbuilders"},
    {"symbol": "COCHINSHIP",    "name": "Cochin Shipyard"},
    {"symbol": "GARUDA",        "name": "Garuda Construction and Engineering"},
    {"symbol": "KAYNES",        "name": "Kaynes Technology India"},
    {"symbol": "SYRMA",         "name": "Syrma SGS Technology"},
    {"symbol": "AVALON",        "name": "Avalon Technologies"},
    {"symbol": "CAMPUS",        "name": "Campus Activewear"},
    {"symbol": "VEDANT",        "name": "Vedant Fashions (Manyavar)"},
    {"symbol": "METROBRAND",    "name": "Metro Brands"},
    {"symbol": "SAPPHIRE",      "name": "Sapphire Foods India (KFC/Pizza Hut)"},
    {"symbol": "DEVYANI",       "name": "Devyani International (KFC/Pizza Hut)"},
    {"symbol": "WESTLIFE",      "name": "Westlife Foodworld (McDonald's)"},
    {"symbol": "BARBEQUE",      "name": "Barbeque Nation Hospitality"},
    {"symbol": "LEMONTREE",     "name": "Lemon Tree Hotels"},
    {"symbol": "CHALET",        "name": "Chalet Hotels"},
    {"symbol": "MAHINDCIE",     "name": "Mahindra CIE Automotive"},
    {"symbol": "SANSERA",       "name": "Sansera Engineering"},
    {"symbol": "CRAFTSMAN",     "name": "Craftsman Automation"},
    {"symbol": "SUPRAJIT",      "name": "Suprajit Engineering"},
    {"symbol": "LUMAXTECH",     "name": "Lumax Auto Technologies"},
    {"symbol": "LUMAXIND",      "name": "Lumax Industries"},
    {"symbol": "SONACOMS",      "name": "Sona BLW Precision Forgings"},
    {"symbol": "MINDAIND",      "name": "Minda Industries"},
    {"symbol": "TITAGARH",      "name": "Titagarh Rail Systems"},
    {"symbol": "TEXRAIL",       "name": "Texmaco Rail & Engineering"},
    {"symbol": "RVNL",          "name": "Rail Vikas Nigam"},
    {"symbol": "IRCON",         "name": "IRCON International"},
    {"symbol": "SJVN",          "name": "SJVN"},
    {"symbol": "NHPC",          "name": "NHPC"},
    {"symbol": "CESC",          "name": "CESC"},
    {"symbol": "TATAPOWER",     "name": "Tata Power Company"},
    {"symbol": "ADANIGREEN",    "name": "Adani Green Energy"},
    {"symbol": "ADANIENSOL",    "name": "Adani Energy Solutions"},
    {"symbol": "TORNTPOWER",    "name": "Torrent Power"},
    {"symbol": "SUZLON",        "name": "Suzlon Energy"},
    {"symbol": "INOXWIND",      "name": "Inox Wind"},
    {"symbol": "WINDWORLD",     "name": "Wind World India"},
    {"symbol": "STERLINWIL",    "name": "Sterling and Wilson Renewable Energy"},
    {"symbol": "BOSCHLTD",      "name": "Bosch India"},
    {"symbol": "SCHAEFFLER",    "name": "Schaeffler India"},
    {"symbol": "TIMKEN",        "name": "Timken India"},
    {"symbol": "SKFINDIA",      "name": "SKF India"},
    {"symbol": "GREAVESCOT",    "name": "Greaves Cotton"},
    {"symbol": "THERMAX",       "name": "Thermax"},
    {"symbol": "ISGEC",         "name": "Isgec Heavy Engineering"},
    {"symbol": "GMMPFAUDLR",    "name": "GMM Pfaudler"},
    {"symbol": "ELGIEQUIP",     "name": "Elgi Equipments"},
    {"symbol": "KIRLOSBROS",    "name": "Kirloskar Brothers"},
    {"symbol": "ASTRAL",        "name": "Astral Poly Technik"},
    {"symbol": "POLYCAB",       "name": "Polycab India"},
    {"symbol": "KEI",           "name": "KEI Industries"},
    {"symbol": "FINOLEX",       "name": "Finolex Cables"},
    {"symbol": "HBLENGINE",     "name": "HBL Engineering"},
    {"symbol": "KPITTECH",      "name": "KPIT Technologies"},
    {"symbol": "CYIENT",        "name": "Cyient"},
    {"symbol": "TATAELXSI",     "name": "Tata Elxsi"},
    {"symbol": "LTTS",          "name": "L&T Technology Services"},
    {"symbol": "PERSISTENT",    "name": "Persistent Systems"},
    {"symbol": "COFORGE",       "name": "Coforge"},
    {"symbol": "MPHASIS",       "name": "Mphasis"},
    {"symbol": "HEXAWARE",      "name": "Hexaware Technologies"},
    {"symbol": "NIIT",          "name": "NIIT"},
    {"symbol": "NIITTECH",      "name": "NIIT Technologies"},
    {"symbol": "MASTEK",        "name": "Mastek"},
    {"symbol": "SASKEN",        "name": "Sasken Technologies"},
    {"symbol": "ZENSAR",        "name": "Zensar Technologies"},
    {"symbol": "ZENSARTECH",    "name": "Zensar Technologies"},
    {"symbol": "INFOBEAN",      "name": "InfoBeans Technologies"},
    {"symbol": "INTELLECT",     "name": "Intellect Design Arena"},
    {"symbol": "RAMCOSYS",      "name": "Ramco Systems"},
    {"symbol": "NEWGEN",        "name": "Newgen Software Technologies"},
    {"symbol": "NUCLEUS",       "name": "Nucleus Software Exports"},
    {"symbol": "TANLA",         "name": "Tanla Platforms"},
    {"symbol": "ZUARI",        "name": "Zuari Agro Chemicals"},
    {"symbol": "ZYDUSLIFE",    "name": "Zydus Lifesciences"},
    {"symbol": "ZYDUSWEL",     "name": "Zydus Wellness"},
    # ── Additional Mid/Small Caps ──
    {"symbol": "360ONE",       "name": "360 ONE WAM"},
    {"symbol": "3MINDIA",      "name": "3M India"},
    {"symbol": "5PAISA",       "name": "5paisa Capital"},
    {"symbol": "ABAN",         "name": "Aban Offshore"},
    {"symbol": "ABMINTLLTD",   "name": "ABM International"},
    {"symbol": "ACCELYA",      "name": "Accelya Solutions India"},
    {"symbol": "ADANIENSOL",   "name": "Adani Energy Solutions"},
    {"symbol": "AGARIND",      "name": "Agar Industries"},
    {"symbol": "AGCNET",       "name": "AGC Networks"},
    {"symbol": "AGIIL",        "name": "AGI Infra"},
    {"symbol": "AGRITECH",     "name": "Agri-Tech India"},
    {"symbol": "AHMEDABADST",  "name": "Ahmedabad Steelcraft"},
    {"symbol": "AIIL",         "name": "Ace Integrated Solutions"},
    {"symbol": "AINATAUTO",    "name": "Ainat Auto"},
    {"symbol": "AIRAN",        "name": "Airan"},
    {"symbol": "AKASH",        "name": "Akash Infra-Projects"},
    {"symbol": "AKGEC",        "name": "AKGE Corporation"},
    {"symbol": "AKMOTORS",     "name": "AK Motors"},
    {"symbol": "ALICON",       "name": "Alicon Castalloy"},
    {"symbol": "ALKALI",       "name": "Alkali Metals"},
    {"symbol": "ALLSEC",       "name": "Allsec Technologies"},
    {"symbol": "ALMONDZ",      "name": "Almondz Global Securities"},
    {"symbol": "ALOKINDS",     "name": "Alok Industries"},
    {"symbol": "ALPHAGEO",     "name": "Alphageo India"},
    {"symbol": "ALSL",         "name": "ALS Ltd"},
    {"symbol": "AMAL",         "name": "AMAL"},
    {"symbol": "AMBALAFLEX",   "name": "Ambala Flexible Packaging"},
    {"symbol": "AMBALALSHA",   "name": "Ambalal Sarabhai Enterprises"},
    {"symbol": "AMJLAND",      "name": "AMJ Land Holdings"},
    {"symbol": "AMRUTANJAN",   "name": "Amrutanjan Health Care"},
    {"symbol": "ANDHRACEME",   "name": "Andhra Cements"},
    {"symbol": "ANDHRSUGAR",   "name": "Andhra Sugars"},
    {"symbol": "ANKITMETAL",   "name": "Ankit Metal & Power"},
    {"symbol": "ANSALAPI",     "name": "Ansal Properties & Infrastructure"},
    {"symbol": "ANTGRAPHIC",   "name": "Antarctica Graphic Industries"},
    {"symbol": "APCL",         "name": "Anjani Portland Cement"},
    {"symbol": "APOLLOPIPE",   "name": "Apollo Pipes"},
    {"symbol": "APOLSINHOT",   "name": "Apollo Sindhuri Hotels"},
    {"symbol": "APTECHT",      "name": "Aptech"},
    {"symbol": "ARCHIDPLY",    "name": "Archidply Industries"},
    {"symbol": "ARCHIES",      "name": "Archies"},
    {"symbol": "ARCOTECH",     "name": "Arcotech"},
    {"symbol": "ARFIN",        "name": "Arfin India"},
    {"symbol": "ARIHANTCAP",   "name": "Arihant Capital Markets"},
    {"symbol": "ARMANFIN",     "name": "Arman Financial Services"},
    {"symbol": "ARNAV",        "name": "Arnav Properties"},
    {"symbol": "ARROWHEAD",    "name": "Arrowhead Separation Engineering"},
    {"symbol": "ARTEDZ",       "name": "Arte Do Brasil"},
    {"symbol": "ARVIND",       "name": "Arvind"},
    {"symbol": "ARVINDFASN",   "name": "Arvind Fashions"},
    {"symbol": "ARVSMART",     "name": "Arvind Smartspaces"},
    {"symbol": "ASALCBR",      "name": "Associated Alcohols & Breweries"},
    {"symbol": "ASHIANA",      "name": "Ashiana Housing"},
    {"symbol": "ASHIMASYN",    "name": "Ashima"},
    {"symbol": "ASHOKA",       "name": "Ashoka Buildcon"},
    {"symbol": "ASIL",         "name": "Autoline Industries"},
    {"symbol": "ASKFINVEST",   "name": "ASK Investment Managers"},
    {"symbol": "ATUL",         "name": "Atul Ltd"},
    {"symbol": "ATULAUTO",     "name": "Atul Auto"},
    {"symbol": "AUSOMENT",     "name": "Ausom Enterprise"},
    {"symbol": "AVONMORE",     "name": "Avonmore Capital & Management Services"},
    {"symbol": "AVROIND",      "name": "AVRO India"},
    {"symbol": "AXISHD",       "name": "Axis Housing Developers"},
    {"symbol": "BAFNAPH",      "name": "Bafna Pharmaceuticals"},
    {"symbol": "BAJAJHFL",     "name": "Bajaj Housing Finance"},
    {"symbol": "BALAJITELE",   "name": "Balaji Telefilms"},
    {"symbol": "BALAXI",       "name": "Balaxi Pharmaceuticals"},
    {"symbol": "BALKRISHNA",   "name": "Balkrishna Paper Mills"},
    {"symbol": "BALLARPUR",    "name": "Ballarpur Industries"},
    {"symbol": "BALPHARMA",    "name": "Bal Pharma"},
    {"symbol": "BANARBEADS",   "name": "Banaras Beads"},
    {"symbol": "BANSALWIRE",   "name": "Bansal Wire Industries"},
    {"symbol": "BARBEQUE",     "name": "Barbeque Nation Hospitality"},
    {"symbol": "BASF",         "name": "BASF India"},
    {"symbol": "BCONCEPTS",    "name": "Business Concepts"},
    {"symbol": "BECKIND",      "name": "Beckett India"},
    {"symbol": "BEDMUTHA",     "name": "Bedmutha Industries"},
    {"symbol": "BERVIN",       "name": "Bervin Investment & Leasing"},
    {"symbol": "BGRENERGY",    "name": "BGR Energy Systems"},
    {"symbol": "BIGBLOC",      "name": "Bigbloc Construction"},
    {"symbol": "BINDALAGRO",   "name": "Bindal Agro Chem"},
    {"symbol": "BIOFILCHEM",   "name": "Biofil Chemicals & Pharmaceuticals"},
    {"symbol": "BIORASI",      "name": "Biora Sciences"},
    {"symbol": "BIRLA",        "name": "Birla Corporation"},
    {"symbol": "BIRLACABLE",   "name": "Birla Cable"},
    {"symbol": "BIRLASEMI",    "name": "Birla Semiconductor"},
    {"symbol": "BIRLASUN",     "name": "Birla Sun Life AMC"},
    {"symbol": "BKMINDST",     "name": "BKM Industries"},
    {"symbol": "BLBLIMITED",   "name": "BLB"},
    {"symbol": "BLKASHYAP",    "name": "B L Kashyap & Sons"},
    {"symbol": "BMSLINVIT",    "name": "BMS InvIT"},
    {"symbol": "BOMDYEING",    "name": "Bombay Dyeing"},
    {"symbol": "BOROLTD",      "name": "Borosil"},
    {"symbol": "BPCL",         "name": "Bharat Petroleum Corporation"},
    {"symbol": "BRENDAFOODS",  "name": "Brenda Foods"},
    {"symbol": "BRIGHTCOM",    "name": "Brightcom Group"},
    {"symbol": "BURNPUR",      "name": "Burnpur Cement"},
    {"symbol": "BVCL",         "name": "Barak Valley Cements"},
    {"symbol": "BYKE",         "name": "The Byke Hospitality"},
    {"symbol": "CALSOFT",      "name": "California Software"},
    {"symbol": "CAMLINFINE",   "name": "Camlin Fine Sciences"},
    {"symbol": "CANOVIND",     "name": "Canov Industries"},
    {"symbol": "CAPACITE",     "name": "Capacit'e Infraprojects"},
    {"symbol": "CARERATING",   "name": "CARE Ratings"},
    {"symbol": "CASTEXTECH",   "name": "Castex Technologies"},
    {"symbol": "CCL",          "name": "CCL Products India"},
    {"symbol": "CCLPROD",      "name": "CCL Products India"},
    {"symbol": "CEINSYS",      "name": "Ceinsys Tech"},
    {"symbol": "CENTURYTEX",   "name": "Century Textiles & Industries"},
    {"symbol": "CERA",         "name": "Cera Sanitaryware"},
    {"symbol": "CGCL",         "name": "Capri Global Capital"},
    {"symbol": "CHEMBOND",     "name": "Chembond Chemicals"},
    {"symbol": "CHEVIOT",      "name": "Cheviot Company"},
    {"symbol": "CHOICEIN",     "name": "Choice International"},
    {"symbol": "CINELINE",     "name": "Cineline India"},
    {"symbol": "CLNINDIA",     "name": "Clean India Ventures"},
    {"symbol": "CMMIPL",       "name": "CMM Infraprojects"},
    {"symbol": "COASTALMED",   "name": "Coastal Media"},
    {"symbol": "COCHINSHIP",   "name": "Cochin Shipyard"},
    {"symbol": "COLGATE",      "name": "Colgate-Palmolive India"},
    {"symbol": "COMPINFO",     "name": "Compucom Software"},
    {"symbol": "CONFIPET",     "name": "Confidence Petroleum India"},
    {"symbol": "CONTINENT",    "name": "Continental Engines"},
    {"symbol": "CREST",        "name": "Crest Ventures"},
    {"symbol": "CROWN",        "name": "Crown Lifters"},
    {"symbol": "CSBBANK",      "name": "CSB Bank"},
    {"symbol": "CSLFINANCE",   "name": "CSL Finance"},
    {"symbol": "CYIENT",       "name": "Cyient"},
    {"symbol": "CYIENTDLM",    "name": "Cyient DLM"},
    {"symbol": "D2LCNEWS",     "name": "D2L News"},
    {"symbol": "DAAWAT",       "name": "LT Foods (Daawat)"},
    {"symbol": "DAMODARIND",   "name": "Damodar Industries"},
    {"symbol": "DANLAW",       "name": "Danlaw Technologies India"},
    {"symbol": "DBREALTY",     "name": "D B Realty"},
    {"symbol": "DCAL",         "name": "Dishman Carbogen Amcis"},
    {"symbol": "DECCANCE",     "name": "Deccan Cements"},
    {"symbol": "DEEPINDS",     "name": "Deep Industries"},
    {"symbol": "DELHIVERY",    "name": "Delhivery"},
    {"symbol": "DELNA",        "name": "Delna International"},
    {"symbol": "DEVYANI",      "name": "Devyani International"},
    {"symbol": "DFMFOODS",     "name": "DFM Foods"},
    {"symbol": "DHAMPURSUG",   "name": "Dhampur Sugar Mills"},
    {"symbol": "DICKSONREAL",  "name": "Dickson Real Estate"},
    {"symbol": "DIGISPICE",    "name": "DigiSpice Technologies"},
    {"symbol": "DIVISLAB",     "name": "Divi's Laboratories"},
    {"symbol": "DLINKINDIA",   "name": "D-Link India"},
    {"symbol": "DLTINDIA",     "name": "DLT India"},
    {"symbol": "DMCC",         "name": "DMCC"},
    {"symbol": "DOLLEX",       "name": "Dollex Agrotech"},
    {"symbol": "DPSCLTD",      "name": "DPSC"},
    {"symbol": "DREDGECORP",   "name": "Dredging Corporation of India"},
    {"symbol": "EIHOTEL",      "name": "EIH (Oberoi Hotels)"},
    {"symbol": "ELGITREATS",   "name": "Elgi Rubber"},
    {"symbol": "EMCO",         "name": "EMCO"},
    {"symbol": "EMKAY",        "name": "Emkay Global Financial Services"},
    {"symbol": "EPIGRAL",      "name": "Epigral"},
    {"symbol": "EPS",          "name": "E P S (India)"},
    {"symbol": "EQUITAS",      "name": "Equitas Holdings"},
    {"symbol": "EROSMEDIA",    "name": "Eros International Media"},
    {"symbol": "ETHOS",        "name": "Ethos"},
    {"symbol": "EVERESTIND",   "name": "Everest Industries"},
    {"symbol": "EXCEL",        "name": "Excel Industries"},
    {"symbol": "EXCELINDUS",   "name": "Excel Industries"},
    {"symbol": "EXPERION",     "name": "Experion Developers"},
    {"symbol": "FCSSOFT",      "name": "FCS Software Solutions"},
    {"symbol": "FEDERALBNK",   "name": "Federal Bank"},
    {"symbol": "FERMENTA",     "name": "Fermenta Biotech"},
    {"symbol": "FIRSTSOURCE",  "name": "Firstsource Solutions"},
    {"symbol": "FLAIR",        "name": "Flair Writing Industries"},
    {"symbol": "FLFL",         "name": "Future Lifestyle Fashions"},
    {"symbol": "FOODSIN",      "name": "Foods & Inns"},
    {"symbol": "FOSECOIND",    "name": "Foseco India"},
    {"symbol": "FRESHTROP",    "name": "Freshtrop Fruits"},
    {"symbol": "GABRIEL",      "name": "Gabriel India"},
    {"symbol": "GARDENSILK",   "name": "Garden Silk Mills"},
    {"symbol": "GATEWAY",      "name": "Gateway Distriparks"},
    {"symbol": "GENUSPOWER",   "name": "Genus Power Infrastructures"},
    {"symbol": "GEOMETRIX",    "name": "Geometrix"},
    {"symbol": "GEOJITFSL",    "name": "Geojit Financial Services"},
    {"symbol": "GESHIP",       "name": "Great Eastern Shipping"},
    {"symbol": "GLAND",        "name": "Gland Pharma"},
    {"symbol": "GLAXO",        "name": "GSK Pharma India"},
    {"symbol": "GLOBUSSPR",    "name": "Globus Spirits"},
    {"symbol": "GMDCLTD",      "name": "GMDC"},
    {"symbol": "GODHA",        "name": "Godha Cabcon & Insulation"},
    {"symbol": "GOPAL",        "name": "Gopal Snacks"},
    {"symbol": "GPIL",         "name": "Godawari Power & Ispat"},
    {"symbol": "GREAVESCOT",   "name": "Greaves Cotton"},
    {"symbol": "GREENPOWER",   "name": "Orient Green Power"},
    {"symbol": "GREENPLY",     "name": "Greenply Industries"},
    {"symbol": "GUJCHEM",      "name": "Gujarat Chemicals"},
    {"symbol": "GUJHOVAL",     "name": "Gujarat Hotel & Allied Industries"},
    {"symbol": "GUJNRE",       "name": "Gujarat NRE Coke"},
    {"symbol": "GULPOLY",      "name": "Gulf Polyols"},
    {"symbol": "HALDYNGAS",    "name": "Haldyn Glass Gujarat"},
    {"symbol": "HARDCASTLE",   "name": "Hardcastle & Waud Manufacturing"},
    {"symbol": "HEIDELBERG",   "name": "HeidelbergCement India"},
    {"symbol": "HEMIPROP",     "name": "Hemisphere Properties India"},
    {"symbol": "HEMISPHEREMEDIA","name": "Hemisphere Media Group"},
    {"symbol": "HERANBA",      "name": "Heranba Industries"},
    {"symbol": "HGSIND",       "name": "HGS Industries"},
    {"symbol": "HIMATSEIDE",   "name": "Himatsingka Seide"},
    {"symbol": "HINDCOPPER",   "name": "Hindustan Copper"},
    {"symbol": "HINDOILEXP",   "name": "Hindustan Oil Exploration"},
    {"symbol": "HINDPETRO",    "name": "Hindustan Petroleum"},
    {"symbol": "HINDZINC",     "name": "Hindustan Zinc"},
    {"symbol": "HOEC",         "name": "Hindustan Oil Exploration"},
    {"symbol": "HONDAPOWER",   "name": "Honda India Power Products"},
    {"symbol": "HSCL",         "name": "Himadri Speciality Chemical"},
    {"symbol": "HUHTAMAKI",    "name": "Huhtamaki India"},
    {"symbol": "ICICLOMBARD",  "name": "ICICI Lombard General Insurance"},
    {"symbol": "IDBI",         "name": "IDBI Bank"},
    {"symbol": "IEGCL",        "name": "IEGCL"},
    {"symbol": "IMAGICAA",     "name": "Imagicaaworld Entertainment"},
    {"symbol": "IMPAL",        "name": "India Motor Parts & Accessories"},
    {"symbol": "IMPEXFERRO",   "name": "Impex Ferro Tech"},
    {"symbol": "INATECH",      "name": "Innodata Technology"},
    {"symbol": "INDBANK",      "name": "Indbank Merchant Banking"},
    {"symbol": "INDIAGLYCO",   "name": "India Glycols"},
    {"symbol": "INDIAINFOLIN", "name": "India Infoline Finance"},
    {"symbol": "INDIALAND",    "name": "India Land & Properties"},
    {"symbol": "INDIANHUME",   "name": "Indian Hume Pipe"},
    {"symbol": "INDORAMA",     "name": "Indo Rama Synthetics"},
    {"symbol": "INDOSTAR",     "name": "IndoStar Capital Finance"},
    {"symbol": "INDOTECH",     "name": "Indo Tech Transformers"},
    {"symbol": "INDOTHAI",     "name": "Indo Thai Securities"},
    {"symbol": "INDSTERLING",  "name": "Ind-Sterling Global Securities"},
    {"symbol": "INDUCTOTHERM", "name": "Inductotherm India"},
    {"symbol": "INDSEC",       "name": "IND Securities"},
    {"symbol": "INFRA",        "name": "Infra IT Solutions"},
    {"symbol": "INOX",         "name": "Inox Leisure"},
    {"symbol": "INOXAPA",      "name": "INOX Air Products"},
    {"symbol": "IOCL",         "name": "Indian Oil Corporation"},
    {"symbol": "IODINE",       "name": "Iodine India"},
    {"symbol": "IPLPOLY",      "name": "IPL Plastics"},
    {"symbol": "IRCON",        "name": "IRCON International"},
    {"symbol": "ISGEC",        "name": "Isgec Heavy Engineering"},
    {"symbol": "ITDC",         "name": "India Tourism Development Corporation"},
    {"symbol": "ITEL",         "name": "Itel Industries"},
    {"symbol": "ITALIKA",      "name": "Italika Industries"},
    {"symbol": "JAMNAAUTO",    "name": "Jamna Auto Industries"},
    {"symbol": "JASCH",        "name": "Jasch Industries"},
    {"symbol": "JAYAGROGN",    "name": "Jayant Agro-Organics"},
    {"symbol": "JAYBPHARMA",   "name": "Jayb Pharmaceuticals"},
    {"symbol": "JBFIND",       "name": "JBF Industries"},
    {"symbol": "JCHAC",        "name": "Johnson Controls-Hitachi Air Conditioning"},
    {"symbol": "JETFREIGHT",   "name": "Jet Freight Logistics"},
    {"symbol": "JETKING",      "name": "Jetking Infotrain"},
    {"symbol": "JKIND",        "name": "JK India"},
    {"symbol": "JMTAUTOLTD",   "name": "JMT Auto"},
    {"symbol": "JOCILLTD",     "name": "Jocil"},
    {"symbol": "JOSWA",        "name": "Joswa"},
    {"symbol": "JUMBO",        "name": "Jumbo Bag"},
    {"symbol": "JUPITERMEN",   "name": "Jupiter Mines"},
    {"symbol": "KAIZENAGRO",   "name": "Kaizen Agro"},
    {"symbol": "KCPSUGIND",    "name": "KCP Sugar & Industries"},
    {"symbol": "KELTECH",      "name": "Keltech Energies"},
    {"symbol": "KENNAMET",     "name": "Kennametal India"},
    {"symbol": "KESORAMIND",   "name": "Kesoram Industries"},
    {"symbol": "KHAITANLTD",   "name": "Khaitan (India)"},
    {"symbol": "KHANDSE",      "name": "Khandwala Securities"},
    {"symbol": "KHANDSINTL",   "name": "Khandwala & Sons"},
    {"symbol": "KHFIN",        "name": "KH Fin"},
    {"symbol": "KITEX",        "name": "Kitex Garments"},
    {"symbol": "KKCL",         "name": "Kewal Kiran Clothing"},
    {"symbol": "KMSUGAR",      "name": "KM Sugar Mills"},
    {"symbol": "KORES",        "name": "Kores India"},
    {"symbol": "KOTARISUG",    "name": "Kothari Sugars & Chemicals"},
    {"symbol": "KOVAI",        "name": "Kovai Medical Center & Hospital"},
    {"symbol": "KPEL",         "name": "K P Energy"},
    {"symbol": "KRBL",         "name": "KRBL (India Gate Rice)"},
    {"symbol": "KREBSBIO",     "name": "Krebs Biochemicals & Industries"},
    {"symbol": "KRISHNADEF",   "name": "Krishna Defence & Allied Industries"},
    {"symbol": "KUNDAN",       "name": "Kundan Care Products"},
    {"symbol": "KUWAITRFM",    "name": "Kuwait RFM"},
    {"symbol": "KVISINDS",     "name": "KVIS Industries"},
    {"symbol": "LANCER",       "name": "Lancer Container Lines"},
    {"symbol": "LANDMARK",     "name": "Landmark Cars"},
    {"symbol": "LEMONTREE",    "name": "Lemon Tree Hotels"},
    {"symbol": "LIBERTSHOE",   "name": "Liberty Shoes"},
    {"symbol": "LIKHITHA",     "name": "Likhitha Infrastructure"},
    {"symbol": "LINGAFORTEC",  "name": "Lingafortec"},
    {"symbol": "LKPFIN",       "name": "LKP Finance"},
    {"symbol": "LLOYDMET",     "name": "Lloyd Metals & Energy"},
    {"symbol": "LNTFINANCE",   "name": "L&T Finance"},
    {"symbol": "LSIND",        "name": "L S Industries"},
    {"symbol": "LUMAXIND",     "name": "Lumax Industries"},
    {"symbol": "LUMAXTECH",    "name": "Lumax Auto Technologies"},
    {"symbol": "MADHAV",       "name": "Madhav Copper"},
    {"symbol": "MAHINDRALT",   "name": "Mahindra Logistics"},
    {"symbol": "MAHLOG",       "name": "Mahindra Logistics"},
    {"symbol": "MAHSCOOTER",   "name": "Maharashtra Scooters"},
    {"symbol": "MAHUAMEDIA",   "name": "Mahua Media"},
    {"symbol": "MALUPAPER",    "name": "Malu Paper Mills"},
    {"symbol": "MARATHON",     "name": "Marathon Nextgen Realty"},
    {"symbol": "MARKSANS",     "name": "Marksans Pharma"},
    {"symbol": "MAXHEALTH",    "name": "Max Healthcare Institute"},
    {"symbol": "MAXIND",       "name": "Max India"},
    {"symbol": "MAZDOCK",      "name": "Mazagon Dock Shipbuilders"},
    {"symbol": "MCLEODRUSS",   "name": "McLeod Russel India"},
    {"symbol": "MEGASOFT",     "name": "Mega Soft"},
    {"symbol": "MENONBE",      "name": "Menon Bearings"},
    {"symbol": "METROBRAND",   "name": "Metro Brands"},
    {"symbol": "MFSL",         "name": "Max Financial Services"},
    {"symbol": "MIDAIND",      "name": "Minda Corporation"},
    {"symbol": "MINDACORP",    "name": "Minda Corporation"},
    {"symbol": "MINDAIND",     "name": "Minda Industries"},
    {"symbol": "MIRCELECTR",   "name": "MIRC Electronics (Onida)"},
    {"symbol": "MITCON",       "name": "MITCON Consultancy & Engineering Services"},
    {"symbol": "MMFL",         "name": "MM Forgings"},
    {"symbol": "MMTC",         "name": "MMTC"},
    {"symbol": "MONTECARLO",   "name": "Monte Carlo Fashions"},
    {"symbol": "MOREPENLAB",   "name": "Morepen Laboratories"},
    {"symbol": "MTNL",         "name": "Mahanagar Telephone Nigam"},
    {"symbol": "MUNJALSHOW",   "name": "Munjal Showa"},
    {"symbol": "MUTHOOTCAP",   "name": "Muthoot Capital Services"},
    {"symbol": "NACLIND",      "name": "NACL Industries"},
    {"symbol": "NAGAFERT",     "name": "Nagarjuna Fertilizers & Chemicals"},
    {"symbol": "NAGARFERT",    "name": "Nagarjuna Fertilizers"},
    {"symbol": "NAGREEKEXP",   "name": "Nagreeka Exports"},
    {"symbol": "NARBADA",      "name": "Narbada Gems & Jewellery"},
    {"symbol": "NATHBIOKEM",   "name": "Nath Bio-Genes India"},
    {"symbol": "NCLIND",       "name": "NCL Industries"},
    {"symbol": "NDTV",         "name": "New Delhi Television"},
    {"symbol": "NELCAST",      "name": "Nelcast"},
    {"symbol": "NELCO",        "name": "Nelco"},
    {"symbol": "NESCO",        "name": "NESCO"},
    {"symbol": "NETWORK18",    "name": "Network18 Media & Investments"},
    {"symbol": "NIFCO",        "name": "Nifco India"},
    {"symbol": "NILAINFRA",    "name": "Nila Infrastructures"},
    {"symbol": "NILASPACES",   "name": "Nila Spaces"},
    {"symbol": "NIPPOBATRY",   "name": "Nippo Batteries"},
    {"symbol": "NITIRAJ",      "name": "Nitiraj Engineers"},
    {"symbol": "NKIND",        "name": "NK Industries"},
    {"symbol": "NOCIL",        "name": "NOCIL"},
    {"symbol": "NRAIL",        "name": "N R Agarwal Industries"},
    {"symbol": "NRL",          "name": "Numaligarh Refinery"},
    {"symbol": "OBEROIRLTY",   "name": "Oberoi Realty"},
    {"symbol": "OCL",          "name": "OCL India"},
    {"symbol": "OILCOUNTUB",   "name": "Oil Country Tubular"},
    {"symbol": "OLECTRA",      "name": "Olectra Greentech"},
    {"symbol": "ONELIFECP",    "name": "One Life Capital Advisors"},
    {"symbol": "ONWARD",       "name": "Onward Technologies"},
    {"symbol": "ORIENTBELL",   "name": "Orient Bell"},
    {"symbol": "ORIENTCEM",    "name": "Orient Cement"},
    {"symbol": "ORIENTHOT",    "name": "Oriental Hotels"},
    {"symbol": "ORTINLABS",    "name": "Ortin Laboratories"},
    {"symbol": "OSWALAGRO",    "name": "Oswal Agro Mills"},
    {"symbol": "OSWALGREEN",   "name": "Oswal Greentech"},
    {"symbol": "PANCHLATEX",   "name": "Panch Tatva Organics"},
    {"symbol": "PANINDIAN",    "name": "Pan India Infraprojects"},
    {"symbol": "PARACABLES",   "name": "Para Cables"},
    {"symbol": "PARAS",        "name": "Paras Defence & Space Technologies"},
    {"symbol": "PARENTERAL",   "name": "Parenteral Drugs India"},
    {"symbol": "PATELENG",     "name": "Patel Engineering"},
    {"symbol": "PATINTLOG",    "name": "Patel Integrated Logistics"},
    {"symbol": "PAVNAWIND",    "name": "Pavna Industries"},
    {"symbol": "PCJEWELLER",   "name": "PC Jeweller"},
    {"symbol": "PDMJEPAPER",   "name": "PDM Jalgaon Paper"},
    {"symbol": "PEARLPOLY",    "name": "Pearl Polymers"},
    {"symbol": "PEL",          "name": "Piramal Enterprises"},
    {"symbol": "PENIND",       "name": "Peninsula Land"},
    {"symbol": "PENINLAND",    "name": "Peninsula Land"},
    {"symbol": "PGHL",         "name": "Procter & Gamble Health"},
    {"symbol": "PGIL",         "name": "Pearl Global Industries"},
    {"symbol": "PHILIPCARB",   "name": "Phillips Carbon Black"},
    {"symbol": "PHOENIXLTD",   "name": "Phoenix Mills"},
    {"symbol": "PILANIINVST",  "name": "Pilani Investment & Industries"},
    {"symbol": "PIRAMALLTD",   "name": "Piramal Capital & Housing Finance"},
    {"symbol": "PNBGILTS",     "name": "PNB Gilts"},
    {"symbol": "PNBHOUSING",   "name": "PNB Housing Finance"},
    {"symbol": "POLYFILM",     "name": "Polyplex Corporation"},
    {"symbol": "POONA",        "name": "Poona Dal & Oil Industries"},
    {"symbol": "POONAWALLA",   "name": "Poonawalla Fincorp"},
    {"symbol": "PREMIERENRG",  "name": "Premier Energies"},
    {"symbol": "PRESTIGE",     "name": "Prestige Estates Projects"},
    {"symbol": "PRIMEHOT",     "name": "Prime Hotels"},
    {"symbol": "PRINCEPIPE",   "name": "Prince Pipes & Fittings"},
    {"symbol": "PRITI",        "name": "Priti International"},
    {"symbol": "PROCTER",      "name": "Procter & Gamble Hygiene & Health Care"},
    {"symbol": "PUNJLLOYD",    "name": "Punj Lloyd"},
    {"symbol": "PVRL",         "name": "PVR INOX"},
    {"symbol": "PVRINOX",      "name": "PVR INOX"},
    {"symbol": "QUESS",        "name": "Quess Corp"},
    {"symbol": "QUICKHEAL",    "name": "Quick Heal Technologies"},
    {"symbol": "RAILTEL",      "name": "RailTel Corporation"},
    {"symbol": "RAIPUR",       "name": "Raipur Alloys & Steel"},
    {"symbol": "RAJESHEXPO",   "name": "Rajesh Exports"},
    {"symbol": "RAMCOCEM",     "name": "Ramco Cements"},
    {"symbol": "RAMCOSYS",     "name": "Ramco Systems"},
    {"symbol": "RANEHOLDIN",   "name": "Rane Holdings"},
    {"symbol": "RATEGAIN",     "name": "RateGain Travel Technologies"},
    {"symbol": "RAYCTEX",      "name": "Raymond"},
    {"symbol": "RAYMOND",      "name": "Raymond"},
    {"symbol": "RCF",          "name": "Rashtriya Chemicals & Fertilizers"},
    {"symbol": "RCOM",         "name": "Reliance Communications"},
    {"symbol": "RDBNIS",       "name": "RDB Nisarg"},
    {"symbol": "REDINGTON",    "name": "Redington"},
    {"symbol": "RELCHEMQ",     "name": "Reliance Chemotex Industries"},
    {"symbol": "RELAXO",       "name": "Relaxo Footwears"},
    {"symbol": "RELIGARE",     "name": "Religare Enterprises"},
    {"symbol": "REMSONSIND",   "name": "Remsons Industries"},
    {"symbol": "RENUKA",       "name": "Shree Renuka Sugars"},
    {"symbol": "RICOAUTO",     "name": "Rico Auto Industries"},
    {"symbol": "RINFRA",       "name": "Reliance Infrastructure"},
    {"symbol": "RITCO",        "name": "Ritco Logistics"},
    {"symbol": "RITES",        "name": "RITES"},
    {"symbol": "RNAMETRO",     "name": "RNA Metro"},
    {"symbol": "RPOWER",       "name": "Reliance Power"},
    {"symbol": "RPSGVENT",     "name": "RPSG Ventures"},
    {"symbol": "RSYSTEMS",     "name": "R Systems International"},
    {"symbol": "RTNIND",       "name": "RTN Industries"},
    {"symbol": "RUBYMILLS",    "name": "Ruby Mills"},
    {"symbol": "RUPA",         "name": "Rupa & Company"},
    {"symbol": "RUSHIL",       "name": "Rushil Décor"},
    {"symbol": "SADBHAV",      "name": "Sadbhav Engineering"},
    {"symbol": "SAHANA",       "name": "Sahana System"},
    {"symbol": "SALASAR",      "name": "Salasar Techno Engineering"},
    {"symbol": "SALONA",       "name": "Salona Cotspin"},
    {"symbol": "SANGHIIND",    "name": "Sanghi Industries"},
    {"symbol": "SANGHVIMOV",   "name": "Sanghvi Movers"},
    {"symbol": "SANOFI",       "name": "Sanofi India"},
    {"symbol": "SANSERA",      "name": "Sansera Engineering"},
    {"symbol": "SAPPHIRE",     "name": "Sapphire Foods India"},
    {"symbol": "SARLAFIBER",   "name": "Sarla Performance Fibers"},
    {"symbol": "SAREGAMA",     "name": "Saregama India"},
    {"symbol": "SASKEN",       "name": "Sasken Technologies"},
    {"symbol": "SATINDLTD",    "name": "Satindra Investments"},
    {"symbol": "SATIN",        "name": "Satin Creditcare Network"},
    {"symbol": "SBCL",         "name": "Shivalik Bimetal Controls"},
    {"symbol": "SBICARD",      "name": "SBI Cards & Payment Services"},
    {"symbol": "SBILIFE",      "name": "SBI Life Insurance"},
    {"symbol": "SESHAPAPER",   "name": "Seshasayee Paper & Boards"},
    {"symbol": "SGBSEC",       "name": "SGB Securities"},
    {"symbol": "SGX",          "name": "SGX India"},
    {"symbol": "SHANTIGEAR",   "name": "Shanthi Gears"},
    {"symbol": "SHAREINDIA",   "name": "Share India Securities"},
    {"symbol": "SHETRONSE",    "name": "Shetron SE"},
    {"symbol": "SHILCHAR",     "name": "Shilchar Technologies"},
    {"symbol": "SHIVALIKBM",   "name": "Shivalik Bimetal Controls"},
    {"symbol": "SHREECEMENT",  "name": "Shree Cement"},
    {"symbol": "SHRIRAMPPS",   "name": "Shriram Pistons & Rings"},
    {"symbol": "SHYAMMETL",    "name": "Shyam Metalics & Energy"},
    {"symbol": "SICAL",        "name": "Sical Logistics"},
    {"symbol": "SIGACHI",      "name": "Sigachi Industries"},
    {"symbol": "SIGNATUREG",   "name": "Signature Global"},
    {"symbol": "SILVERTON",    "name": "Silverton Industries"},
    {"symbol": "SIMPLEXINF",   "name": "Simplex Infrastructures"},
    {"symbol": "SIRCA",        "name": "Sirca Paints India"},
    {"symbol": "SKIPPER",      "name": "Skipper"},
    {"symbol": "SKTFIN",       "name": "SKT Finance"},
    {"symbol": "SKM",          "name": "SKM Egg Products Export"},
    {"symbol": "SKMEGG",       "name": "SKM Egg Products Export"},
    {"symbol": "SKMOIC",       "name": "SKM Commodities"},
    {"symbol": "SNOWMAN",      "name": "Snowman Logistics"},
    {"symbol": "SOBHA",        "name": "Sobha"},
    {"symbol": "SOLAREDGE",    "name": "SolarEdge India"},
    {"symbol": "SONACOMS",     "name": "Sona BLW Precision Forgings"},
    {"symbol": "SPARC",        "name": "Sun Pharma Advanced Research"},
    {"symbol": "SPENCERS",     "name": "Spencer's Retail"},
    {"symbol": "SPIC",         "name": "Southern Petrochemical Industries"},
    {"symbol": "SPLIL",        "name": "SPL Industries"},
    {"symbol": "SPTL",         "name": "Sintex Plastics Technology"},
    {"symbol": "SRHHYPOLTD",   "name": "SRH Hypo"},
    {"symbol": "SRPL",         "name": "Shree Ram Proteins"},
    {"symbol": "SSWL",         "name": "Steel Strips Wheels"},
    {"symbol": "STAMPEDE",     "name": "Stampede Capital"},
    {"symbol": "STAR",         "name": "Star Health & Allied Insurance"},
    {"symbol": "STARTECK",     "name": "Starteck Finance"},
    {"symbol": "STCINDIA",     "name": "State Trading Corporation of India"},
    {"symbol": "STEELCAS",     "name": "Steelcast"},
    {"symbol": "STICO",        "name": "STICO"},
    {"symbol": "STLTECH",      "name": "STL Technology Solutions"},
    {"symbol": "STONENEW",     "name": "Stone New"},
    {"symbol": "SUBCAPCITY",   "name": "Subhash Projects & Marketing"},
    {"symbol": "SUMICHEM",     "name": "Sumitomo Chemical India"},
    {"symbol": "SUNTECK",      "name": "Sunteck Realty"},
    {"symbol": "SUPRAJIT",     "name": "Suprajit Engineering"},
    {"symbol": "SURROUNDS",    "name": "Surrounds Media"},
    {"symbol": "SUULD",        "name": "Suuld"},
    {"symbol": "SVPGLOB",      "name": "SVP Global Ventures"},
    {"symbol": "SWELECTES",    "name": "Swelect Energy Systems"},
    {"symbol": "SWING",        "name": "Swing Industries"},
    {"symbol": "SYNCOMF",      "name": "Syncom Formulations India"},
    {"symbol": "SYNGENE",      "name": "Syngene International"},
    {"symbol": "SYRMA",        "name": "Syrma SGS Technology"},
    {"symbol": "TAINWALA",     "name": "Tainwala Personal Care Products"},
    {"symbol": "TATAINVEST",   "name": "Tata Investment Corporation"},
    {"symbol": "TATAMETALI",   "name": "Tata Metaliks"},
    {"symbol": "TATVA",        "name": "Tatva Chintan Pharma Chem"},
    {"symbol": "TEAMLEASE",    "name": "TeamLease Services"},
    {"symbol": "TIPSINDLTD",   "name": "Tips Industries"},
    {"symbol": "TITAGARH",     "name": "Titagarh Rail Systems"},
    {"symbol": "TITANIAIND",   "name": "Titania Industries"},
    {"symbol": "TNECSOL",      "name": "TNEC Solutions"},
    {"symbol": "TNPHARMA",     "name": "TN Pharma"},
    {"symbol": "TNTELE",       "name": "TN Telecom"},
    {"symbol": "TOKYOPLAST",   "name": "Tokyo Plast International"},
    {"symbol": "TORRENTPHARM", "name": "Torrent Pharmaceuticals"},
    {"symbol": "TPLPLASTEH",   "name": "TPL Plastech"},
    {"symbol": "TREEHOUSE",    "name": "Tree House Education & Accessories"},
    {"symbol": "TRITON",       "name": "Triton Valves"},
    {"symbol": "TRIVENI",      "name": "Triveni Engineering & Industries"},
    {"symbol": "TRUEBLK",      "name": "True Blue Creations"},
    {"symbol": "TTK",          "name": "TTK Healthcare"},
    {"symbol": "TTKPRESTIG",   "name": "TTK Prestige"},
    {"symbol": "TV18BRDCST",   "name": "TV18 Broadcast"},
    {"symbol": "TVSMOTOR",     "name": "TVS Motor Company"},
    {"symbol": "TVSSRICHAK",   "name": "TVS Srichakra"},
    {"symbol": "TWOSPACES",    "name": "Two Spaces Communications"},
    {"symbol": "UGARSUGAR",    "name": "The Ugar Sugar Works"},
    {"symbol": "UGROCAP",      "name": "Ugro Capital"},
    {"symbol": "UJJIVAN",      "name": "Ujjivan Financial Services"},
    {"symbol": "UNIPHOS",      "name": "United Phosphorus"},
    {"symbol": "UNIVERSOFTS",  "name": "Uni Softwares"},
    {"symbol": "URJA",         "name": "Urja Global"},
    {"symbol": "V2RETAIL",     "name": "V2 Retail"},
    {"symbol": "VAARAD",       "name": "Vaarad Ventures"},
    {"symbol": "VADILALIND",   "name": "Vadilal Industries"},
    {"symbol": "VAKRANGEE",    "name": "Vakrangee"},
    {"symbol": "VALIANTLAB",   "name": "Valiant Laboratories"},
    {"symbol": "VALUEIND",     "name": "Value Industries"},
    {"symbol": "VARIMAN",      "name": "Variman Global Enterprises"},
    {"symbol": "VEDANT",       "name": "Vedant Fashions (Manyavar)"},
    {"symbol": "VEDL",         "name": "Vedanta"},
    {"symbol": "VEEDOL",       "name": "Veedol International"},
    {"symbol": "VERSARAIL",    "name": "Versa Rail"},
    {"symbol": "VERYWARMKART", "name": "Very Warm Kart"},
    {"symbol": "VIJAYA",       "name": "Vijaya Diagnostic Centre"},
    {"symbol": "VIKASLIFE",    "name": "Vikas Lifecare"},
    {"symbol": "VISHNU",       "name": "Vishnu Chemicals"},
    {"symbol": "VISHNUPRT",    "name": "Vishnu Prakash R Punglia"},
    {"symbol": "VOLTAMP",      "name": "Voltamp Transformers"},
    {"symbol": "VPRPL",        "name": "VPRPL"},
    {"symbol": "VRLLOG",       "name": "VRL Logistics"},
    {"symbol": "VSTTILLERS",   "name": "VST Tillers Tractors"},
    {"symbol": "VSTIND",       "name": "VST Industries"},
    {"symbol": "VVINDIA",      "name": "VV India"},
    {"symbol": "WALCHANNAG",   "name": "Walchandnagar Industries"},
    {"symbol": "WATERBASE",    "name": "Waterbase"},
    {"symbol": "WELCORP",      "name": "Welspun Corp"},
    {"symbol": "WESTLIFE",     "name": "Westlife Foodworld"},
    {"symbol": "WSTCSTPAPR",   "name": "West Coast Paper Mills"},
    {"symbol": "XCHANGING",    "name": "Xchanging Solutions"},
    {"symbol": "XPRO",         "name": "Xpro India"},
    {"symbol": "YASHO",        "name": "Yasho Industries"},
    {"symbol": "YATHARTH",     "name": "Yatharth Hospital & Trauma Care Services"},
    {"symbol": "YUKEN",        "name": "Yuken India"},
    {"symbol": "ZENSARTECH",   "name": "Zensar Technologies"},
    {"symbol": "ZENTEC",       "name": "Zen Technologies"},
    # ── Commonly searched by company name (not symbol) ──
    {"symbol": "FRACTALINK",    "name": "Fractal Analytics"},
    {"symbol": "DELHIVERY",     "name": "Delhivery"},
    {"symbol": "NYKAA",         "name": "FSN E-Commerce Ventures (Nykaa)"},
    {"symbol": "PAYTM",         "name": "One97 Communications (Paytm)"},
    {"symbol": "POLICYBZR",     "name": "PB Fintech (PolicyBazaar)"},
    {"symbol": "CARTRADE",      "name": "CarTrade Tech"},
    {"symbol": "EASEMYTRIP",    "name": "Easy Trip Planners"},
    {"symbol": "ZOMATO",        "name": "Zomato"},
    {"symbol": "NAUKRI",        "name": "Info Edge India (Naukri)"},
    {"symbol": "JUSTDIAL",      "name": "Just Dial"},
    {"symbol": "INDIAMART",     "name": "IndiaMART InterMESH"},
    {"symbol": "MATRIMONY",     "name": "Matrimony.com"},
    {"symbol": "INDIGOPNTS",    "name": "Indigo Paints"},
    {"symbol": "LATENTVIEW",    "name": "Latent View Analytics"},
    {"symbol": "HAPPSTMNDS",    "name": "Happiest Minds Technologies"},
    {"symbol": "TANLA",         "name": "Tanla Platforms"},
    {"symbol": "ROUTE",         "name": "Route Mobile"},
    {"symbol": "RATEGAIN",      "name": "RateGain Travel Technologies"},
    {"symbol": "MTAR",          "name": "MTAR Technologies"},
    {"symbol": "NAZARA",        "name": "Nazara Technologies"},
    {"symbol": "HFCL",          "name": "HFCL"},
    {"symbol": "RAILTEL",       "name": "RailTel Corporation of India"},
    {"symbol": "IRCTC",         "name": "Indian Railway Catering & Tourism (IRCTC)"},
    {"symbol": "MAZDOCK",       "name": "Mazagon Dock Shipbuilders"},
    {"symbol": "COCHINSHIP",    "name": "Cochin Shipyard"},
    {"symbol": "GARUDA",        "name": "Garuda Construction and Engineering"},
    {"symbol": "KAYNES",        "name": "Kaynes Technology India"},
    {"symbol": "SYRMA",         "name": "Syrma SGS Technology"},
    {"symbol": "AVALON",        "name": "Avalon Technologies"},
    {"symbol": "CAMPUS",        "name": "Campus Activewear"},
    {"symbol": "VEDANT",        "name": "Vedant Fashions (Manyavar)"},
    {"symbol": "METROBRAND",    "name": "Metro Brands"},
    {"symbol": "SAPPHIRE",      "name": "Sapphire Foods India (KFC/Pizza Hut)"},
    {"symbol": "DEVYANI",       "name": "Devyani International (KFC/Pizza Hut)"},
    {"symbol": "WESTLIFE",      "name": "Westlife Foodworld (McDonald's)"},
    {"symbol": "BARBEQUE",      "name": "Barbeque Nation Hospitality"},
    {"symbol": "LEMONTREE",     "name": "Lemon Tree Hotels"},
    {"symbol": "CHALET",        "name": "Chalet Hotels"},
    {"symbol": "MAHINDCIE",     "name": "Mahindra CIE Automotive"},
    {"symbol": "SANSERA",       "name": "Sansera Engineering"},
    {"symbol": "CRAFTSMAN",     "name": "Craftsman Automation"},
    {"symbol": "SUPRAJIT",      "name": "Suprajit Engineering"},
    {"symbol": "LUMAXTECH",     "name": "Lumax Auto Technologies"},
    {"symbol": "LUMAXIND",      "name": "Lumax Industries"},
    {"symbol": "SONACOMS",      "name": "Sona BLW Precision Forgings"},
    {"symbol": "MINDAIND",      "name": "Minda Industries"},
    {"symbol": "TITAGARH",      "name": "Titagarh Rail Systems"},
    {"symbol": "TEXRAIL",       "name": "Texmaco Rail & Engineering"},
    {"symbol": "RVNL",          "name": "Rail Vikas Nigam"},
    {"symbol": "IRCON",         "name": "IRCON International"},
    {"symbol": "SJVN",          "name": "SJVN"},
    {"symbol": "NHPC",          "name": "NHPC"},
    {"symbol": "CESC",          "name": "CESC"},
    {"symbol": "TATAPOWER",     "name": "Tata Power Company"},
    {"symbol": "ADANIGREEN",    "name": "Adani Green Energy"},
    {"symbol": "ADANIENSOL",    "name": "Adani Energy Solutions"},
    {"symbol": "TORNTPOWER",    "name": "Torrent Power"},
    {"symbol": "SUZLON",        "name": "Suzlon Energy"},
    {"symbol": "INOXWIND",      "name": "Inox Wind"},
    {"symbol": "WINDWORLD",     "name": "Wind World India"},
    {"symbol": "STERLINWIL",    "name": "Sterling and Wilson Renewable Energy"},
    {"symbol": "BOSCHLTD",      "name": "Bosch India"},
    {"symbol": "SCHAEFFLER",    "name": "Schaeffler India"},
    {"symbol": "TIMKEN",        "name": "Timken India"},
    {"symbol": "SKFINDIA",      "name": "SKF India"},
    {"symbol": "GREAVESCOT",    "name": "Greaves Cotton"},
    {"symbol": "THERMAX",       "name": "Thermax"},
    {"symbol": "ISGEC",         "name": "Isgec Heavy Engineering"},
    {"symbol": "GMMPFAUDLR",    "name": "GMM Pfaudler"},
    {"symbol": "ELGIEQUIP",     "name": "Elgi Equipments"},
    {"symbol": "KIRLOSBROS",    "name": "Kirloskar Brothers"},
    {"symbol": "ASTRAL",        "name": "Astral Poly Technik"},
    {"symbol": "POLYCAB",       "name": "Polycab India"},
    {"symbol": "KEI",           "name": "KEI Industries"},
    {"symbol": "FINOLEX",       "name": "Finolex Cables"},
    {"symbol": "HBLENGINE",     "name": "HBL Engineering"},
    {"symbol": "KPITTECH",      "name": "KPIT Technologies"},
    {"symbol": "CYIENT",        "name": "Cyient"},
    {"symbol": "TATAELXSI",     "name": "Tata Elxsi"},
    {"symbol": "LTTS",          "name": "L&T Technology Services"},
    {"symbol": "PERSISTENT",    "name": "Persistent Systems"},
    {"symbol": "COFORGE",       "name": "Coforge"},
    {"symbol": "MPHASIS",       "name": "Mphasis"},
    {"symbol": "HEXAWARE",      "name": "Hexaware Technologies"},
    {"symbol": "NIIT",          "name": "NIIT"},
    {"symbol": "NIITTECH",      "name": "NIIT Technologies"},
    {"symbol": "MASTEK",        "name": "Mastek"},
    {"symbol": "SASKEN",        "name": "Sasken Technologies"},
    {"symbol": "ZENSAR",        "name": "Zensar Technologies"},
    {"symbol": "ZENSARTECH",    "name": "Zensar Technologies"},
    {"symbol": "INFOBEAN",      "name": "InfoBeans Technologies"},
    {"symbol": "INTELLECT",     "name": "Intellect Design Arena"},
    {"symbol": "RAMCOSYS",      "name": "Ramco Systems"},
    {"symbol": "NEWGEN",        "name": "Newgen Software Technologies"},
    {"symbol": "NUCLEUS",       "name": "Nucleus Software Exports"},
    {"symbol": "TANLA",         "name": "Tanla Platforms"},
    {"symbol": "ZUARI",        "name": "Zuari Agro Chemicals"},
    {"symbol": "ZYDUSLIFE",    "name": "Zydus Lifesciences"},
    {"symbol": "ZYDUSWEL",     "name": "Zydus Wellness"},
]



def _norm_search_text(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def get_search_universe():
    """Build one deduplicated search universe from all stock lists."""
    combined = []

    for var_name in ["SEARCH_STOCKS_EXTENDED", "WATCHLIST", "SCREEN_STOCKS"]:
        items = globals().get(var_name, [])
        if not items:
            continue

        if isinstance(items, list) and items and isinstance(items[0], str):
            for sym in items:
                combined.append({"symbol": sym, "name": sym})
        else:
            for item in items:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol", "")).strip().upper()
                name = str(item.get("name", sym)).strip()
                if sym:
                    combined.append({"symbol": sym, "name": name})

    aliases = [
        {"symbol": "RELIANCE", "name": "Reliance Industries", "aliases": ["RIL", "RELIANCE", "RELIANCEIND"]},
        {"symbol": "INFY", "name": "Infosys", "aliases": ["INFOSYS", "INFY"]},
        {"symbol": "TCS", "name": "Tata Consultancy Services", "aliases": ["TCS", "TATA CONSULTANCY", "TATA CONSULTANCY SERVICES"]},
        {"symbol": "HDFCBANK", "name": "HDFC Bank", "aliases": ["HDFC", "HDFC BANK", "HDFCBANK"]},
        {"symbol": "ICICIBANK", "name": "ICICI Bank", "aliases": ["ICICI", "ICICI BANK"]},
        {"symbol": "SBIN", "name": "State Bank of India", "aliases": ["SBI", "STATE BANK", "STATE BANK OF INDIA"]},
        {"symbol": "LT", "name": "Larsen & Toubro", "aliases": ["L&T", "LT", "LARSEN", "LARSEN TOUBRO"]},
        {"symbol": "M&M", "name": "Mahindra & Mahindra", "aliases": ["M&M", "MM", "MAHINDRA", "MAHINDRA AND MAHINDRA"]},
        {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto", "aliases": ["BAJAJ AUTO", "BAJAJAUTO"]},
        {"symbol": "COCHINSHIP", "name": "Cochin Shipyard", "aliases": ["COCHIN", "COCHIN SHIPYARD"]},
        {"symbol": "KPITTECH", "name": "KPIT Technologies", "aliases": ["KPIT", "KPIT TECH"]},
        {"symbol": "ABCAPITAL", "name": "Aditya Birla Capital", "aliases": ["AB CAPITAL", "ADITYA BIRLA CAPITAL"]},
        {"symbol": "ACMESOLAR", "name": "ACME Solar Holdings", "aliases": ["ACME SOLAR", "ACME"]},
        {"symbol": "LODHA", "name": "Macrotech Developers (Lodha)", "aliases": ["LODHA", "MACROTECH"]},
    ]

    alias_rows = []
    for item in aliases:
        for alias in item.get("aliases", []):
            alias_rows.append({"symbol": item["symbol"], "name": item["name"], "alias": alias})

    dedup = {}
    for item in combined:
        sym = _norm_search_text(item.get("symbol"))
        if not sym:
            continue
        if sym not in dedup:
            dedup[sym] = {
                "symbol": str(item.get("symbol", "")).strip().upper(),
                "name": str(item.get("name", "")).strip(),
                "aliases": []
            }

    for item in alias_rows:
        sym = _norm_search_text(item.get("symbol"))
        if sym not in dedup:
            dedup[sym] = {
                "symbol": str(item.get("symbol", "")).strip().upper(),
                "name": str(item.get("name", "")).strip(),
                "aliases": []
            }
        dedup[sym]["aliases"].append(item["alias"])

    return list(dedup.values())


@lru_cache(maxsize=1)
def _search_universe_cached():
    return get_search_universe()


@app.route("/api/search-universe")
def search_universe():
    universe = _search_universe_cached()
    return jsonify({"success": True, "count": len(universe), "stocks": universe[:200]})


def _score_candidate(query_raw, item):
    q = query_raw.strip().upper()
    qn = _norm_search_text(q)

    symbol = str(item.get("symbol", "")).upper()
    name = str(item.get("name", ""))
    aliases = item.get("aliases", []) or []

    symbol_n = _norm_search_text(symbol)
    name_n = _norm_search_text(name)
    alias_ns = [_norm_search_text(a) for a in aliases]

    score = 0
    reasons = []

    if q == symbol or qn == symbol_n:
        score += 1000
        reasons.append("exact_symbol")
    if q == name.upper() or qn == name_n:
        score += 950
        reasons.append("exact_name")
    if q in [a.upper() for a in aliases] or qn in alias_ns:
        score += 900
        reasons.append("exact_alias")

    if symbol.startswith(q) or symbol_n.startswith(qn):
        score += 350
        reasons.append("symbol_prefix")
    if name.upper().startswith(q) or name_n.startswith(qn):
        score += 300
        reasons.append("name_prefix")
    if any(a.upper().startswith(q) or _norm_search_text(a).startswith(qn) for a in aliases):
        score += 260
        reasons.append("alias_prefix")

    if q in symbol or qn in symbol_n:
        score += 220
        reasons.append("symbol_contains")
    if q in name.upper() or qn in name_n:
        score += 180
        reasons.append("name_contains")
    if any(q in a.upper() or qn in _norm_search_text(a) for a in aliases):
        score += 150
        reasons.append("alias_contains")

    fuzzy_scores = [
        difflib.SequenceMatcher(None, qn, symbol_n).ratio(),
        difflib.SequenceMatcher(None, qn, name_n).ratio(),
    ]
    for a in alias_ns:
        fuzzy_scores.append(difflib.SequenceMatcher(None, qn, a).ratio())

    best_fuzzy = max(fuzzy_scores) if fuzzy_scores else 0

    if best_fuzzy >= 0.92:
        score += 260
        reasons.append("fuzzy_very_strong")
    elif best_fuzzy >= 0.84:
        score += 180
        reasons.append("fuzzy_strong")
    elif best_fuzzy >= 0.75:
        score += 90
        reasons.append("fuzzy_ok")

    score -= max(len(symbol_n) - len(qn), 0) * 0.3

    return {
        "symbol": symbol,
        "name": name,
        "score": round(score, 2),
        "match_quality": round(best_fuzzy, 3),
        "aliases": aliases[:5],
        "reasons": reasons[:5],
    }


def _rank_search_results(raw_q, limit=30):
    universe = _search_universe_cached()
    q = (raw_q or "").strip()
    if not q:
        return []

    ranked = []
    for item in universe:
        scored = _score_candidate(q, item)
        if scored["score"] > 0:
            ranked.append(scored)

    ranked.sort(key=lambda x: (-x["score"], -x["match_quality"], len(x["symbol"]), x["symbol"]))

    final = []
    seen = set()
    for item in ranked:
        if item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        final.append(item)
        if len(final) >= limit:
            break

    return final


@app.route("/api/search")
def search_stocks():
    raw_q = (freq.args.get("q") or "").strip()

    try:
        limit = int(freq.args.get("limit", 10) or 10)
    except Exception:
        limit = 10

    limit = min(max(limit, 1), 50)
    enrich = str(freq.args.get("enrich", "0")).lower() in ("1", "true", "yes")

    if not raw_q:
        return jsonify({"success": True, "query": "", "count": 0, "results": []})

    try:
        results = _rank_search_results(raw_q, limit=limit)
    except Exception as e:
        return jsonify({
            "success": False,
            "query": raw_q,
            "count": 0,
            "results": [],
            "error": f"Search failed safely: {str(e)[:120]}"
        }), 200

    if not results:
        return jsonify({
            "success": True,
            "query": raw_q,
            "count": 0,
            "results": [],
            "message": "No exact match found. Try company name or NSE symbol."
        })

    if enrich:
        def fetch_change(item):
            sym = item["symbol"]
            cached = cache_get(f"price:{sym}")
            if cached:
                item["price"] = cached.get("price")
                item["change_pct"] = cached.get("pct")
                return item
            try:
                r = http_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=2d", headers=YFH, timeout=4)
                if r.ok:
                    meta = r.json()["chart"]["result"][0]["meta"]
                    p = meta.get("regularMarketPrice", 0)
                    prev = meta.get("chartPreviousClose", 0)
                    item["price"] = round(p, 2) if p else None
                    item["change_pct"] = round((p - prev) / prev * 100, 2) if prev else 0
                else:
                    item["price"] = None
                    item["change_pct"] = None
            except Exception:
                item["price"] = None
                item["change_pct"] = None
            return item

        top = results[:8]
        rest = results[8:]
        with ThreadPoolExecutor(max_workers=6) as pool:
            top = list(pool.map(fetch_change, top))
        results = top + rest

    return jsonify({"success": True, "query": raw_q, "count": len(results), "results": results})

@app.route("/api/screener")
def screener():
    # All filter params — 9999/-9999 = no filter applied
    def fp(k, default): 
        v = freq.args.get(k, "")
        try: return float(v) if v.strip() else default
        except: return default

    max_pe       = fp("max_pe",    9999)
    min_pe       = fp("min_pe",    0)
    max_peg      = fp("max_peg",   9999)
    min_roe      = fp("min_roe",   -9999)
    max_debt     = fp("max_debt",  9999)
    min_mcap     = fp("min_mcap",  0)          # in Crores
    max_mcap     = fp("max_mcap",  9999999999)
    min_promoter = fp("min_promoter", 0)
    max_promoter = fp("max_promoter", 100)
    min_fii      = fp("min_fii",   0)
    max_fii      = fp("max_fii",   100)
    min_dii      = fp("min_dii",   0)
    max_dii      = fp("max_dii",   100)
    min_public   = fp("min_public", 0)
    max_public   = fp("max_public", 100)
    symbols      = freq.args.get("symbols", ",".join(SCREEN_STOCKS)).split(",")

    def screen_one(sym):
        def safe_raw(d, k):
            try:
                v = d.get(k)
                if isinstance(v, dict): return v.get("raw")
                return v
            except: return None
        def safe_pct(d, k):
            try:
                v = safe_raw(d, k)
                if v is None: return 0.0
                return round(float(v) * 100, 2)
            except: return 0.0
        def safe_float(v, default=0.0):
            try: return float(v) if v is not None else default
            except: return default

        for base in ["query1", "query2"]:
            try:
                url = (f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/{sym}.NS"
                       f"?modules=defaultKeyStatistics%2CfinancialData%2CsummaryDetail%2CmajorHoldersBreakdown")
                r = requests.get(url, headers=YFH, timeout=8)
                if not r.ok: continue
                text = r.text.strip()
                if not text or text[0] not in '[{': continue
                data = r.json()
                result_list = data.get("quoteSummary", {}).get("result") or []
                if not result_list: continue
                res = result_list[0]
                fd  = res.get("financialData", {}) or {}
                sd  = res.get("summaryDetail", {}) or {}
                ks  = res.get("defaultKeyStatistics", {}) or {}
                mh  = res.get("majorHoldersBreakdown") or {}

                pe           = safe_float(safe_raw(sd, "trailingPE"))
                peg          = safe_float(safe_raw(ks, "pegRatio"))
                roe          = safe_pct(fd, "returnOnEquity")
                debt         = safe_float(safe_raw(fd, "debtToEquity"))
                price        = safe_float(safe_raw(sd, "regularMarketPrice")) or safe_float(safe_raw(fd, "currentPrice"))
                mcap_raw     = safe_float(safe_raw(sd, "marketCap"))
                mcap_cr      = round(mcap_raw / 1e7, 1) if mcap_raw else 0
                rev_growth   = safe_pct(fd, "revenueGrowth")
                profit_margin= safe_pct(fd, "profitMargins")
                promoter_pct = safe_pct(mh, "insidersPercentHeld")
                inst_pct     = safe_pct(mh, "institutionsPercentHeld")
                fii_pct      = inst_pct
                public_pct   = max(0.0, round(100 - promoter_pct - inst_pct, 1))

                # Apply filters — only filter if value exists (non-zero) or filter explicitly set
                if min_pe > 0      and pe    < min_pe:      return None
                if max_pe < 9999   and pe    > max_pe:      return None
                if max_peg < 9999  and peg   > max_peg:     return None
                if min_roe > -9999 and roe   < min_roe:     return None
                if max_debt < 9999 and debt  > max_debt:    return None
                if min_mcap > 0    and mcap_cr < min_mcap:  return None
                if max_mcap < 9999999999 and mcap_cr > max_mcap: return None
                if min_promoter > 0   and promoter_pct < min_promoter: return None
                if max_promoter < 100 and promoter_pct > max_promoter: return None
                if min_fii > 0    and fii_pct    < min_fii:    return None
                if max_fii < 100  and fii_pct    > max_fii:    return None
                if min_public > 0 and public_pct  < min_public: return None
                if max_public < 100 and public_pct > max_public: return None

                return {
                    "symbol":        sym,
                    "price":         round(price, 2)        if price    else None,
                    "pe":            round(pe, 1)           if pe       else None,
                    "peg":           round(peg, 2)          if peg      else None,
                    "roe":           round(roe, 1)          if roe      else None,
                    "debt_equity":   round(debt, 2)         if debt     else None,
                    "mcap_raw":      mcap_raw,
                    "mcap_cr":       mcap_cr,
                    "rev_growth":    round(rev_growth, 1),
                    "profit_margin": round(profit_margin, 1),
                    "promoter":      round(promoter_pct, 1),
                    "fii":           round(fii_pct, 1),
                    "public":        round(public_pct, 1),
                }
            except Exception:
                continue
        return None

    # Cap at 40 stocks, 5 workers to stay within Render timeout
    symbols_to_scan = symbols[:40]
    results = []
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(screen_one, sym): sym for sym in symbols_to_scan}
            for future in as_completed(futures, timeout=28):
                try:
                    r = future.result(timeout=4)
                    if r is not None:
                        results.append(r)
                except Exception:
                    pass
    except Exception:
        pass  # TimeoutError or any other error — return whatever we have so far
    results.sort(key=lambda x: (x.get("roe") or 0), reverse=True)
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
