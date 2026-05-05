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
from agent import agent_bp, run_stock_agent
from flask import Flask, jsonify, send_from_directory, request as freq
request = freq   # safety alias — allows both `freq.args` and `request.args` throughout
from flask_cors import CORS
import requests, re, os, time, json, threading, datetime, difflib, uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import lru_cache, wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── New modules (drop access_logger.py, search_engine.py, report_generator.py next to app.py) ──
try:
    from access_logger import register_logging, log_search, log_error as log_err
    _LOGGER_OK = True
except ImportError:
    _LOGGER_OK = False
    def log_search(*a, **k): pass
    def log_err(*a, **k): pass

try:
    from search_engine import global_search, warm_popular_cache, get_trending_stocks, resolve_symbol
    _SEARCH_OK = True
except ImportError:
    _SEARCH_OK = False

try:
    from report_generator import register_report_routes
    _REPORT_OK = True
except ImportError:
    _REPORT_OK = False

# static_url_path="" was removed: on Flask 2.3+ it causes the static blueprint to
# compete with @app.route("/") at exactly the root URL, resulting in a 200 empty body.
# All CSS/JS in this project is inline or CDN-hosted, so no local /static/ URLs are needed.
app = Flask(__name__, static_folder=None)
app.register_blueprint(agent_bp)

# ── Admin auth decorator ──────────────────────────────────────
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _ADMIN_TOKEN:
            return jsonify({"success": False, "error": "Admin not configured."}), 403
        token = freq.headers.get("X-Admin-Token", "").strip()
        if token != _ADMIN_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized."}), 401
        return f(*args, **kwargs)
    return decorated
CORS(app)

if _LOGGER_OK:
    register_logging(app)

if _SEARCH_OK:
    import threading as _t
    _t.Thread(target=warm_popular_cache, daemon=True).start()

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


# --------------------------------------------------------------
# User watchlist persistence
# --------------------------------------------------------------
# Render's free tier has an EPHEMERAL filesystem — anything written next
# to app.py is wiped on every redeploy/restart. To survive restarts:
#
#   1. Set env var WATCHLIST_PATH to a path on a Render persistent disk
#      (e.g. /var/data/user_watchlist.json), OR
#   2. Mount a disk at /var/data — auto-detected below, OR
#   3. Rely on the localStorage sync we added in index.html: the browser
#      re-uploads the user's saved symbols on page load via
#      /api/watchlist/sync, so the server-side cache repopulates itself
#      even when the JSON file is gone.
# --------------------------------------------------------------
def _resolve_watchlist_path():
    env_path = os.environ.get("WATCHLIST_PATH", "").strip()
    if env_path:
        try:
            os.makedirs(os.path.dirname(env_path) or ".", exist_ok=True)
        except Exception:
            pass
        return env_path
    # Render persistent disk default mount
    if os.path.isdir("/var/data"):
        return "/var/data/user_watchlist.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_watchlist.json")

WATCHLIST_FILE = _resolve_watchlist_path()
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
        tmp = WATCHLIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        os.replace(tmp, WATCHLIST_FILE)
    except Exception as e:
        try:
            print(f"[watchlist] save failed for {WATCHLIST_FILE}: {e}")
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
    """Yahoo Finance chart — supports NSE (.NS), BSE (.BO), and US stocks (no suffix)."""
    sym = sym.upper().strip()
    # Build candidate list: if already has suffix use as-is, else try NSE then US then BSE
    if sym.endswith(".NS") or sym.endswith(".BO"):
        candidates = [sym]
    else:
        candidates = [sym + ".NS", sym, sym + ".BO"]
    for ticker in candidates:
        for base in ["query1", "query2"]:
            try:
                url = (f"https://{base}.finance.yahoo.com/v8/finance/chart/"
                       f"{ticker}?interval={interval}&range={range_}")
                r = requests.get(url, headers=YFH, timeout=10)
                if r.ok:
                    result = r.json().get("chart", {}).get("result")
                    if result:
                        result[0]["_ticker_used"] = ticker
                        return result[0]
            except Exception:
                continue
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
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

@app.route("/")
def index():
    index_file = os.path.join(_STATIC_DIR, "index.html")
    if os.path.isfile(index_file):
        return send_from_directory(_STATIC_DIR, "index.html")
    return "<h1>Kevin Kataria Stock Intelligence</h1><p>static/index.html not found on server.</p>", 404

@app.route("/<path:filename>")
def serve_static(filename):
    """Serve any file from the static/ folder (JS, icons, etc.)."""
    file_path = os.path.join(_STATIC_DIR, filename)
    if os.path.isfile(file_path):
        return send_from_directory(_STATIC_DIR, filename)
    # For SPA deep-links fall back to index
    index_file = os.path.join(_STATIC_DIR, "index.html")
    if os.path.isfile(index_file):
        return send_from_directory(_STATIC_DIR, "index.html")
    return "Not found", 404

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
    # Try NSE first, then US (no suffix), then BSE
    if sym.endswith(".NS") or sym.endswith(".BO"):
        ticker_list = [sym]
    else:
        ticker_list = [sym + ".NS", sym, sym + ".BO"]
    for ticker in ticker_list:
        for base in ["query1", "query2"]:
            try:
                r = http_get(
                    f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d",
                    headers=YFH, timeout=8)
                if not r.ok: continue
                result = r.json().get("chart", {}).get("result")
                if not result: continue
                meta = result[0]["meta"]
                prev = meta.get("chartPreviousClose", 0)
                p    = meta.get("regularMarketPrice", 0)
                if not p: continue
                data = {
                    "success":    True,
                    "source":     "Yahoo Finance",
                    "symbol":     sym,
                    "ticker":     ticker,
                    "market":     "US" if not ticker.endswith((".NS",".BO")) else ("NSE" if ticker.endswith(".NS") else "BSE"),
                    "name":       meta.get("longName") or meta.get("shortName", sym),
                    "price":      round(p, 2),
                    "prev_close": round(prev, 2),
                    "change":     round(p - prev, 2),
                    "pct":        round((p - prev) / prev * 100, 2) if prev else 0,
                    "high":       meta.get("regularMarketDayHigh", 0),
                    "low":        meta.get("regularMarketDayLow", 0),
                    "week52_high": meta.get("fiftyTwoWeekHigh", 0),
                    "week52_low":  meta.get("fiftyTwoWeekLow", 0),
                    "volume":     meta.get("regularMarketVolume", 0),
                    "currency":   meta.get("currency", "INR"),
                }
                cache_set(f"price:{sym}", data, ttl=60)
                return jsonify(data)
            except Exception:
                continue
    return jsonify({
        "success": False,
        "error": f"Could not fetch price for '{sym}'. "
                 f"For NSE try: RELIANCE, TCS, HDFCBANK. "
                 f"For US try: AAPL, TSLA, PLTR, NVDA. "
                 f"For BSE try: RELIANCE.BO"
    })

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
def _yahoo_live_price(sym):
    """Cheap live-price probe (no crumb required)."""
    for base in ("query1", "query2"):
        for ticker in (f"{sym}.NS", sym, f"{sym}.BO"):
            try:
                pr = http_get(
                    f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d",
                    headers=YFH, timeout=5)
                if pr.ok:
                    meta = (pr.json().get("chart", {}).get("result") or [{}])[0].get("meta", {})
                    p = meta.get("regularMarketPrice")
                    if p: return p
            except Exception:
                continue
    return None

def _empty_fund_payload(sym, source="Unavailable", note=""):
    """Skeleton response so the Fundamentals tab always renders."""
    return {
        "success": True, "source": source, "partial": True, "note": note,
        "price": None,
        "mcap": "N/A", "pe": "N/A", "fwd_pe": "N/A", "peg": "N/A",
        "pb": "N/A", "eps": "N/A", "book_value": "N/A", "dividend": "N/A",
        "revenue": "N/A", "rev_growth": "N/A", "earnings_growth": "N/A",
        "profit_margin": "N/A", "operating_margin": "N/A",
        "roe": "N/A", "roce": "N/A", "roa": "N/A",
        "debt_equity": "N/A", "current_ratio": "N/A", "free_cashflow": "N/A",
        "cagr_5y": "N/A", "sales_cagr": "N/A", "profit_cagr": "N/A",
        "promoter": "N/A", "public": "N/A", "fii": "N/A", "dii": "N/A",
        "insider_holding": "N/A", "institution_holding": "N/A", "short_ratio": "N/A",
    }

@app.route("/api/fundamental/<symbol>")
def fundamental(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"fund:{sym}")
    if cached: return jsonify(cached)

    # ---------- Screener.in parsing (more resilient) ----------
    def parse_screener(html):
        # Strip script/style blocks first to avoid noisy matches
        clean = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
        clean = re.sub(r"<style[\s\S]*?</style>",  " ", clean, flags=re.I)

        def fv(label):
            patterns = [
                # Most common Screener.in markup: <li ...><span class="name">Label</span>... <span class="number">123 <span class="unit">%</span></span>
                r'<span[^>]*class="[^"]*name[^"]*"[^>]*>\s*' + re.escape(label) + r'\s*</span>[\s\S]{0,250}?<span[^>]*class="[^"]*(?:number|value)[^"]*"[^>]*>\s*([\-\d,\.]+)',
                # li followed by spans (no class assumption)
                r'<li[^>]*>\s*<span[^>]*>\s*' + re.escape(label) + r'\s*</span>[\s\S]{0,250}?<span[^>]*>\s*([\-\d,\.]+)',
                # generic name-span -> number-span pattern
                re.escape(label) + r'\s*</span>[\s\S]{0,200}?<span[^>]*>\s*([\-\d,\.]+)',
                # table cell pattern
                re.escape(label) + r'\s*</td>\s*<td[^>]*>\s*([\-\d,\.]+)',
                # last-resort proximity match
                re.escape(label) + r'[\s\S]{0,160}?([\-\d]+(?:[,\.]\d+)+)',
            ]
            for pat in patterns:
                try:
                    m = re.search(pat, clean, re.IGNORECASE)
                    if m:
                        val = m.group(1).replace(",", "").strip().rstrip(".")
                        if not val or val in ("-", "."): continue
                        try:
                            if float(val) == 0: continue
                        except ValueError:
                            continue
                        return val
                except re.error:
                    continue
            return "N/A"

        def fc(metric):
            # Compounded growth rows (5 Years column)
            for period in ["5 Years", "5 Yrs"]:
                m = re.search(metric + r'[\s\S]{0,800}?' + period + r'[\s\S]{0,80}?([\-\d.]+)\s*%',
                              clean, re.IGNORECASE)
                if m:
                    return m.group(1) + "%"
            return "N/A"

        def fh(label):
            # Shareholding section: "FII   4.32%"
            m = re.search(re.escape(label) + r'[^%<]{0,120}?([\-\d.]+)\s*%', clean, re.IGNORECASE)
            return (m.group(1) + "%") if m else "N/A"

        r = {
            "pe":   fv("Stock P/E"),       "mcap": fv("Market Cap"),
            "pb":   fv("Price to Book"),   "div":  fv("Dividend Yield"),
            "roe":  fv("Return on equity"),"roce": fv("ROCE"),
            "debt": fv("Debt to equity"),  "cr":   fv("Current ratio"),
            "eps":  fv("EPS"),             "bv":   fv("Book Value"),
            "sc5":  fc("Sales"),           "pc5":  fc("Profit"),
            "promoter": fh("Promoter"),    "public": fh("Public"),
            "fii": fh("FII"),              "dii": fh("DII"),
        }
        if all(v == "N/A" for v in r.values()):
            return None

        def pf(v): return (v + "%") if v != "N/A" else "N/A"

        live_price = _yahoo_live_price(sym)

        return {
            "success": True, "source": "Screener.in", "partial": False,
            "price": live_price,
            "mcap": (r["mcap"] + " Cr") if r["mcap"] != "N/A" else "N/A",
            "pe": r["pe"], "fwd_pe": "N/A", "peg": "N/A", "pb": r["pb"], "eps": r["eps"],
            "book_value": r["bv"], "dividend": pf(r["div"]),
            "revenue": "N/A", "rev_growth": "N/A", "earnings_growth": "N/A",
            "profit_margin": "N/A", "operating_margin": "N/A",
            "roe": pf(r["roe"]), "roce": pf(r["roce"]),
            "roa": "N/A", "debt_equity": r["debt"], "current_ratio": r["cr"],
            "free_cashflow": "N/A", "cagr_5y": r["sc5"],
            "sales_cagr": r["sc5"], "profit_cagr": r["pc5"],
            "promoter": r["promoter"], "public": r["public"],
            "fii": r["fii"], "dii": r["dii"],
            "insider_holding": r["promoter"], "institution_holding": "N/A",
            "short_ratio": "N/A",
        }

    for suffix in ["/consolidated/", "/"]:
        try:
            r = http_get(f"https://www.screener.in/company/{sym}{suffix}",
                         headers=SCH, timeout=12)
            if r.status_code == 200 and len(r.text) > 5000:
                result = parse_screener(r.text)
                if result:
                    cache_set(f"fund:{sym}", result, ttl=3600)
                    return jsonify(result)
        except Exception:
            continue

    # ---------- Yahoo v11 quoteSummary (best when not blocked) ----------
    for base in ["query1", "query2"]:
        try:
            url = (f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/"
                   f"{sym}.NS?modules=defaultKeyStatistics%2CfinancialData%2C"
                   f"summaryDetail%2CmajorHoldersBreakdown%2Cprice")
            r = http_get(url, headers=YFH, timeout=8)
            if not r.ok:
                continue
            payload = r.json().get("quoteSummary", {})
            if payload.get("error") or not payload.get("result"):
                continue
            res = payload["result"][0]
            fd = res.get("financialData", {})       or {}
            sd = res.get("summaryDetail", {})       or {}
            ks = res.get("defaultKeyStatistics", {}) or {}
            mh = res.get("majorHoldersBreakdown", {}) or {}
            pr = res.get("price", {})                or {}

            def fv2(d, k):
                v = d.get(k)
                if isinstance(v, dict):
                    return v.get("fmt", str(v.get("raw", "N/A"))) or "N/A"
                return str(v) if v not in (None, "") else "N/A"

            def pv(d, k):
                v = d.get(k)
                if isinstance(v, dict):
                    raw = v.get("raw")
                    if raw is not None:
                        return str(round(float(raw) * 100, 2)) + "%"
                return "N/A"

            yf_price = None
            try:
                yf_price = ((pr.get("regularMarketPrice") or {}).get("raw")
                            or (sd.get("regularMarketPrice") or {}).get("raw")
                            or (fd.get("currentPrice") or {}).get("raw"))
            except Exception:
                pass
            if not yf_price:
                yf_price = _yahoo_live_price(sym)

            data = {
                "success": True, "source": "Yahoo Finance", "partial": False,
                "price": yf_price,
                "mcap": fv2(sd, "marketCap"),
                "pe": fv2(sd, "trailingPE"), "fwd_pe": fv2(sd, "forwardPE"),
                "peg": fv2(ks, "pegRatio"), "pb": fv2(ks, "priceToBook"),
                "eps": fv2(ks, "trailingEps"), "book_value": fv2(ks, "bookValue"),
                "dividend": pv(sd, "dividendYield"),
                "revenue": fv2(fd, "totalRevenue"),
                "rev_growth": pv(fd, "revenueGrowth"),
                "earnings_growth": pv(fd, "earningsGrowth"),
                "profit_margin": pv(fd, "profitMargins"),
                "operating_margin": pv(fd, "operatingMargins"),
                "roe": pv(fd, "returnOnEquity"),
                "roce": "N/A", "roa": pv(fd, "returnOnAssets"),
                "debt_equity": fv2(fd, "debtToEquity"),
                "current_ratio": fv2(fd, "currentRatio"),
                "free_cashflow": fv2(fd, "freeCashflow"),
                "cagr_5y": "N/A", "sales_cagr": "N/A", "profit_cagr": "N/A",
                "promoter": pv(mh, "insidersPercentHeld"),
                "public": "N/A", "fii": "N/A", "dii": "N/A",
                "insider_holding": pv(mh, "insidersPercentHeld"),
                "institution_holding": pv(mh, "institutionsPercentHeld"),
                "short_ratio": fv2(ks, "shortRatio"),
            }
            # If literally everything is N/A (Yahoo answered but with empty modules),
            # don't cache as success — fall through to v7 quote.
            non_na = sum(1 for k, v in data.items()
                         if k not in ("success", "source", "partial", "price")
                         and v not in ("N/A", None, ""))
            if non_na >= 3:
                cache_set(f"fund:{sym}", data, ttl=3600)
                return jsonify(data)
        except Exception:
            continue

    # ---------- Yahoo v7 quote fallback (no crumb required) ----------
    for base in ["query1", "query2"]:
        for ticker in (f"{sym}.NS", sym, f"{sym}.BO"):
            try:
                url = f"https://{base}.finance.yahoo.com/v7/finance/quote?symbols={ticker}"
                r = http_get(url, headers=YFH, timeout=6)
                if not r.ok: continue
                arr = (r.json().get("quoteResponse") or {}).get("result") or []
                if not arr: continue
                q = arr[0]

                def gv(k):
                    v = q.get(k)
                    return str(v) if v not in (None, "") else "N/A"

                def gp(k):
                    v = q.get(k)
                    try:
                        return str(round(float(v) * 100, 2)) + "%" if v not in (None, "") else "N/A"
                    except Exception:
                        return "N/A"

                payload = _empty_fund_payload(sym, source="Yahoo Finance (lite)",
                    note="Detailed fundamentals temporarily unavailable; showing snapshot.")
                payload.update({
                    "partial": True,
                    "price": q.get("regularMarketPrice"),
                    "mcap":  gv("marketCap"),
                    "pe":    gv("trailingPE"),
                    "fwd_pe":gv("forwardPE"),
                    "eps":   gv("epsTrailingTwelveMonths"),
                    "book_value": gv("bookValue"),
                    "pb":    gv("priceToBook"),
                    "dividend": gp("dividendYield") if q.get("dividendYield") else "N/A",
                })
                cache_set(f"fund:{sym}", payload, ttl=1800)
                return jsonify(payload)
            except Exception:
                continue

    # ---------- Last resort: empty skeleton so the UI still renders ----------
    fallback = _empty_fund_payload(sym, source="Unavailable",
        note=f"Fundamental data sources didn't respond for {sym}. Showing live price only — try again in 30s.")
    fallback["price"] = _yahoo_live_price(sym)
    return jsonify(fallback)

