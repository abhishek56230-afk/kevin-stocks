# -*- coding: utf-8 -*-
"""
search_engine.py  —  Global Dynamic Stock Search
==================================================
Supports:
  🇮🇳 NSE  — all ~2000+ listed stocks (.NS suffix)
  🇮🇳 BSE  — Sensex + all listed stocks (.BO suffix)
  🇺🇸 US   — NYSE, NASDAQ, S&P500, Dow Jones + all listed (no suffix)

Strategy (all free, no API key needed):
  1. Yahoo Finance search API  → live, covers ALL global markets
  2. Fuzzy match against a seeded popular-stocks cache for instant results
  3. Fallback: append .NS / .BO / no-suffix and try direct price fetch

Zero hardcoded stock lists. No paid APIs.

Usage in app.py:
    from search_engine import global_search, warm_popular_cache
    # At startup: warm_popular_cache()  (background thread, optional)
    # In route:   results = global_search(query, limit=12)
"""

import requests, time, threading, json, re, os
from concurrent.futures import ThreadPoolExecutor, as_completed

UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
YFH = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
       "Accept-Language": "en-US,en;q=0.9"}

# ── In-memory cache ──────────────────────────────────────────────────────────
_search_cache: dict = {}
_cache_lock   = threading.Lock()

def _scache_get(key: str):
    with _cache_lock:
        item = _search_cache.get(key)
        if item and time.time() < item["exp"]:
            return item["data"]
    return None

def _scache_set(key: str, data, ttl: int = 300):
    with _cache_lock:
        _search_cache[key] = {"data": data, "exp": time.time() + ttl}

