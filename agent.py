# -*- coding: utf-8 -*-
"""
agent.py  —  100% FREE Agentic AI for NSE Stock Intelligence
=============================================================
Stack (all free, no token fees):
  LLM backbone   → Groq API  (Llama 3.3 70B  — free tier, 6000 req/day)
  Web search     → DuckDuckGo (no API key, unlimited, pip install duckduckgo-search)
  Price data     → Yahoo Finance (free, already in your app)
  Fundamentals   → Yahoo Finance v11 quoteSummary (free)
  News           → Google News RSS  (free, already in your app)
  Technicals     → Calculated from Yahoo Finance OHLCV (free)

How agentic works:
  1. User asks a question
  2. Llama 3.3 reads the question and DECIDES which tools to call
  3. We run those tools (search, price, fundamentals…)
  4. Llama reads results and DECIDES if it needs more tools
  5. Loop repeats up to MAX_ITER times
  6. Final answer is synthesised from all gathered data

Drop this file next to app.py and add:
    from agent import run_stock_agent, agent_bp
    app.register_blueprint(agent_bp)
"""

import json, time, re, os, threading
import requests
import xml.etree.ElementTree as ET
from flask import Blueprint, request as freq, jsonify
from concurrent.futures import ThreadPoolExecutor

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
AGENT_MODEL  = "llama-3.3-70b-versatile"   # supports tool calling, free
FAST_MODEL   = "llama-3.1-8b-instant"       # for simple sub-tasks, even faster
MAX_ITER     = 6                             # max agent loop iterations
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
YFH = {"User-Agent": UA, "Accept": "application/json,*/*"}

# ── Agent cache (reduces Groq calls, stays within free limits) ────────────────
_agent_cache: dict = {}
_agent_lock  = threading.Lock()

def _acache_get(key: str):
    with _agent_lock:
        item = _agent_cache.get(key)
        if item and time.time() < item["exp"]:
            return item["data"]
    return None

def _acache_set(key: str, data, ttl: int = 600):
    with _agent_lock:
        _agent_cache[key] = {"data": data, "exp": time.time() + ttl}

# ── Flask blueprint ───────────────────────────────────────────────────────────
agent_bp = Blueprint("agent", __name__)

# ═════════════════════════════════════════════════════════════════════════════
# TOOL IMPLEMENTATIONS  (all free data sources)
# ═════════════════════════════════════════════════════════════════════════════

def tool_web_search(query: str, max_results: int = 6) -> list:
    """DuckDuckGo search — completely free, no API key, works on Render."""
    cache_key = f"ddg:{query[:80]}"
    cached = _acache_get(cache_key)
    if cached:
        return cached
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title", ""),
                    "snippet": r.get("body",  "")[:400],
                    "url":     r.get("href",  ""),
                    "source":  r.get("source", "Web"),
                })
        _acache_set(cache_key, results, ttl=300)
        return results
    except ImportError:
        # fallback: Google News RSS if duckduckgo-search not installed
        return _google_news_search(query)
    except Exception as e:
        return [{"error": str(e), "title": "Search failed", "snippet": str(e)}]


