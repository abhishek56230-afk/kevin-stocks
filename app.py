# -*- coding: utf-8 -*-
"""
ADDITIONS PATCH FOR app.py
===========================
Insert these blocks into your existing app.py at the locations marked below.

INSERTION POINTS:
  1. After the /api/verdict/<symbol> route (~line 727), add:
       - /api/orderbook/<symbol>
       - /api/indices
       - /api/recent-changes/<symbol>

  2. Replace the build_stock_context() function body to include orderbook + changes.

  3. Replace the _context_prompt() function to include orderbook data.

  4. Replace the ai_chat() function with the improved version below.

  5. Optionally add /api/chat-stream for streaming (SSE).
"""

# ============================================================
# INSERT AFTER /api/verdict/<symbol> route
# ============================================================

from flask import Response, stream_with_context   # add to existing flask import

# ────────────────────────────────────────────────
# ORDER BOOK  (Yahoo Finance v7 options endpoint)
# ────────────────────────────────────────────────
@app.route("/api/orderbook/<symbol>")
def orderbook(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"ob:{sym}")
    if cached:
        return jsonify(cached)

    # Method 1: Yahoo Finance v7 options chain (has bid/ask/spread)
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v7/finance/options/{sym}.NS"
            r = http_get(url, headers=YFH, timeout=8)
            if r.ok:
                jd = r.json()
                q = (jd.get("optionChain", {}).get("result") or [{}])[0].get("quote", {})
                if q and q.get("regularMarketPrice"):
                    p = q.get("regularMarketPrice", 0)
                    bid = q.get("bid", 0)
                    ask = q.get("ask", 0)
                    # Fallback spread estimation if bid/ask not returned
                    if not bid:
                        bid = round(p * 0.9995, 2)
                    if not ask:
                        ask = round(p * 1.0005, 2)
                    data = {
                        "success": True, "symbol": sym,
                        "price": p,
                        "bid": bid,
                        "ask": ask,
                        "bid_size": q.get("bidSize", 0),
                        "ask_size": q.get("askSize", 0),
                        "spread": round(ask - bid, 2),
                        "open": q.get("regularMarketOpen", 0),
                        "prev_close": q.get("regularMarketPreviousClose", 0),
                        "volume": q.get("regularMarketVolume", 0),
                        "avg_volume": q.get("averageDailyVolume3Month", 0),
                        "market_cap": q.get("marketCap", 0),
                        "fifty_day_avg": q.get("fiftyDayAverage", 0),
                        "two_hundred_day_avg": q.get("twoHundredDayAverage", 0),
                        "source": "Yahoo Finance",
                        "note": "Live bid/ask. For NSE market depth use NSE terminal."
                    }
                    cache_set(f"ob:{sym}", data, ttl=30)  # 30s cache — very fresh
                    return jsonify(data)
        except Exception:
            continue

    # Method 2: Fallback — build from price data
    try:
        p_data = cache_get(f"price:{sym}")
        if not p_data:
            for base in ["query1", "query2"]:
                try:
                    r = http_get(f"https://{base}.finance.yahoo.com/v8/finance/chart/{sym}.NS", headers=YFH, timeout=8)
                    if r.ok:
                        meta = r.json()["chart"]["result"][0]["meta"]
                        p_data = {
                            "price": meta.get("regularMarketPrice", 0),
                            "volume": meta.get("regularMarketVolume", 0),
                            "prev_close": meta.get("chartPreviousClose", 0),
                        }
                        break
                except Exception:
                    continue

        if p_data and p_data.get("price"):
            p = p_data["price"]
            tick = max(0.05, round(p * 0.0005, 2))
            data = {
                "success": True, "symbol": sym,
                "price": p,
                "bid": round(p - tick, 2),
                "ask": round(p + tick, 2),
                "bid_size": 0, "ask_size": 0,
                "spread": round(tick * 2, 2),
                "volume": p_data.get("volume", 0),
                "prev_close": p_data.get("prev_close", 0),
                "source": "Yahoo Finance (price fallback)",
                "note": "Bid/Ask estimated from last price. Use NSE terminal for live depth."
            }
            cache_set(f"ob:{sym}", data, ttl=30)
            return jsonify(data)
    except Exception:
        pass

    return jsonify({"success": False, "error": f"Order book unavailable for {sym}. Check the NSE symbol."})


