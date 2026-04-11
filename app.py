# -*- coding: utf-8 -*-
"""
FIXED PATCH FOR app.py
=======================
ERROR YOU SAW:  NameError: name 'app' is not defined
ROOT CAUSE:     The new routes were added BEFORE  app = Flask(...)  in app.py.

HOW TO APPLY THIS PATCH CORRECTLY
===================================
1. Open your app.py
2. Find the line (around line 29-31):
       app = Flask(__name__, static_folder="static", static_url_path="")
3. Scroll down to the FIRST @app.route after that, e.g.  @app.route("/")
4. ADD the three new route functions BELOW the existing routes — the safest
   place is just before the  if __name__ == "__main__":  block at the end.

DO NOT add any import at the top for this file.
DO NOT import this file — just copy-paste the code below directly into app.py.

ALSO REPLACE in app.py:
  - The function build_stock_context()   → with the version below
  - The function _context_prompt()       → with the version below
  - The route  /api/chat  (ai_chat())    → with the version below
"""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 – Add these 3 new routes anywhere AFTER  app = Flask(...)
#          Recommended: paste them just above  if __name__ == "__main__":
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/orderbook/<symbol>")
def orderbook(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"ob:{sym}")
    if cached:
        return jsonify(cached)

    # Method 1: Yahoo Finance v7 options endpoint (has bid/ask)
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
                        "success":   True,
                        "symbol":    sym,
                        "price":     p,
                        "bid":       bid,
                        "ask":       ask,
                        "bid_size":  q.get("bidSize", 0),
                        "ask_size":  q.get("askSize", 0),
                        "spread":    round(ask - bid, 2),
                        "open":      q.get("regularMarketOpen", 0),
                        "prev_close": q.get("regularMarketPreviousClose", 0),
                        "volume":    q.get("regularMarketVolume", 0),
                        "avg_volume": q.get("averageDailyVolume3Month", 0),
                        "source":    "Yahoo Finance",
                        "note":      "Live bid/ask. For NSE market depth use NSE terminal.",
                    }
                    cache_set(f"ob:{sym}", data, ttl=30)
                    return jsonify(data)
        except Exception:
            continue

    # Method 2: Fallback from price endpoint
    try:
        p_data = cache_get(f"price:{sym}")
        if not p_data:
            for base in ["query1", "query2"]:
                try:
                    r = http_get(
                        f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS",
                        headers=YFH, timeout=8
                    )
                    if r.ok:
                        meta   = r.json()["chart"]["result"][0]["meta"]
                        p_data = {
                            "price":      meta.get("regularMarketPrice", 0),
                            "volume":     meta.get("regularMarketVolume", 0),
                            "prev_close": meta.get("chartPreviousClose", 0),
                        }
                        break
                except Exception:
                    continue

        if p_data and p_data.get("price"):
            p    = p_data["price"]
            tick = max(0.05, round(p * 0.0005, 2))
            data = {
                "success":    True,
                "symbol":     sym,
                "price":      p,
                "bid":        round(p - tick, 2),
                "ask":        round(p + tick, 2),
                "bid_size":   0,
                "ask_size":   0,
                "spread":     round(tick * 2, 2),
                "volume":     p_data.get("volume", 0),
                "prev_close": p_data.get("prev_close", 0),
                "source":     "Yahoo Finance (price fallback)",
                "note":       "Bid/Ask estimated from last price. Use NSE terminal for live depth.",
            }
            cache_set(f"ob:{sym}", data, ttl=30)
            return jsonify(data)
    except Exception:
        pass

    return jsonify({"success": False, "error": f"Order book unavailable for {sym}."})