# ── Popular stocks seed (for instant-response fuzzy matching) ─────────────────
# These are NOT a hardcoded search universe — they're just warm-up hints.
# The actual search ALWAYS hits Yahoo Finance live.
POPULAR_NSE = [
    # Nifty 50 + heavily traded
    ("RELIANCE",   "Reliance Industries",        "NSE"), ("TCS",       "Tata Consultancy Services",   "NSE"),
    ("HDFCBANK",   "HDFC Bank",                  "NSE"), ("INFY",      "Infosys",                     "NSE"),
    ("ICICIBANK",  "ICICI Bank",                 "NSE"), ("HINDUNILVR","Hindustan Unilever",           "NSE"),
    ("SBIN",       "State Bank of India",        "NSE"), ("BAJFINANCE","Bajaj Finance",               "NSE"),
    ("BHARTIARTL", "Bharti Airtel",              "NSE"), ("KOTAKBANK", "Kotak Mahindra Bank",         "NSE"),
    ("LT",         "Larsen & Toubro",            "NSE"), ("AXISBANK",  "Axis Bank",                  "NSE"),
    ("ADANIENT",   "Adani Enterprises",          "NSE"), ("ADANIPORTS","Adani Ports",                "NSE"),
    ("WIPRO",      "Wipro",                      "NSE"), ("HCLTECH",   "HCL Technologies",           "NSE"),
    ("ITC",        "ITC Limited",                "NSE"), ("ONGC",      "Oil & Natural Gas Corp",      "NSE"),
    ("NTPC",       "NTPC Limited",               "NSE"), ("POWERGRID", "Power Grid Corp",            "NSE"),
    ("TECHM",      "Tech Mahindra",              "NSE"), ("SUNPHARMA", "Sun Pharmaceutical",         "NSE"),
    ("DRREDDY",    "Dr. Reddy's Laboratories",   "NSE"), ("CIPLA",     "Cipla",                      "NSE"),
    ("DIVISLAB",   "Divi's Laboratories",        "NSE"), ("TITAN",     "Titan Company",              "NSE"),
    ("NESTLEIND",  "Nestle India",               "NSE"), ("BRITANNIA", "Britannia Industries",       "NSE"),
    ("MARUTI",     "Maruti Suzuki India",        "NSE"), ("MM",        "Mahindra & Mahindra",        "NSE"),
    ("BAJAJFINSV", "Bajaj Finserv",              "NSE"), ("TATAMOTORS","Tata Motors",                "NSE"),
    ("TATASTEEL",  "Tata Steel",                 "NSE"), ("HINDALCO",  "Hindalco Industries",        "NSE"),
    ("JSWSTEEL",   "JSW Steel",                  "NSE"), ("COALINDIA", "Coal India",                 "NSE"),
    ("BPCL",       "BPCL",                       "NSE"), ("HEROMOTOCO","Hero MotoCorp",              "NSE"),
    ("EICHERMOT",  "Eicher Motors",              "NSE"), ("UPL",       "UPL",                        "NSE"),
    ("GRASIM",     "Grasim Industries",          "NSE"), ("ULTRACEMCO","UltraTech Cement",           "NSE"),
    ("ASIANPAINT", "Asian Paints",               "NSE"), ("BAJAJ-AUTO","Bajaj Auto",                 "NSE"),
    ("INDUSINDBK", "IndusInd Bank",              "NSE"), ("APOLLOHOSP","Apollo Hospitals",           "NSE"),
    ("PIDILITIND",  "Pidilite Industries",       "NSE"), ("HAVELLS",   "Havells India",              "NSE"),
    ("ZOMATO",     "Zomato",                     "NSE"), ("NYKAA",     "FSN E-Commerce (Nykaa)",     "NSE"),
    ("PAYTM",      "One97 Communications",       "NSE"), ("DMART",     "Avenue Supermarts (DMart)",  "NSE"),
    ("POLYCAB",    "Polycab India",              "NSE"), ("ASTRAL",    "Astral Ltd",                 "NSE"),
    ("ABCAPITAL",  "Aditya Birla Capital",       "NSE"), ("SBICARD",   "SBI Cards",                 "NSE"),
    ("IRCTC",      "IRCTC",                      "NSE"), ("PNB",       "Punjab National Bank",       "NSE"),
    ("BANKBARODA", "Bank of Baroda",             "NSE"), ("CANBK",     "Canara Bank",                "NSE"),
    ("HDFCLIFE",   "HDFC Life Insurance",        "NSE"), ("ICICIPRULI","ICICI Prudential Life",      "NSE"),
    ("SBILIFE",    "SBI Life Insurance",         "NSE"), ("MUTHOOTFIN","Muthoot Finance",            "NSE"),
    ("CHOLAFIN",   "Cholamandalam Finance",      "NSE"), ("MANAPPURAM","Manappuram Finance",         "NSE"),
    ("IDFCFIRSTB", "IDFC First Bank",            "NSE"), ("FEDERALBNK","Federal Bank",              "NSE"),
    ("BANDHANBNK", "Bandhan Bank",               "NSE"), ("RBLBANK",   "RBL Bank",                  "NSE"),
    ("YESBANK",    "Yes Bank",                   "NSE"), ("KPITTECH",  "KPIT Technologies",         "NSE"),
    ("PERSISTENT", "Persistent Systems",         "NSE"), ("LTIM",      "LTIMindtree",               "NSE"),
    ("MPHASIS",    "Mphasis",                    "NSE"), ("COFORGE",   "Coforge",                   "NSE"),
    ("TATACOMM",   "Tata Communications",        "NSE"), ("ROUTE",     "Route Mobile",              "NSE"),
    ("TATAPOWER",  "Tata Power",                 "NSE"), ("ADANIGREEN","Adani Green Energy",        "NSE"),
    ("ADANISOL",   "Adani Solar",               "NSE"),  ("ACMESOLAR",  "ACME Solar Holdings",      "NSE"),
    ("IREDA",      "IREDA",                      "NSE"), ("NHPC",      "NHPC",                      "NSE"),
    ("SJVN",       "SJVN",                       "NSE"), ("RECLTD",    "REC Limited",               "NSE"),
    ("PFC",        "Power Finance Corporation",  "NSE"), ("IOC",       "Indian Oil Corporation",    "NSE"),
    ("GAIL",       "GAIL India",                 "NSE"), ("MGL",       "Mahanagar Gas",             "NSE"),
    ("IGL",        "Indraprastha Gas",           "NSE"), ("PETRONET",  "Petronet LNG",              "NSE"),
    ("CGPOWER",    "CG Power",                   "NSE"), ("SIEMENS",   "Siemens India",             "NSE"),
    ("ABB",        "ABB India",                  "NSE"), ("BHEL",      "BHEL",                      "NSE"),
    ("SAIL",       "SAIL",                       "NSE"), ("NMDC",      "NMDC",                      "NSE"),
    ("VEDL",       "Vedanta",                    "NSE"), ("HINDZINC",  "Hindustan Zinc",            "NSE"),
    ("CONCOR",     "Container Corp of India",    "NSE"), ("MCDOWELL-N","United Spirits",            "NSE"),
    ("RADICO",     "Radico Khaitan",             "NSE"), ("UNITDSPR",  "United Breweries",          "NSE"),
    ("GODREJCP",   "Godrej Consumer Products",   "NSE"), ("GODREJPROP","Godrej Properties",         "NSE"),
    ("OBEROIRLTY", "Oberoi Realty",              "NSE"), ("DLF",       "DLF",                       "NSE"),
    ("PRESTIGE",   "Prestige Estates",           "NSE"), ("BRIGADE",   "Brigade Enterprises",       "NSE"),
    ("VOLTAS",     "Voltas",                     "NSE"), ("WHIRLPOOL", "Whirlpool of India",        "NSE"),
    ("DIXON",      "Dixon Technologies",         "NSE"), ("AMBER",     "Amber Enterprises",         "NSE"),
    ("KAYNES",     "Kaynes Technology",          "NSE"), ("AFFLE",     "Affle India",               "NSE"),
    ("TATAELXSI",  "Tata Elxsi",                "NSE"), ("INOXWIND",  "Inox Wind",                 "NSE"),
]

