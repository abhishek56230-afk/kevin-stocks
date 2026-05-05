# -*- coding: utf-8 -*-
"""
report_generator.py — Kevin Kataria Stock Intelligence
=======================================================
Generates a professional PDF stock report with:
  • "Kevin Kataria AI Assistant" diagonal watermark on every page
  • Full price, technical, fundamental, sentiment & news data
  • Registered via register_report_routes(app, build_stock_context)

Route:  GET /api/report/<SYMBOL>?format=pdf
"""

from flask import Blueprint, request, jsonify, make_response
import datetime, io, math

# ── Try reportlab (preferred) ─────────────────────────────────
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.pdfgen import canvas as rl_canvas
    _RL_OK = True
except ImportError:
    _RL_OK = False

report_bp = Blueprint("report", __name__)

# ── Colour palette ─────────────────────────────────────────────
C_BG       = colors.HexColor("#060d1a")
C_BRAND    = colors.HexColor("#38bdf8")
C_TEAL     = colors.HexColor("#2dd4bf")
C_GREEN    = colors.HexColor("#10b981")
C_RED      = colors.HexColor("#f87171")
C_AMBER    = colors.HexColor("#fbbf24")
C_TEXT     = colors.HexColor("#e8f0ff")
C_MUTED    = colors.HexColor("#5a7090")
C_PANEL    = colors.HexColor("#0c1628")
C_LINE     = colors.HexColor("#1a2840")
C_WHITE    = colors.white
C_BLACK    = colors.black

WATERMARK_TEXT = "Marvin / Kevin"