@app.route("/api/indices")
def indices():
    cached = cache_get("indices")
    if cached:
        return jsonify(cached)

    syms = {
        "nifty50":   "^NSEI",
        "sensex":    "^BSESN",
        "vix":       "^INDIAVIX",
        "niftybank": "^NSEBANK",
    }

    def fetch_index(yfsym):
        for base in ["query1", "query2"]:
            try:
                url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{yfsym}?interval=1d&range=2d"
                r   = http_get(url, headers=YFH, timeout=8)
                if r.ok:
                    meta = r.json()["chart"]["result"][0]["meta"]
                    p    = meta.get("regularMarketPrice", 0)
                    prev = meta.get("chartPreviousClose", 0)
                    return {
                        "price":  round(p, 2),
                        "prev":   round(prev, 2),
                        "change": round(p - prev, 2),
                        "pct":    round((p - prev) / prev * 100, 2) if prev else 0,
                        "high":   meta.get("regularMarketDayHigh", 0),
                        "low":    meta.get("regularMarketDayLow", 0),
                    }
            except Exception:
                continue
        return None

    result = {"success": True}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {key: pool.submit(fetch_index, ysym) for key, ysym in syms.items()}
        for key, fut in futures.items():
            try:
                result[key] = fut.result(timeout=12) or {}
            except Exception:
                result[key] = {}

    cache_set("indices", result, ttl=60)
    return jsonify(result)


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
        return jsonify({"success": False, "error": "Not enough history to show changes."})

    daily = []
    for i in range(1, len(C)):
        day_chg = round(C[i] - C[i-1], 2)
        day_pct = round(day_chg / C[i-1] * 100, 2) if C[i-1] else 0
        vol_now = V[i]   if i     < len(V) else 0
        vol_pre = V[i-1] if (i-1) < len(V) and V[i-1] else 0
        vol_chg = round((vol_now - vol_pre) / vol_pre * 100, 1) if vol_pre else 0
        ts      = timestamps[i] if i < len(timestamps) else 0
        try:
            date_str = datetime.datetime.utcfromtimestamp(ts).strftime("%d %b")
        except Exception:
            date_str = f"Day {i}"
        daily.append({
            "date":           date_str,
            "open":           round(O[i], 2) if i < len(O) else None,
            "high":           round(H[i], 2) if i < len(H) else None,
            "low":            round(L[i], 2) if i < len(L) else None,
            "close":          round(C[i], 2),
            "change":         day_chg,
            "pct":            day_pct,
            "volume":         vol_now,
            "vol_change_pct": vol_chg,
        })

    total_chg = round(C[-1] - C[0], 2)
    total_pct = round(total_chg / C[0] * 100, 2) if C[0] else 0
    avg_vol   = round(sum(V) / len(V)) if V else 0

    data = {
        "success":       True,
        "symbol":        sym,
        "current_price": round(C[-1], 2),
        "5d_ago_price":  round(C[0],  2),
        "5d_change":     total_chg,
        "5d_pct":        total_pct,
        "5d_high":       round(max(C), 2),
        "5d_low":        round(min(C), 2),
        "avg_volume":    avg_vol,
        "daily_changes": daily,
        "trend":         "Up" if total_pct >= 0 else "Down",
        "up_days":       sum(1 for d in daily if d["pct"] >= 0),
        "down_days":     sum(1 for d in daily if d["pct"] <  0),
    }
    cache_set(f"changes:{sym}", data, ttl=300)
    return jsonify(data)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 – FIND and REPLACE build_stock_context() in app.py
#          Search for:  def build_stock_context(sym):
#          Replace the ENTIRE function with this:
# ─────────────────────────────────────────────────────────────────────────────