POPULAR_US = [
    # Dow Jones 30
    ("AAPL",  "Apple Inc",               "US"), ("MSFT",  "Microsoft Corporation",     "US"),
    ("GOOGL", "Alphabet Inc (Google)",   "US"), ("AMZN",  "Amazon.com Inc",            "US"),
    ("NVDA",  "NVIDIA Corporation",      "US"), ("META",  "Meta Platforms (Facebook)", "US"),
    ("TSLA",  "Tesla Inc",               "US"), ("BERKB", "Berkshire Hathaway B",      "US"),
    ("BRK-B", "Berkshire Hathaway",      "US"), ("JPM",   "JPMorgan Chase",            "US"),
    ("V",     "Visa Inc",                "US"), ("UNH",   "UnitedHealth Group",        "US"),
    ("JNJ",   "Johnson & Johnson",       "US"), ("WMT",   "Walmart Inc",               "US"),
    ("PG",    "Procter & Gamble",        "US"), ("MA",    "Mastercard Inc",            "US"),
    ("HD",    "Home Depot Inc",          "US"), ("CVX",   "Chevron Corporation",       "US"),
    ("MRK",   "Merck & Co",             "US"),  ("ABBV",  "AbbVie Inc",               "US"),
    ("KO",    "Coca-Cola Company",       "US"), ("PEP",   "PepsiCo Inc",               "US"),
    ("BAC",   "Bank of America",         "US"), ("XOM",   "ExxonMobil Corporation",    "US"),
    ("CSCO",  "Cisco Systems",           "US"), ("INTC",  "Intel Corporation",         "US"),
    ("AMD",   "Advanced Micro Devices",  "US"), ("NFLX",  "Netflix Inc",               "US"),
    ("ORCL",  "Oracle Corporation",      "US"), ("IBM",   "IBM Corporation",           "US"),
    ("CRM",   "Salesforce Inc",          "US"), ("ADBE",  "Adobe Inc",                 "US"),
    ("QCOM",  "Qualcomm Inc",            "US"), ("TXN",   "Texas Instruments",         "US"),
    ("AVGO",  "Broadcom Inc",            "US"), ("MU",    "Micron Technology",         "US"),
    ("AMAT",  "Applied Materials",       "US"), ("LRCX",  "Lam Research",             "US"),
    ("SBUX",  "Starbucks Corporation",   "US"), ("NKE",   "Nike Inc",                  "US"),
    ("DIS",   "Walt Disney Company",     "US"), ("GS",    "Goldman Sachs",             "US"),
    ("MS",    "Morgan Stanley",          "US"), ("C",     "Citigroup Inc",             "US"),
    ("WFC",   "Wells Fargo",             "US"), ("AXP",   "American Express",          "US"),
    ("CAT",   "Caterpillar Inc",         "US"), ("BA",    "Boeing Company",            "US"),
    ("MMM",   "3M Company",              "US"), ("HON",   "Honeywell International",   "US"),
    ("GE",    "GE Aerospace",            "US"), ("RTX",   "RTX Corporation",           "US"),
    ("LMT",   "Lockheed Martin",         "US"), ("NOC",   "Northrop Grumman",          "US"),
    ("PFE",   "Pfizer Inc",              "US"), ("BMY",   "Bristol-Myers Squibb",      "US"),
    ("GILD",  "Gilead Sciences",         "US"), ("AMGN",  "Amgen Inc",                 "US"),
    ("BIIB",  "Biogen Inc",              "US"), ("ISRG",  "Intuitive Surgical",        "US"),
    ("MDT",   "Medtronic plc",           "US"), ("ABT",   "Abbott Laboratories",       "US"),
    ("TMO",   "Thermo Fisher Scientific","US"), ("DHR",   "Danaher Corporation",       "US"),
    ("SPGI",  "S&P Global Inc",         "US"),  ("ICE",   "Intercontinental Exchange", "US"),
    ("MCO",   "Moody's Corporation",     "US"), ("BLK",   "BlackRock Inc",             "US"),
    ("SCHW",  "Charles Schwab",          "US"), ("COIN",  "Coinbase Global",           "US"),
    ("PYPL",  "PayPal Holdings",         "US"), ("SQ",    "Block Inc (Square)",        "US"),
    ("SHOP",  "Shopify Inc",             "US"), ("ABNB",  "Airbnb Inc",                "US"),
    ("UBER",  "Uber Technologies",       "US"), ("LYFT",  "Lyft Inc",                  "US"),
    ("DASH",  "DoorDash Inc",            "US"), ("RBLX",  "Roblox Corporation",        "US"),
    ("U",     "Unity Software",          "US"), ("SNOW",  "Snowflake Inc",             "US"),
    ("DDOG",  "Datadog Inc",             "US"), ("NET",   "Cloudflare Inc",            "US"),
    ("ZS",    "Zscaler Inc",             "US"), ("CRWD",  "CrowdStrike Holdings",      "US"),
    ("PANW",  "Palo Alto Networks",      "US"), ("OKTA",  "Okta Inc",                  "US"),
    ("NOW",   "ServiceNow Inc",          "US"), ("WORK",  "Slack Technologies",        "US"),
    ("ZOOM",  "Zoom Video Communications","US"),("DOCN",  "DigitalOcean Holdings",     "US"),
    ("TWLO",  "Twilio Inc",              "US"), ("MDB",   "MongoDB Inc",               "US"),
    ("ESTC",  "Elastic NV",              "US"), ("GTLB",  "GitLab Inc",                "US"),
    ("ARM",   "ARM Holdings",            "US"), ("SMCI",  "Super Micro Computer",      "US"),
    ("PLTR",  "Palantir Technologies",   "US"), ("AI",    "C3.ai Inc",                 "US"),
    ("TSM",   "TSMC",                    "US"), ("ASML",  "ASML Holding NV",           "US"),
    ("SAP",   "SAP SE",                  "US"), ("TM",    "Toyota Motor Corp",         "US"),
    ("SONY",  "Sony Group Corporation",  "US"), ("NVO",   "Novo Nordisk",              "US"),
    ("SPOT",  "Spotify Technology",      "US"), ("NTES",  "NetEase Inc",               "US"),
    ("JD",    "JD.com Inc",              "US"), ("BABA",  "Alibaba Group",             "US"),
    ("PDD",   "PDD Holdings (Temu/Pinduoduo)","US"),
]