def _google_news_search(query: str) -> list:
    """Google News RSS — free fallback if DuckDuckGo library missing."""
    try:
        q  = query.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        r   = requests.get(url, headers={"User-Agent": "Googlebot/2.1"}, timeout=8)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)[:6]
        results = []
        for item in items:
            title  = re.findall(r"<title>(.*?)</title>",       item)
            link   = re.findall(r"<link/>(.*?)\n",             item)
            source = re.findall(r"<source[^>]*>(.*?)</source>", item)
            if title:
                results.append({
                    "title":   title[0],
                    "snippet": title[0],
                    "url":     link[0].strip() if link else "#",
                    "source":  source[0] if source else "Google News",
                })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def tool_get_price(symbol: str) -> dict:
    """Live price from Yahoo Finance — free."""
    sym = symbol.upper().strip()
    cached = _acache_get(f"price:{sym}")
    if cached:
        return cached
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=2d"
            r   = requests.get(url, headers=YFH, timeout=8)
            if not r.ok:
                continue
            meta = r.json()["chart"]["result"][0]["meta"]
            p    = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", 0)
            data = {
                "symbol":     sym,
                "price":      round(p, 2),
                "prev_close": round(prev, 2),
                "change":     round(p - prev, 2),
                "change_pct": round((p - prev) / prev * 100, 2) if prev else 0,
                "day_high":   meta.get("regularMarketDayHigh", 0),
                "day_low":    meta.get("regularMarketDayLow", 0),
                "volume":     meta.get("regularMarketVolume", 0),
                "week52_high": meta.get("fiftyTwoWeekHigh", 0),
                "week52_low":  meta.get("fiftyTwoWeekLow", 0),
                "name":       meta.get("longName") or meta.get("shortName", sym),
            }
            _acache_set(f"price:{sym}", data, ttl=60)
            return data
        except Exception:
            continue
    return {"error": f"Could not fetch price for {sym}. Check NSE symbol."}


def tool_get_fundamentals(symbol: str) -> dict:
    """Fundamentals from Yahoo Finance v11 — free, no scraping fragility."""
    sym = symbol.upper().strip()
    cached = _acache_get(f"fund_agent:{sym}")
    if cached:
        return cached
    for base in ["query1", "query2"]:
        try:
            mods = "defaultKeyStatistics%2CfinancialData%2CsummaryDetail%2CmajorHoldersBreakdown"
            url  = f"https://{base}.finance.yahoo.com/v11/finance/quoteSummary/{sym}.NS?modules={mods}"
            r    = requests.get(url, headers=YFH, timeout=10)
            if not r.ok:
                continue
            res  = r.json()["quoteSummary"]["result"][0]
            fd   = res.get("financialData",           {}) or {}
            sd   = res.get("summaryDetail",            {}) or {}
            ks   = res.get("defaultKeyStatistics",     {}) or {}
            mh   = res.get("majorHoldersBreakdown",    {}) or {}

            def raw(d, k):
                v = d.get(k)
                return v.get("raw") if isinstance(v, dict) else v

            def pct(d, k):
                v = raw(d, k)
                return round((v or 0) * 100, 1)

            data = {
                "symbol":             sym,
                "pe_ratio":           round(raw(sd, "trailingPE") or 0, 1),
                "pb_ratio":           round(raw(ks, "priceToBook") or 0, 2),
                "eps":                round(raw(ks, "trailingEps") or 0, 2),
                "roe_pct":            pct(fd, "returnOnEquity"),
                "roa_pct":            pct(fd, "returnOnAssets"),
                "profit_margin_pct":  pct(fd, "profitMargins"),
                "revenue_growth_pct": pct(fd, "revenueGrowth"),
                "debt_to_equity":     round(raw(fd, "debtToEquity") or 0, 2),
                "current_ratio":      round(raw(fd, "currentRatio") or 0, 2),
                "mcap_cr":            round((raw(sd, "marketCap") or 0) / 1e7, 0),
                "promoter_pct":       pct(mh, "insidersPercentHeld"),
                "institutional_pct":  pct(mh, "institutionsPercentHeld"),
                "dividend_yield_pct": round((raw(sd, "dividendYield") or 0) * 100, 2),
                "beta":               round(raw(sd, "beta") or 0, 2),
            }
            data["public_pct"] = max(0, round(100 - data["promoter_pct"] - data["institutional_pct"], 1))
            _acache_set(f"fund_agent:{sym}", data, ttl=1800)
            return data
        except Exception:
            continue
    return {"error": f"Could not fetch fundamentals for {sym}"}