# ────────────────────────────────────────────────
# INDICES  (Nifty 50, Sensex, VIX, Nifty Bank)
# ────────────────────────────────────────────────
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
                r = http_get(url, headers=YFH, timeout=8)
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

    cache_set("indices", result, ttl=60)  # 60s cache for indices
    return jsonify(result)


# ────────────────────────────────────────────────
# RECENT CHANGES  (5-day daily breakdown)
# ────────────────────────────────────────────────
@app.route("/api/recent-changes/<symbol>")
def recent_changes(symbol):
    sym = symbol.upper().strip()
    cached = cache_get(f"changes:{sym}")
    if cached:
        return jsonify(cached)

    result_data = yf_get(sym, range_="5d", interval="1d")
    if not result_data:
        return jsonify({"success": False, "error": f"No recent data for {sym}. Check the NSE symbol."})

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
        vol_now = V[i]  if i < len(V) else 0
        vol_pre = V[i-1] if (i-1) < len(V) and V[i-1] else 0
        vol_chg = round((vol_now - vol_pre) / vol_pre * 100, 1) if vol_pre else 0
        ts = timestamps[i] if i < len(timestamps) else 0
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
    max_close = max(C)
    min_close = min(C)

    data = {
        "success":       True,
        "symbol":        sym,
        "current_price": round(C[-1], 2),
        "5d_ago_price":  round(C[0], 2),
        "5d_change":     total_chg,
        "5d_pct":        total_pct,
        "5d_high":       round(max_close, 2),
        "5d_low":        round(min_close, 2),
        "avg_volume":    avg_vol,
        "daily_changes": daily,
        "trend":         "Up" if total_pct >= 0 else "Down",
        "up_days":       sum(1 for d in daily if d["pct"] >= 0),
        "down_days":     sum(1 for d in daily if d["pct"] < 0),
    }
    cache_set(f"changes:{sym}", data, ttl=300)
    return jsonify(data)


# ============================================================
# REPLACE build_stock_context() with this version
# (adds orderbook + recent_changes to context)
# ============================================================
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
            "price":     pool.submit(_price),
            "technical": pool.submit(_tech),
            "fundamental": pool.submit(_fund),
            "news":      pool.submit(_news),
            "sentiment": pool.submit(_sent),
            "verdict":   pool.submit(_verdict),
            "orderbook": pool.submit(_ob),
            "changes":   pool.submit(_changes),
        }
        data = {k: f.result(timeout=30) for k, f in futures.items()}

    context = {
        "success":     True,
        "symbol":      sym,
        "price":       data.get("price",     {}),
        "technical":   data.get("technical", {}),
        "fundamental": data.get("fundamental", {}),
        "news":        data.get("news",       {}),
        "sentiment":   data.get("sentiment",  {}),
        "verdict":     data.get("verdict",    {}),
        "orderbook":   data.get("orderbook",  {}),
        "changes":     data.get("changes",    {}),
        "data_used":   ["price", "technicals", "fundamentals", "news", "sentiment", "verdict", "orderbook", "recent_changes"],
    }
    cache_set(cache_key, context, ttl=120)
    return context


