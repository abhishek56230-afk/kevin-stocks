# -*- coding: utf-8 -*-
"""
access_logger.py  —  Complete Visitor & Access Logging for Kevin Kataria Stock Intelligence
============================================================================================
Tracks:
  - Every HTTP request: IP, timestamp, method, path, status, response_time_ms
  - User agent (browser, device, OS)
  - Referrer source
  - Search queries performed
  - Errors and failed API calls
  - Country (from IP, no external API needed — uses ip-api.com free tier)

Storage: SQLite (works on Render free tier, no Redis/Postgres needed)
         File: logs/access.db

Admin endpoint: GET /api/admin/logs?secret=<ADMIN_SECRET>&limit=100&offset=0
Search logs:    GET /api/admin/logs?secret=<ADMIN_SECRET>&type=search
Error logs:     GET /api/admin/logs?secret=<ADMIN_SECRET>&type=errors
Stats:          GET /api/admin/stats?secret=<ADMIN_SECRET>

Set env var: ADMIN_SECRET=your_secret_here
"""

import sqlite3, os, time, threading, json, re
from datetime import datetime, timedelta
from flask import request as freq, g
from functools import wraps

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH      = os.path.join(os.path.dirname(__file__), "logs", "access.db")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "kevin_admin_2025")
_db_lock     = threading.Lock()
_ip_cache    = {}  # simple in-memory geo cache

# ── Init DB ───────────────────────────────────────────────────────────────────
def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Access log - every request
        c.execute("""
            CREATE TABLE IF NOT EXISTS access_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                ts_unix     REAL    NOT NULL,
                ip          TEXT,
                country     TEXT,
                method      TEXT,
                path        TEXT,
                query       TEXT,
                status      INTEGER,
                resp_ms     INTEGER,
                user_agent  TEXT,
                browser     TEXT,
                device      TEXT,
                os          TEXT,
                referrer    TEXT,
                is_bot      INTEGER DEFAULT 0
            )
        """)
        # Search log - every search query
        c.execute("""
            CREATE TABLE IF NOT EXISTS search_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                ts_unix     REAL    NOT NULL,
                ip          TEXT,
                query       TEXT,
                results_ct  INTEGER,
                result_syms TEXT
            )
        """)
        # Error log - failed API calls and exceptions
        c.execute("""
            CREATE TABLE IF NOT EXISTS error_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                ts_unix     REAL    NOT NULL,
                ip          TEXT,
                path        TEXT,
                status      INTEGER,
                error_msg   TEXT,
                stack       TEXT
            )
        """)
        # Daily summary (auto-computed, for fast stats)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                date        TEXT    PRIMARY KEY,
                hits        INTEGER DEFAULT 0,
                unique_ips  TEXT    DEFAULT '{}',
                top_paths   TEXT    DEFAULT '{}',
                top_queries TEXT    DEFAULT '{}'
            )
        """)
        # Indexes for performance
        c.execute("CREATE INDEX IF NOT EXISTS idx_access_ts    ON access_log(ts_unix)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_access_ip    ON access_log(ip)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_access_path  ON access_log(path)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_query ON search_log(query)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_error_ts     ON error_log(ts_unix)")
        conn.commit()
        conn.close()

# ── UA Parser (no external lib needed) ───────────────────────────────────────
def parse_ua(ua_string):
    ua = ua_string or ""
    ua_lower = ua.lower()

    # Bot detection
    bots = ["bot", "crawler", "spider", "scraper", "python-requests", "curl", "wget",
            "postman", "insomnia", "httpx", "httpie", "go-http", "java/", "okhttp"]
    is_bot = any(b in ua_lower for b in bots)

    # Browser
    if "edg/" in ua_lower:       browser = "Edge"
    elif "chrome/" in ua_lower:  browser = "Chrome"
    elif "firefox/" in ua_lower: browser = "Firefox"
    elif "safari/" in ua_lower and "chrome" not in ua_lower: browser = "Safari"
    elif "opera" in ua_lower:    browser = "Opera"
    elif "msie" in ua_lower or "trident" in ua_lower: browser = "IE"
    else:                        browser = "Other"

    # OS
    if "windows" in ua_lower:    os_name = "Windows"
    elif "mac os" in ua_lower:   os_name = "macOS"
    elif "android" in ua_lower:  os_name = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower: os_name = "iOS"
    elif "linux" in ua_lower:    os_name = "Linux"
    else:                        os_name = "Other"

    # Device
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        device = "Mobile"
    elif "tablet" in ua_lower or "ipad" in ua_lower:
        device = "Tablet"
    else:
        device = "Desktop"

    return browser, device, os_name, is_bot

# ── IP Geo (uses ip-api.com free tier — 45 req/min, cached) ──────────────────
def get_country(ip):
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "Local"
    if ip in _ip_cache:
        return _ip_cache[ip]
    try:
        import requests as req
        r = req.get(f"http://ip-api.com/json/{ip}?fields=country,countryCode",
                    timeout=3)
        if r.ok:
            data = r.json()
            country = f"{data.get('country', '?')} ({data.get('countryCode', '?')})"
            _ip_cache[ip] = country
            return country
    except Exception:
        pass
    return "Unknown"

# ── Core Log Writers ─────────────────────────────────────────────────────────
def _log_access(ip, path, query, status, resp_ms, ua, referrer):
    """Write one row to access_log. Non-blocking via thread."""
    def _write():
        ts_now  = datetime.utcnow()
        ts_str  = ts_now.strftime("%Y-%m-%d %H:%M:%S")
        ts_unix = time.time()
        browser, device, os_name, is_bot = parse_ua(ua)
        country = get_country(ip)
        try:
            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO access_log
                    (ts, ts_unix, ip, country, method, path, query, status,
                     resp_ms, user_agent, browser, device, os, referrer, is_bot)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (ts_str, ts_unix, ip, country, freq.method if freq else "?",
                      path, query, status, resp_ms, ua, browser, device,
                      os_name, referrer, int(is_bot)))
                # Keep only last 50k rows to avoid disk bloat on free tier
                conn.execute("""
                    DELETE FROM access_log WHERE id NOT IN
                    (SELECT id FROM access_log ORDER BY id DESC LIMIT 50000)
                """)
                conn.commit()
                conn.close()
        except Exception:
            pass
    threading.Thread(target=_write, daemon=True).start()


def log_search(ip, query, results_count, result_symbols):
    """Log a user search query."""
    def _write():
        ts_str  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        ts_unix = time.time()
        syms_json = json.dumps(result_symbols[:10]) if result_symbols else "[]"
        try:
            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO search_log (ts, ts_unix, ip, query, results_ct, result_syms)
                    VALUES (?,?,?,?,?,?)
                """, (ts_str, ts_unix, ip, query, results_count, syms_json))
                conn.execute("""
                    DELETE FROM search_log WHERE id NOT IN
                    (SELECT id FROM search_log ORDER BY id DESC LIMIT 20000)
                """)
                conn.commit()
                conn.close()
        except Exception:
            pass
    threading.Thread(target=_write, daemon=True).start()