def tool_get_technicals(symbol: str) -> dict:
    """Technical indicators computed from Yahoo Finance OHLCV — free."""
    sym = symbol.upper().strip()
    cached = _acache_get(f"tech_agent:{sym}")
    if cached:
        return cached
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=1y"
            r   = requests.get(url, headers=YFH, timeout=10)
            if not r.ok:
                continue
            result = r.json()["chart"]["result"][0]
            q  = result["indicators"]["quote"][0]
            C  = [c for c in q.get("close",  []) if c is not None]
            H  = [h for h in q.get("high",   []) if h is not None]
            L  = [l for l in q.get("low",    []) if l is not None]
            V  = [v for v in q.get("volume", []) if v is not None]
            if len(C) < 50:
                return {"error": f"Not enough history for {sym}"}

            # moving averages
            def sma(d, p): return round(sum(d[-p:]) / p, 2) if len(d) >= p else None
            def ema(d, p):
                k, e = 2 / (p + 1), d[0]
                for x in d[1:]: e = x * k + e * (1 - k)
                return e

            ma20  = sma(C, 20)
            ma50  = sma(C, 50)
            ma200 = sma(C, 200)
            p     = C[-1]

            # RSI 14
            g, ls = [], []
            for i in range(1, 15):
                d = C[-i] - C[-i - 1]
                (g if d > 0 else ls).append(abs(d))
            ag = sum(g) / 14 if g else 0
            al = sum(ls) / 14 if ls else 0.001
            rsi = round(100 - (100 / (1 + ag / al)), 1)

            # MACD
            e12   = ema(C[-60:], 12)
            e26   = ema(C[-60:], 26)
            macd  = e12 - e26
            mlist = [ema(C[-(60 + i):-i], 12) - ema(C[-(60 + i):-i], 26) for i in range(20, 0, -1)]
            sig   = ema(mlist, 9)
            hist  = round(macd - sig, 3)

            # Bollinger
            std20 = (sum((c - ma20) ** 2 for c in C[-20:]) / 20) ** 0.5
            bbu   = round(ma20 + 2 * std20, 2)
            bbl   = round(ma20 - 2 * std20, 2)

            # ATR
            tr  = [max(H[-i] - L[-i], abs(H[-i] - C[-i - 1]), abs(L[-i] - C[-i - 1])) for i in range(1, 15)]
            atr = round(sum(tr) / len(tr), 2)

            # Bull score
            b, br = 0, 0
            if p > ma20: b += 1
            else: br += 1
            if p > ma50: b += 1
            else: br += 1
            if ma200:
                if p > ma200: b += 2
                else: br += 2
            if rsi > 50: b += 1
            else: br += 1
            if rsi <= 30: b += 2
            if rsi >= 70: br += 2
            if hist > 0: b += 2
            else: br += 2
            total = b + br or 1
            score = round((b / total) * 100)

            if score >= 70:   sig_txt = "STRONG BUY"
            elif score >= 57: sig_txt = "BUY"
            elif score >= 43: sig_txt = "HOLD"
            elif score >= 30: sig_txt = "SELL"
            else:             sig_txt = "STRONG SELL"

            rl = min(L[-20:])
            rh = max(H[-20:])
            avg_v = sum(V[-20:]) / 20 if V else 1
            vol_ratio = round(V[-1] / avg_v, 1) if avg_v and V else 0

            data = {
                "symbol": sym, "price": round(p, 2),
                "signal": sig_txt, "bull_score": score,
                "rsi": rsi, "macd_hist": hist,
                "ma20": round(ma20, 2), "ma50": round(ma50, 2),
                "ma200": round(ma200, 2) if ma200 else None,
                "bb_upper": bbu, "bb_lower": bbl,
                "atr": atr,
                "support": round(rl, 2), "resistance": round(rh, 2),
                "stop_loss": round(p - 2 * atr, 2),
                "target1":   round(p + 3 * atr, 2),
                "target2":   round(p + 6 * atr, 2),
                "vol_ratio": vol_ratio,
                "golden_cross": bool(ma50 and ma200 and ma50 > ma200),
            }
            _acache_set(f"tech_agent:{sym}", data, ttl=300)
            return data
        except Exception:
            continue
    return {"error": f"Could not compute technicals for {sym}"}