# Build fast lookup dict from popular seeds
_POPULAR_LOOKUP: dict = {}
for sym, name, market in POPULAR_NSE + POPULAR_US:
    _POPULAR_LOOKUP[sym.upper()] = {"symbol": sym.upper(), "name": name, "market": market}

# ── Yahoo Finance Live Search ─────────────────────────────────────────────────
def yahoo_search(query: str, max_results: int = 15) -> list:
    """
    Hit Yahoo Finance search API — returns stocks from ALL global markets.
    This is the primary search engine.
    """
    cache_key = f"yf_search:{query.lower()[:50]}"
    cached = _scache_get(cache_key)
    if cached is not None:
        return cached

    results = []
    try:
        url = (f"https://query1.finance.yahoo.com/v1/finance/search"
               f"?q={requests.utils.quote(query)}"
               f"&quotesCount={max_results}&newsCount=0&enableFuzzyQuery=true"
               f"&quotesQueryId=tss_match_phrase_query&multiQuoteQueryId=multi_quote_single_token_query")
        r = requests.get(url, headers=YFH, timeout=8)
        if not r.ok:
            # Try alternate endpoint
            url2 = (f"https://query2.finance.yahoo.com/v1/finance/search"
                    f"?q={requests.utils.quote(query)}&quotesCount={max_results}&newsCount=0")
            r = requests.get(url2, headers=YFH, timeout=8)
        if r.ok:
            data = r.json()
            quotes = data.get("quotes", [])
            for q in quotes:
                qtype = q.get("quoteType", "")
                # Filter: only stocks and ETFs, not futures/options/currency
                if qtype not in ("EQUITY", "ETF", "MUTUALFUND", "INDEX"):
                    continue
                sym     = q.get("symbol", "")
                name    = q.get("longname") or q.get("shortname") or sym
                exch    = q.get("exchDisp", "") or q.get("exchange", "")
                market  = _detect_market(sym, exch)
                results.append({
                    "symbol":   sym,
                    "name":     name,
                    "exchange": exch,
                    "market":   market,
                    "type":     qtype,
                    "score":    100,
                })
    except Exception:
        pass

    _scache_set(cache_key, results, ttl=120)  # 2 min cache
    return results