def log_error(ip, path, status, error_msg, stack=""):
    """Log an API error."""
    def _write():
        ts_str  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        ts_unix = time.time()
        try:
            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO error_log (ts, ts_unix, ip, path, status, error_msg, stack)
                    VALUES (?,?,?,?,?,?,?)
                """, (ts_str, ts_unix, ip, path, status,
                      str(error_msg)[:500], str(stack)[:1000]))
                conn.execute("""
                    DELETE FROM error_log WHERE id NOT IN
                    (SELECT id FROM error_log ORDER BY id DESC LIMIT 10000)
                """)
                conn.commit()
                conn.close()
        except Exception:
            pass
    threading.Thread(target=_write, daemon=True).start()

# ── Flask Middleware ──────────────────────────────────────────────────────────
def register_logging(app):
    """
    Call this ONCE after app = Flask(...):
        from access_logger import register_logging
        register_logging(app)
    """
    init_db()

    # Paths we DON'T log (static assets etc.)
    SKIP_PATHS = {"/favicon.ico", "/static", "/_next", "/health"}

    @app.before_request
    def before():
        g._start_time = time.time()

    @app.after_request
    def after(response):
        try:
            path = freq.path
            if any(path.startswith(s) for s in SKIP_PATHS):
                return response
            resp_ms  = int((time.time() - getattr(g, "_start_time", time.time())) * 1000)
            ip       = (freq.headers.get("X-Forwarded-For") or freq.remote_addr or "").split(",")[0].strip()
            ua       = freq.headers.get("User-Agent", "")
            referrer = freq.headers.get("Referer", "")
            query    = freq.query_string.decode("utf-8", errors="replace")[:200]
            status   = response.status_code
            _log_access(ip, path, query, status, resp_ms, ua, referrer)
            # Log errors
            if status >= 400:
                log_error(ip, path, status, f"HTTP {status}", "")
        except Exception:
            pass
        return response

    # Register admin routes
    _register_admin_routes(app)
    return app


def _register_admin_routes(app):
    from flask import jsonify, request, render_template_string
    import json

    def check_secret():
        secret = request.args.get("secret") or request.headers.get("X-Admin-Secret")
        return secret == ADMIN_SECRET

    @app.route("/api/admin/logs")
    def admin_logs():
        if not check_secret():
            return jsonify({"error": "Unauthorized. Pass ?secret=YOUR_SECRET"}), 401

        log_type = request.args.get("type", "access")
        limit    = min(int(request.args.get("limit",  100)), 1000)
        offset   = int(request.args.get("offset", 0))
        ip_filter  = request.args.get("ip")
        date_filter = request.args.get("date")  # YYYY-MM-DD

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        if log_type == "search":
            q = "SELECT * FROM search_log"
            params = []
            if ip_filter:
                q += " WHERE ip = ?"; params.append(ip_filter)
            if date_filter:
                sep = " AND " if ip_filter else " WHERE "
                q += sep + "ts LIKE ?"; params.append(date_filter + "%")
            q += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params += [limit, offset]
            rows = conn.execute(q, params).fetchall()
            result = [dict(r) for r in rows]

        elif log_type == "errors":
            q = "SELECT * FROM error_log ORDER BY id DESC LIMIT ? OFFSET ?"
            rows = conn.execute(q, [limit, offset]).fetchall()
            result = [dict(r) for r in rows]

        else:  # access
            q = "SELECT id,ts,ip,country,method,path,query,status,resp_ms,browser,device,os,referrer,is_bot FROM access_log"
            params = []
            where = []
            if ip_filter:  where.append(("ip = ?", ip_filter))
            if date_filter: where.append(("ts LIKE ?", date_filter + "%"))
            if where:
                q += " WHERE " + " AND ".join(w[0] for w in where)
                params = [w[1] for w in where]
            q += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params += [limit, offset]
            rows = conn.execute(q, params).fetchall()
            result = [dict(r) for r in rows]

        total_q = {"access": "SELECT COUNT(*) FROM access_log",
                   "search": "SELECT COUNT(*) FROM search_log",
                   "errors": "SELECT COUNT(*) FROM error_log"}.get(log_type, "SELECT 0")
        total = conn.execute(total_q).fetchone()[0]
        conn.close()

        return jsonify({
            "success": True,
            "type": log_type,
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": result
        })

    @app.route("/api/admin/stats")
    def admin_stats():
        if not check_secret():
            return jsonify({"error": "Unauthorized"}), 401

        conn = sqlite3.connect(DB_PATH)
        now = time.time()

        def q(sql, params=()):
            return conn.execute(sql, params).fetchone()

        total_hits     = q("SELECT COUNT(*) FROM access_log")[0]
        today_str      = datetime.utcnow().strftime("%Y-%m-%d")
        today_hits     = q("SELECT COUNT(*) FROM access_log WHERE ts LIKE ?", (today_str+"%",))[0]
        unique_ips_all = q("SELECT COUNT(DISTINCT ip) FROM access_log")[0]
        unique_ips_day = q("SELECT COUNT(DISTINCT ip) FROM access_log WHERE ts LIKE ?", (today_str+"%",))[0]
        total_searches = q("SELECT COUNT(*) FROM search_log")[0]
        total_errors   = q("SELECT COUNT(*) FROM error_log")[0]
        avg_resp       = q("SELECT AVG(resp_ms) FROM access_log WHERE ts LIKE ?", (today_str+"%",))[0]

        # Top paths today
        top_paths = conn.execute("""
            SELECT path, COUNT(*) as hits FROM access_log WHERE ts LIKE ?
            GROUP BY path ORDER BY hits DESC LIMIT 10
        """, (today_str+"%",)).fetchall()

        # Top IPs today
        top_ips = conn.execute("""
            SELECT ip, country, COUNT(*) as hits FROM access_log WHERE ts LIKE ?
            GROUP BY ip ORDER BY hits DESC LIMIT 15
        """, (today_str+"%",)).fetchall()

        # Top search queries
        top_searches = conn.execute("""
            SELECT query, COUNT(*) as ct FROM search_log
            GROUP BY query ORDER BY ct DESC LIMIT 20
        """).fetchall()

        # Hourly traffic today
        hourly = conn.execute("""
            SELECT substr(ts,12,2) as hour, COUNT(*) as hits
            FROM access_log WHERE ts LIKE ?
            GROUP BY hour ORDER BY hour
        """, (today_str+"%",)).fetchall()

        # Country breakdown
        countries = conn.execute("""
            SELECT country, COUNT(*) as hits FROM access_log
            WHERE ts LIKE ?
            GROUP BY country ORDER BY hits DESC LIMIT 10
        """, (today_str+"%",)).fetchall()

        # Browser breakdown
        browsers = conn.execute("""
            SELECT browser, COUNT(*) as hits FROM access_log WHERE ts LIKE ?
            GROUP BY browser ORDER BY hits DESC
        """, (today_str+"%",)).fetchall()

        # Device breakdown
        devices = conn.execute("""
            SELECT device, COUNT(*) as hits FROM access_log WHERE ts LIKE ?
            GROUP BY device ORDER BY hits DESC
        """, (today_str+"%",)).fetchall()

        # Recent errors
        recent_errors = conn.execute("""
            SELECT ts, ip, path, status, error_msg FROM error_log
            ORDER BY id DESC LIMIT 20
        """).fetchall()

        conn.close()

        return jsonify({
            "success": True,
            "overview": {
                "total_hits_ever": total_hits,
                "hits_today": today_hits,
                "unique_ips_ever": unique_ips_all,
                "unique_ips_today": unique_ips_day,
                "total_searches": total_searches,
                "total_errors": total_errors,
                "avg_response_ms_today": round(avg_resp or 0, 1)
            },
            "top_paths_today":   [{"path": r[0], "hits": r[1]} for r in top_paths],
            "top_ips_today":     [{"ip": r[0], "country": r[1], "hits": r[2]} for r in top_ips],
            "top_searches":      [{"query": r[0], "count": r[1]} for r in top_searches],
            "hourly_today":      [{"hour": r[0]+"h", "hits": r[1]} for r in hourly],
            "countries_today":   [{"country": r[0], "hits": r[1]} for r in countries],
            "browsers_today":    [{"browser": r[0], "hits": r[1]} for r in browsers],
            "devices_today":     [{"device": r[0], "hits": r[1]} for r in devices],
            "recent_errors":     [{"ts": r[0], "ip": r[1], "path": r[2], "status": r[3], "msg": r[4]} for r in recent_errors],
        })

    @app.route("/admin")
    @app.route("/admin/dashboard")
    def admin_dashboard():
        """Beautiful HTML admin dashboard — no React/JS frameworks needed."""
        secret = request.args.get("secret", "")
        if secret != ADMIN_SECRET:
            return """