def tool_get_recent_changes(symbol: str) -> dict:
    """5-day daily price breakdown from Yahoo Finance — free."""
    sym = symbol.upper().strip()
    cached = _acache_get(f"chg_agent:{sym}")
    if cached:
        return cached
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS?interval=1d&range=5d"
            r   = requests.get(url, headers=YFH, timeout=8)
            if not r.ok:
                continue
            result = r.json()["chart"]["result"][0]
            q      = result["indicators"]["quote"][0]
            ts     = result.get("timestamp", [])
            C = [c for c in q.get("close",  []) if c is not None]
            V = [v for v in q.get("volume", []) if v is not None]
            if len(C) < 2:
                return {"error": "Not enough data"}
            daily = []
            import datetime
            for i in range(1, len(C)):
                chg = round(C[i] - C[i - 1], 2)
                pct = round(chg / C[i - 1] * 100, 2) if C[i - 1] else 0
                try:
                    date_str = datetime.datetime.utcfromtimestamp(ts[i]).strftime("%d %b")
                except Exception:
                    date_str = f"Day {i}"
                daily.append({"date": date_str, "close": round(C[i], 2),
                               "change": chg, "pct": pct,
                               "volume": V[i] if i < len(V) else 0})
            total_pct = round((C[-1] - C[0]) / C[0] * 100, 2) if C[0] else 0
            data = {
                "symbol": sym,
                "5d_change_pct": total_pct,
                "5d_trend": "Up" if total_pct >= 0 else "Down",
                "up_days": sum(1 for d in daily if d["pct"] >= 0),
                "down_days": sum(1 for d in daily if d["pct"] < 0),
                "current_price": round(C[-1], 2),
                "5d_ago_price": round(C[0], 2),
                "daily": daily,
            }
            _acache_set(f"chg_agent:{sym}", data, ttl=300)
            return data
        except Exception:
            continue
    return {"error": f"Could not fetch recent changes for {sym}"}


def tool_get_news(symbol: str) -> dict:
    """Google News RSS for a stock — free, no API key."""
    cached = _acache_get(f"news_agent:{symbol}")
    if cached:
        return cached
    try:
        q   = symbol.replace("-", "+").upper() + "+NSE+India+stock"
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        r   = requests.get(url, headers={"User-Agent": "Googlebot/2.1"}, timeout=8)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)[:8]
        news  = []
        for item in items:
            title  = re.findall(r"<title>(.*?)</title>",        item)
            link   = re.findall(r"<link/>(.*?)\n",              item)
            pub    = re.findall(r"<pubDate>(.*?)</pubDate>",     item)
            src    = re.findall(r"<source[^>]*>(.*?)</source>",  item)
            if title:
                news.append({
                    "title":  title[0],
                    "url":    link[0].strip() if link else "#",
                    "date":   pub[0][:16] if pub else "",
                    "source": src[0] if src else "News",
                })
        data = {"symbol": symbol, "count": len(news), "items": news}
        _acache_set(f"news_agent:{symbol}", data, ttl=600)
        return data
    except Exception as e:
        return {"error": str(e), "items": []}


# ═════════════════════════════════════════════════════════════════════════════
# TOOL REGISTRY  (maps name → function)
# ═════════════════════════════════════════════════════════════════════════════

TOOL_REGISTRY = {
    "web_search":       lambda args: tool_web_search(args.get("query", "")),
    "get_live_price":   lambda args: tool_get_price(args.get("symbol", "")),
    "get_fundamentals": lambda args: tool_get_fundamentals(args.get("symbol", "")),
    "get_technicals":   lambda args: tool_get_technicals(args.get("symbol", "")),
    "get_recent_changes": lambda args: tool_get_recent_changes(args.get("symbol", "")),
    "get_news":         lambda args: tool_get_news(args.get("symbol", "")),
}