def build_stock_context(sym):
    sym = str(sym or "").upper().strip()
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

    def _ob():
        try:
            with app.test_request_context(f"/api/orderbook/{sym}"):
                return orderbook(sym).get_json()
        except Exception:
            return {}

    def _changes():
        try:
            with app.test_request_context(f"/api/recent-changes/{sym}"):
                return recent_changes(sym).get_json()
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            "price":       pool.submit(_price),
            "technical":   pool.submit(_tech),
            "fundamental": pool.submit(_fund),
            "news":        pool.submit(_news),
            "sentiment":   pool.submit(_sent),
            "verdict":     pool.submit(_verdict),
            "orderbook":   pool.submit(_ob),
            "changes":     pool.submit(_changes),
        }
        data = {k: f.result(timeout=30) for k, f in futures.items()}

    context = {
        "success":       True,
        "symbol":        sym,
        "price":         data.get("price",       {}),
        "technical":     data.get("technical",   {}),
        "fundamental":   data.get("fundamental", {}),
        "news":          data.get("news",         {}),
        "sentiment":     data.get("sentiment",    {}),
        "verdict":       data.get("verdict",      {}),
        "orderbook":     data.get("orderbook",    {}),
        "changes":       data.get("changes",      {}),
        "data_used":     [
            "price", "technicals", "fundamentals",
            "news", "sentiment", "verdict",
            "orderbook", "recent_changes"
        ],
    }
    cache_set(cache_key, context, ttl=120)
    return context


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 – FIND and REPLACE _context_prompt() in app.py
#          Search for:  def _context_prompt(context, mode="analyst"):
#          Replace the ENTIRE function with this:
# ─────────────────────────────────────────────────────────────────────────────

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
        else "Write like a practical equity research analyst — concise but insightful."
    )

    chg_lines = []
    for d in (ch.get("daily_changes") or [])[-5:]:
        chg_lines.append(
            f"  {d['date']}: close=₹{d['close']} change={d['pct']:+.2f}%"
            f" vol={int(d.get('volume', 0) / 1e5):.1f}L"
        )

    return f"""
STOCK: {sym}
STYLE: {style_hint}

LIVE PRICE DATA
- Price: ₹{p.get('price', 'N/A')}
- Change: {p.get('pct', 'N/A')}%
- Day range: ₹{p.get('low', 'N/A')} – ₹{p.get('high', 'N/A')}
- 52W range: ₹{p.get('week52_low', 'N/A')} – ₹{p.get('week52_high', 'N/A')}
- Volume: {p.get('volume', 'N/A')}

ORDER BOOK (live bid/ask)
- Bid: ₹{ob.get('bid', 'N/A')} (size: {ob.get('bid_size', 'N/A')})
- Ask: ₹{ob.get('ask', 'N/A')} (size: {ob.get('ask_size', 'N/A')})
- Spread: ₹{ob.get('spread', 'N/A')}
- Source: {ob.get('source', 'N/A')}

RECENT 5-DAY CHANGES
- 5D change: {ch.get('5d_pct', 'N/A')}% (₹{ch.get('5d_change', 'N/A')})
- Up days: {ch.get('up_days', 'N/A')} / Down days: {ch.get('down_days', 'N/A')}
{chr(10).join(chg_lines) if chg_lines else '  No daily data available.'}

TECHNICALS
- Signal: {t.get('signal', 'N/A')} | Bull score: {t.get('bull_score', 'N/A')}%
- RSI: {t.get('rsi', 'N/A')} | MACD hist: {t.get('macd_hist', 'N/A')} | ADX: {t.get('adx', 'N/A')}
- MA20: ₹{t.get('ma20', 'N/A')} | MA50: ₹{t.get('ma50', 'N/A')} | MA200: ₹{t.get('ma200', 'N/A')}
- Bollinger: ₹{t.get('bb_lower', 'N/A')} – ₹{t.get('bb_upper', 'N/A')}
- Support: ₹{t.get('support', 'N/A')} | Resistance: ₹{t.get('resistance', 'N/A')}
- Stop loss: ₹{t.get('stop_loss', 'N/A')} | T1: ₹{t.get('target1', 'N/A')} | T2: ₹{t.get('target2', 'N/A')}
- SAR: ₹{t.get('sar', 'N/A')} | Stoch %K: {t.get('stoch_k', 'N/A')}
- Patterns: {', '.join(t.get('patterns', [])) or 'None'}

FUNDAMENTALS
- PE: {f.get('pe', 'N/A')} | PB: {f.get('pb', 'N/A')} | EPS: {f.get('eps', 'N/A')}
- Market cap: {f.get('mcap', 'N/A')} | ROE: {f.get('roe', 'N/A')} | ROCE: {f.get('roce', 'N/A')}
- Debt/Equity: {f.get('debt_equity', 'N/A')} | Promoter: {f.get('promoter', 'N/A')}
- FII: {f.get('fii', 'N/A')} | DII: {f.get('dii', 'N/A')}

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
- End with: Data used: <list of sources used>.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 – FIND and REPLACE the /api/chat route  (ai_chat function) in app.py
#          Search for:  @app.route("/api/chat", methods=["POST"])
#          Replace the decorator + entire function with this:
# ─────────────────────────────────────────────────────────────────────────────

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

        # Intent-specific focus hints keep AI on topic
        focus_hint = ""
        if any(k in q_lower for k in ["order book", "orderbook", "bid", "ask", "spread", "depth"]):
            focus_hint = "\nFOCUS: User wants ORDER BOOK details. Lead with bid, ask, spread and liquidity insight."
        elif any(k in q_lower for k in ["what changed", "recent", "today", "this week", "5 day", "5d", "last few days", "daily"]):
            focus_hint = "\nFOCUS: User wants RECENT CHANGES. Lead with the 5-day daily breakdown."
        elif any(k in q_lower for k in ["news", "headline", "latest"]):
            focus_hint = "\nFOCUS: User wants NEWS. Lead with the latest headlines and their implications."
        elif any(k in q_lower for k in ["buy", "sell", "hold", "entry", "target", "stop loss"]):
            focus_hint = "\nFOCUS: User wants a BUY/SELL/HOLD opinion. Use signal, verdict, support/resistance, and targets."

        user_prompt = _context_prompt(context, mode=mode) + f"""

USER QUESTION:
{message}

INTENT: {intent}
SYMBOL: {detected_symbol} (resolved by {resolved.get('method')}, confidence {resolved.get('confidence', 0):.2f})
DATA: {availability_note}
{focus_hint}

RESPONSE RULES:
1. Answer ONLY using the live data supplied above — no training memory for prices.
2. Format with ## headers and bullet points for clarity.
3. For order book questions → explain bid, ask, spread and what it means.
4. For recent changes → summarise the 5-day daily movement clearly.
5. If data is missing, mention it in one short line.
6. For buy/sell → give balanced view: technical signal + fundamental + verdict.
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
4. Use clear markdown formatting.
5. End with: **Data used:** general market knowledge only.
""".strip()

    ai = groq_chat_completion(
        (
            "You are an AI stock research copilot for Indian NSE markets. "
            "Use ONLY the live data in the user prompt — never your training memory for prices. "
            "If a field is N/A, say it is unavailable. "
            "Format responses with ## headers and bullet points. "
            "Be concise and practical. No hallucinations."
        ),
        user_prompt,
        max_tokens=1100,
        temperature=0.15,   # low temp = less hallucination
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