def _detect_market(symbol: str, exchange: str) -> str:
    """Detect NSE / BSE / US based on symbol and exchange string."""
    s = symbol.upper()
    e = (exchange or "").upper()
    if s.endswith(".NS") or "NSE" in e:
        return "NSE"
    if s.endswith(".BO") or "BSE" in e or "BOM" in e:
        return "BSE"
    if ".NS" not in s and ".BO" not in s and ("NYSE" in e or "NASDAQ" in e or "NMS" in e
        or "NYQ" in e or "PCX" in e or "AMEX" in e or e == ""):
        return "US"
    return exchange or "GLOBAL"


# ── Fuzzy local search (instant, for popular stocks) ─────────────────────────
def _fuzzy_popular(query: str, limit: int = 6) -> list:
    """Fast local fuzzy match against the popular-stocks seed list."""
    q = query.upper().strip()
    q_lower = query.lower().strip()
    results = []
    for sym, info in _POPULAR_LOOKUP.items():
        name_lower = info["name"].lower()
        sym_score = 0
        # Exact prefix match on symbol
        if sym.startswith(q):
            sym_score = 90 + (10 if sym == q else 0)
        # Substring match on symbol
        elif q in sym:
            sym_score = 70
        # Name prefix/substring
        elif name_lower.startswith(q_lower):
            sym_score = 65
        elif q_lower in name_lower:
            sym_score = 50
        # Check individual words in name
        elif any(w.startswith(q_lower) for w in name_lower.split()):
            sym_score = 45

        if sym_score > 0:
            results.append({
                "symbol": sym,
                "name": info["name"],
                "market": info["market"],
                "exchange": info["market"],
                "type": "EQUITY",
                "score": sym_score,
            })

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ── Price enrichment ─────────────────────────────────────────────────────────
def _fetch_price_for_result(item: dict) -> dict:
    """Try to fetch live price for a search result."""
    sym = item.get("symbol", "")
    if not sym:
        return item
    # Cache check
    cache_key = f"sp:{sym}"
    cached = _scache_get(cache_key)
    if cached:
        item.update(cached)
        return item
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2d"
        r = requests.get(url, headers=YFH, timeout=4)
        if r.ok:
            meta  = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price:
                pct = round((price - prev) / prev * 100, 2) if prev else 0
                price_data = {"price": round(price, 2), "change_pct": pct}
                _scache_set(cache_key, price_data, ttl=60)
                item.update(price_data)
    except Exception:
        pass
    return item


# ── Main search function ──────────────────────────────────────────────────────
def global_search(query: str, limit: int = 12, enrich: bool = True) -> list:
    """
    Search stocks globally across NSE, BSE, and all US markets.
    Returns a list of result dicts with symbol, name, market, price, change_pct.

    This is the single function to call from app.py.
    """
    if not query or len(query.strip()) < 1:
        return []

    q = query.strip()

    # 1. Try Yahoo Finance live search first (covers everything)
    yf_results = yahoo_search(q, max_results=limit + 5)

    # 2. Also get local fuzzy hits (for instant popular stocks)
    local_results = _fuzzy_popular(q, limit=6)

    # 3. Merge: Yahoo Finance results take priority, local fills gaps
    seen_syms = set()
    merged = []
    for r in yf_results:
        sym = r["symbol"]
        if sym not in seen_syms:
            seen_syms.add(sym)
            merged.append(r)
    for r in local_results:
        sym = r["symbol"]
        if sym not in seen_syms:
            seen_syms.add(sym)
            merged.append(r)

    # 4. Sort: exact matches first, then by market order (NSE, US, BSE, others)
    q_upper = q.upper()
    def sort_key(r):
        sym   = r["symbol"].upper()
        score = r.get("score", 50)
        # Boost exact symbol match
        if sym == q_upper or sym == q_upper + ".NS" or sym == q_upper + ".BO":
            score += 200
        # Boost prefix match
        elif sym.startswith(q_upper):
            score += 100
        return -score

    merged.sort(key=sort_key)
    merged = merged[:limit]

    # 5. Enrich with prices (parallel, top 8 only to be fast)
    if enrich and merged:
        top    = merged[:8]
        rest   = merged[8:]
        with ThreadPoolExecutor(max_workers=6) as pool:
            top = list(pool.map(_fetch_price_for_result, top))
        merged = top + rest

    return merged