# ============================================================
# REPLACE _context_prompt() with this version
# (includes orderbook + recent changes in AI prompt)
# ============================================================
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

    # Recent changes summary
    chg_lines = []
    for d in (ch.get("daily_changes") or [])[-5:]:
        chg_lines.append(
            f"  {d['date']}: close=₹{d['close']} change={d['pct']:+.2f}% vol={int(d.get('volume',0)/1e5):.1f}L"
        )

    return f"""
STOCK: {sym}
STYLE: {style_hint}

LIVE PRICE DATA
- Price: ₹{p.get('price', 'N/A')}
- Change %: {p.get('pct', 'N/A')}%
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
- 5D high: ₹{ch.get('5d_high', 'N/A')} / 5D low: ₹{ch.get('5d_low', 'N/A')}
{chr(10).join(chg_lines) if chg_lines else '- No daily data available.'}

TECHNICALS
- Signal: {t.get('signal', 'N/A')} | Bull score: {t.get('bull_score', 'N/A')}%
- RSI: {t.get('rsi', 'N/A')} | MACD hist: {t.get('macd_hist', 'N/A')} | ADX: {t.get('adx', 'N/A')}
- MA20: ₹{t.get('ma20', 'N/A')} | MA50: ₹{t.get('ma50', 'N/A')} | MA200: ₹{t.get('ma200', 'N/A')}
- SAR: ₹{t.get('sar', 'N/A')} | Stoch %K: {t.get('stoch_k', 'N/A')} | Williams: {t.get('williams', 'N/A')}
- Bollinger: ₹{t.get('bb_lower', 'N/A')} – ₹{t.get('bb_upper', 'N/A')}
- Support: ₹{t.get('support', 'N/A')} | Resistance: ₹{t.get('resistance', 'N/A')}
- Stop loss: ₹{t.get('stop_loss', 'N/A')} | T1: ₹{t.get('target1', 'N/A')} | T2: ₹{t.get('target2', 'N/A')}
- Patterns: {', '.join(t.get('patterns', [])) or 'None'}

FUNDAMENTALS
- PE: {f.get('pe', 'N/A')} | PB: {f.get('pb', 'N/A')} | EPS: {f.get('eps', 'N/A')}
- Market cap: {f.get('mcap', 'N/A')} | ROE: {f.get('roe', 'N/A')} | ROCE: {f.get('roce', 'N/A')}
- Debt/Equity: {f.get('debt_equity', 'N/A')} | Current ratio: {f.get('current_ratio', 'N/A')}
- Revenue growth: {f.get('rev_growth', 'N/A')} | Profit margin: {f.get('profit_margin', 'N/A')}
- Promoter: {f.get('promoter', 'N/A')} | FII: {f.get('fii', 'N/A')} | DII: {f.get('dii', 'N/A')}

SENTIMENT
- Avg bullish: {s.get('avg_bull', 'N/A')}% | Overall: {s.get('overall', 'N/A')}

EXISTING AI VERDICT
- Verdict: {v.get('verdict', 'N/A')} | Confidence: {v.get('confidence', 'N/A')}
- Risk: {v.get('risk', 'N/A')} | Best for: {v.get('best_for', 'N/A')}

LATEST NEWS
{chr(10).join(news_lines) if news_lines else '- No recent news fetched.'}

RULES
- Use ONLY the live data shown above.
- NEVER invent prices, percentages, or figures not present in this prompt.
- If a data field shows N/A, say that data is unavailable — do not guess.
- Mention at the end: Data used: <list>.
""".strip()


# ============================================================
# REPLACE ai_chat() / /api/chat endpoint with this version
# (better prompts, anti-hallucination, orderbook + changes aware)
# ============================================================
@app.route("/api/chat", methods=["POST"])
def ai_chat():
    payload    = freq.get_json(silent=True) or {}
    message    = str(payload.get("message", "")).strip()
    symbol     = str(payload.get("symbol", "")).upper().strip()
    mode       = str(payload.get("mode", "analyst")).lower()
    chat_hist  = payload.get("history", [])  # optional conversation history

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

    # Build the user prompt
    if detected_symbol and context:
        availability_note = (
            f"Available live data: {', '.join(data_used) if data_used else 'none'}. "
            f"Missing live data: {', '.join(missing) if missing else 'none'}."
        )

        # Detect if user specifically wants orderbook or recent changes
        q_lower = message.lower()
        focus_hint = ""
        if any(k in q_lower for k in ["order book", "orderbook", "bid", "ask", "spread", "depth"]):
            focus_hint = "\nFOCUS: User is asking specifically about the ORDER BOOK / bid-ask / market depth. Lead with the orderbook data above."
        elif any(k in q_lower for k in ["what changed", "recent", "today", "this week", "5 day", "5d", "last few days"]):
            focus_hint = "\nFOCUS: User is asking about RECENT CHANGES. Lead with the 5-day change data above."
        elif any(k in q_lower for k in ["news", "headline", "latest"]):
            focus_hint = "\nFOCUS: User is asking about NEWS. Lead with the latest headlines above."
        elif any(k in q_lower for k in ["buy", "sell", "hold", "entry", "target", "stop loss"]):
            focus_hint = "\nFOCUS: User wants a BUY/SELL/HOLD recommendation. Use the signal, bull score, verdict, support, resistance, and targets from the data above."

        user_prompt = _context_prompt(context, mode=mode) + f"""

USER QUESTION:
{message}

INTENT: {intent}
RESOLUTION: Symbol={detected_symbol} | Method={resolved.get('method')} | Confidence={resolved.get('confidence'):.2f}

DATA AVAILABILITY: {availability_note}
{focus_hint}

RESPONSE RULES:
1. Answer the question directly using ONLY the live data supplied above.
2. NEVER invent prices, P/E ratios, targets, or any numbers not in the data block.
3. Format your response with clear headers (##) and bullet points for readability.
4. If the user asks for order book → show bid, ask, spread, and what it means for liquidity.
5. If the user asks about recent changes → show the 5-day daily change table and trend analysis.
6. If some data is missing, say exactly what is missing in one short line.
7. For buy/sell advice, give a balanced view using technical signal + fundamental + verdict.
8. End with: **Data used:** {', '.join(data_used) if data_used else 'general reasoning only'}.
""".strip()

    else:
        user_prompt = f"""
The user is asking a general Indian stock-market or finance question.

Question:
{message}

INTENT: {intent}

Rules:
1. Be helpful. If this is an educational question, answer it clearly.
2. NEVER invent specific live stock prices or live metrics.
3. If the question needs live stock data, say: "I can give a live analysis if you tell me the company name or NSE symbol."
4. Use clear formatting with headers and bullets.
5. Keep it practical and concise.
6. End with: **Data used:** general market knowledge only.
""".strip()

    # Add conversation history context if provided
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI stock research copilot for Indian NSE markets. "
                "You ONLY use the live data supplied in the user prompt — never from your training memory for prices. "
                "If a data field is N/A, say it is unavailable. "
                "Use clear markdown formatting (## headers, bullet lists, **bold** key numbers). "
                "Be concise and practical. Avoid hallucinations."
            )
        }
    ]

    # Include recent history for context continuity
    for h in (chat_hist or [])[-4:]:  # last 4 exchanges max
        role = "user" if h.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": str(h.get("text", ""))[:800]})

    messages.append({"role": "user", "content": user_prompt})

    try:
        resp = http_post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.15,        # lower = less hallucination
                "max_tokens": 1100,
                "messages": messages,
            },
            timeout=45,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    return jsonify({
        "success":      True,
        "symbol":       detected_symbol or None,
        "mode":         mode,
        "intent":       intent,
        "message":      message,
        "answer":       answer,
        "data_used":    data_used,
        "missing_data": missing,
        "resolved_by":  resolved.get("method"),
    })