# ============================================================
# NEWS - Google News RSS (always works)
# ============================================================
@app.route("/api/news/<symbol>")
def news(symbol):
    sym = symbol.upper().strip()
    refresh = str(freq.args.get("refresh", "")).lower() in ("1", "true", "yes")
    cache_key = f"news:{sym}"
    if not refresh:
        cached = cache_get(cache_key)
        if cached:
            return jsonify(cached)

    all_items = []
    seen = set()

    def parse_pubdate(raw):
        try:
            dt = parsedate_to_datetime(raw)
            if not dt:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except Exception:
            return None

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
                if not title:
                    continue
                norm_title = re.sub(r'\s+', ' ', title)
                if norm_title in seen:
                    continue
                seen.add(norm_title)
                dt = parse_pubdate(raw_date)
                out.append({
                    "title": norm_title,
                    "link": link,
                    "date": dt.strftime("%d %b %Y %H:%M UTC") if dt else raw_date[:25],
                    "published_at": dt.isoformat() if dt else "",
                    "source": source,
                    "query": q
                })
            return out
        except Exception:
            return []

    queries = [
        f'"{sym}" NSE stock latest today',
        f'"{sym}" share price today India',
        f'"{sym}" results OR order OR deal OR guidance OR target today'
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for items in pool.map(fetch, queries):
            all_items.extend(items)

    def sort_key(item):
        raw = item.get("published_at") or ""
        try:
            return datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except Exception:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    final = sorted(all_items, key=sort_key, reverse=True)
    data = {"success": True, "news": final[:12], "fetched_at": datetime.datetime.utcnow().isoformat() + "Z"}
    cache_set(cache_key, data, ttl=120)
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
def build_verdict_data(sym):
    sym = sym.upper().strip()
    cached = cache_get(f"verdict:{sym}")
    if cached:
        return cached

    def get_tech():
        with app.test_request_context(f"/api/technical/{sym}"):
            return technical(sym).get_json()

    def get_fund():
        with app.test_request_context(f"/api/fundamental/{sym}"):
            return fundamental(sym).get_json()

    def get_news_():
        with app.test_request_context(f"/api/news/{sym}"):
            return news(sym).get_json()

    def get_sent():
        with app.test_request_context(f"/api/sentiment/{sym}"):
            return sentiment(sym).get_json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        tf = pool.submit(get_tech)
        ff = pool.submit(get_fund)
        nf = pool.submit(get_news_)
        sf = pool.submit(get_sent)
        t = tf.result(timeout=20)
        f = ff.result(timeout=15)
        n = nf.result(timeout=12)
        s = sf.result(timeout=12)

    if not t.get("success"):
        return {"success": False, "error": "Technical data failed: " + t.get("error", "")}

    avg_bull = s.get("avg_bull", 50)
    news_txt = "\n".join([x["title"] for x in n.get("news", [])[:5]])
    prompt = (
        f"LIVE DATA FOR {sym} -- USE ONLY THESE NUMBERS:\n"
        f"Price:Rs.{t['price']} | MA20:Rs.{t['ma20']} | MA50:Rs.{t['ma50']} | MA200:Rs.{t.get('ma200','N/A')}\n"
        f"RSI:{t['rsi']} | MACD:{t['macd_hist']} | ADX:{t['adx']} | Stoch:{t['stoch_k']}%\n"
        f"Bollinger:Rs.{t['bb_lower']}-Rs.{t['bb_upper']} | SAR:Rs.{t['sar']}\n"
        f"Score:{t['bull_score']}% -> {t['signal']} | Support:Rs.{t['support']} | Resistance:Rs.{t['resistance']}\n"
        f"Stop:Rs.{t['stop_loss']} | T1:Rs.{t['target1']} | T2:Rs.{t['target2']}\n"
        f"PE:{f.get('pe','N/A')} | ROE:{f.get('roe','N/A')} | ROCE:{f.get('roce','N/A')} | Promoter:{f.get('promoter','N/A')}\n"
        f"Sentiment:{avg_bull}% Bullish\nHeadlines:\n{news_txt}\n\n"
        "VERDICT: [BUY/SELL/HOLD]\nCONFIDENCE: [X%]\nREASONING:\n- point with number\n- point\n- point\n- point\n"
        f"CURRENT PRICE: Rs.{t['price']}\nTARGET 3M: Rs.[]\nTARGET 12M: Rs.[]\nSTOP LOSS: Rs.{t['stop_loss']}\n"
        "RISK: [Low/Medium/High]\nBEST FOR: [Short-term/Long-term/Both]\nDISCLAIMER: Not financial advice."
    )
    resp = http_post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "llama-3.3-70b-versatile",
            "max_tokens": 800,
            "messages": [
                {"role": "system", "content": "Stock analyst. Use ONLY the numbers provided. Never use training memory for prices."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=25,
    )
    resp.raise_for_status()
    txt = resp.json()["choices"][0]["message"]["content"]

    def find(p):
        m = re.search(p, txt)
        return m.group(1).strip() if m else None

    data = {
        "success": True,
        "symbol": sym,
        "price": t["price"],
        "verdict": find(r"VERDICT:\s*(.+)") or t["signal"],
        "confidence": find(r"CONFIDENCE:\s*(.+)") or "N/A",
        "target_3m": find(r"TARGET 3M:\s*Rs\.([0-9,./]+)") or str(t["target1"]),
        "target_12m": find(r"TARGET 12M:\s*Rs\.([0-9,./]+)") or str(t["target2"]),
        "stop_loss": find(r"STOP LOSS:\s*Rs\.([0-9,./]+)") or str(t["stop_loss"]),
        "risk": find(r"RISK:\s*(.+)") or "Medium",
        "best_for": find(r"BEST FOR:\s*(.+)") or "Both",
        "reasoning": re.findall(r"- (.+)", txt)[:4],
        "full_text": txt,
        "tech": t,
        "fundamental": f,
        "sentiment": avg_bull,
    }
    cache_set(f"verdict:{sym}", data, ttl=300)
    return data

# ============================================================
# SOCIAL VIDEOS - YouTube (server-side) + IG/FB/X/TikTok deep-links
# ============================================================
# Free, no-API-key approach:
#  1. Try a list of public Invidious instances (privacy proxies that
#     return JSON for YouTube searches).
#  2. Fall back to scraping YouTube's HTML search page.
#  3. Always return deep-link URLs for Instagram, Facebook, X and
#     TikTok so the frontend can render "Browse on X" buttons even
#     when the YouTube fetch is empty.
# ============================================================
INVIDIOUS_INSTANCES = [
    "https://invidious.fdn.fr",
    "https://yewtu.be",
    "https://invidious.privacydev.net",
    "https://inv.nadeko.net",
    "https://invidious.protokolla.fi",
]

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.reallyaweso.me",
]

def _yt_thumb(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

def _format_duration_seconds(secs):
    try:
        s = int(secs)
    except Exception:
        return ""
    if s <= 0: return ""
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")

def _fetch_invidious(query, limit=8):
    """Try Invidious instances IN PARALLEL with short timeouts — max 6s total."""
    headers = {"User-Agent": UA, "Accept": "application/json"}

    def _try_instance(inst):
        try:
            url = f"{inst}/api/v1/search?q={requests.utils.quote(query)}&type=video&sort_by=relevance"
            r = requests.get(url, headers=headers, timeout=3)
            if not r.ok:
                return []
            arr = r.json() if r.text else []
            if not isinstance(arr, list):
                return []
            out = []
            for v in arr[:limit * 2]:
                if v.get("type") and v.get("type") != "video":
                    continue
                vid = v.get("videoId")
                if not vid:
                    continue
                length_s = int(v.get("lengthSeconds") or 0)
                thumb = None
                for t in (v.get("videoThumbnails") or []):
                    if (t.get("quality") or "").lower() in ("medium", "high", "default"):
                        thumb = t.get("url")
                        if thumb:
                            break
                out.append({
                    "video_id": vid,
                    "title": (v.get("title") or "").strip(),
                    "channel": (v.get("author") or "").strip(),
                    "published": v.get("publishedText") or "",
                    "views": int(v.get("viewCount") or 0),
                    "duration_seconds": length_s,
                    "duration": _format_duration_seconds(length_s),
                    "is_short": 0 < length_s <= 60,
                    "thumbnail": thumb or _yt_thumb(vid),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "shorts_url": f"https://www.youtube.com/shorts/{vid}",
                    "source": "Invidious",
                })
                if len(out) >= limit:
                    break
            return out
        except Exception:
            return []

    # Race all instances — return first non-empty result within 6s
    with ThreadPoolExecutor(max_workers=len(INVIDIOUS_INSTANCES)) as ex:
        futs = {ex.submit(_try_instance, inst): inst for inst in INVIDIOUS_INSTANCES}
        for fut in as_completed(futs, timeout=6):
            try:
                result = fut.result()
                if result:
                    return result
            except Exception:
                continue
    return []


def _fetch_piped(query, limit=12):
    """Try Piped.video API instances IN PARALLEL — returns first non-empty result within 6s."""
    headers = {"User-Agent": UA, "Accept": "application/json"}

    def _try_instance(inst):
        try:
            url = f"{inst}/search?q={requests.utils.quote(query)}&filter=videos"
            r = requests.get(url, headers=headers, timeout=3)
            if not r.ok:
                return []
            data = r.json() if r.text else {}
            items = data.get("items") or []
            if not isinstance(items, list):
                return []
            out = []
            for v in items:
                # Piped items have url like /watch?v=VIDEO_ID
                raw_url = v.get("url") or ""
                vid_match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", raw_url)
                if not vid_match:
                    # shorts format: /shorts/VIDEO_ID
                    vid_match = re.search(r"/shorts/([A-Za-z0-9_-]{11})", raw_url)
                if not vid_match:
                    continue
                vid = vid_match.group(1)
                length_s = int(v.get("duration") or 0)
                thumb = v.get("thumbnail") or _yt_thumb(vid)
                # Piped returns relative thumbnail URLs sometimes
                if thumb and thumb.startswith("/"):
                    thumb = _yt_thumb(vid)
                out.append({
                    "video_id":        vid,
                    "title":           (v.get("title") or "").strip(),
                    "channel":         (v.get("uploaderName") or v.get("uploader") or "").strip(),
                    "published":       v.get("uploadedDate") or v.get("uploaded") or "",
                    "views":           int(v.get("views") or 0),
                    "duration_seconds": length_s,
                    "duration":        _format_duration_seconds(length_s),
                    "is_short":        0 < length_s <= 60,
                    "thumbnail":       thumb,
                    "url":             f"https://www.youtube.com/watch?v={vid}",
                    "shorts_url":      f"https://www.youtube.com/shorts/{vid}",
                    "source":          "Piped",
                })
                if len(out) >= limit:
                    break
            return out
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=len(PIPED_INSTANCES)) as ex:
        futs = {ex.submit(_try_instance, inst): inst for inst in PIPED_INSTANCES}
        for fut in as_completed(futs, timeout=6):
            try:
                result = fut.result()
                if result:
                    return result
            except Exception:
                continue
    return []

def _fetch_youtube_scrape(query, limit=12):
    """Last-resort: scrape YouTube's search page for ytInitialData."""
    out = []
    try:
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        r = http_get(url, headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,*/*",
        }, timeout=5)
        if not r.ok or len(r.text) < 5000:
            return out
        # The search results live inside `var ytInitialData = {...};`
        m = re.search(r"var ytInitialData\s*=\s*({.+?});\s*</script>", r.text, re.DOTALL)
        if not m:
            m = re.search(r"ytInitialData\"\]\s*=\s*({.+?});", r.text, re.DOTALL)
        if not m:
            return out
        data = json.loads(m.group(1))
        # Walk the nested structure looking for videoRenderer entries
        def walk(node):
            if isinstance(node, dict):
                if "videoRenderer" in node:
                    yield node["videoRenderer"]
                for v in node.values():
                    yield from walk(v)
            elif isinstance(node, list):
                for v in node:
                    yield from walk(v)
        for vr in walk(data):
            try:
                vid = vr.get("videoId")
                if not vid: continue
                title = ""
                t_runs = (vr.get("title") or {}).get("runs") or []
                if t_runs: title = t_runs[0].get("text", "")
                channel = ""
                c_runs = ((vr.get("ownerText") or {}).get("runs") or
                          (vr.get("longBylineText") or {}).get("runs") or [])
                if c_runs: channel = c_runs[0].get("text", "")
                published = ((vr.get("publishedTimeText") or {}).get("simpleText") or "")
                views_text = ((vr.get("viewCountText") or {}).get("simpleText") or "")
                views_num = int(re.sub(r"\D", "", views_text) or 0)
                # duration
                dur_text = (((vr.get("lengthText") or {}).get("simpleText")) or "")
                length_s = 0
                if dur_text:
                    parts = [int(p) for p in dur_text.split(":") if p.isdigit()]
                    if len(parts) == 3:   length_s = parts[0]*3600 + parts[1]*60 + parts[2]
                    elif len(parts) == 2: length_s = parts[0]*60 + parts[1]
                    elif len(parts) == 1: length_s = parts[0]
                # thumbnail
                thumbs = ((vr.get("thumbnail") or {}).get("thumbnails") or [])
                thumb = thumbs[-1]["url"] if thumbs else _yt_thumb(vid)
                out.append({
                    "video_id": vid,
                    "title": title.strip(),
                    "channel": channel.strip(),
                    "published": published,
                    "views": views_num,
                    "duration_seconds": length_s,
                    "duration": dur_text or _format_duration_seconds(length_s),
                    "is_short": 0 < length_s <= 60,
                    "thumbnail": thumb,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "shorts_url": f"https://www.youtube.com/shorts/{vid}",
                    "source": "YouTube",
                })
                if len(out) >= limit:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return out

def _build_social_links(symbol, query):
    """Deep-links into platforms that block server-side scraping."""
    q  = requests.utils.quote(query)
    sy = requests.utils.quote(symbol)
    return [
        {
            "platform": "Instagram", "key": "instagram",
            "tag": f"https://www.instagram.com/explore/tags/{sy.lower()}/",
            "search": f"https://www.instagram.com/explore/search/keyword/?q={q}",
            "reels": f"https://www.instagram.com/explore/tags/{sy.lower()}/?type=reels",
            "note": "Reels & posts. Requires Instagram login.",
        },
        {
            "platform": "Facebook", "key": "facebook",
            "search": f"https://www.facebook.com/search/posts/?q={q}",
            "reels":  f"https://www.facebook.com/reel/?q={q}",
            "videos": f"https://www.facebook.com/search/videos/?q={q}",
            "note": "Posts, reels & videos. Requires Facebook login.",
        },
        {
            "platform": "X / Twitter", "key": "twitter",
            "search":   f"https://twitter.com/search?q=%24{sy}%20OR%20{q}&src=typed_query&f=live",
            "cashtag":  f"https://twitter.com/search?q=%24{sy}&src=typed_query&f=live",
            "videos":   f"https://twitter.com/search?q={q}&src=typed_query&f=video",
            "note": "Live cashtag stream. Free without login (basic).",
        },
        {
            "platform": "TikTok", "key": "tiktok",
            "search": f"https://www.tiktok.com/search?q={q}",
            "tag":    f"https://www.tiktok.com/tag/{sy.lower()}",
            "videos": f"https://www.tiktok.com/search/video?q={q}",
            "note": "Search & hashtag pages. Browser-only viewing on free tier.",
        },
    ]

@app.route("/api/social-videos/<symbol>")
def social_videos(symbol):
    sym = symbol.upper().strip()
    refresh = str(request.args.get("refresh", "")).lower() in ("1", "true", "yes")
    cache_key = f"social:{sym}"
    if not refresh:
        cached = cache_get(cache_key)
        if cached:
            return jsonify(cached)

    # Build a focused query — adding "stock" filters out unrelated music/people
    base_q = f"{sym} stock NSE India"

    # Always build platform links first so we can return them even if YouTube fails
    platforms = _build_social_links(sym, base_q)

    videos = []
    youtube_source = "none"
    yt_error = None
    try:
        # Race all three fetchers in parallel — fastest non-empty result wins
        def _run_invidious(): return _fetch_invidious(base_q, limit=12)
        def _run_piped():     return _fetch_piped(base_q, limit=12)
        def _run_scrape():    return _fetch_youtube_scrape(base_q, limit=12)

        with ThreadPoolExecutor(max_workers=3) as ex:
            fut_inv   = ex.submit(_run_invidious)
            fut_piped = ex.submit(_run_piped)
            fut_scr   = ex.submit(_run_scrape)
            for fut in as_completed([fut_inv, fut_piped, fut_scr], timeout=12):
                try:
                    result = fut.result()
                    if result:
                        videos = result
                        break
                except Exception:
                    continue

        if videos:
            youtube_source = videos[0].get("source", "unknown")
    except Exception as exc:
        yt_error = str(exc)

    # Split shorts vs regular for the UI
    shorts  = [v for v in videos if v.get("is_short")]
    regular = [v for v in videos if not v.get("is_short")]

    payload = {
        "success": True,
        "symbol": sym,
        "query": base_q,
        "videos": regular[:8],
        "shorts": shorts[:8],
        "video_count": len(videos),
        "platforms": platforms,
        "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
        "youtube_source": youtube_source,
        "note": ("Instagram, Facebook, X and TikTok are link-outs because "
                 "they require platform login or paid APIs to fetch programmatically."),
    }
    if yt_error:
        payload["youtube_error"] = yt_error
    cache_set(cache_key, payload, ttl=1800)  # 30 min
    return jsonify(payload)

@app.route("/api/verdict/<symbol>")
def verdict(symbol):
    sym = symbol.upper().strip()
    try:
        return jsonify(build_verdict_data(sym))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

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

@app.route("/api/watchlist/sync", methods=["POST"])
def sync_watchlist():
    """
    Browser-side restore endpoint. The frontend caches the user's
    additions in localStorage and POSTs them here on page load so that
    server-side state survives Render restarts/redeploys (which wipe
    the filesystem on the free tier).

    Body: {"items": [{"symbol":"INFY","name":"Infosys","ref_price":1500}, ...]}
    """
    data = freq.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        return jsonify({"success": False, "error": "items must be a list"}), 400

    added = 0
    with _watchlist_lock:
        existing = {str(x.get("symbol", "")).upper().strip() for x in WATCHLIST}
        existing.update(str(x.get("symbol", "")).upper().strip() for x in USER_WATCHLIST)
        for raw in items:
            if not isinstance(raw, dict): continue
            sym = str(raw.get("symbol", "")).upper().strip()
            if not sym or sym in existing: continue
            USER_WATCHLIST.append({
                "name": str(raw.get("name", sym)).strip() or sym,
                "symbol": sym,
                "ref_price": float(raw.get("ref_price", 0) or 0),
                "promoter": float(raw.get("promoter", 0) or 0),
            })
            existing.add(sym)
            added += 1
        if added:
            _save_user_watchlist(USER_WATCHLIST)

    if added:
        clear_cache_key("watchlist")
    return jsonify({"success": True, "restored": added,
                    "total": len(USER_WATCHLIST), "path": WATCHLIST_FILE})

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

    try:
        verdict_data = build_verdict_data(sym)
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

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
    whatsapp_app_link = f"whatsapp://send?phone={mobile}&text={encoded}" if mobile else f"whatsapp://send?text={encoded}"
    whatsapp_web_link = f"https://wa.me/{mobile}?text={encoded}" if mobile else f"https://wa.me/?text={encoded}"

    return jsonify({
        "success":True,
        "symbol":sym,
        "mobile":mobile,
        "channel":channel,
        "share_text":share_text,
        "sms_link":sms_link,
        "whatsapp_link":whatsapp_web_link,
        "whatsapp_app_link":whatsapp_app_link,
        "whatsapp_web_link":whatsapp_web_link,
        "note":"This prepares the AI verdict message for WhatsApp or SMS and opens the correct handoff link on the user's device or browser."
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
    raw_q  = (freq.args.get("q") or "").strip()
    try:    limit = int(freq.args.get("limit", 12) or 12)
    except: limit = 12
    limit  = min(max(limit, 1), 30)
    enrich = str(freq.args.get("enrich", "1")).lower() in ("1","true","yes")

    if not raw_q:
        return jsonify({"success": True, "query": "", "count": 0, "results": []})

    ip = (freq.headers.get("X-Forwarded-For") or freq.remote_addr or "").split(",")[0].strip()

    # Use global Yahoo Finance search if available, else fall back to old system
    if _SEARCH_OK:
        try:
            results = global_search(raw_q, limit=limit, enrich=enrich)
        except Exception as e:
            log_err(ip, "/api/search", 500, str(e))
            results = []
        log_search(ip, raw_q, len(results), [r.get("symbol","") for r in results])
        if not results:
            # suggest via shorter query
            suggestions = []
            try:
                if len(raw_q) >= 3:
                    suggestions = global_search(raw_q[:3], limit=4, enrich=False)
            except Exception:
                pass
            return jsonify({
                "success":     True,
                "query":       raw_q,
                "count":       0,
                "results":     [],
                "suggestions": [{"symbol": r["symbol"], "name": r.get("name","")} for r in suggestions],
                "hint": "Try: NSE symbol (RELIANCE), US ticker (AAPL, TSLA, PLTR), or company name"
            })
        return jsonify({"success": True, "query": raw_q, "count": len(results), "results": results})

    # Legacy fallback (original hardcoded search)
    try:
        results = _rank_search_results(raw_q, limit=limit)
    except Exception as e:
        return jsonify({"success": False, "query": raw_q, "count": 0, "results": [],
                        "error": f"Search failed: {str(e)[:120]}"}), 200
    if not results:
        return jsonify({"success": True, "query": raw_q, "count": 0, "results": [],
                        "message": "No exact match. Try NSE symbol or company name."})
    if enrich:
        def fetch_change(item):
            s = item["symbol"]
            cached = cache_get(f"price:{s}")
            if cached:
                item["price"] = cached.get("price"); item["change_pct"] = cached.get("pct"); return item
            try:
                r = http_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s}.NS?interval=1d&range=2d",
                             headers=YFH, timeout=4)
                if r.ok:
                    meta = r.json()["chart"]["result"][0]["meta"]
                    p = meta.get("regularMarketPrice",0); prev = meta.get("chartPreviousClose",0)
                    item["price"] = round(p,2) if p else None
                    item["change_pct"] = round((p-prev)/prev*100,2) if prev else 0
                else: item["price"] = None; item["change_pct"] = None
            except: item["price"] = None; item["change_pct"] = None
            return item
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(fetch_change, results[:8])) + results[8:]
    return jsonify({"success": True, "query": raw_q, "count": len(results), "results": results})


@app.route("/api/screener")
def screener():
    """
    Render-safe screener that always returns JSON quickly.

    Design goals:
      - never let the request hang for ~1 minute
      - prefer partial results over timeout / HTML gateway errors
      - use Yahoo first, then optional Screener.in enrichment for a short shortlist
    """
    try:
        req_started = time.time()

        def fp(k, default):
            v = freq.args.get(k, "")
            try:
                return float(v) if str(v).strip() else default
            except Exception:
                return default

        def to_num(v):
            try:
                if v is None:
                    return None
                if isinstance(v, str):
                    s = v.strip().replace(",", "").replace("%", "").replace("Cr", "").replace("cr", "")
                    if s in ("", "N/A", "NA", "--", "-", "Nil", "nil", "None"):
                        return None
                    return float(s)
                return float(v)
            except Exception:
                return None

        def first_not_none(*values):
            for v in values:
                if v is not None:
                    return v
            return None

        def value_passes(val, min_val=None, max_val=None):
            if val is None:
                return True
            if min_val is not None and val < min_val:
                return False
            if max_val is not None and val > max_val:
                return False
            return True

        def extract_screener_num(html, label):
            patterns = [
                r'<li[^>]*>\s*<span[^>]*>\s*' + re.escape(label) + r'\s*</span>\s*<span[^>]*>\s*([^<]+)',
                re.escape(label) + r'[^<]{0,140}</span>\s*<span[^>]*>\s*([^<]+)',
                re.escape(label) + r'[^<]*</td>\s*<td[^>]*>\s*([^<]+)',
                re.escape(label) + r'[^:]{0,20}:\s*([0-9,.\-% ]+)',
            ]
            for pat in patterns:
                try:
                    m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
                    if m:
                        raw = re.sub(r"\s+", " ", m.group(1)).strip()
                        num = to_num(raw)
                        if num is not None:
                            return num
                except Exception:
                    pass
            return None

        def parse_screener_metrics(sym):
            cache_key = f"screen_sc:{sym}"
            cached = cache_get(cache_key)
            if cached is not None:
                return cached

            for suffix in ["/consolidated/", "/"]:
                try:
                    r = http_get(f"https://www.screener.in/company/{sym}{suffix}", headers=SCH, timeout=5)
                    if r.status_code != 200 or len(r.text) < 3000:
                        continue
                    html = r.text
                    data = {
                        "pe": extract_screener_num(html, "Stock P/E"),
                        "roe": extract_screener_num(html, "Return on equity"),
                        "roce": extract_screener_num(html, "ROCE"),
                        "debt_equity": extract_screener_num(html, "Debt to equity"),
                        "mcap_cr": extract_screener_num(html, "Market Cap"),
                        "promoter": extract_screener_num(html, "Promoter"),
                        "public": extract_screener_num(html, "Public"),
                        "fii": extract_screener_num(html, "FII"),
                        "dii": extract_screener_num(html, "DII"),
                        "pb": extract_screener_num(html, "Price to Book"),
                        "dividend": extract_screener_num(html, "Dividend Yield"),
                        "eps": extract_screener_num(html, "EPS in Rs"),
                    }
                    if any(v is not None for v in data.values()):
                        cache_set(cache_key, data, ttl=1800)
                        return data
                except Exception:
                    continue

            cache_set(cache_key, {}, ttl=600)
            return {}

        def parse_yahoo_metrics(sym):
            cache_key = f"screen_yh:{sym}"
            cached = cache_get(cache_key)
            if cached is not None:
                return cached

            def safe_raw(d, k):
                try:
                    v = d.get(k)
                    if isinstance(v, dict):
                        return v.get("raw")
                    return v
                except Exception:
                    return None

            def safe_pct(d, k):
                raw = safe_raw(d, k)
                if raw is None:
                    return None
                try:
                    return round(float(raw) * 100, 2)
                except Exception:
                    return None

            for base in ["query1", "query2"]:
                try:
                    url = (
                        f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/{sym}.NS"
                        f"?modules=defaultKeyStatistics%2CfinancialData%2CsummaryDetail%2CmajorHoldersBreakdown"
                    )
                    r = http_get(url, headers=YFH, timeout=4)
                    if not r.ok:
                        continue
                    payload = r.json()
                    result_list = payload.get("quoteSummary", {}).get("result") or []
                    if not result_list:
                        continue
                    res = result_list[0]
                    fd = res.get("financialData", {}) or {}
                    sd = res.get("summaryDetail", {}) or {}
                    ks = res.get("defaultKeyStatistics", {}) or {}
                    mh = res.get("majorHoldersBreakdown", {}) or {}

                    price = first_not_none(to_num(safe_raw(sd, "regularMarketPrice")), to_num(safe_raw(fd, "currentPrice")))
                    mcap_raw = to_num(safe_raw(sd, "marketCap"))
                    inst_pct = safe_pct(mh, "institutionsPercentHeld")
                    insider_pct = safe_pct(mh, "insidersPercentHeld")

                    data = {
                        "price": price,
                        "pe": to_num(safe_raw(sd, "trailingPE")),
                        "peg": to_num(safe_raw(ks, "pegRatio")),
                        "roe": safe_pct(fd, "returnOnEquity"),
                        "debt_equity": to_num(safe_raw(fd, "debtToEquity")),
                        "mcap_raw": mcap_raw,
                        "mcap_cr": round(mcap_raw / 1e7, 1) if mcap_raw is not None else None,
                        "rev_growth": safe_pct(fd, "revenueGrowth"),
                        "profit_margin": safe_pct(fd, "profitMargins"),
                        "promoter": insider_pct,
                        "fii": inst_pct,
                        "dii": inst_pct,
                        "public": (
                            round(max(0.0, 100 - (insider_pct or 0) - (inst_pct or 0)), 1)
                            if (insider_pct is not None or inst_pct is not None) else None
                        ),
                    }
                    cache_set(cache_key, data, ttl=900)
                    return data
                except Exception:
                    continue

            cache_set(cache_key, {}, ttl=300)
            return {}

        max_pe = fp("max_pe", 9999)
        min_pe = fp("min_pe", 0)
        max_peg = fp("max_peg", 9999)
        min_roe = fp("min_roe", -9999)
        max_debt = fp("max_debt", 9999)
        min_mcap = fp("min_mcap", 0)
        max_mcap = fp("max_mcap", 9999999999)
        min_promoter = fp("min_promoter", 0)
        max_promoter = fp("max_promoter", 100)
        min_fii = fp("min_fii", 0)
        max_fii = fp("max_fii", 100)
        min_dii = fp("min_dii", 0)
        max_dii = fp("max_dii", 100)
        min_public = fp("min_public", 0)
        max_public = fp("max_public", 100)

        raw_symbols = [s.strip().upper() for s in freq.args.get("symbols", ",".join(SCREEN_STOCKS)).split(",") if s.strip()]
        symbols = list(dict.fromkeys(raw_symbols)) or SCREEN_STOCKS

        filters_payload = {
            "symbols": symbols,
            "min_pe": min_pe, "max_pe": max_pe, "max_peg": max_peg, "min_roe": min_roe,
            "max_debt": max_debt, "min_mcap": min_mcap, "max_mcap": max_mcap,
            "min_promoter": min_promoter, "max_promoter": max_promoter,
            "min_fii": min_fii, "max_fii": max_fii,
            "min_dii": min_dii, "max_dii": max_dii,
            "min_public": min_public, "max_public": max_public
        }
        cache_key = "screener:" + json.dumps(filters_payload, sort_keys=True)
        cached = cache_get(cache_key)
        if cached:
            return jsonify(cached)

        holdings_filters_active = any([
            min_promoter > 0, max_promoter < 100,
            min_fii > 0, max_fii < 100,
            min_dii > 0, max_dii < 100,
            min_public > 0, max_public < 100
        ])

        # Hard cap for cloud safety.
        max_scan = min(len(symbols), 40 if holdings_filters_active else 60)
        symbols_to_scan = symbols[:max_scan]

        stage1_rows = []
        results = []
        errors = 0

        def yahoo_pass(sym):
            yh = parse_yahoo_metrics(sym)
            if not yh:
                return None

            pe = yh.get("pe")
            peg = yh.get("peg")
            roe = yh.get("roe")
            debt = yh.get("debt_equity")
            mcap_cr = yh.get("mcap_cr")
            promoter = yh.get("promoter")
            fii = yh.get("fii")
            dii = yh.get("dii")
            public = yh.get("public")

            if not value_passes(pe, min_pe if min_pe > 0 else None, max_pe if max_pe < 9999 else None):
                return None
            if not value_passes(peg, None, max_peg if max_peg < 9999 else None):
                return None
            if not value_passes(roe, min_roe if min_roe > -9999 else None, None):
                return None
            if not value_passes(debt, None, max_debt if max_debt < 9999 else None):
                return None
            if not value_passes(mcap_cr, min_mcap if min_mcap > 0 else None, max_mcap if max_mcap < 9999999999 else None):
                return None

            if not holdings_filters_active:
                if not value_passes(promoter, min_promoter if min_promoter > 0 else None, max_promoter if max_promoter < 100 else None):
                    return None
                if not value_passes(fii, min_fii if min_fii > 0 else None, max_fii if max_fii < 100 else None):
                    return None
                if not value_passes(dii, min_dii if min_dii > 0 else None, max_dii if max_dii < 100 else None):
                    return None
                if not value_passes(public, min_public if min_public > 0 else None, max_public if max_public < 100 else None):
                    return None

            return {"symbol": sym, "yh": yh}

        # Stage 1: fast Yahoo pass
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(yahoo_pass, sym): sym for sym in symbols_to_scan}
                for future in as_completed(futures, timeout=12):
                    if time.time() - req_started > 12:
                        break
                    try:
                        row = future.result(timeout=0.5)
                        if row is not None:
                            stage1_rows.append(row)
                    except Exception:
                        errors += 1
        except Exception:
            pass

        # Stage 2: enrich only a very small shortlist
        candidate_rows = stage1_rows[:15] if holdings_filters_active else stage1_rows[:30]

        def finalize_row(stage1):
            sym = stage1["symbol"]
            yh = stage1.get("yh", {}) or {}
            need_sc = holdings_filters_active or yh.get("pe") is None or yh.get("roe") is None or yh.get("debt_equity") is None
            sc = parse_screener_metrics(sym) if need_sc else {}

            pe = first_not_none(sc.get("pe"), yh.get("pe"))
            peg = first_not_none(yh.get("peg"), sc.get("peg"))
            roe = first_not_none(sc.get("roe"), yh.get("roe"))
            debt = first_not_none(sc.get("debt_equity"), yh.get("debt_equity"))
            mcap_cr = first_not_none(sc.get("mcap_cr"), yh.get("mcap_cr"))
            mcap_raw = yh.get("mcap_raw")
            price = yh.get("price")
            promoter = first_not_none(sc.get("promoter"), yh.get("promoter"))
            fii = first_not_none(sc.get("fii"), yh.get("fii"))
            dii = first_not_none(sc.get("dii"), yh.get("dii"))
            public = first_not_none(sc.get("public"), yh.get("public"))

            if not value_passes(pe, min_pe if min_pe > 0 else None, max_pe if max_pe < 9999 else None):
                return None
            if not value_passes(peg, None, max_peg if max_peg < 9999 else None):
                return None
            if not value_passes(roe, min_roe if min_roe > -9999 else None, None):
                return None
            if not value_passes(debt, None, max_debt if max_debt < 9999 else None):
                return None
            if not value_passes(mcap_cr, min_mcap if min_mcap > 0 else None, max_mcap if max_mcap < 9999999999 else None):
                return None
            if not value_passes(promoter, min_promoter if min_promoter > 0 else None, max_promoter if max_promoter < 100 else None):
                return None
            if not value_passes(fii, min_fii if min_fii > 0 else None, max_fii if max_fii < 100 else None):
                return None
            if not value_passes(dii, min_dii if min_dii > 0 else None, max_dii if max_dii < 100 else None):
                return None
            if not value_passes(public, min_public if min_public > 0 else None, max_public if max_public < 100 else None):
                return None

            return {
                "symbol": sym,
                "price": round(price, 2) if price is not None else None,
                "pe": round(pe, 1) if pe is not None else None,
                "peg": round(peg, 2) if peg is not None else None,
                "roe": round(roe, 1) if roe is not None else None,
                "debt_equity": round(debt, 2) if debt is not None else None,
                "mcap_raw": mcap_raw,
                "mcap_cr": round(mcap_cr, 1) if mcap_cr is not None else None,
                "promoter": round(promoter, 1) if promoter is not None else None,
                "fii": round(fii, 1) if fii is not None else None,
                "dii": round(dii, 1) if dii is not None else None,
                "public": round(public, 1) if public is not None else None,
                "source": {
                    "fundamentals": "Screener.in + Yahoo Finance" if sc else "Yahoo Finance",
                    "price": "Yahoo Finance"
                }
            }

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(finalize_row, row): row["symbol"] for row in candidate_rows}
                for future in as_completed(futures, timeout=8):
                    if time.time() - req_started > 18:
                        break
                    try:
                        row = future.result(timeout=0.5)
                        if row is not None:
                            results.append(row)
                    except Exception:
                        errors += 1
        except Exception:
            pass

        results.sort(key=lambda x: ((x.get("pe") if x.get("pe") is not None else 999999), -(x.get("roe") if x.get("roe") is not None else -9999)))

        response = {
            "success": True,
            "stocks": results[:25],
            "total": len(results[:25]),
            "scanned": len(stage1_rows),
            "universe": len(symbols_to_scan),
            "candidates": len(candidate_rows),
            "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
            "note": "Render-safe screener returned best available latest data without waiting for slow endpoints.",
            "errors": errors,
            "elapsed_sec": round(time.time() - req_started, 2)
        }
        cache_set(cache_key, response, ttl=180)
        return jsonify(response)

    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Screener failed safely: {str(e)[:160]}",
            "stocks": [],
            "total": 0
        }), 200


# ============================================================
# PORTFOLIO & ALERTS (file-based persistence)
# ============================================================
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
ALERTS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.json")
LEADS_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "membership_leads.json")
MEMBERS_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memberships.json")

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize_plan(plan):
    plan = str(plan or "starter").strip().lower()
    aliases = {"free":"starter", "pro":"pro", "partner":"partner"}
    return aliases.get(plan, plan if plan in {"starter","pro","partner"} else "starter")

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
    holdings = load_json(PORTFOLIO_FILE)

    def fetch_price_h(h):
        sym  = h.get("symbol","").upper().strip()
        qty  = float(h.get("qty", 0))
        buy  = float(h.get("buy_price", 0))
        invested = round(qty * buy, 2)

        # Use the robust helper that tries .NS, bare, .BO across query1+query2
        live = _yahoo_live_price(sym)

        # Also grab today's change % from the chart meta
        day_change_pct = None
        if live:
            try:
                for base in ("query1", "query2"):
                    for ticker in (f"{sym}.NS", sym, f"{sym}.BO"):
                        try:
                            r = http_get(
                                f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d",
                                headers=YFH, timeout=5)
                            if r.ok:
                                meta = (r.json().get("chart",{}).get("result") or [{}])[0].get("meta",{})
                                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                                cur  = meta.get("regularMarketPrice")
                                if prev and cur and prev > 0:
                                    day_change_pct = round(((cur - prev) / prev) * 100, 2)
                                break
                        except Exception:
                            continue
                    if day_change_pct is not None:
                        break
            except Exception:
                pass

        if live:
            current = round(qty * live, 2)
            pnl     = round(current - invested, 2)
            pnl_pct = round((pnl / invested) * 100, 2) if invested else 0
            return {**h,
                    "live_price":   round(live, 2),
                    "invested":     invested,
                    "current":      current,
                    "pnl":          pnl,
                    "pnl_pct":      pnl_pct,
                    "day_change_pct": day_change_pct,
                    "price_ok":     True}
        else:
            return {**h,
                    "live_price":   None,
                    "invested":     invested,
                    "current":      0,
                    "pnl":          -invested,
                    "pnl_pct":      -100,
                    "day_change_pct": None,
                    "price_ok":     False}

    with ThreadPoolExecutor(max_workers=8) as pool:
        enriched = list(pool.map(fetch_price_h, holdings))

    ti = sum(h.get("invested", 0) or 0 for h in enriched)
    tc = sum(h.get("current",  0) or 0 for h in enriched)
    tp = round(tc - ti, 2)
    return jsonify({
        "success":         True,
        "holdings":        enriched,
        "total_invested":  round(ti, 2),
        "total_current":   round(tc, 2),
        "total_pnl":       tp,
        "total_pnl_pct":   round((tp / ti) * 100, 2) if ti else 0,
        "fetched_at":      datetime.datetime.utcnow().isoformat() + "Z",
    })

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
# MONETIZATION / MEMBERSHIP LEADS
# ============================================================
@app.route("/api/monetization/interest", methods=["POST"])
def monetization_interest():
    try:
        data = freq.get_json() or {}
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        phone = str(data.get("phone", "")).strip()
        plan = normalize_plan(data.get("plan"))
        notes = str(data.get("notes", "")).strip()
        source = str(data.get("source", "website")).strip() or "website"
        if not name or not email:
            return jsonify({"success": False, "error": "Name and email are required."}), 400
        leads = load_json(LEADS_FILE)
        existing = None
        for lead in leads:
            if str(lead.get("email", "")).lower() == email and normalize_plan(lead.get("plan")) == plan:
                existing = lead
                break
        if existing:
            existing["name"] = name
            existing["phone"] = phone
            existing["notes"] = notes
            existing["source"] = source
            existing["updated_at"] = now_iso()
            existing["status"] = existing.get("status") or "interested"
            lead_id = existing["id"]
            message = f"Updated existing {plan.title()} interest."
        else:
            lead_id = "lead_" + uuid.uuid4().hex[:10]
            leads.insert(0, {
                "id": lead_id,
                "name": name,
                "email": email,
                "phone": phone,
                "plan": plan,
                "notes": notes,
                "source": source,
                "status": "interested",
                "created_at": now_iso(),
                "updated_at": now_iso()
            })
            message = f"Interest saved for {plan.title()}."
        save_json(LEADS_FILE, leads)
        return jsonify({"success": True, "message": message, "lead_id": lead_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/monetization/summary", methods=["GET"])
def monetization_summary():
    leads = load_json(LEADS_FILE)
    members = load_json(MEMBERS_FILE)
    def count_plan(items, plan):
        return sum(1 for x in items if normalize_plan(x.get("plan")) == plan)
    active_members = [m for m in members if str(m.get("status", "")).lower() == "active"]
    recent_leads = sorted(leads, key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)[:12]
    return jsonify({
        "success": True,
        "summary": {
            "total_leads": len(leads),
            "starter_interest": count_plan(leads, "starter"),
            "pro_interest": count_plan(leads, "pro"),
            "partner_interest": count_plan(leads, "partner"),
            "active_members": len(active_members),
            "paid_pro_members": sum(1 for m in active_members if normalize_plan(m.get("plan")) == "pro")
        },
        "recent_leads": recent_leads,
        "members": sorted(members, key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)[:12]
    })

@app.route("/api/monetization/memberships", methods=["GET"])
def monetization_memberships():
    leads = sorted(load_json(LEADS_FILE), key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    members = sorted(load_json(MEMBERS_FILE), key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    return jsonify({"success": True, "leads": leads, "members": members})

@app.route("/api/monetization/activate", methods=["POST"])
def monetization_activate():
    try:
        data = freq.get_json() or {}
        lead_id = str(data.get("lead_id", "")).strip()
        status = str(data.get("status", "active")).strip().lower() or "active"
        if not lead_id:
            return jsonify({"success": False, "error": "lead_id is required."}), 400
        leads = load_json(LEADS_FILE)
        members = load_json(MEMBERS_FILE)
        lead = next((x for x in leads if x.get("id") == lead_id), None)
        if not lead:
            return jsonify({"success": False, "error": "Lead not found."}), 404
        lead["status"] = status
        lead["updated_at"] = now_iso()
        existing = next((m for m in members if str(m.get("lead_id")) == lead_id), None)
        if existing:
            existing.update({
                "name": lead.get("name"),
                "email": lead.get("email"),
                "phone": lead.get("phone"),
                "plan": normalize_plan(lead.get("plan")),
                "status": status,
                "updated_at": now_iso()
            })
            member_id = existing["id"]
        else:
            member_id = "mem_" + uuid.uuid4().hex[:10]
            members.insert(0, {
                "id": member_id,
                "lead_id": lead_id,
                "name": lead.get("name"),
                "email": lead.get("email"),
                "phone": lead.get("phone"),
                "plan": normalize_plan(lead.get("plan")),
                "status": status,
                "created_at": now_iso(),
                "updated_at": now_iso()
            })
        save_json(LEADS_FILE, leads)
        save_json(MEMBERS_FILE, members)
        return jsonify({"success": True, "message": f"Membership marked as {status}.", "member_id": member_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# AI COPILOT / RESEARCH LAYER
# ============================================================
def _route_json(route_path, fn, *args, **kwargs):
    try:
        with app.test_request_context(route_path):
            resp = fn(*args, **kwargs)
            if hasattr(resp, "get_json"):
                return resp.get_json()
            if isinstance(resp, tuple) and hasattr(resp[0], "get_json"):
                return resp[0].get_json()
            return resp
    except Exception as e:
        return {"success": False, "error": str(e)}

def _safe_news_lines(news_data, limit=6):
    items = (news_data or {}).get("news", [])[:limit]
    lines = []
    for item in items:
        title = (item.get("title") or "").strip()
        source = (item.get("source") or "News").strip()
        date = (item.get("date") or "").strip()
        if title:
            lines.append(f"- {title} ({source}{' · ' + date if date else ''})")
    return lines

def build_stock_context(sym):
    sym = (sym or "").upper().strip()
    if not sym:
        return {"success": False, "error": "Symbol is required."}

    cache_key = f"context:{sym}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    def _price():
        return _route_json(f"/api/price/{sym}", price, sym)

    def _tech():
        return _route_json(f"/api/technical/{sym}", technical, sym)

    def _fund():
        return _route_json(f"/api/fundamental/{sym}", fundamental, sym)

    def _news():
        return _route_json(f"/api/news/{sym}", news, sym)

    def _sent():
        return _route_json(f"/api/sentiment/{sym}", sentiment, sym)

    def _verdict():
        try:
            return build_verdict_data(sym)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _kevin_ai():
        """Pull the 7-engine Kevin AI analysis for the report (uses cache when available)."""
        try:
            cached = cache_get(f"full:{sym}")
            if cached:
                return cached
            # Build inline without an HTTP round-trip
            t = _route_json(f"/api/technical/{sym}", technical, sym) or {}
            f = _route_json(f"/api/fundamental/{sym}", fundamental, sym) or {}
            health   = _compute_financial_health_score(f, t)
            risk     = _compute_risk_meter(t, f)
            strategy = _build_trading_strategy(t)
            return {
                "success": True,
                "health_score": health,
                "risk": risk,
                "strategy": strategy,
                # kevin_verdict comes from groq — skip in sync context build to stay fast
                "kevin_verdict": cache_get(f"full:{sym}_verdict") or {},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {
            "price":    pool.submit(_price),
            "technical":pool.submit(_tech),
            "fundamental":pool.submit(_fund),
            "news":     pool.submit(_news),
            "sentiment":pool.submit(_sent),
            "verdict":  pool.submit(_verdict),
            "kevin_ai": pool.submit(_kevin_ai),
        }
        data = {k: f.result(timeout=30) for k, f in futures.items()}

    context = {
        "success": True,
        "symbol": sym,
        "price":       data.get("price", {}),
        "technical":   data.get("technical", {}),
        "fundamental": data.get("fundamental", {}),
        "news":        data.get("news", {}),
        "sentiment":   data.get("sentiment", {}),
        "verdict":     data.get("verdict", {}),
        "kevin_ai":    data.get("kevin_ai", {}),
        "data_used": ["price", "technicals", "fundamentals", "news", "sentiment", "verdict", "kevin_ai"],
    }
    cache_set(cache_key, context, ttl=120)
    return context

def groq_chat_completion(system_prompt, user_prompt, max_tokens=900, temperature=0.2):
    if not GROQ_API_KEY or GROQ_API_KEY == "YOUR_GROQ_KEY_HERE":
        return {
            "success": False,
            "error": "GROQ_API_KEY is not configured. Add it in Render environment variables."
        }
    try:
        resp = http_post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=45,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return {"success": True, "text": text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _context_prompt(context, mode="analyst"):
    sym = context.get("symbol", "")
    p   = context.get("price",       {}) or {}
    t   = context.get("technical",   {}) or {}
    f   = context.get("fundamental", {}) or {}
    n   = context.get("news",        {}) or {}
    s   = context.get("sentiment",   {}) or {}
    v   = context.get("verdict",     {}) or {}
    ob  = context.get("orderbook",   {}) or {}
    ch  = context.get("changes",     {}) or {}

    news_lines = _safe_news_lines(n, limit=6)

    style_hint = (
        "Write in simple, plain English for a beginner investor."
        if str(mode).lower() == "beginner"
        else "Write like a practical equity research analyst, concise but insightful."
    )

    chg_lines = []
    for d in (ch.get("daily_changes") or [])[-5:]:
        chg_lines.append(
            f"  {d['date']}: close=Rs.{d['close']} change={d['pct']:+.2f}%"
            f" vol={int(d.get('volume', 0) / 1e5):.1f}L"
        )

    return f"""
STOCK: {sym}
STYLE: {style_hint}

LIVE PRICE DATA
- Price: Rs.{p.get('price', 'N/A')}
- Change: {p.get('pct', 'N/A')}%
- Day range: Rs.{p.get('low', 'N/A')} - Rs.{p.get('high', 'N/A')}
- 52W range: Rs.{p.get('week52_low', 'N/A')} - Rs.{p.get('week52_high', 'N/A')}
- Volume: {p.get('volume', 'N/A')}

ORDER BOOK (live bid/ask)
- Bid: Rs.{ob.get('bid', 'N/A')} (size: {ob.get('bid_size', 'N/A')})
- Ask: Rs.{ob.get('ask', 'N/A')} (size: {ob.get('ask_size', 'N/A')})
- Spread: Rs.{ob.get('spread', 'N/A')}
- Source: {ob.get('source', 'N/A')}

RECENT 5-DAY CHANGES
- 5D change: {ch.get('5d_pct', 'N/A')}% (Rs.{ch.get('5d_change', 'N/A')})
- Up days: {ch.get('up_days', 'N/A')} / Down days: {ch.get('down_days', 'N/A')}
{chr(10).join(chg_lines) if chg_lines else '  No daily data available.'}

TECHNICALS
- Signal: {t.get('signal', 'N/A')} | Bull score: {t.get('bull_score', 'N/A')}%
- RSI: {t.get('rsi', 'N/A')} | MACD hist: {t.get('macd_hist', 'N/A')} | ADX: {t.get('adx', 'N/A')}
- MA20: Rs.{t.get('ma20', 'N/A')} | MA50: Rs.{t.get('ma50', 'N/A')} | MA200: Rs.{t.get('ma200', 'N/A')}
- Bollinger: Rs.{t.get('bb_lower', 'N/A')} - Rs.{t.get('bb_upper', 'N/A')}
- Support: Rs.{t.get('support', 'N/A')} | Resistance: Rs.{t.get('resistance', 'N/A')}
- Stop: Rs.{t.get('stop_loss', 'N/A')} | T1: Rs.{t.get('target1', 'N/A')} | T2: Rs.{t.get('target2', 'N/A')}
- SAR: Rs.{t.get('sar', 'N/A')} | Stoch %K: {t.get('stoch_k', 'N/A')} | Williams: {t.get('williams', 'N/A')}
- Patterns: {", ".join(t.get('patterns', [])) or 'None'}

FUNDAMENTALS
- PE: {f.get('pe', 'N/A')} | PB: {f.get('pb', 'N/A')} | EPS: {f.get('eps', 'N/A')}
- Market cap: {f.get('mcap', 'N/A')} | ROE: {f.get('roe', 'N/A')} | ROCE: {f.get('roce', 'N/A')}
- Debt/Equity: {f.get('debt_equity', 'N/A')} | Current ratio: {f.get('current_ratio', 'N/A')}
- Revenue growth: {f.get('rev_growth', 'N/A')} | Profit margin: {f.get('profit_margin', 'N/A')}
- Promoter: {f.get('promoter', 'N/A')} | FII: {f.get('fii', 'N/A')} | DII: {f.get('dii', 'N/A')}

SENTIMENT
- Avg bullish: {s.get('avg_bull', 'N/A')}% | Overall: {s.get('overall', 'N/A')}

AI VERDICT
- Verdict: {v.get('verdict', 'N/A')} | Confidence: {v.get('confidence', 'N/A')}
- Risk: {v.get('risk', 'N/A')} | Best for: {v.get('best_for', 'N/A')}

LATEST NEWS
{chr(10).join(news_lines) if news_lines else '- No recent news.'}

RULES
- Use ONLY the live data shown above.
- NEVER invent prices, ratios, or percentages not in this data block.
- If a field shows N/A, say that data is unavailable.
- End with: Data used: <list>.
""".strip()


@app.route("/api/research/<symbol>")
def research_report(symbol):
    sym = symbol.upper().strip()
    mode = str(freq.args.get("mode", "analyst")).lower()
    context = build_stock_context(sym)
    if not context.get("success"):
        return jsonify(context)

    prompt = _context_prompt(context, mode=mode) + """

Create a structured research report with these headings:
1. Business Summary
2. Latest Market Context
3. News Summary
4. Bullish Factors
5. Bearish Factors
6. Technical View
7. Fundamental / Valuation View
8. Key Risks
9. Short-Term View
10. Long-Term View
11. Final AI Conclusion

Keep it evidence-based and practical.
""".strip()

    ai = groq_chat_completion(
        "You are an Indian stock research copilot. Be factual, structured, and evidence-based.",
        prompt,
        max_tokens=1200,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({
        "success": True,
        "symbol": sym,
        "mode": mode,
        "report": ai["text"],
        "data_used": context["data_used"],
        "context": context,
    })

@app.route("/api/news-intelligence/<symbol>")
def news_intelligence(symbol):
    sym = symbol.upper().strip()
    mode = str(freq.args.get("mode", "analyst")).lower()
    context = build_stock_context(sym)
    news_data = context.get("news", {})
    news_lines = _safe_news_lines(news_data, limit=8)
    if not news_lines:
        return jsonify({"success": False, "error": f"No recent news available for {sym}."})

    prompt = _context_prompt(context, mode=mode) + """

Focus only on news.
Return:
- Key developments today / recently
- What changed
- Bullish implications
- Bearish implications
- What investors should watch next
""".strip()

    ai = groq_chat_completion(
        "You are a news-driven Indian equity research assistant. Use only the provided headlines and data.",
        prompt,
        max_tokens=700,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({
        "success": True,
        "symbol": sym,
        "mode": mode,
        "summary": ai["text"],
        "news": news_data.get("news", []),
        "data_used": ["news", "price", "sentiment"],
    })

@app.route("/api/what-changed/<symbol>")
def what_changed(symbol):
    sym = symbol.upper().strip()
    context = build_stock_context(sym)
    prompt = _context_prompt(context, mode="analyst") + """

Answer this: What changed for this stock today / recently?
Use these sections:
- Price action change
- Technical change
- News change
- Sentiment change
- Bottom line
Keep it concise.
""".strip()
    ai = groq_chat_completion(
        "You are a market changes summarizer for Indian equities. Stay concrete and concise.",
        prompt,
        max_tokens=500,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({
        "success": True,
        "symbol": sym,
        "summary": ai["text"],
        "data_used": context["data_used"],
    })

@app.route("/api/bull-bear/<symbol>")
def bull_bear(symbol):
    sym = symbol.upper().strip()
    mode = str(freq.args.get("mode", "analyst")).lower()
    context = build_stock_context(sym)
    prompt = _context_prompt(context, mode=mode) + """

Create a short Bull vs Bear debate.
Format:
BULL CASE
- 3 to 5 bullets

BEAR CASE
- 3 to 5 bullets

BALANCED TAKE
- 1 short conclusion
""".strip()
    ai = groq_chat_completion(
        "You are a balanced Indian stock analyst. Present both sides clearly from the supplied data only.",
        prompt,
        max_tokens=700,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({"success": True, "symbol": sym, "analysis": ai["text"], "data_used": context["data_used"]})

@app.route("/api/risk/<symbol>")
def risk_analysis(symbol):
    sym = symbol.upper().strip()
    mode = str(freq.args.get("mode", "analyst")).lower()
    context = build_stock_context(sym)
    prompt = _context_prompt(context, mode=mode) + """

Focus only on risk.
Return:
- Market / technical risks
- Fundamental / balance sheet risks
- News / sentiment risks
- Risk level: Low / Medium / High
- Suitable investor type
""".strip()
    ai = groq_chat_completion(
        "You are a stock risk analyst. Be conservative and transparent. Use only supplied data.",
        prompt,
        max_tokens=600,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({"success": True, "symbol": sym, "risk_report": ai["text"], "data_used": context["data_used"]})

@app.route("/api/compare")
def compare_stocks():
    raw = str(freq.args.get("symbols", "")).strip()
    mode = str(freq.args.get("mode", "analyst")).lower()
    symbols = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if len(symbols) < 2:
        return jsonify({"success": False, "error": "Pass at least two symbols in ?symbols=INFY,TCS"})
    symbols = symbols[:2]
    contexts = [build_stock_context(sym) for sym in symbols]

    compare_blob = []
    for ctx in contexts:
        compare_blob.append(_context_prompt(ctx, mode=mode))
        compare_blob.append("=" * 60)

    prompt = "\n".join(compare_blob) + """

Compare these two stocks.
Return:
1. Quick Winner
2. Which looks stronger technically
3. Which looks stronger fundamentally
4. Which has better news / sentiment support
5. Key risks in each
6. Best for short-term
7. Best for long-term
8. Final verdict

Do not invent facts. If any live data is missing, say so.
""".strip()

    ai = groq_chat_completion(
        "You are an Indian equity comparison copilot. Compare only from supplied live data.",
        prompt,
        max_tokens=1000,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({
        "success": True,
        "symbols": symbols,
        "comparison": ai["text"],
        "contexts": contexts,
        "data_used": ["price", "technicals", "fundamentals", "news", "sentiment", "verdict"],
    })

@app.route("/api/portfolio-insights")
def portfolio_insights():
    try:
        portfolio_data = load_json(PORTFOLIO_FILE)
    except Exception:
        portfolio_data = []
    holdings = [h for h in portfolio_data if h.get("symbol")]
    if not holdings:
        return jsonify({"success": False, "error": "Portfolio is empty."})

    holdings = holdings[:8]
    lines = []
    for h in holdings:
        sym = str(h.get("symbol", "")).upper().strip()
        try:
            ctx = build_stock_context(sym)
            p = ctx.get("price", {})
            t = ctx.get("technical", {})
            v = ctx.get("verdict", {})
            lines.append(
                f"{sym}: buy price={h.get('buy_price')} qty={h.get('qty')} live={p.get('price', 'N/A')} "
                f"change%={p.get('pct', 'N/A')} signal={t.get('signal', 'N/A')} "
                f"bull_score={t.get('bull_score', 'N/A')} verdict={v.get('verdict', 'N/A')} risk={v.get('risk', 'N/A')}"
            )
        except Exception as e:
            lines.append(f"{sym}: live context unavailable ({e})")

    prompt = """
You are a portfolio insights copilot for Indian stocks.
Use only the supplied live data lines.

Portfolio lines:
""" + "\n".join(lines) + """

Return:
- Portfolio snapshot
- Strongest names
- Weakest / highest-risk names
- What needs watching
- Practical next-step ideas

Keep it concise.
""".strip()

    ai = groq_chat_completion(
        "You are a practical portfolio copilot. Use only supplied facts.",
        prompt,
        max_tokens=800,
        temperature=0.2,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})
    return jsonify({
        "success": True,
        "count": len(holdings),
        "insights": ai["text"],
        "data_used": ["portfolio", "price", "technicals", "verdict"],
    })



def resolve_symbol_from_text(text, explicit_symbol=""):
    explicit = str(explicit_symbol or "").upper().strip()
    if explicit:
        return {"symbol": explicit, "method": "explicit", "confidence": 1.0}

    raw = str(text or "").strip()
    if not raw:
        return {"symbol": "", "method": "none", "confidence": 0.0}

    ticker_like = re.findall(r"\b[A-Z]{2,15}\b", raw.upper())
    for token in ticker_like:
        ranked = _rank_search_results(token, limit=3)
        if ranked and ranked[0]["symbol"] == token:
            return {"symbol": token, "method": "ticker_in_text", "confidence": 0.99}

    cleaned = re.sub(r"[^A-Za-z0-9\s]", " ", raw.lower())
    filler = {
        "should","i","buy","sell","hold","is","are","the","a","an","now","today","good",
        "stock","share","shares","price","latest","news","for","on","of","to","and","vs",
        "compare","risk","analysis","please","can","you","me","tell","about","what","why",
        "research","summary","summarize","explain","best","worst","limited","ltd"
    }
    words = [w for w in cleaned.split() if w and w not in filler]
    queries = []
    if words:
        queries.append(" ".join(words))
        for size in (4, 3, 2, 1):
            for i in range(0, max(0, len(words) - size + 1)):
                q = " ".join(words[i:i+size])
                if q not in queries:
                    queries.append(q)
    if cleaned.strip() and cleaned.strip() not in queries:
        queries.append(cleaned.strip())

    best = None
    for q in queries[:12]:
        ranked = _rank_search_results(q, limit=5)
        if ranked:
            cand = ranked[0]
            if (best is None) or (cand.get("score", 0) > best.get("score", 0)):
                best = cand
                if cand.get("score", 0) >= 900:
                    break

    if best and (best.get("score", 0) >= 140 or best.get("match_quality", 0) >= 0.75):
        return {
            "symbol": best["symbol"],
            "method": "name_search",
            "confidence": float(best.get("match_quality", 0)),
            "name": best.get("name", "")
        }

    return {"symbol": "", "method": "none", "confidence": 0.0}


def detect_chat_intent(message):
    q = str(message or "").lower()
    if any(k in q for k in ["compare", " vs ", "versus"]):
        return "compare"
    if any(k in q for k in ["news", "headline", "summar"]):
        return "news"
    if any(k in q for k in ["risk", "downside", "danger"]):
        return "risk"
    if any(k in q for k in ["buy", "sell", "hold", "entry", "target", "stop loss"]):
        return "recommendation"
    if any(k in q for k in ["what is", "how does", "explain", "difference between", "learn"]):
        return "education"
    return "general"


def available_data_labels(context):
    labels = []
    mapping = {
        "price": ["price"],
        "technical": ["signal", "rsi", "bull_score", "price"],
        "fundamental": ["pe", "mcap", "roe", "price"],
        "news": ["news"],
        "sentiment": ["avg_bull", "overall"],
        "verdict": ["verdict", "reasoning", "price"],
    }
    for key, probes in mapping.items():
        obj = context.get(key, {}) or {}
        ok = False
        if key == "news":
            ok = bool(obj.get("news"))
        else:
            for probe in probes:
                val = obj.get(probe)
                if val not in (None, "", [], {}, "N/A"):
                    ok = True
                    break
        if ok:
            labels.append("technicals" if key == "technical" else ("fundamentals" if key == "fundamental" else key))
    return labels


def missing_data_labels(context):
    all_labels = ["price", "technicals", "fundamentals", "news", "sentiment", "verdict"]
    available = set(available_data_labels(context))
    return [x for x in all_labels if x not in available]

@app.route("/api/chat", methods=["POST"])
def ai_chat():
    payload         = freq.get_json(silent=True) or {}
    message         = str(payload.get("message", "")).strip()
    symbol          = str(payload.get("symbol", "")).upper().strip()
    mode            = str(payload.get("mode", "analyst")).lower()

    if not message:
        return jsonify({"success": False, "error": "Message is required."}), 400

    intent          = detect_chat_intent(message)
    resolved        = resolve_symbol_from_text(message, explicit_symbol=symbol)
    detected_symbol = resolved.get("symbol", "")

    context    = None
    data_used  = []
    missing    = []

    if detected_symbol:
        context = build_stock_context(detected_symbol)
        if context.get("success"):
            data_used = available_data_labels(context)
            missing   = missing_data_labels(context)
        else:
            context         = None
            detected_symbol = ""

    q_lower = message.lower()

    if detected_symbol and context:
        availability_note = (
            f"Available: {', '.join(data_used) if data_used else 'none'}. "
            f"Missing: {', '.join(missing) if missing else 'none'}."
        )

        focus_hint = ""
        if any(k in q_lower for k in ["order book", "orderbook", "bid", "ask", "spread", "depth"]):
            focus_hint = "\nFOCUS: User wants ORDER BOOK details. Lead with bid, ask, spread and liquidity."
        elif any(k in q_lower for k in ["what changed", "recent", "today", "5 day", "5d", "last few days", "daily"]):
            focus_hint = "\nFOCUS: User wants RECENT CHANGES. Lead with the 5-day daily breakdown."
        elif any(k in q_lower for k in ["news", "headline", "latest"]):
            focus_hint = "\nFOCUS: User wants NEWS. Lead with the latest headlines and implications."
        elif any(k in q_lower for k in ["buy", "sell", "hold", "entry", "target", "stop loss"]):
            focus_hint = "\nFOCUS: User wants a BUY/SELL/HOLD opinion. Use signal, verdict, targets."

        user_prompt = _context_prompt(context, mode=mode) + f"""

USER QUESTION:
{message}

INTENT: {intent}
SYMBOL: {detected_symbol} (resolved by {resolved.get('method')}, confidence {resolved.get('confidence', 0):.2f})
DATA: {availability_note}
{focus_hint}

RESPONSE RULES:
1. Answer ONLY using the live data supplied above -- never use training memory for prices.
2. Format with ## headers and bullet points.
3. For order book -- explain bid, ask, spread and liquidity.
4. For recent changes -- summarise the 5-day daily movement.
5. If data is missing, mention it in one short line.
6. For buy/sell -- give balanced view using signal + fundamental + verdict.
7. End with: **Data used:** {', '.join(data_used) if data_used else 'general reasoning only'}.
""".strip()

    else:
        user_prompt = f"""
The user is asking a general Indian stock-market or finance question.

Question: {message}
Intent: {intent}

Rules:
1. Be helpful. Answer educational questions directly.
2. NEVER invent live stock prices or live metrics.
3. If live data would help, say: mention a company name or NSE symbol for live analysis.
4. Use ## headers and bullet points.
5. End with: **Data used:** general market knowledge only.
""".strip()

    ai = groq_chat_completion(
        (
            "You are an AI stock research copilot for Indian NSE markets. "
            "Use ONLY the live data in the user prompt -- never your training memory for prices. "
            "If a field is N/A, say it is unavailable. "
            "Format responses with ## headers and bullet points. "
            "Be concise and practical. No hallucinations."
        ),
        user_prompt,
        max_tokens=1100,
        temperature=0.15,
    )
    if not ai.get("success"):
        return jsonify({"success": False, "error": ai.get("error")})

    return jsonify({
        "success":      True,
        "symbol":       detected_symbol or None,
        "mode":         mode,
        "intent":       intent,
        "message":      message,
        "answer":       ai["text"],
        "data_used":    data_used,
        "missing_data": missing,
        "resolved_by":  resolved.get("method"),
    })

# STARTUP
# ============================================================

# ============================================================
# ORDER BOOK
# ============================================================
@app.route("/api/orderbook/<symbol>")
def orderbook(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"ob:{sym}")
    if cached:
        return jsonify(cached)
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v7/finance/options/{sym}.NS"
            r = http_get(url, headers=YFH, timeout=8)
            if r.ok:
                jd = r.json()
                q = (jd.get("optionChain", {}).get("result") or [{}])[0].get("quote", {})
                if q and q.get("regularMarketPrice"):
                    p   = q.get("regularMarketPrice", 0)
                    bid = q.get("bid", 0) or round(p * 0.9995, 2)
                    ask = q.get("ask", 0) or round(p * 1.0005, 2)
                    data = {
                        "success": True, "symbol": sym, "price": p,
                        "bid": bid, "ask": ask,
                        "bid_size": q.get("bidSize", 0),
                        "ask_size": q.get("askSize", 0),
                        "spread": round(ask - bid, 2),
                        "open": q.get("regularMarketOpen", 0),
                        "prev_close": q.get("regularMarketPreviousClose", 0),
                        "volume": q.get("regularMarketVolume", 0),
                        "avg_volume": q.get("averageDailyVolume3Month", 0),
                        "source": "Yahoo Finance",
                        "note": "Live bid/ask. For NSE market depth use NSE terminal.",
                    }
                    cache_set(f"ob:{sym}", data, ttl=30)
                    return jsonify(data)
        except Exception:
            continue
    try:
        p_data = cache_get(f"price:{sym}")
        if not p_data:
            for base in ["query1", "query2"]:
                try:
                    r = http_get(f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS", headers=YFH, timeout=8)
                    if r.ok:
                        meta = r.json()["chart"]["result"][0]["meta"]
                        p_data = {"price": meta.get("regularMarketPrice", 0),
                                  "volume": meta.get("regularMarketVolume", 0),
                                  "prev_close": meta.get("chartPreviousClose", 0)}
                        break
                except Exception:
                    continue
        if p_data and p_data.get("price"):
            p = p_data["price"]
            tick = max(0.05, round(p * 0.0005, 2))
            data = {
                "success": True, "symbol": sym, "price": p,
                "bid": round(p - tick, 2), "ask": round(p + tick, 2),
                "bid_size": 0, "ask_size": 0, "spread": round(tick * 2, 2),
                "volume": p_data.get("volume", 0), "prev_close": p_data.get("prev_close", 0),
                "source": "Yahoo Finance (estimated)",
                "note": "Bid/Ask estimated from last price. Use NSE terminal for live depth.",
            }
            cache_set(f"ob:{sym}", data, ttl=30)
            return jsonify(data)
    except Exception:
        pass
    return jsonify({"success": False, "error": f"Order book unavailable for {sym}."})


# ============================================================
# INDICES  (Nifty 50, Sensex, VIX, Nifty Bank)
# ============================================================
@app.route("/api/indices")
def indices():
    cached = cache_get("indices")
    if cached:
        return jsonify(cached)
    syms = {"nifty50": "^NSEI", "sensex": "^BSESN", "vix": "^INDIAVIX", "niftybank": "^NSEBANK"}
    def fetch_index(yfsym):
        for base in ["query1", "query2"]:
            try:
                r = http_get(f"https://{base}.finance.yahoo.com/v8/finance/chart/{yfsym}?interval=1d&range=2d", headers=YFH, timeout=8)
                if r.ok:
                    meta = r.json()["chart"]["result"][0]["meta"]
                    p = meta.get("regularMarketPrice", 0)
                    prev = meta.get("chartPreviousClose", 0)
                    return {"price": round(p,2), "prev": round(prev,2),
                            "change": round(p-prev,2), "pct": round((p-prev)/prev*100,2) if prev else 0,
                            "high": meta.get("regularMarketDayHigh",0), "low": meta.get("regularMarketDayLow",0)}
            except Exception:
                continue
        return None
    result = {"success": True}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {k: pool.submit(fetch_index, v) for k, v in syms.items()}
        for k, fut in futures.items():
            try:
                result[k] = fut.result(timeout=12) or {}
            except Exception:
                result[k] = {}
    cache_set("indices", result, ttl=60)
    return jsonify(result)


# ============================================================
# RECENT CHANGES  (5-day daily breakdown)
# ============================================================
@app.route("/api/recent-changes/<symbol>")
def recent_changes(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"changes:{sym}")
    if cached:
        return jsonify(cached)
    result_data = yf_get(sym, range_="5d", interval="1d")
    if not result_data:
        return jsonify({"success": False, "error": f"No recent data for {sym}."})
    q          = result_data["indicators"]["quote"][0]
    timestamps = result_data.get("timestamp", [])
    C = [c for c in q.get("close",  []) if c is not None]
    V = [v for v in q.get("volume", []) if v is not None]
    H = [h for h in q.get("high",   []) if h is not None]
    L = [l for l in q.get("low",    []) if l is not None]
    O = [o for o in q.get("open",   []) if o is not None]
    if len(C) < 2:
        return jsonify({"success": False, "error": "Not enough history."})
    daily = []
    for i in range(1, len(C)):
        day_chg = round(C[i] - C[i-1], 2)
        day_pct = round(day_chg / C[i-1] * 100, 2) if C[i-1] else 0
        vol_now = V[i]   if i     < len(V) else 0
        vol_pre = V[i-1] if (i-1) < len(V) and V[i-1] else 0
        vol_chg = round((vol_now - vol_pre) / vol_pre * 100, 1) if vol_pre else 0
        ts = timestamps[i] if i < len(timestamps) else 0
        try:
            date_str = datetime.datetime.utcfromtimestamp(ts).strftime("%d %b")
        except Exception:
            date_str = f"Day {i}"
        daily.append({"date": date_str,
                       "open":  round(O[i], 2) if i < len(O) else None,
                       "high":  round(H[i], 2) if i < len(H) else None,
                       "low":   round(L[i], 2) if i < len(L) else None,
                       "close": round(C[i], 2), "change": day_chg, "pct": day_pct,
                       "volume": vol_now, "vol_change_pct": vol_chg})
    total_chg = round(C[-1] - C[0], 2)
    total_pct = round(total_chg / C[0] * 100, 2) if C[0] else 0
    data = {
        "success": True, "symbol": sym,
        "current_price": round(C[-1], 2), "5d_ago_price": round(C[0], 2),
        "5d_change": total_chg, "5d_pct": total_pct,
        "5d_high": round(max(C), 2), "5d_low": round(min(C), 2),
        "avg_volume": round(sum(V)/len(V)) if V else 0,
        "daily_changes": daily, "trend": "Up" if total_pct >= 0 else "Down",
        "up_days": sum(1 for d in daily if d["pct"] >= 0),
        "down_days": sum(1 for d in daily if d["pct"] < 0),
    }
    cache_set(f"changes:{sym}", data, ttl=300)
    return jsonify(data)

# ── Trending stocks ──────────────────────────────────────────────────────────
@app.route("/api/trending")
def trending_stocks():
    cached = cache_get("trending_full")
    if cached: return jsonify(cached)
    stocks = []
    if _SEARCH_OK:
        try:
            trending = get_trending_stocks()
            for item in trending[:12]:
                sym = item.get("symbol", "")
                if not sym: continue
                try:
                    r = http_get(
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d",
                        headers=YFH, timeout=4)
                    if r.ok:
                        res = r.json().get("chart",{}).get("result")
                        if res:
                            meta = res[0]["meta"]
                            p    = meta.get("regularMarketPrice", 0)
                            prev = meta.get("chartPreviousClose", 0)
                            stocks.append({
                                "symbol":     sym,
                                "name":       meta.get("longName") or meta.get("shortName", sym),
                                "price":      round(p, 2),
                                "change_pct": round((p-prev)/prev*100,2) if prev else 0,
                                "market":     item.get("market","NSE"),
                            })
                except Exception:
                    continue
        except Exception:
            pass
    data = {"success": True, "count": len(stocks), "stocks": stocks}
    cache_set("trending_full", data, ttl=600)
    return jsonify(data)


@app.route("/api/resolve/<symbol>")
def resolve_stock_symbol(symbol):
    sym = symbol.upper().strip()
    if _SEARCH_OK:
        try:
            result = resolve_symbol(sym)
            return jsonify({"success": True, **result})
        except Exception as e:
            return jsonify({"success": False, "error": str(e), "symbol": sym}), 200
    return jsonify({"success": True, "symbol": sym + ".NS", "market": "NSE"})


# Wire up PDF/DOCX report download routes
if _REPORT_OK:
    register_report_routes(app, build_stock_context)


# ============================================================
# USER REGISTRATION & DASHBOARD
# ============================================================
_USERS_FILE = os.path.join(
    os.environ.get("WATCHLIST_PATH", "").replace("user_watchlist.json", "") or
    ("/var/data" if os.path.isdir("/var/data") else os.path.dirname(os.path.abspath(__file__))),
    "registered_users.json"
)
_users_lock = threading.Lock()

def _load_users():
    try:
        if os.path.exists(_USERS_FILE):
            with open(_USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []

def _save_users(users):
    try:
        tmp = _USERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _USERS_FILE)
    except Exception as e:
        print(f"[users] save failed: {e}")

_REGISTERED_USERS = _load_users()

@app.route("/api/register", methods=["POST"])
def register_user():
    data = freq.get_json(force=True, silent=True) or {}
    name     = str(data.get("name", "")).strip()
    email    = str(data.get("email", "")).strip().lower()
    location = str(data.get("location", "")).strip()
    phone    = str(data.get("phone", "")).strip()
    if not name or not email:
        return jsonify({"success": False, "error": "Name and email are required."}), 400
    with _users_lock:
        # Check duplicate email
        for u in _REGISTERED_USERS:
            if u.get("email", "").lower() == email:
                return jsonify({"success": False, "error": "This email is already registered."}), 409
        user = {
            "id":         str(uuid.uuid4())[:8],
            "name":       name,
            "email":      email,
            "location":   location,
            "phone":      phone,
            "registered": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }
        _REGISTERED_USERS.append(user)
        _save_users(_REGISTERED_USERS)
    return jsonify({"success": True, "user": user})

@app.route("/api/users")
@require_admin
def get_users():
    with _users_lock:
        users = list(_REGISTERED_USERS)
    return jsonify({"success": True, "count": len(users), "users": users})

@app.route("/api/users/<user_id>", methods=["DELETE"])
@require_admin
def delete_user(user_id):
    with _users_lock:
        before = len(_REGISTERED_USERS)
        _REGISTERED_USERS[:] = [u for u in _REGISTERED_USERS if u.get("id") != user_id]
        if len(_REGISTERED_USERS) == before:
            return jsonify({"success": False, "error": "User not found."}), 404
        _save_users(_REGISTERED_USERS)
    return jsonify({"success": True})



# ============================================================
# FULL ANALYSIS — 7-Engine Comprehensive Dashboard
# Endpoint: /api/full-analysis/<symbol>
# Returns: fundamentals, technicals, sentiment, macro, risk,
#          trading strategy, and Kevin AI Verdict
# ============================================================

def _compute_financial_health_score(f, t):
    """Score 0-100 from fundamentals + price data."""
    score = 50  # neutral baseline
    notes = []

    def pct_to_float(v):
        try:
            return float(str(v).replace("%", "").strip())
        except Exception:
            return None

    def to_float(v):
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return None

    # ROE > 15% is good
    roe = pct_to_float(f.get("roe", "N/A"))
    if roe is not None:
        if roe >= 20:   score += 10; notes.append("Strong ROE >20%")
        elif roe >= 15: score += 6;  notes.append("Decent ROE >15%")
        elif roe >= 8:  score += 2
        elif roe < 0:   score -= 8;  notes.append("Negative ROE — concern")

    # ROCE > 15%
    roce = pct_to_float(f.get("roce", "N/A"))
    if roce is not None:
        if roce >= 20:   score += 8;  notes.append("Excellent ROCE >20%")
        elif roce >= 15: score += 5
        elif roce < 8:   score -= 5;  notes.append("Low ROCE <8%")

    # Profit margin > 15%
    pm = pct_to_float(f.get("profit_margin", "N/A"))
    if pm is not None:
        if pm >= 20:   score += 8;  notes.append("High profit margin")
        elif pm >= 12: score += 4
        elif pm < 5:   score -= 6;  notes.append("Thin profit margin <5%")
        elif pm < 0:   score -= 12; notes.append("Loss-making company")

    # D/E < 1 is healthy
    de = to_float(f.get("debt_equity", "N/A"))
    if de is not None:
        if de == 0:    score += 8;  notes.append("Zero debt")
        elif de < 0.5: score += 5;  notes.append("Low debt D/E<0.5")
        elif de < 1.0: score += 2
        elif de < 2.0: score -= 4
        else:          score -= 8;  notes.append("High debt D/E>2")

    # Revenue growth > 10%
    rg = pct_to_float(f.get("rev_growth", "N/A"))
    if rg is not None:
        if rg >= 20:   score += 8;  notes.append("Strong revenue growth >20%")
        elif rg >= 10: score += 5
        elif rg >= 0:  score += 1
        else:          score -= 6;  notes.append("Declining revenue")

    # Current ratio > 1.5 is healthy
    cr = to_float(f.get("current_ratio", "N/A"))
    if cr is not None:
        if cr >= 2.0:   score += 5;  notes.append("Strong liquidity")
        elif cr >= 1.5: score += 3
        elif cr < 1.0:  score -= 6;  notes.append("Liquidity risk CR<1")

    # EPS > 0
    eps = to_float(f.get("eps", "N/A"))
    if eps is not None:
        if eps > 0:    score += 4
        else:          score -= 8;  notes.append("Negative EPS")

    # PE sanity (not sky-high)
    pe = to_float(f.get("pe", "N/A"))
    if pe is not None and pe > 0:
        if pe < 15:    score += 4;  notes.append("Attractive valuation PE<15")
        elif pe < 25:  score += 2
        elif pe > 60:  score -= 4;  notes.append("Expensive PE>60")

    score = max(0, min(100, score))
    if score >= 75:   grade = "Excellent"
    elif score >= 60: grade = "Good"
    elif score >= 45: grade = "Average"
    elif score >= 30: grade = "Weak"
    else:             grade = "Poor"

    return {"score": score, "grade": grade, "notes": notes[:5]}


def _compute_risk_meter(t, f):
    """Return risk level Low/Medium/High + score 0-100 (higher = more risky)."""
    risk_score = 30  # baseline
    factors = []

    def to_float(v):
        try: return float(str(v).replace(",", "").strip())
        except: return None

    # Volatility (ATR as % of price)
    atr   = to_float(t.get("atr", 0))
    price = to_float(t.get("price", 1)) or 1
    vol_pct = (atr / price * 100) if atr else 0
    if vol_pct > 4:    risk_score += 20; factors.append(f"High volatility ATR {round(vol_pct,1)}%")
    elif vol_pct > 2:  risk_score += 10; factors.append(f"Moderate volatility")
    elif vol_pct < 1:  risk_score -= 5

    # Beta
    beta = to_float(f.get("beta", "N/A")) if f else None
    if beta is not None:
        if beta > 1.5:   risk_score += 12; factors.append(f"High beta {beta}")
        elif beta > 1.0: risk_score += 5
        elif beta < 0.6: risk_score -= 5;  factors.append("Low beta defensive stock")

    # Debt risk
    de = None
    try:
        de = float(str(f.get("debt_equity", "N/A")).replace(",", ""))
    except Exception:
        pass
    if de is not None:
        if de > 2.0:   risk_score += 15; factors.append("Overleveraged D/E>2")
        elif de > 1.0: risk_score += 7

    # RSI extremes
    rsi = to_float(t.get("rsi", 50))
    if rsi is not None:
        if rsi > 75:   risk_score += 8;  factors.append("Overbought RSI>75")
        elif rsi < 25: risk_score += 8;  factors.append("Oversold RSI<25 (volatility risk)")

    # Earnings inconsistency proxy: profit margin < 5% or negative
    pm = None
    try:
        pm = float(str(f.get("profit_margin", "N/A")).replace("%", ""))
    except Exception:
        pass
    if pm is not None:
        if pm < 0:   risk_score += 15; factors.append("Company reporting losses")
        elif pm < 5: risk_score += 8;  factors.append("Thin margins <5%")

    # Distance from 52-week low (downside buffer)
    w52h = to_float(t.get("week52_high", 0))
    w52l = to_float(t.get("week52_low", 0))
    if w52h and w52l and price:
        from_peak = (w52h - price) / w52h * 100 if w52h else 0
        if from_peak > 40:   risk_score += 10; factors.append(f"Down {round(from_peak)}% from 52wk high")
        elif from_peak > 20: risk_score += 5

    risk_score = max(0, min(100, risk_score))
    if risk_score >= 65:   level = "High"
    elif risk_score >= 40: level = "Medium"
    else:                  level = "Low"

    # Downside probability estimate
    downside_prob = min(85, max(10, risk_score - 5))

    return {
        "score": risk_score,
        "level": level,
        "downside_probability": downside_prob,
        "factors": factors[:4],
    }


def _build_trading_strategy(t):
    """Generate entry/exit/stop-loss/R:R from technical data."""
    price = t.get("price", 0)
    atr   = t.get("atr", 0)
    support    = t.get("support", 0)
    resistance = t.get("resistance", 0)

    # Entry: at current price or wait for minor dip to support/MA20
    entry = round(price * 0.995, 2)  # 0.5% below current for limit order
    entry_note = "Limit order 0.5% below CMP"

    # Use support as entry if it's reasonably close
    if support and support > price * 0.93 and support < price:
        entry = round(support * 1.005, 2)
        entry_note = "Buy near support level"

    # Stop-loss: 2 ATR below entry OR at support - 1%
    stop = round(entry - 2 * atr, 2) if atr else round(entry * 0.94, 2)
    if support and support > stop:
        stop = round(support * 0.99, 2)
    stop_note = f"2× ATR below entry (Rs.{stop})"

    # Targets
    tgt1 = t.get("target1", round(price * 1.05, 2))
    tgt2 = t.get("target2", round(price * 1.10, 2))
    if resistance and resistance > price:
        tgt1 = round(min(tgt1, resistance * 0.99), 2)

    # R:R
    risk   = round(entry - stop, 2) if entry > stop else 1
    reward = round(tgt1 - entry, 2) if tgt1 > entry else 1
    rr     = round(reward / risk, 1) if risk > 0 else 0

    return {
        "entry_price": entry,
        "entry_note": entry_note,
        "stop_loss": stop,
        "stop_note": stop_note,
        "target_1": tgt1,
        "target_2": tgt2,
        "risk_reward": rr,
        "risk_per_share": risk,
        "reward_per_share": reward,
        "position_sizing_note": f"Risk Rs.{risk}/share → size position so total risk ≤ 1-2% of capital",
    }


def _build_macro_impact(sym, all_sectors):
    """Match the stock symbol to a sector and pull relevant macro context."""
    sector_map = {
        "Oil & Gas": ["RELIANCE", "ONGC", "IOC", "BPCL", "OIL", "GAIL", "PETRONET", "HINDPETRO"],
        "IT / Tech": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "LTTS", "COFORGE", "MPHASIS", "KPITTECH"],
        "Banking": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "BANDHANBNK", "AUBANK"],
        "NBFC / Finance": ["BAJFINANCE", "BAJAJFINSV", "ABCAPITAL", "CHOLAFIN", "MUTHOOTFIN", "M&MFIN"],
        "Metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "SAIL", "NATIONALUM", "NMDC", "JINDALSTEL"],
        "Auto": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "ASHOKLEY"],
        "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "DABUR", "MARICO", "EMAMILTD", "GODREJCP", "BRITANNIA"],
        "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "LUPIN", "DIVISLAB", "AUROPHARMA", "ALKEM"],
        "Infra / Power": ["NTPC", "POWERGRID", "ACMESOLAR", "JSWENERGY", "SJVN", "COCHINSHIP", "KEC"],
        "Real Estate": ["LODHA", "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD"],
        "Defence": ["HAL", "BEL", "HBLENGINE", "COCHINSHIP", "BDL", "MAZDOCK"],
        "Chemicals": ["PIDILITIND", "ATUL", "ARROWGREEN", "FINEOTEX", "DEEPAKNTR", "NAVINFLUOR"],
    }

    sector = "General"
    for s, syms in sector_map.items():
        if sym in syms:
            sector = s
            break

    macro_risks = {
        "Oil & Gas":        ["Crude oil price volatility", "Rupee depreciation impact", "OPEC+ production decisions"],
        "IT / Tech":        ["USD/INR exchange rate", "US recession risk", "Visa & immigration policy changes"],
        "Banking":          ["RBI repo rate decisions", "NPA & credit quality", "Inflation trajectory"],
        "NBFC / Finance":   ["Interest rate cycle", "RBI liquidity norms", "Credit cost pressures"],
        "Metals":           ["China demand slowdown", "Global steel/metal prices", "Import duty changes"],
        "Auto":             ["EV disruption risk", "Raw material costs", "Fuel price impact on demand"],
        "FMCG":             ["Rural consumption trends", "Inflation impact on margins", "Monsoon outlook"],
        "Pharma":           ["USFDA approval risk", "API import costs", "Pricing pressure in US generics"],
        "Infra / Power":    ["Government capex spending", "Interest rate impact on project costs", "Land acquisition delays"],
        "Real Estate":      ["Interest rate impact on affordability", "Regulatory RERA compliance", "Input cost inflation"],
        "Defence":          ["Government defence budget", "Make-in-India opportunities", "Geopolitical tensions"],
        "Chemicals":        ["China dumping risk", "Raw material price swings", "Global demand cycles"],
        "General":          ["RBI monetary policy", "Global macro uncertainty", "FII/DII flows"],
    }

    risks = macro_risks.get(sector, macro_risks["General"])

    # Pull relevant sector news from geopolitical cache
    sector_news = []
    for k, v in (all_sectors or {}).items():
        if any(w.lower() in sector.lower() or sector.lower() in w.lower() for w in k.split()):
            sector_news.extend(v[:2])

    return {
        "sector": sector,
        "macro_risks": risks,
        "sector_news": sector_news[:3],
        "macro_indicator": "Elevated" if len(risks) > 2 else "Moderate",
    }


@app.route("/api/full-analysis/<symbol>")
def full_analysis(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"full:{sym}")
    if cached:
        return jsonify(cached)

    # ── Parallel fetch of all data sources ───────────────────
    def get_tech():
        with app.test_request_context(f"/api/technical/{sym}"):
            return technical(sym).get_json()

    def get_fund():
        with app.test_request_context(f"/api/fundamental/{sym}"):
            return fundamental(sym).get_json()

    def get_sent():
        with app.test_request_context(f"/api/sentiment/{sym}"):
            return sentiment(sym).get_json()

    def get_news_data():
        with app.test_request_context(f"/api/news/{sym}"):
            return news(sym).get_json()

    def get_geo():
        with app.test_request_context("/api/geopolitical"):
            return geopolitical().get_json()

    def get_price_data():
        with app.test_request_context(f"/api/price/{sym}"):
            return price(sym).get_json()

    with ThreadPoolExecutor(max_workers=6) as pool:
        tf = pool.submit(get_tech)
        ff = pool.submit(get_fund)
        sf = pool.submit(get_sent)
        nf = pool.submit(get_news_data)
        gf = pool.submit(get_geo)
        pf = pool.submit(get_price_data)
        t  = tf.result(timeout=25)
        f  = ff.result(timeout=20)
        s  = sf.result(timeout=15)
        n  = nf.result(timeout=12)
        g  = gf.result(timeout=15)
        p  = pf.result(timeout=10)

    if not t.get("success"):
        return jsonify({"success": False, "error": "Technical data failed: " + t.get("error", "No data")})

    # ── Engine 1: Financial Health Score ─────────────────────
    health = _compute_financial_health_score(f, t)

    # ── Engine 2: Technical Signal (already in t) ────────────
    technical_summary = {
        "signal":       t.get("signal", "HOLD"),
        "bull_score":   t.get("bull_score", 50),
        "rsi":          t.get("rsi"),
        "macd_hist":    t.get("macd_hist"),
        "ma50":         t.get("ma50"),
        "ma200":        t.get("ma200"),
        "golden_cross": t.get("golden_cross", False),
        "adx":          t.get("adx"),
        "patterns":     t.get("patterns", []),
        "support":      t.get("support"),
        "resistance":   t.get("resistance"),
        "atr":          t.get("atr"),
    }

    # ── Engine 3: Sentiment Score ────────────────────────────
    avg_bull = s.get("avg_bull", 50)
    if avg_bull > 60:   sentiment_label = "Bullish"
    elif avg_bull < 40: sentiment_label = "Bearish"
    else:               sentiment_label = "Neutral"

    sentiment_summary = {
        "score":   avg_bull,
        "label":   sentiment_label,
        "sources": s.get("sources", []),
        "news_headlines": [x["title"] for x in n.get("news", [])[:5]],
    }

    # ── Engine 4: Macro Intelligence ─────────────────────────
    macro = _build_macro_impact(sym, g.get("sectors_impacted", {}))

    # ── Engine 5: Risk Meter ─────────────────────────────────
    risk = _compute_risk_meter(t, f)

    # ── Engine 6: Trading Strategy ───────────────────────────
    strategy = _build_trading_strategy(t)

    # ── Engine 7: Kevin AI Verdict (Groq) ────────────────────
    news_txt  = "\n".join([f"• {x}" for x in sentiment_summary["news_headlines"]])
    risk_txt  = "\n".join([f"• {r}" for r in risk["factors"]])
    macro_txt = "\n".join([f"• {r}" for r in macro["macro_risks"][:3]])

    kevin_prompt = (
        f"STOCK: {sym}\n"
        f"LIVE PRICE: Rs.{t.get('price')}\n\n"
        f"=== FUNDAMENTAL ANALYSIS ===\n"
        f"PE: {f.get('pe','N/A')} | PEG: {f.get('peg','N/A')} | PB: {f.get('pb','N/A')}\n"
        f"EPS: {f.get('eps','N/A')} | ROE: {f.get('roe','N/A')} | ROCE: {f.get('roce','N/A')}\n"
        f"Revenue Growth: {f.get('rev_growth','N/A')} | Profit Margin: {f.get('profit_margin','N/A')}\n"
        f"Debt/Equity: {f.get('debt_equity','N/A')} | Current Ratio: {f.get('current_ratio','N/A')}\n"
        f"Free Cash Flow: {f.get('free_cashflow','N/A')}\n"
        f"Financial Health Score: {health['score']}/100 ({health['grade']})\n\n"
        f"=== TECHNICAL ANALYSIS ===\n"
        f"Signal: {t.get('signal')} | Bull Score: {t.get('bull_score')}%\n"
        f"RSI: {t.get('rsi')} | MACD Hist: {t.get('macd_hist')} | ADX: {t.get('adx')}\n"
        f"MA50: Rs.{t.get('ma50')} | MA200: Rs.{t.get('ma200','N/A')}\n"
        f"Support: Rs.{t.get('support')} | Resistance: Rs.{t.get('resistance')}\n"
        f"Golden Cross: {'Yes' if t.get('golden_cross') else 'No'}\n"
        f"Patterns: {', '.join(t.get('patterns', []))}\n\n"
        f"=== SENTIMENT ANALYSIS ===\n"
        f"Sentiment: {sentiment_label} ({avg_bull}% bullish)\n"
        f"Latest Headlines:\n{news_txt}\n\n"
        f"=== MACRO RISK ===\n"
        f"Sector: {macro['sector']}\n"
        f"Key Macro Risks:\n{macro_txt}\n\n"
        f"=== RISK ASSESSMENT ===\n"
        f"Risk Level: {risk['level']} (Score: {risk['score']}/100)\n"
        f"Risk Factors:\n{risk_txt}\n\n"
        f"=== TRADING STRATEGY ===\n"
        f"Entry: Rs.{strategy['entry_price']} ({strategy['entry_note']})\n"
        f"Target 1: Rs.{strategy['target_1']} | Target 2: Rs.{strategy['target_2']}\n"
        f"Stop Loss: Rs.{strategy['stop_loss']}\n"
        f"Risk:Reward = 1:{strategy['risk_reward']}\n\n"
        f"TASK: Based ONLY on the above data, provide:\n"
        f"VERDICT: [BUY / HOLD / SELL]\n"
        f"CONFIDENCE: [percentage, e.g., 72%]\n"
        f"SUMMARY:\n- [key reason 1 with number]\n- [key reason 2]\n- [key reason 3]\n- [key reason 4]\n- [key reason 5]\n"
        f"BEST FOR: [Short-term / Long-term / Both / Avoid]\n"
        f"DISCLAIMER: Not financial advice. Do your own due diligence."
    )

    ai_verdict = "N/A"
    verdict_parsed = {}
    try:
        resp = http_post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "max_tokens": 900,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": (
                        "You are Kevin AI — a professional stock analyst. "
                        "Use ONLY the provided data. Never guess prices. "
                        "Be precise, evidence-based, and reference the numbers given."
                    )},
                    {"role": "user", "content": kevin_prompt},
                ],
            },
            timeout=28,
        )
        resp.raise_for_status()
        ai_verdict = resp.json()["choices"][0]["message"]["content"]

        def _find(pattern):
            m = re.search(pattern, ai_verdict, re.IGNORECASE)
            return m.group(1).strip() if m else None

        verdict_parsed = {
            "verdict":    _find(r"VERDICT:\s*(.+?)(?:\n|$)") or t.get("signal", "HOLD"),
            "confidence": _find(r"CONFIDENCE:\s*(.+?)(?:\n|$)") or "N/A",
            "best_for":   _find(r"BEST FOR:\s*(.+?)(?:\n|$)") or "Both",
            "bullets":    re.findall(r"^- (.+)", ai_verdict, re.MULTILINE)[:5],
            "full_text":  ai_verdict,
        }
    except Exception as e:
        verdict_parsed = {
            "verdict":    t.get("signal", "HOLD"),
            "confidence": "N/A",
            "best_for":   "N/A",
            "bullets":    [f"AI verdict temporarily unavailable: {str(e)[:80]}"],
            "full_text":  "",
        }

    # ── Compose final response ────────────────────────────────
    result = {
        "success": True,
        "symbol":  sym,
        "name":    p.get("name", sym),
        "price":   t.get("price"),
        "currency": p.get("currency", "INR"),

        # Engine 1
        "financial_health": {
            **health,
            "pe":             f.get("pe", "N/A"),
            "peg":            f.get("peg", "N/A"),
            "pb":             f.get("pb", "N/A"),
            "eps":            f.get("eps", "N/A"),
            "roe":            f.get("roe", "N/A"),
            "roce":           f.get("roce", "N/A"),
            "revenue_growth": f.get("rev_growth", "N/A"),
            "profit_margin":  f.get("profit_margin", "N/A"),
            "operating_margin": f.get("operating_margin", "N/A"),
            "debt_equity":    f.get("debt_equity", "N/A"),
            "current_ratio":  f.get("current_ratio", "N/A"),
            "free_cashflow":  f.get("free_cashflow", "N/A"),
            "mcap":           f.get("mcap", "N/A"),
            "dividend":       f.get("dividend", "N/A"),
            "sales_cagr":     f.get("sales_cagr", "N/A"),
            "profit_cagr":    f.get("profit_cagr", "N/A"),
        },

        # Engine 2
        "technical": {
            **technical_summary,
            "price":      t.get("price"),
            "stoch_k":    t.get("stoch_k"),
            "bb_upper":   t.get("bb_upper"),
            "bb_lower":   t.get("bb_lower"),
            "sar":        t.get("sar"),
            "williams":   t.get("williams"),
            "obv_rising": t.get("obv_rising"),
            "week52_high": t.get("week52_high"),
            "week52_low":  t.get("week52_low"),
            "vol_ratio":  t.get("vol_ratio"),
            "fib_382":    t.get("fib_382"),
            "fib_500":    t.get("fib_500"),
            "fib_618":    t.get("fib_618"),
        },

        # Engine 3
        "sentiment": sentiment_summary,

        # Engine 4
        "macro": macro,

        # Engine 5
        "risk": risk,

        # Engine 6
        "strategy": strategy,

        # Engine 7
        "kevin_verdict": verdict_parsed,

        "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
    }

    cache_set(f"full:{sym}", result, ttl=300)
    return jsonify(result)


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Kevin Kataria Stock Intelligence - Production")
    print(f"  Open: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