<!DOCTYPE html><html><head><title>Admin Login</title>
<style>*{box-sizing:border-box;margin:0;padding:0}
body{background:#060d1a;display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui}
.box{background:#0c1628;border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:40px;width:360px;text-align:center}
h2{color:#38bdf8;margin-bottom:24px;font-size:22px}
input{width:100%;padding:12px 16px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);
  border-radius:10px;color:#e8f0ff;font-size:14px;margin-bottom:16px;outline:none}
button{width:100%;padding:12px;background:linear-gradient(135deg,#38bdf8,#2dd4bf);border:none;
  border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;color:#060d1a}
</style></head><body>
<div class="box"><h2>Admin Access</h2>
<form onsubmit="go(event)">
<input type="password" id="s" placeholder="Enter admin secret" autofocus>
<button type="submit">Enter Dashboard</button>
</form></div>
<script>function go(e){e.preventDefault();
  window.location='/admin/dashboard?secret='+document.getElementById('s').value}</script>
</body></html>""", 200

        return f"""
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Dashboard — Stock Intelligence Pro</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#060d1a;color:#e8f0ff;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}}
.header{{background:#0c1628;border-bottom:1px solid rgba(255,255,255,.08);padding:16px 24px;
  display:flex;align-items:center;justify-content:space-between}}
.header h1{{font-size:18px;font-weight:700;color:#38bdf8}}
.header span{{font-size:12px;color:#5a7090}}
.shell{{max-width:1400px;margin:0 auto;padding:24px}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:20px}}
.card{{background:#0c1628;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:18px}}
.stat-val{{font-size:28px;font-weight:700;font-family:monospace;color:#38bdf8;margin:6px 0}}
.stat-label{{font-size:11px;color:#5a7090;font-weight:600;text-transform:uppercase;letter-spacing:.08em}}
.section-title{{font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:12px;text-transform:uppercase;letter-spacing:.08em}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{color:#5a7090;font-weight:600;text-transform:uppercase;letter-spacing:.06em;padding:6px 10px;
  border-bottom:1px solid rgba(255,255,255,.06);text-align:left}}
td{{padding:7px 10px;border-bottom:1px solid rgba(255,255,255,.04);color:#b8c8e8}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.badge{{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;font-weight:700}}
.badge-ok{{background:rgba(16,185,129,.15);color:#10b981}}
.badge-err{{background:rgba(248,113,113,.15);color:#f87171}}
.bar-wrap{{height:6px;background:rgba(255,255,255,.06);border-radius:99px;overflow:hidden;margin-top:4px}}
.bar-fill{{height:100%;background:linear-gradient(90deg,#38bdf8,#2dd4bf);border-radius:99px}}
.refresh-btn{{background:rgba(56,189,248,.1);border:1px solid rgba(56,189,248,.25);color:#38bdf8;
  padding:7px 16px;border-radius:8px;font-size:12px;cursor:pointer;font-weight:600}}
.refresh-btn:hover{{background:rgba(56,189,248,.2)}}
.tabs{{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}}
.tab{{padding:7px 16px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid rgba(255,255,255,.08);background:transparent;color:#5a7090;transition:all .15s}}
.tab.active,.tab:hover{{background:rgba(56,189,248,.1);color:#38bdf8;border-color:rgba(56,189,248,.3)}}
#raw-table{{max-height:400px;overflow-y:auto;display:none}}
</style></head><body>
<div class="header">
  <h1>📊 Stock Intelligence Pro — Admin Dashboard</h1>
  <div style="display:flex;gap:10px;align-items:center">
    <span id="clock"></span>
    <button class="refresh-btn" onclick="loadStats()">↺ Refresh</button>
  </div>
</div>
<div class="shell">
  <div id="stats-container">
    <div style="text-align:center;padding:60px;color:#5a7090">Loading stats…</div>
  </div>
  <div class="card" style="margin-top:20px">
    <div class="section-title">Raw Log Viewer</div>
    <div class="tabs">
      <button class="tab active" onclick="loadLogs('access',this)">Access Logs</button>
      <button class="tab" onclick="loadLogs('search',this)">Search Queries</button>
      <button class="tab" onclick="loadLogs('errors',this)">Errors</button>
    </div>
    <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <input id="ip-filter" placeholder="Filter by IP…" style="background:rgba(255,255,255,.05);
        border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:7px 12px;color:#e8f0ff;
        font-size:12px;width:180px;outline:none">
      <input id="date-filter" type="date" style="background:rgba(255,255,255,.05);
        border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:7px 12px;color:#e8f0ff;
        font-size:12px;outline:none">
      <button class="refresh-btn" onclick="applyFilter()">Filter</button>
      <button class="refresh-btn" onclick="exportCSV()">⬇ Export CSV</button>
    </div>
    <div id="log-table" style="overflow-x:auto;max-height:450px;overflow-y:auto"></div>
  </div>
</div>
<script>
const SECRET = '{secret}';
let currentType = 'access';
let currentData = [];

async function loadStats() {{
  const r = await fetch('/api/admin/stats?secret='+SECRET);
  const d = await r.json();
  if (!d.success) return;
  const o = d.overview;
  document.getElementById('stats-container').innerHTML = `
    <div class="grid4">
      <div class="card"><div class="stat-label">Total Hits (ever)</div><div class="stat-val">${{o.total_hits_ever.toLocaleString()}}</div></div>
      <div class="card"><div class="stat-label">Hits Today</div><div class="stat-val" style="color:#2dd4bf">${{o.hits_today.toLocaleString()}}</div></div>
      <div class="card"><div class="stat-label">Unique IPs Today</div><div class="stat-val" style="color:#a78bfa">${{o.unique_ips_today.toLocaleString()}}</div></div>
      <div class="card"><div class="stat-label">Avg Response (ms)</div><div class="stat-val" style="color:#fbbf24">${{o.avg_response_ms_today}}</div></div>
    </div>
    <div class="grid4">
      <div class="card"><div class="stat-label">Total Searches</div><div class="stat-val">${{o.total_searches.toLocaleString()}}</div></div>
      <div class="card"><div class="stat-label">Total Errors</div><div class="stat-val" style="color:#f87171">${{o.total_errors.toLocaleString()}}</div></div>
      <div class="card"><div class="stat-label">Unique IPs (ever)</div><div class="stat-val">${{o.unique_ips_ever.toLocaleString()}}</div></div>
      <div class="card"><div class="stat-label">DB Status</div><div class="stat-val" style="color:#10b981;font-size:20px">LIVE</div></div>
    </div>
    <div class="grid3">
      <div class="card">
        <div class="section-title">Top Pages Today</div>
        <table><thead><tr><th>Path</th><th>Hits</th></tr></thead><tbody>
        ${{d.top_paths_today.map(r=>`<tr><td style="font-family:monospace;color:#38bdf8">${{r.path}}</td><td>${{r.hits}}</td></tr>`).join('')}}
        </tbody></table>
      </div>
      <div class="card">
        <div class="section-title">Countries Today</div>
        ${{d.countries_today.map(r=>`
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px">
          <span>${{r.country||'Unknown'}}</span><span style="color:#38bdf8;font-weight:700">${{r.hits}}</span>
        </div>
        <div class="bar-wrap"><div class="bar-fill" style="width:${{Math.min(100,r.hits/${{d.countries_today[0]?.hits||1}}*100)}}%"></div></div>`).join('')}}
      </div>
      <div class="card">
        <div class="section-title">Devices Today</div>
        ${{d.devices_today.map(r=>`<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:8px">
          <span>${{r.device}}</span><span style="color:#2dd4bf;font-weight:700">${{r.hits}}</span>
        </div>`).join('')}}
        <div class="section-title" style="margin-top:16px">Browsers Today</div>
        ${{d.browsers_today.map(r=>`<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:8px">
          <span>${{r.browser}}</span><span style="color:#a78bfa;font-weight:700">${{r.hits}}</span>
        </div>`).join('')}}
      </div>
    </div>
    <div class="grid2">
      <div class="card">
        <div class="section-title">Top Search Queries</div>
        <table><thead><tr><th>Query</th><th>Count</th></tr></thead><tbody>
        ${{d.top_searches.map(r=>`<tr><td style="color:#fbbf24">${{r.query}}</td><td>${{r.count}}</td></tr>`).join('')}}
        </tbody></table>
      </div>
      <div class="card">
        <div class="section-title">Top IPs Today</div>
        <table><thead><tr><th>IP</th><th>Country</th><th>Hits</th></tr></thead><tbody>
        ${{d.top_ips_today.map(r=>`<tr><td style="font-family:monospace">${{r.ip}}</td><td style="color:#5a7090">${{r.country||'?'}}</td><td style="color:#38bdf8;font-weight:700">${{r.hits}}</td></tr>`).join('')}}
        </tbody></table>
      </div>
    </div>
    <div class="card">
      <div class="section-title">Recent Errors</div>
      <table><thead><tr><th>Time</th><th>IP</th><th>Path</th><th>Status</th><th>Message</th></tr></thead><tbody>
      ${{d.recent_errors.map(r=>`<tr>
        <td style="color:#5a7090;white-space:nowrap">${{r.ts}}</td>
        <td style="font-family:monospace">${{r.ip||'?'}}</td>
        <td style="color:#38bdf8;font-family:monospace">${{r.path}}</td>
        <td><span class="badge ${{r.status>=500?'badge-err':'badge-ok'}}">${{r.status}}</span></td>
        <td style="color:#f87171">${{r.msg}}</td>
      </tr>`).join('')}}
      </tbody></table>
    </div>
  `;
}}

async function loadLogs(type, btn) {{
  currentType = type;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  if(btn) btn.classList.add('active');
  await applyFilter();
}}

async function applyFilter() {{
  const ip = document.getElementById('ip-filter').value;
  const dt = document.getElementById('date-filter').value;
  let url = `/api/admin/logs?secret=${{SECRET}}&type=${{currentType}}&limit=200`;
  if(ip) url += `&ip=${{ip}}`;
  if(dt) url += `&date=${{dt}}`;
  const r = await fetch(url);
  const d = await r.json();
  currentData = d.rows || [];
  renderTable(currentData, currentType);
}}

function renderTable(rows, type) {{
  let html = '';
  if (type === 'access') {{
    html = `<table><thead><tr><th>#</th><th>Time</th><th>IP</th><th>Country</th>
      <th>Method</th><th>Path</th><th>Status</th><th>ms</th><th>Browser</th><th>Device</th><th>OS</th><th>Referrer</th></tr></thead><tbody>
      ${{rows.map(r=>`<tr>
        <td style="color:#5a7090">${{r.id}}</td>
        <td style="white-space:nowrap;color:#5a7090">${{r.ts}}</td>
        <td style="font-family:monospace">${{r.ip||'?'}}</td>
        <td style="color:#5a7090">${{r.country||'?'}}</td>
        <td style="color:#2dd4bf">${{r.method}}</td>
        <td style="color:#38bdf8;font-family:monospace;max-width:200px;overflow:hidden;text-overflow:ellipsis">${{r.path}}</td>
        <td><span class="badge ${{r.status>=400?'badge-err':'badge-ok'}}">${{r.status}}</span></td>
        <td style="color:${{r.resp_ms>1000?'#f87171':r.resp_ms>300?'#fbbf24':'#10b981'}}">${{r.resp_ms}}</td>
        <td>${{r.browser||'?'}}</td><td>${{r.device||'?'}}</td><td>${{r.os||'?'}}</td>
        <td style="color:#5a7090;max-width:150px;overflow:hidden;text-overflow:ellipsis">${{r.referrer||'—'}}</td>
      </tr>`).join('')}}</tbody></table>`;
  }} else if (type === 'search') {{
    html = `<table><thead><tr><th>#</th><th>Time</th><th>IP</th><th>Query</th><th>Results</th><th>Symbols</th></tr></thead><tbody>
      ${{rows.map(r=>`<tr>
        <td style="color:#5a7090">${{r.id}}</td>
        <td style="white-space:nowrap;color:#5a7090">${{r.ts}}</td>
        <td style="font-family:monospace">${{r.ip||'?'}}</td>
        <td style="color:#fbbf24;font-weight:600">${{r.query}}</td>
        <td style="color:#38bdf8">${{r.results_ct}}</td>
        <td style="color:#5a7090">${{r.result_syms||'[]'}}</td>
      </tr>`).join('')}}</tbody></table>`;
  }} else {{
    html = `<table><thead><tr><th>#</th><th>Time</th><th>IP</th><th>Path</th><th>Status</th><th>Error</th></tr></thead><tbody>
      ${{rows.map(r=>`<tr>
        <td style="color:#5a7090">${{r.id}}</td>
        <td style="white-space:nowrap;color:#5a7090">${{r.ts}}</td>
        <td style="font-family:monospace">${{r.ip||'?'}}</td>
        <td style="color:#38bdf8;font-family:monospace">${{r.path}}</td>
        <td><span class="badge badge-err">${{r.status}}</span></td>
        <td style="color:#f87171">${{r.error_msg}}</td>
      </tr>`).join('')}}</tbody></table>`;
  }}
  document.getElementById('log-table').innerHTML = html;
}}

function exportCSV() {{
  if(!currentData.length) return;
  const keys = Object.keys(currentData[0]);
  const csv = [keys.join(','), ...currentData.map(r=>keys.map(k=>JSON.stringify(r[k]??'')).join(','))].join('\\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download = `logs_${{currentType}}_${{new Date().toISOString().slice(0,10)}}.csv`;
  a.click();
}}

// Clock
setInterval(()=>{{document.getElementById('clock').textContent=new Date().toLocaleTimeString('en-IN',{{timeZone:'Asia/Kolkata',hour12:true}})+' IST'}}, 1000);

// Auto-load
loadStats();
loadLogs('access', document.querySelector('.tab.active'));
setInterval(loadStats, 30000);
</script></body></html>
""", 200