# ── Market suffix helpers for analysis routes ─────────────────────────────────
def resolve_symbol(sym: str) -> dict:
    """
    Given a raw symbol like "RELIANCE", "RELIANCE.NS", "TSLA", or "TCS.BO",
    try to resolve to valid ticker(s) and return metadata.
    Returns {"symbol": "RELIANCE.NS", "market": "NSE", "valid": True}
    """
    sym = sym.upper().strip()
    cache_key = f"resolve:{sym}"
    cached = _scache_get(cache_key)
    if cached:
        return cached

    # If already has suffix
    if sym.endswith(".NS"):
        result = {"symbol": sym, "market": "NSE", "valid": True}
        _scache_set(cache_key, result, ttl=3600)
        return result
    if sym.endswith(".BO"):
        result = {"symbol": sym, "market": "BSE", "valid": True}
        _scache_set(cache_key, result, ttl=3600)
        return result

    # Popular US stocks (no suffix)
    if sym in _POPULAR_LOOKUP and _POPULAR_LOOKUP[sym]["market"] == "US":
        result = {"symbol": sym, "market": "US", "valid": True}
        _scache_set(cache_key, result, ttl=3600)
        return result

    # Try NSE first, then BSE, then US (no suffix) — check which has a valid price
    candidates = [(sym + ".NS", "NSE"), (sym + ".BO", "BSE"), (sym, "US")]
    for ticker, market in candidates:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
            r   = requests.get(url, headers=YFH, timeout=5)
            if r.ok and r.json().get("chart", {}).get("result"):
                result = {"symbol": ticker, "market": market, "valid": True}
                _scache_set(cache_key, result, ttl=3600)
                return result
        except Exception:
            continue

    # Last resort: return NSE with valid=False
    result = {"symbol": sym + ".NS", "market": "NSE", "valid": False}
    _scache_set(cache_key, result, ttl=120)
    return result


# ── Trending / Popular ────────────────────────────────────────────────────────
def get_trending_stocks() -> list:
    """
    Get trending stocks from Yahoo Finance trending tickers API.
    Falls back to a curated popular list if unavailable.
    """
    cache_key = "trending"
    cached = _scache_get(cache_key)
    if cached:
        return cached

    trending = []
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/trending/IN?count=10"
        r = requests.get(url, headers=YFH, timeout=6)
        if r.ok:
            quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
            trending = [{"symbol": q.get("symbol",""), "market": "NSE"} for q in quotes[:10]]
    except Exception:
        pass

    if not trending:
        trending = [
            {"symbol": "RELIANCE.NS", "market": "NSE"}, {"symbol": "TCS.NS", "market": "NSE"},
            {"symbol": "HDFCBANK.NS", "market": "NSE"}, {"symbol": "INFY.NS",  "market": "NSE"},
            {"symbol": "NVDA",        "market": "US"},   {"symbol": "TSLA",     "market": "US"},
            {"symbol": "AAPL",        "market": "US"},   {"symbol": "ADANIENT.NS","market": "NSE"},
        ]

    _scache_set(cache_key, trending, ttl=600)
    return trending


# ── Warm up cache in background ───────────────────────────────────────────────
def warm_popular_cache():
    """Pre-warm price cache for top 30 stocks. Call at startup in a thread."""
    def _warm():
        top30 = ["RELIANCE.NS","TCS.NS","HDFCBANK.NS","INFY.NS","ICICIBANK.NS",
                 "SBIN.NS","BAJFINANCE.NS","LT.NS","ITC.NS","AXISBANK.NS",
                 "AAPL","MSFT","NVDA","TSLA","GOOGL","AMZN","META","AMD","NFLX","JPM"]
        for sym in top30:
            try:
                _fetch_price_for_result({"symbol": sym})
                time.sleep(0.1)
            except Exception:
                pass
    threading.Thread(target=_warm, daemon=True).start()