# ═════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS  (sent to Groq so Llama knows what tools exist)
# ═════════════════════════════════════════════════════════════════════════════

GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for LIVE information: stock news, earnings results, "
                "analyst targets, corporate events, SEBI/BSE/NSE filings, management commentary, "
                "promoter activity, FII flows, sector news, economy updates. "
                "Use specific queries like 'RELIANCE Q4 2025 results revenue profit' "
                "or 'HDFC Bank analyst price target Motilal Oswal 2025'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Specific search query. Be precise — include company name, year, and topic."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_price",
            "description": "Get live NSE stock price, day range, 52-week range, volume, change %.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE symbol e.g. RELIANCE, TCS, HDFCBANK"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": (
                "Get fundamental/valuation data: PE ratio, PB, ROE, ROCE, profit margin, "
                "revenue growth, debt/equity, current ratio, market cap, promoter holding, "
                "FII/institutional holding, dividend yield."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE symbol"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_technicals",
            "description": (
                "Get technical analysis: RSI, MACD histogram, moving averages (20/50/200), "
                "Bollinger bands, ATR, bull score, BUY/SELL/HOLD signal, support, resistance, "
                "stop loss, price targets, golden cross status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE symbol"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_changes",
            "description": "Get 5-day daily price movement: each day's close, change %, volume. Use for 'what changed', 'recent moves', 'this week' questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE symbol"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get latest news headlines for a specific NSE stock from Google News.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "NSE symbol"}
                },
                "required": ["symbol"]
            }
        }
    },
]

# ═════════════════════════════════════════════════════════════════════════════
# AGENT SYSTEM PROMPTS
# ═════════════════════════════════════════════════════════════════════════════

AGENT_SYSTEM = """You are an expert Indian NSE stock market research agent with real-time tools.

YOUR JOB:
1. Read the question carefully
2. Call the RIGHT tools to get live data BEFORE answering
3. If results seem incomplete, call more tools
4. Synthesise a clear, accurate, well-structured answer

TOOL STRATEGY:
- Price question → get_live_price
- Buy/sell/hold → get_live_price + get_technicals (always both)
- Fundamentals/valuation → get_fundamentals
- News/events/earnings → web_search (with specific query) + get_news
- "What changed recently" → get_recent_changes + web_search
- Deep research → all tools
- Comparison → run all tools for BOTH stocks
- General market question → web_search

CRITICAL RULES:
- NEVER answer with made-up prices or numbers. Always call tools first.
- If a number seems wrong (PE of 0, price of 0), call the tool again with a slightly different approach.
- When searching, be SPECIFIC: use company full name + "NSE India" + year + topic.
- Format your final answer with ## sections and bullet points.
- Always end with: **Sources:** [list tools you used and what you found].
- If Groq hits a rate limit, simplify your response using only the data you already have.
"""

# ═════════════════════════════════════════════════════════════════════════════
# CORE AGENT LOOP
# ═════════════════════════════════════════════════════════════════════════════