# ════════════════════════════════════════════════════════════════
# WATERMARK CANVAS
# ════════════════════════════════════════════════════════════════
class WatermarkCanvas(rl_canvas.Canvas):
    """Draws the diagonal watermark on every page."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pages = []

    def showPage(self):
        self.pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        for state in self.pages:
            self.__dict__.update(state)
            self._draw_watermark()
            self._draw_footer()
            super().showPage()
        super().save()

    def _draw_watermark(self):
        self.saveState()
        self.setFont("Helvetica", 52)
        self.setFillColorRGB(0.22, 0.47, 0.62, alpha=0.07)
        w, h = A4
        self.translate(w / 2, h / 2)
        self.rotate(40)
        self.drawCentredString(0,  60, WATERMARK_TEXT)
        self.drawCentredString(0, -60, WATERMARK_TEXT)
        self.drawCentredString(0, 180, WATERMARK_TEXT)
        self.drawCentredString(0,-180, WATERMARK_TEXT)
        self.restoreState()

    def _draw_footer(self):
        self.saveState()
        w, h = A4
        self.setFont("Helvetica", 7)
        self.setFillColorRGB(0.35, 0.44, 0.56)
        footer = f"Marvin / Kevin · Stock Intelligence Pro · Generated {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · Educational use only — not financial advice"
        self.drawCentredString(w / 2, 22, footer)
        self.restoreState()


# ════════════════════════════════════════════════════════════════
# PDF BUILDER
# ════════════════════════════════════════════════════════════════
def _safe(v, fmt=None, suffix=""):
    """Safely format a value."""
    try:
        if v is None or v == "" or str(v).strip() in ("None", "null", "—", "-", "Nil"):
            return "—"
        if fmt == "money":
            return f"₹{float(v):,.2f}{suffix}"
        if fmt == "pct":
            return f"{float(v):+.2f}%"
        if fmt == "f2":
            return f"{float(v):.2f}{suffix}"
        if fmt == "int":
            return f"{int(v):,}{suffix}"
        return str(v)
    except Exception:
        return "—"


def build_pdf(sym, ctx):
    """Build and return PDF bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=1.8*cm,
        leftMargin=1.8*cm,
        topMargin=2.2*cm,
        bottomMargin=2.2*cm,
        title=f"{sym} — Stock Intelligence Report",
        author="Kevin Kataria AI Assistant",
        subject="NSE Stock Analysis",
    )

    styles = getSampleStyleSheet()

    # ── Custom styles ──────────────────────────────────────────
    H1 = ParagraphStyle("H1", parent=styles["Normal"],
        fontSize=26, fontName="Helvetica-Bold",
        textColor=C_WHITE, spaceAfter=4, spaceBefore=0,
        leading=30)
    H2 = ParagraphStyle("H2", parent=styles["Normal"],
        fontSize=14, fontName="Helvetica-Bold",
        textColor=C_BRAND, spaceAfter=6, spaceBefore=14,
        leading=18)
    H3 = ParagraphStyle("H3", parent=styles["Normal"],
        fontSize=11, fontName="Helvetica-Bold",
        textColor=C_TEAL, spaceAfter=4, spaceBefore=8)
    BODY = ParagraphStyle("BODY", parent=styles["Normal"],
        fontSize=9, fontName="Helvetica",
        textColor=C_TEXT, leading=14, spaceAfter=4)
    MUTED = ParagraphStyle("MUTED", parent=styles["Normal"],
        fontSize=8, fontName="Helvetica",
        textColor=C_MUTED, leading=12, spaceAfter=3)
    LABEL = ParagraphStyle("LABEL", parent=styles["Normal"],
        fontSize=7.5, fontName="Helvetica-Bold",
        textColor=C_MUTED, spaceAfter=2, leading=10)
    CENTER = ParagraphStyle("CENTER", parent=BODY,
        alignment=TA_CENTER)
    VERDICT = ParagraphStyle("VERDICT", parent=styles["Normal"],
        fontSize=18, fontName="Helvetica-Bold",
        textColor=C_WHITE, alignment=TA_CENTER, spaceAfter=6)

    story = []

    # ── Header bar ────────────────────────────────────────────
    price_data = ctx.get("price", {})
    tech_data  = ctx.get("technical", {})
    fund_data  = ctx.get("fundamental", {})
    news_data  = ctx.get("news", {})
    sent_data  = ctx.get("sentiment", {})

    p     = price_data.get("price", 0)
    pct   = price_data.get("pct", 0)
    name  = price_data.get("name", sym)
    mkt   = price_data.get("market", "NSE")
    sig   = tech_data.get("signal", "—")
    score = tech_data.get("bull_score", 50)

    sig_color = C_GREEN if "BUY" in sig else C_RED if "SELL" in sig else C_AMBER

    header_tbl = Table(
        [[
            Paragraph(f"<b>{name}</b>", H1),
            Paragraph(
                f"<font color='#{C_BRAND.hexval()[2:]}' size='18'><b>{_safe(p,'money')}</b></font>"
                f"<br/><font color='{'#10b981' if pct>=0 else '#f87171'}' size='10'>"
                f"{_safe(pct,'pct')}</font>",
                ParagraphStyle("PR", parent=BODY, alignment=TA_RIGHT)
            ),
        ]],
        colWidths=[12*cm, 5*cm],
    )
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), C_PANEL),
        ("ROUNDEDCORNERS", (0,0),(-1,-1), [8]),
        ("TOPPADDING",   (0,0),(-1,-1), 14),
        ("BOTTOMPADDING",(0,0),(-1,-1), 14),
        ("LEFTPADDING",  (0,0),(-1,-1), 16),
        ("RIGHTPADDING", (0,0),(-1,-1), 16),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 10))

    # Sub header row
    sub_data = [
        ["Symbol", sym],
        ["Exchange", mkt],
        ["Report Date", datetime.datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")],
        ["AI Signal", sig],
        ["Bull Score", f"{score}/100"],
    ]
    sub_tbl = Table(
        [[Paragraph(f"<b>{k}</b>", LABEL), Paragraph(str(v), BODY)] for k,v in sub_data],
        colWidths=[3.5*cm, 13.5*cm],
    )
    sub_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), C_BG),
        ("GRID",         (0,0),(-1,-1), 0.4, C_LINE),
        ("TOPPADDING",   (0,0),(-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING",  (0,0),(-1,-1), 10),
        ("RIGHTPADDING", (0,0),(-1,-1), 10),
        ("TEXTCOLOR",    (1,3),( 1,3), sig_color),
    ]))
    story.append(sub_tbl)
    story.append(Spacer(1, 14))

    # ══════════════════════════════════════════════════════════════
    # ── KEVIN AI — PRINCIPAL FIRST (verdict before everything) ───
    # ══════════════════════════════════════════════════════════════
    kevin = ctx.get("kevin_ai", {})
    kv    = kevin.get("kevin_verdict", {})
    hs    = kevin.get("health_score", {})
    rm    = kevin.get("risk", {})
    st    = kevin.get("strategy", {})

    # Only render this block when we have at least a verdict or health score
    if kv or hs or rm or st:
        story.append(Paragraph("Kevin AI — 7-Engine Analysis", H2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
        story.append(Spacer(1, 6))

        # ── 1. THE VERDICT (principal — most important thing first) ──
        verdict_val  = kv.get("verdict", tech_data.get("signal", "HOLD")).upper()
        confidence   = kv.get("confidence", "")
        best_for     = kv.get("best_for", "")
        v_color      = C_GREEN if "BUY" in verdict_val else C_RED if "SELL" in verdict_val else C_AMBER

        verdict_row = Table(
            [[
                Paragraph(
                    f"<font color='#{'10b981' if 'BUY' in verdict_val else 'f87171' if 'SELL' in verdict_val else 'fbbf24'}' size='20'><b>{verdict_val}</b></font>",
                    ParagraphStyle("VC", parent=BODY, alignment=TA_CENTER)
                ),
                Paragraph(
                    f"<b>Confidence</b><br/><font size='14'>{confidence or '—'}</font>",
                    ParagraphStyle("VCC", parent=BODY, alignment=TA_CENTER)
                ),
                Paragraph(
                    f"<b>Best For</b><br/>{best_for or '—'}",
                    ParagraphStyle("VCB", parent=BODY, alignment=TA_CENTER)
                ),
            ]],
            colWidths=[5.67*cm, 5.67*cm, 5.67*cm],
        )
        verdict_row.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (0,0), C_PANEL),
            ("BACKGROUND",    (1,0), (1,0), C_BG),
            ("BACKGROUND",    (2,0), (2,0), C_BG),
            ("GRID",          (0,0), (-1,-1), 0.4, C_LINE),
            ("TOPPADDING",    (0,0), (-1,-1), 12),
            ("BOTTOMPADDING", (0,0), (-1,-1), 12),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ]))
        story.append(verdict_row)
        story.append(Spacer(1, 8))

        # ── 2. WHY — summary bullets (plain english) ──────────────
        bullets = kv.get("summary", kv.get("reasoning", []))
        if isinstance(bullets, str):
            bullets = [l.strip("•- ").strip() for l in bullets.split("\n") if l.strip()]
        if bullets:
            story.append(Paragraph("<b>Why Kevin says so:</b>", BODY))
            for b in bullets[:5]:
                if b:
                    story.append(Paragraph(f"• {b}", BODY))
            story.append(Spacer(1, 8))

        # ── 3. THE 7 ENGINES — simple one-line each ───────────────
        story.append(Paragraph("<b>The 7 Engines at a Glance</b>", H3))

        def _grade_color(g):
            return {"A":"#10b981","B":"#38bdf8","C":"#fbbf24","D":"#f97316","F":"#f87171"}.get(str(g).upper(),"#e8f0ff")

        def _risk_color(lvl):
            return {"LOW":"#10b981","MEDIUM":"#fbbf24","HIGH":"#f87171"}.get(str(lvl).upper(),"#e8f0ff")

        hs_score = hs.get("score","—")
        hs_grade = hs.get("grade","—")
        rm_level = rm.get("level","—")
        rm_score = rm.get("score","—")
        rm_down  = rm.get("downside_probability","—")

        entry    = _safe(st.get("entry"), "money")   if st else "—"
        stop     = _safe(st.get("stop"),  "money")   if st else "—"
        tgt1     = _safe(st.get("target1"),"money")  if st else "—"
        tgt2     = _safe(st.get("target2"),"money")  if st else "—"
        rr       = st.get("risk_reward","—")         if st else "—"

        eng_rows = [
            ["Engine", "What It Checks", "Your Reading", "Plain Meaning"],
            ["1 · Financial Health",
             "Revenue, margins, debt, ROE, ROCE",
             f"Score {hs_score}/100  Grade {hs_grade}",
             "Above 60 = healthy. Below 40 = caution."],
            ["2 · Buy/Sell Signal",
             "Trend, MA50/200, RSI, MACD, S&R",
             sig,
             "BUY = uptrend momentum. SELL = downtrend."],
            ["3 · Sentiment",
             "News, Reddit, Moneycontrol, ET",
             f"Bull {sent_data.get('overall_bullish', sent_data.get('avg_bull','—'))}%",
             "Above 55% = positive crowd mood."],
            ["4 · Macro Risk",
             "Rates, inflation, sector risks",
             "See Macro section",
             "Check if economy helps or hurts stock."],
            ["5 · Risk Meter",
             "ATR, beta, debt, 52-wk position",
             f"{rm_level}  (score {rm_score}/100)",
             "HIGH = smaller position size."],
            ["6 · Trade Setup",
             "Entry, stop-loss, targets, R:R",
             f"Entry {entry}  Stop {stop}",
             f"T1 {tgt1}  T2 {tgt2}  R:R {rr}"],
            ["7 · Kevin AI Verdict",
             "Groq Llama reads all 6 engines",
             verdict_val,
             f"Confidence {confidence or '—'}. Act when >65%."],
        ]
        et = Table(eng_rows, colWidths=[3.5*cm, 4.5*cm, 4.0*cm, 5.0*cm])
        et_style = [
            ("BACKGROUND",    (0,0), (-1,0), C_PANEL),
            ("TEXTCOLOR",     (0,0), (-1,0), C_BRAND),
            ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTNAME",      (0,1), (0,-1), "Helvetica-Bold"),
            ("TEXTCOLOR",     (0,1), (0,-1), C_AMBER),
            ("FONTSIZE",      (0,0), (-1,-1), 8),
            ("GRID",          (0,0), (-1,-1), 0.4, C_LINE),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 7),
            ("RIGHTPADDING",  (0,0), (-1,-1), 7),
            ("TEXTCOLOR",     (1,1), (-1,-1), C_TEXT),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [C_BG, C_PANEL]),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ]
        et.setStyle(TableStyle(et_style))
        story.append(et)
        story.append(Spacer(1, 14))

    # ── AI Verdict ──────────────────────────────────────────────
    verdict_text = ctx.get("verdict_text") or ctx.get("ai_verdict") or ""
    if verdict_text:
        story.append(Paragraph("AI Verdict", H2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
        story.append(Spacer(1, 6))
        # clean markdown-like symbols
        clean = verdict_text.replace("**","").replace("##","").replace("# ","").replace("* ","• ")
        for line in clean.split("\n"):
            line = line.strip()
            if not line:
                story.append(Spacer(1, 4))
            elif line.startswith("•"):
                story.append(Paragraph(line, BODY))
            else:
                story.append(Paragraph(line, BODY))
        story.append(Spacer(1, 10))

    # ── Price Summary ───────────────────────────────────────────
    story.append(Paragraph("Price Summary", H2))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
    story.append(Spacer(1, 6))

    price_rows = [
        ["Metric", "Value", "Metric", "Value"],
        ["Current Price",  _safe(p,"money"),         "Prev Close",      _safe(price_data.get('prev_close'),"money")],
        ["Change",         _safe(price_data.get('change'),"f2"," ₹"),   "Change %",        _safe(pct,"pct")],
        ["Day High",       _safe(price_data.get('high'),"money"),        "Day Low",         _safe(price_data.get('low'),"money")],
        ["52W High",       _safe(price_data.get('week52_high'),"money"), "52W Low",         _safe(price_data.get('week52_low'),"money")],
        ["Volume",         _safe(price_data.get('volume'),"int"),        "Market",          mkt],
    ]
    pt = Table(price_rows, colWidths=[4.5*cm, 4.5*cm, 4.5*cm, 4.5*cm])
    _tbl_style(pt, has_header=True)
    story.append(pt)
    story.append(Spacer(1, 14))

    # ── Technical Indicators ────────────────────────────────────
    story.append(Paragraph("Technical Indicators", H2))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
    story.append(Spacer(1, 6))

    tech_rows = [
        ["Indicator", "Value", "Indicator", "Value"],
        ["RSI (14)",      _safe(tech_data.get('rsi'),"f2"),         "Signal",          sig],
        ["MACD",          _safe(tech_data.get('macd'),"f2"),        "MACD Hist",       _safe(tech_data.get('macd_hist'),"f2")],
        ["MA 20",         _safe(tech_data.get('ma20'),"money"),     "MA 50",           _safe(tech_data.get('ma50'),"money")],
        ["MA 200",        _safe(tech_data.get('ma200'),"money"),    "ATR",             _safe(tech_data.get('atr'),"f2")],
        ["ADX",           _safe(tech_data.get('adx'),"f2"),         "Stoch %K",        _safe(tech_data.get('stoch_k'),"f2")],
        ["BB Upper",      _safe(tech_data.get('bb_upper'),"money"), "BB Lower",        _safe(tech_data.get('bb_lower'),"money")],
        ["Support",       _safe(tech_data.get('support'),"money"),  "Resistance",      _safe(tech_data.get('resistance'),"money")],
        ["Target 1",      _safe(tech_data.get('target1'),"money"),  "Target 2",        _safe(tech_data.get('target2'),"money")],
        ["Stop Loss",     _safe(tech_data.get('stop_loss'),"money"),"Bull Score",      f"{score}/100"],
        ["Golden Cross",  "Yes" if tech_data.get('golden_cross') else "No",
                                                                     "OBV Rising",     "Yes" if tech_data.get('obv_rising') else "No"],
    ]
    tt = Table(tech_rows, colWidths=[4.5*cm, 4.5*cm, 4.5*cm, 4.5*cm])
    _tbl_style(tt, has_header=True)
    story.append(tt)

    # Candlestick patterns
    patterns = tech_data.get("patterns", [])
    if patterns:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Candlestick Patterns Detected", H3))
        for pat in patterns:
            story.append(Paragraph(f"• {pat}", BODY))
    story.append(Spacer(1, 14))

    # ── Fundamentals ────────────────────────────────────────────
    if fund_data and fund_data.get("success"):
        story.append(Paragraph("Fundamentals", H2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
        story.append(Spacer(1, 6))
        fund_rows = [
            ["Metric", "Value", "Metric", "Value"],
            ["P/E Ratio",    _safe(fund_data.get('pe')),         "PEG Ratio",     _safe(fund_data.get('peg'))],
            ["ROE %",        _safe(fund_data.get('roe')),        "ROCE %",        _safe(fund_data.get('roce'))],
            ["Debt/Equity",  _safe(fund_data.get('de')),         "Market Cap Cr", _safe(fund_data.get('mcap'),"f2")],
            ["Promoter %",   _safe(fund_data.get('promoter')),   "FII %",         _safe(fund_data.get('fii'))],
            ["DII %",        _safe(fund_data.get('dii')),        "Public %",      _safe(fund_data.get('public'))],
            ["Revenue Cr",   _safe(fund_data.get('revenue')),    "Profit Cr",     _safe(fund_data.get('profit'))],
        ]
        ft = Table(fund_rows, colWidths=[4.5*cm, 4.5*cm, 4.5*cm, 4.5*cm])
        _tbl_style(ft, has_header=True)
        story.append(ft)
        story.append(Spacer(1, 14))

    # ── Sentiment ────────────────────────────────────────────────
    if sent_data:
        story.append(Paragraph("Market Sentiment", H2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
        story.append(Spacer(1, 6))
        overall = sent_data.get("overall_sentiment", "—")
        score_s = sent_data.get("score", "—")
        sent_rows = [
            ["Overall Sentiment", str(overall), "Score", str(score_s)],
        ]
        details = sent_data.get("details", {})
        for k, v in details.items():
            sent_rows.append([k, str(v), "", ""])
        st = Table(sent_rows, colWidths=[4.5*cm, 4.5*cm, 4.5*cm, 4.5*cm])
        _tbl_style(st, has_header=False)
        story.append(st)
        story.append(Spacer(1, 14))

    # ── News Headlines ───────────────────────────────────────────
    news_items = []
    if isinstance(news_data, dict):
        news_items = news_data.get("articles", news_data.get("items", []))
    elif isinstance(news_data, list):
        news_items = news_data

    if news_items:
        story.append(Paragraph("Latest News Headlines", H2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
        story.append(Spacer(1, 6))
        for item in news_items[:10]:
            title  = item.get("title", "") if isinstance(item, dict) else str(item)
            source = item.get("source", "") if isinstance(item, dict) else ""
            date   = item.get("date","")   if isinstance(item, dict) else ""
            meta   = " · ".join(filter(None, [source, date]))
            story.append(Paragraph(f"• <b>{title}</b>", BODY))
            if meta:
                story.append(Paragraph(f"  {meta}", MUTED))
        story.append(Spacer(1, 14))

    # ── Disclaimer ───────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE))
    story.append(Spacer(1, 6))
    disc = (
        "DISCLAIMER: This report is generated by Kevin Kataria AI Assistant for "
        "educational and informational purposes only. It does not constitute "
        "financial advice or a solicitation to buy or sell securities. Stock "
        "markets are subject to market risks. Always consult a SEBI-registered "
        "financial advisor before making investment decisions."
    )
    story.append(Paragraph(disc, MUTED))

    doc.build(story, canvasmaker=WatermarkCanvas)
    buf.seek(0)
    return buf.read()


def _tbl_style(tbl, has_header=True):
    style = [
        ("BACKGROUND",    (0,0), (-1, 0 if has_header else -1), C_PANEL),
        ("TEXTCOLOR",     (0,0), (-1, 0 if has_header else -1), C_BRAND),
        ("FONTNAME",      (0,0), (-1, 0 if has_header else -1), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8.5),
        ("GRID",          (0,0), (-1,-1), 0.4, C_LINE),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ("TEXTCOLOR",     (0,1 if has_header else 0), (-1,-1), C_TEXT),
        ("ROWBACKGROUNDS",(0,1 if has_header else 0), (-1,-1), [C_BG, C_PANEL]),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]
    tbl.setStyle(TableStyle(style))


# ════════════════════════════════════════════════════════════════
# FALLBACK: plain-text PDF if reportlab missing
# ════════════════════════════════════════════════════════════════
def _fallback_pdf(sym, ctx):
    """Minimal PDF bytes using only stdlib (very basic)."""
    lines = [
        f"Kevin Kataria AI Assistant — Stock Report",
        f"Symbol: {sym}",
        f"Generated: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Install reportlab for a fully-designed PDF:",
        "  pip install reportlab",
        "",
        "Watermark: " + WATERMARK_TEXT,
    ]
    price = ctx.get("price", {})
    if price:
        lines += [
            "", "=== PRICE ===",
            f"Price:   {_safe(price.get('price'),'money')}",
            f"Change:  {_safe(price.get('pct'),'pct')}",
            f"52W H:   {_safe(price.get('week52_high'),'money')}",
            f"52W L:   {_safe(price.get('week52_low'),'money')}",
        ]
    tech = ctx.get("technical", {})
    if tech:
        lines += [
            "", "=== TECHNICALS ===",
            f"Signal:  {tech.get('signal','—')}",
            f"RSI:     {_safe(tech.get('rsi'),'f2')}",
            f"MA20:    {_safe(tech.get('ma20'),'money')}",
            f"MA50:    {_safe(tech.get('ma50'),'money')}",
        ]
    text = "\n".join(lines)
    # Wrap in a minimal valid PDF
    content = text.encode("latin-1", errors="replace")
    return content  # caller sends as text/plain fallback


# ════════════════════════════════════════════════════════════════
# ROUTE REGISTRATION
# ════════════════════════════════════════════════════════════════
def register_report_routes(app, build_stock_context):
    """Called from app.py: register_report_routes(app, build_stock_context)"""

    @app.route("/api/report/<symbol>")
    def download_report(symbol):
        sym    = symbol.upper().strip()
        fmt    = request.args.get("format", "pdf").lower()
        ctx    = build_stock_context(sym)

        if not ctx.get("success"):
            return jsonify(ctx), 400

        if fmt == "pdf":
            if _RL_OK:
                try:
                    pdf_bytes = build_pdf(sym, ctx)
                    resp = make_response(pdf_bytes)
                    resp.headers["Content-Type"]        = "application/pdf"
                    resp.headers["Content-Disposition"] = f'attachment; filename="{sym}_report.pdf"'
                    resp.headers["Cache-Control"]       = "no-cache"
                    return resp
                except Exception as e:
                    # Reportlab error — return JSON with the error
                    return jsonify({"success": False, "error": f"PDF build error: {e}"}), 500
            else:
                # reportlab not installed — return plain text report
                text = _fallback_text(sym, ctx)
                resp = make_response(text.encode("utf-8"))
                resp.headers["Content-Type"]        = "text/plain; charset=utf-8"
                resp.headers["Content-Disposition"] = f'attachment; filename="{sym}_report.txt"'
                return resp

        # format=docx falls through to JSON for now
        return jsonify({"success": False, "error": "Format not supported. Use ?format=pdf"}), 400

    return app


def _fallback_text(sym, ctx):
    price = ctx.get("price", {})
    tech  = ctx.get("technical", {})
    lines = [
        "=" * 60,
        f"  {WATERMARK_TEXT}",
        f"  Stock Intelligence Report — {sym}",
        f"  {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
        f"Name:          {price.get('name', sym)}",
        f"Price:         {_safe(price.get('price'),'money')}",
        f"Change:        {_safe(price.get('pct'),'pct')}",
        f"52W High:      {_safe(price.get('week52_high'),'money')}",
        f"52W Low:       {_safe(price.get('week52_low'),'money')}",
        "",
        "--- TECHNICAL ---",
        f"Signal:        {tech.get('signal','—')}",
        f"Bull Score:    {tech.get('bull_score','—')}/100",
        f"RSI:           {_safe(tech.get('rsi'),'f2')}",
        f"MACD Hist:     {_safe(tech.get('macd_hist'),'f2')}",
        f"MA20:          {_safe(tech.get('ma20'),'money')}",
        f"MA50:          {_safe(tech.get('ma50'),'money')}",
        f"Support:       {_safe(tech.get('support'),'money')}",
        f"Resistance:    {_safe(tech.get('resistance'),'money')}",
        f"Target 1:      {_safe(tech.get('target1'),'money')}",
        f"Stop Loss:     {_safe(tech.get('stop_loss'),'money')}",
        "",
        "DISCLAIMER: Educational use only. Not financial advice.",
        "Install reportlab for a full PDF: pip install reportlab",
    ]
    return "\n".join(lines)