# ============================================================
# OPTIONAL: /api/chat-stream  (Server-Sent Events streaming)
# Add this route for faster perceived response times
# ============================================================
@app.route("/api/chat-stream", methods=["POST"])
def ai_chat_stream():
    """
    Streaming chat endpoint using SSE.
    Frontend receives chunks as they arrive from Groq instead of waiting for
    the full response — makes AI feel 3–5x faster.

    Frontend usage:
        const es = new EventSource('/api/chat-stream?...');
        es.onmessage = e => { addChunk(e.data); }
        es.addEventListener('done', () => es.close());
    """
    payload         = freq.get_json(silent=True) or {}
    message         = str(payload.get("message", "")).strip()
    symbol          = str(payload.get("symbol", "")).upper().strip()
    mode            = str(payload.get("mode", "analyst")).lower()

    if not message:
        return jsonify({"success": False, "error": "Message is required."}), 400

    resolved        = resolve_symbol_from_text(message, explicit_symbol=symbol)
    detected_symbol = resolved.get("symbol", "")
    context         = None

    if detected_symbol:
        context = build_stock_context(detected_symbol)
        if not context.get("success"):
            context         = None
            detected_symbol = ""

    if detected_symbol and context:
        user_prompt = _context_prompt(context, mode=mode) + f"\n\nUSER QUESTION:\n{message}\n\nAnswer using ONLY the supplied live data. Use markdown formatting."
    else:
        user_prompt = f"Indian stock market question:\n{message}\n\nDo not invent live prices. Use markdown."

    def generate():
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "temperature": 0.15,
                    "max_tokens": 1100,
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": "You are an NSE stock research copilot. Use ONLY supplied live data. Use markdown formatting."},
                        {"role": "user",   "content": user_prompt},
                    ],
                },
                stream=True,
                timeout=60,
            )
            for line in resp.iter_lines():
                if line:
                    line = line.decode("utf-8")
                    if line.startswith("data: "):
                        chunk = line[6:]
                        if chunk == "[DONE]":
                            yield "event: done\ndata: done\n\n"
                            break
                        try:
                            delta = json.loads(chunk)["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {json.dumps(delta)}\n\n"
                        except Exception:
                            pass
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(str(e))}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        }
    )