def run_stock_agent(
    question: str,
    symbol: str = "",
    mode: str = "analyst",
    history: list = None,
) -> dict:
    """
    Main agentic loop. Uses Groq tool-calling to let Llama decide
    which tools to invoke and when to stop.

    Returns dict: {
        success, answer, tools_called, iterations, symbol
    }
    """
    if not GROQ_API_KEY or GROQ_API_KEY == "YOUR_GROQ_KEY_HERE":
        return {"success": False, "error": "GROQ_API_KEY not set in environment variables."}

    # Check agent cache (same question + symbol recently answered?)
    cache_key = f"agent:{symbol}:{question[:100]}"
    cached = _acache_get(cache_key)
    if cached:
        return {**cached, "from_cache": True}

    # Build initial messages
    style = (
        "Explain in simple plain English, avoid jargon."
        if mode == "beginner"
        else "Be a sharp analyst — concise, evidence-based, reference real numbers."
    )

    messages = [
        {"role": "system", "content": AGENT_SYSTEM + f"\n\nSTYLE: {style}"}
    ]

    # Include conversation history for context (last 4 exchanges)
    for h in (history or [])[-4:]:
        messages.append({"role": "user",      "content": str(h.get("user", ""))[:600]})
        messages.append({"role": "assistant", "content": str(h.get("assistant", ""))[:600]})

    # Initial user message with symbol hint if known
    user_msg = question
    if symbol:
        user_msg = f"Stock: {symbol} (NSE)\n\n{question}"
    messages.append({"role": "user", "content": user_msg})

    tools_called  = []
    iterations    = 0
    final_answer  = ""
    detected_sym  = symbol

    while iterations < MAX_ITER:
        iterations += 1

        try:
            resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json"
                },
                json={
                    "model":       AGENT_MODEL,
                    "messages":    messages,
                    "tools":       GROQ_TOOLS,
                    "tool_choice": "auto",
                    "max_tokens":  2000,
                    "temperature": 0.1,
                },
                timeout=40,
            )

            if resp.status_code == 429:
                # Rate limited — return best answer we have so far
                if final_answer:
                    break
                return {
                    "success": False,
                    "error": "Groq rate limit hit. Try again in a moment (free tier: 30 req/min).",
                    "tools_called": tools_called,
                }

            resp.raise_for_status()
            data    = resp.json()
            choice  = data["choices"][0]
            message = choice["message"]

            # Add assistant turn to history
            messages.append(message)

            finish = choice.get("finish_reason", "")

            # ── DONE: no more tool calls ──────────────────────────────────
            if finish == "stop" or not message.get("tool_calls"):
                final_answer = message.get("content", "") or final_answer
                break

            # ── TOOL CALLS: execute each one ─────────────────────────────
            for tc in message.get("tool_calls", []):
                tool_name = tc["function"]["name"]
                try:
                    tool_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    tool_args = {}

                # Auto-detect symbol from tool calls
                sym_arg = tool_args.get("symbol", "")
                if sym_arg and not detected_sym:
                    detected_sym = sym_arg.upper().strip()

                # Execute the tool
                tool_fn = TOOL_REGISTRY.get(tool_name)
                if tool_fn:
                    tool_result = tool_fn(tool_args)
                else:
                    tool_result = {"error": f"Unknown tool: {tool_name}"}

                tools_called.append({
                    "tool":   tool_name,
                    "args":   tool_args,
                    "ok":     "error" not in tool_result,
                })

                # Feed result back to conversation
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      json.dumps(tool_result, default=str)[:3000],
                })

        except requests.exceptions.Timeout:
            return {
                "success": False,
                "error": "Groq timed out. Try a simpler question or try again.",
                "tools_called": tools_called,
            }
        except Exception as e:
            return {
                "success": False,
                "error":   f"Agent error: {str(e)[:200]}",
                "tools_called": tools_called,
            }

    if not final_answer:
        # Fallback: ask model to summarise with whatever data it has
        messages.append({
            "role": "user",
            "content": "Summarise everything you found above into a clear final answer now."
        })
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": AGENT_MODEL, "messages": messages, "max_tokens": 1200, "temperature": 0.1},
                timeout=30,
            )
            if resp.ok:
                final_answer = resp.json()["choices"][0]["message"].get("content", "")
        except Exception:
            pass

    result = {
        "success":     bool(final_answer),
        "answer":      final_answer or "Agent could not generate an answer. Please try again.",
        "symbol":      detected_sym or None,
        "tools_called": tools_called,
        "iterations":  iterations,
        "from_cache":  False,
    }

    # Cache successful answers for 10 minutes
    if result["success"]:
        _acache_set(cache_key, result, ttl=600)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES  (registered as a blueprint)
# ═════════════════════════════════════════════════════════════════════════════

@agent_bp.route("/api/agent-chat", methods=["POST"])
def agent_chat():
    """
    Main agentic chat endpoint.
    Replaces /api/chat when agent mode is on.
    """
    payload  = freq.get_json(silent=True) or {}
    question = str(payload.get("message", "")).strip()
    symbol   = str(payload.get("symbol",  "")).upper().strip()
    mode     = str(payload.get("mode",    "analyst")).lower()
    history  = payload.get("history", [])

    if not question:
        return jsonify({"success": False, "error": "Message is required."}), 400

    result = run_stock_agent(
        question=question,
        symbol=symbol,
        mode=mode,
        history=history,
    )
    return jsonify(result)


@agent_bp.route("/api/agent-research/<symbol>")
def agent_research(symbol):
    """
    Full deep-research report — agent fetches everything about a stock.
    Cached 30 minutes to save Groq quota.
    """
    sym = symbol.upper().strip()
    cached = _acache_get(f"research:{sym}")
    if cached:
        return jsonify(cached)

    deep_question = (
        f"Give me a complete research report on {sym} (NSE India). "
        f"Find: 1) Latest quarterly earnings and revenue, "
        f"2) Analyst price targets from major brokerages, "
        f"3) Recent news and corporate events, "
        f"4) Technical outlook (RSI, trend, key levels), "
        f"5) Fundamental health (PE, ROE, debt), "
        f"6) Key risks and red flags, "
        f"7) Your final verdict (BUY/HOLD/SELL) with reasoning. "
        f"Use ALL available tools. Be thorough."
    )

    result = run_stock_agent(question=deep_question, symbol=sym, mode="analyst")
    result["type"] = "deep_research"
    _acache_set(f"research:{sym}", result, ttl=1800)
    return jsonify(result)


@agent_bp.route("/api/agent-compare")
def agent_compare():
    """
    Compare two stocks using the agent.
    ?symbols=TCS,INFY
    """
    raw     = str(freq.args.get("symbols", "")).strip()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if len(symbols) < 2:
        return jsonify({"success": False, "error": "Pass ?symbols=SYM1,SYM2"}), 400

    s1, s2 = symbols[0], symbols[1]
    question = (
        f"Compare {s1} vs {s2} (both NSE India). "
        f"Fetch live data for BOTH stocks. "
        f"Cover: price + technicals + fundamentals + news + recent changes. "
        f"Tell me: which is stronger technically, which is better fundamentally, "
        f"which has better news sentiment, key risks in each, "
        f"which is better for short term vs long term, and your final verdict."
    )

    result = run_stock_agent(question=question, symbol="", mode="analyst")
    result["symbols"] = [s1, s2]
    return jsonify(result)


@agent_bp.route("/api/agent-status")
def agent_status():
    """Health check — shows if DuckDuckGo is installed and Groq key is set."""
    try:
        from duckduckgo_search import DDGS
        ddg_ok = True
    except ImportError:
        ddg_ok = False

    return jsonify({
        "success":      True,
        "groq_key_set": bool(GROQ_API_KEY and GROQ_API_KEY != "YOUR_GROQ_KEY_HERE"),
        "ddg_search":   ddg_ok,
        "ddg_fallback": "Google News RSS (free)",
        "model":        AGENT_MODEL,
        "cache_keys":   len(_agent_cache),
        "cost":         "$0.00 — 100% free stack",
    })
# === PASTE AT THE VERY BOTTOM OF agent.py ===

@agent_bp.route("/api/agent/5year/<symbol>")
def agent_5year_history(symbol):
    sym = symbol.upper().strip()
    question = (
        f"Act as a Senior Equity Analyst. Analyze the last 5 years of history for {sym}. "
        f"1. Overall 5-year performance (growth/decline). "
        f"2. Major structural changes, corporate actions, leadership changes, or product shifts. "
        f"3. Detailed reasons behind these changes and their impact on the stock price. "
        f"Format the output beautifully using Markdown with clear headings and bullet points."
    )
    # Uses your existing Groq + DuckDuckGo setup
    from agent import run_stock_agent
    result = run_stock_agent(question=question, symbol=sym, mode="analyst")
    return jsonify(result)
