"""
renderer.py  —  Slide-type renderers.

Supported types
---------------
  title        Big title, tagline, status badges
  bullets      1- or 2-column bullet/numbered list + optional code block
  cards        Row of N auto-sized coloured cards + optional stats strip / footer strip
  table        Column headers + data rows + optional callout + optional before→after pairs
  metrics      Big-number metric boxes + optional detail table below
  flow         Source boxes → golden-record output box + optional bullets
  split        Left bullet list + right code/query panel
  swimlanes    N vertical lane columns (roadmap)
  checklist    Full-height icon checklist (closing slide)
  features     Two-column bar+title+body item layout (observability)
  architecture Top rows + layer boxes + lambda boxes
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

from theme import (
    _blank, _white, _strip, _box, _t, _blt, _rule, _vline, _header, _footer,
    resolve_color,
    CHARCOAL, DARK_BLUE, DEEP_BLUE, ORANGE, SKY, GREEN, PURPLE,
    WHITE, LIGHT_BG, RULE_C, TEXT, MUTED, GREEN_OK, AMBER, RED,
    FH, FB, FM,
    CX, CW, GX, BY, BH, FTY, SH, TY, TH, STY, STH, RY,
)

# ── Helpers shared by multiple renderers ──────────────────────────────────────

def _sd_footer(s, sd: dict):
    _footer(s, sd.get("footer", ""))


def _auto_card_layout(n: int):
    """Return (card_width, x_positions) for n equally spaced cards."""
    GAP   = Inches(0.15)
    AVAIL = GX - CX - Inches(0.1)
    cw    = (AVAIL - (n - 1) * GAP) / n
    xs    = [CX + i * (cw + GAP) for i in range(n)]
    return cw, xs


def _render_callout(s, callout, cal_y: float):
    """Render a callout box (string or dict-with-parts) at y=cal_y."""
    if not callout:
        return
    if isinstance(callout, str):
        _box(s, CX, cal_y, CW, Inches(0.46), fill=DEEP_BLUE)
        _t(s, callout,
           CX + Inches(0.15), cal_y + Inches(0.08), CW - Inches(0.25), Inches(0.34),
           sz=Pt(13), bold=True, color=WHITE, align=PP_ALIGN.CENTER, font=FH)
    elif isinstance(callout, dict):
        parts  = callout.get("parts", [])
        bg     = resolve_color(callout.get("background", "light_bg"))
        cal_h  = Inches(max(0.12 + len(parts) * 0.36, 0.5))
        _box(s, CX, cal_y, CW, cal_h, fill=bg)
        for pi, part in enumerate(parts):
            py = cal_y + Inches(0.1) + pi * Inches(0.36)
            c  = resolve_color(part.get("color", "text"))
            b  = bool(part.get("bold", False))
            _t(s, str(part.get("text", "")),
               CX + Inches(0.15), py, CW - Inches(0.25), Inches(0.32),
               sz=Pt(13) if b else Pt(12), bold=b, color=c, font=FH if b else FB)


# ── title ─────────────────────────────────────────────────────────────────────
def render_title(prs, sd: dict):
    s = _blank(prs); _white(s); _strip(s)
    _t(s, sd.get("title", ""),
       CX, Inches(0.65), Inches(10.0), Inches(1.2),
       sz=Pt(44), bold=True, color=CHARCOAL, font=FH)
    if tagline := sd.get("tagline"):
        _t(s, tagline, CX, Inches(1.95), Inches(10.0), Inches(0.42),
           sz=Pt(16), color=DEEP_BLUE, font=FB)
    _rule(s, Inches(2.45), color=DEEP_BLUE, thick=Pt(2))
    if desc := sd.get("description"):
        _t(s, desc, CX, Inches(2.58), Inches(9.8), Inches(0.9),
           sz=Pt(16), color=TEXT, font=FB)
    for i, badge in enumerate(sd.get("badges", [])):
        bx = CX + i * Inches(3.0)
        _box(s, bx, Inches(3.75), Inches(2.75), Inches(0.46),
             fill=resolve_color(badge.get("color", "deep_blue")))
        _t(s, badge.get("label", ""), bx + Inches(0.1), Inches(3.79),
           Inches(2.55), Inches(0.38),
           sz=Pt(13), bold=True, color=WHITE, align=PP_ALIGN.CENTER)
    _sd_footer(s, sd)


# ── bullets ───────────────────────────────────────────────────────────────────
def render_bullets(prs, sd: dict):
    """
    Slide options
    -------------
    left / right : { heading, heading_color, items: [...], numbered: bool }
    items        : shorthand single-column list (no heading)
    numbered     : bool — top-level numbered list if using 'items' shorthand
    code_block   : string shown in a monospace box below the content
    divider      : bool (default true when both left+right present)
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    left  = sd.get("left")
    right = sd.get("right")
    code  = sd.get("code_block")

    # With a code block: leave room for code box (0.56") + gap (0.12") + footer (7.10")
    # body_h = 7.10 - 0.08(gap) - 0.56(code) - 0.12(spacer) - 1.82(BY) = 4.52"
    body_h = Inches(4.52) if code else BH
    code_y = BY + body_h + Inches(0.12)

    if left and right:
        C1W = Inches(4.75)
        C2X = CX + Inches(5.2)
        C2W = Inches(4.75)
        vline_h = Inches(3.38) if code else body_h
        if sd.get("divider", True):
            _vline(s, CX + Inches(5.0), BY, vline_h)
        _blt(s, left.get("items", []), CX, BY, C1W, body_h,
             sz=Pt(12.5), color=TEXT,
             hdg=left.get("heading"), hc=left.get("heading_color"),
             numbered=left.get("numbered", False))
        _blt(s, right.get("items", []), C2X, BY, C2W, body_h,
             sz=Pt(12.5), color=TEXT,
             hdg=right.get("heading"), hc=right.get("heading_color"),
             numbered=right.get("numbered", False))
    elif left:
        _blt(s, left.get("items", []), CX, BY, CW, body_h,
             sz=Pt(13), color=TEXT,
             hdg=left.get("heading"), hc=left.get("heading_color"),
             numbered=left.get("numbered", False))
    elif items := sd.get("items"):
        _blt(s, items, CX, BY, CW, body_h,
             sz=Pt(13), color=TEXT, numbered=sd.get("numbered", False))

    if code:
        _box(s, CX, code_y, CW, Inches(0.56), fill=RGBColor(0xF0, 0xF4, 0xF8))
        _t(s, code, CX + Inches(0.15), code_y + Inches(0.06),
           CW - Inches(0.25), Inches(0.48),
           sz=Pt(11), color=DEEP_BLUE, font=FM)

    _sd_footer(s, sd)


# ── cards ─────────────────────────────────────────────────────────────────────
def render_cards(prs, sd: dict):
    """
    Slide options
    -------------
    cards        : list of { title|label, color, active|status, body }
                   active=false → light background (inactive/coming-soon style)
    stats_strip  : { background, items: [{ value, label, value_color, label_color }] }
    footer_strip : string — narrow strip at very bottom
    below_bullets: { heading, heading_color, items: [...] }
                   rendered below the cards (cards are shortened to make room)
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    cards       = sd.get("cards", [])
    n           = len(cards)
    cw, xs      = _auto_card_layout(n)

    has_stats  = bool(sd.get("stats_strip"))
    has_fstrip = bool(sd.get("footer_strip"))
    has_below  = bool(sd.get("below_bullets"))

    if has_stats:
        card_bottom = Inches(6.38)
    elif has_below:
        card_bottom = Inches(5.08)
    elif has_fstrip:
        card_bottom = Inches(7.0)
    else:
        card_bottom = Inches(7.1)
    card_h = card_bottom - BY

    for i, card in enumerate(cards):
        x      = xs[i]
        status = card.get("active", card.get("status", True))
        active = status not in (False, "inactive", "pending", "future", 0)
        fill   = resolve_color(card.get("color", "deep_blue")) if active else LIGHT_BG
        lc     = None if active else RULE_C
        lw     = Pt(0) if active else Pt(1)
        tc     = WHITE if active else CHARCOAL

        _box(s, x, BY, cw, card_h, fill=fill, lc=lc, lw=lw)

        # Optional status icon row
        has_icon = "status" in card or "active" in card or card.get("show_icon", False)
        if has_icon:
            icon = "✅" if active else "🔲"
            _t(s, icon, x + Inches(0.1), BY + Inches(0.1), cw - Inches(0.15), Inches(0.38),
               sz=Pt(18), color=tc)
        icon_offset = Inches(0.52) if has_icon else Inches(0.0)

        # Title / label
        lbl = card.get("title", card.get("label", ""))
        lbl_y = BY + Inches(0.12) + icon_offset
        lbl_sz = Pt(14) if n <= 4 else Pt(12)
        _t(s, lbl, x + Inches(0.12), lbl_y, cw - Inches(0.2), Inches(0.48),
           sz=lbl_sz, bold=True, color=tc, font=FH)

        # In-card separator
        sep_y = lbl_y + Inches(0.52)
        _box(s, x + Inches(0.08), sep_y, cw - Inches(0.16), Pt(1),
             fill=WHITE if active else RULE_C)

        # Body text
        body_c = RGBColor(0xEE, 0xEE, 0xEE) if active else MUTED
        body_y = sep_y + Inches(0.12)
        _t(s, card.get("body", ""),
           x + Inches(0.12), body_y, cw - Inches(0.2),
           card_bottom - body_y - Inches(0.2),
           sz=Pt(12) if n <= 4 else Pt(11), color=body_c, font=FB)

    # Stats strip
    if has_stats:
        strip_items = sd["stats_strip"].get("items", [])
        bg          = resolve_color(sd["stats_strip"].get("background", "deep_blue"))
        _box(s, CX, Inches(6.5), CW, Inches(0.68), fill=bg)
        sw = CW / len(strip_items) if strip_items else CW
        for i, it in enumerate(strip_items):
            ix = CX + i * sw
            _t(s, str(it.get("value", "")),
               ix + Inches(0.05), Inches(6.53), sw - Inches(0.08), Inches(0.35),
               sz=Pt(16), bold=True,
               color=resolve_color(it.get("value_color", "white")),
               align=PP_ALIGN.CENTER, font=FH)
            _t(s, str(it.get("label", "")),
               ix + Inches(0.05), Inches(6.87), sw - Inches(0.08), Inches(0.28),
               sz=Pt(11),
               color=resolve_color(it.get("label_color", "#CCDDFF")),
               align=PP_ALIGN.CENTER, font=FB)

    # Footer strip
    if has_fstrip:
        _box(s, CX, Inches(7.02), CW, Inches(0.38), fill=DEEP_BLUE)
        _t(s, sd["footer_strip"],
           CX + Inches(0.15), Inches(7.08), CW - Inches(0.25), Inches(0.28),
           sz=Pt(12), bold=True, color=WHITE, align=PP_ALIGN.CENTER, font=FH)

    # Below-card bullets
    if has_below:
        bb = sd["below_bullets"]
        _blt(s, bb.get("items", []), CX, Inches(5.15), CW, Inches(1.7),
             sz=Pt(12.5), color=TEXT,
             hdg=bb.get("heading"), hc=bb.get("heading_color"))

    _sd_footer(s, sd)


# ── table ─────────────────────────────────────────────────────────────────────
def render_table(prs, sd: dict):
    """
    Slide options
    -------------
    columns    : list of { header, color, width }  (width in inches)
    rows       : list of lists  (each inner list = one row, same length as columns)
    row_height : float in inches (default 0.5)
    callout    : string  OR  { background, parts: [{ text, bold, color }] }
    pairs      : list of { before, after }  — before→after boxes below callout
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    cols     = sd.get("columns", [])
    rows     = sd.get("rows",    [])
    row_h    = Inches(float(sd.get("row_height", 0.5)))
    callout  = sd.get("callout")
    pairs    = sd.get("pairs", [])

    # Column x-positions (tight, no gap between columns)
    col_xs = []
    x_cur  = CX
    for col in cols:
        col_xs.append(x_cur)
        x_cur += Inches(float(col.get("width", 3.0)))

    # Header row
    for col, cx in zip(cols, col_xs):
        w = Inches(float(col.get("width", 3.0)))
        _box(s, cx, BY, w, Inches(0.4), fill=col.get("color", "charcoal"))
        _t(s, col.get("header", ""),
           cx + Inches(0.1), BY + Inches(0.08), w - Inches(0.15), Inches(0.3),
           sz=Pt(12), bold=True, color=WHITE, font=FH)

    # Data rows (alternating light_bg / white)
    for ri, row in enumerate(rows):
        rt = BY + Inches(0.4) + ri * row_h
        bg = WHITE if ri % 2 == 0 else LIGHT_BG
        for cell, cx, col in zip(row, col_xs, cols):
            w = Inches(float(col.get("width", 3.0)))
            _box(s, cx, rt, w, row_h, fill=bg)
            _t(s, str(cell),
               cx + Inches(0.1), rt + Inches(0.07), w - Inches(0.15),
               row_h - Inches(0.1),
               sz=Pt(11.5), color=TEXT, font=FB)

    rows_h_total = Inches(0.4) + len(rows) * row_h
    cal_y = BY + rows_h_total + Inches(0.08)

    _render_callout(s, callout, cal_y)

    # Before → After pairs
    if pairs:
        cal_h = Inches(0)
        if callout:
            if isinstance(callout, str):
                cal_h = Inches(0.54)
            elif isinstance(callout, dict):
                np = len(callout.get("parts", []))
                cal_h = Inches(max(0.12 + np * 0.36, 0.5))
        pairs_y = cal_y + cal_h + Inches(0.1)
        for i, pair in enumerate(pairs):
            bx = CX + i * Inches(2.5)
            _box(s, bx, pairs_y, Inches(1.15), Inches(0.65),
                 fill=RGBColor(0xFF, 0xEE, 0xEE))
            _t(s, pair.get("before", ""),
               bx + Inches(0.05), pairs_y + Inches(0.05), Inches(1.05), Inches(0.58),
               sz=Pt(10), color=RED, align=PP_ALIGN.CENTER, font=FB)
            _t(s, "→",
               bx + Inches(1.18), pairs_y + Inches(0.2),
               Inches(0.22), Inches(0.28),
               sz=Pt(12), bold=True, color=CHARCOAL, align=PP_ALIGN.CENTER)
            _box(s, bx + Inches(1.43), pairs_y, Inches(1.0), Inches(0.65),
                 fill=RGBColor(0xE8, 0xF5, 0xE9))
            _t(s, pair.get("after", ""),
               bx + Inches(1.48), pairs_y + Inches(0.05), Inches(0.9), Inches(0.58),
               sz=Pt(10), color=GREEN_OK, align=PP_ALIGN.CENTER, font=FB)

    _sd_footer(s, sd)


# ── metrics ───────────────────────────────────────────────────────────────────
def render_metrics(prs, sd: dict):
    """
    Slide options
    -------------
    metrics  : list of { value, label, color }
    table    : { headers: [...], widths: [...], rows: [[...], ...] }
    note     : string shown below the table (small muted text)
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    metrics = sd.get("metrics", [])
    n       = len(metrics)
    mw      = CW / n if n else CW

    for i, m in enumerate(metrics):
        mx = CX + i * mw
        _box(s, mx, BY, mw - Inches(0.1), Inches(1.95),
             fill=resolve_color(m.get("color", "deep_blue")))
        _t(s, str(m.get("value", "")), mx, BY + Inches(0.12),
           mw - Inches(0.1), Inches(0.95),
           sz=Pt(32), bold=True, color=WHITE, align=PP_ALIGN.CENTER, font=FH)
        _t(s, str(m.get("label", "")), mx, BY + Inches(1.1),
           mw - Inches(0.1), Inches(0.75),
           sz=Pt(11), color=RGBColor(0xEE, 0xEE, 0xEE),
           align=PP_ALIGN.CENTER, font=FB)

    tbl = sd.get("table")
    table_bottom = Inches(6.35)  # default note position when no table is present
    if tbl:
        hdrs     = tbl.get("headers", [])
        rows     = tbl.get("rows",    [])
        widths   = [float(w) for w in tbl.get("widths", [])]
        row_h    = Inches(float(tbl.get("row_height", 0.44)))
        if not widths:
            nh = len(hdrs)
            widths = [float(CW.inches / nh)] * nh if nh else []

        col_xs, x_cur = [], CX
        for w in widths:
            col_xs.append(x_cur)
            x_cur += Inches(w)

        ht = BY + Inches(2.12)
        for h, cx, w in zip(hdrs, col_xs, widths):
            _box(s, cx, ht, Inches(w), Inches(0.36), fill=DEEP_BLUE)
            _t(s, h, cx + Inches(0.07), ht + Inches(0.06),
               Inches(w) - Inches(0.1), Inches(0.28),
               sz=Pt(11), bold=True, color=WHITE, font=FH)

        for ri, (row, bg) in enumerate(zip(rows, [WHITE, LIGHT_BG] * 50)):
            rt = ht + Inches(0.36) + ri * row_h
            for cell, cx, w in zip(row, col_xs, widths):
                _box(s, cx, rt, Inches(w), row_h, fill=bg)
                c = GREEN_OK if str(cell) == "✅" else TEXT
                _t(s, str(cell), cx + Inches(0.07), rt + Inches(0.06),
                   Inches(w) - Inches(0.1), row_h - Inches(0.08),
                   sz=Pt(11), color=c, font=FB,
                   align=PP_ALIGN.CENTER if str(cell) == "✅" else PP_ALIGN.LEFT)

        table_bottom = ht + Inches(0.36) + len(rows) * row_h

    if note := sd.get("note"):
        note_y = table_bottom + Inches(0.1) if tbl else table_bottom
        _t(s, note, CX, note_y, CW, Inches(0.32),
           sz=Pt(11), color=MUTED, font=FB)

    _sd_footer(s, sd)


# ── flow ──────────────────────────────────────────────────────────────────────
def render_flow(prs, sd: dict):
    """
    Slide options
    -------------
    sources     : list of { title, color, fields }
    merge_label : string shown between sources and output
    output      : { label, color, description }
    bullets     : list of strings shown below the output box
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    sources = sd.get("sources", [])
    n       = len(sources)
    GAP     = Inches(0.25)
    AVAIL   = GX - CX - Inches(0.1)
    bw      = (AVAIL - (n - 1) * GAP) / n if n else AVAIL

    for i, src in enumerate(sources):
        sx = CX + i * (bw + GAP)
        _box(s, sx, BY, bw, Inches(2.05),
             fill=resolve_color(src.get("color", "deep_blue")))
        _t(s, src.get("title", ""), sx + Inches(0.12), BY + Inches(0.1),
           bw - Inches(0.2), Inches(0.42),
           sz=Pt(13), bold=True, color=WHITE, font=FH)
        _t(s, src.get("fields", ""), sx + Inches(0.12), BY + Inches(0.56),
           bw - Inches(0.2), Inches(1.4),
           sz=Pt(12), color=RGBColor(0xDD, 0xEE, 0xFF), font=FB)

    ml = sd.get("merge_label", "⬇  MATCH & MERGE  ⬇")
    _t(s, ml, CX + AVAIL / 2 - Inches(1.5), BY + Inches(2.12),
       Inches(3.0), Inches(0.42),
       sz=Pt(14), bold=True, color=ORANGE, align=PP_ALIGN.CENTER, font=FH)

    out   = sd.get("output", {})
    out_c = resolve_color(out.get("color", "orange"))
    _box(s, CX, BY + Inches(2.62), AVAIL, Inches(1.45),
         fill=RGBColor(0xFF, 0xF8, 0xF0), lc=out_c, lw=Pt(1.5))
    _t(s, out.get("label", ""), CX + Inches(0.15), BY + Inches(2.70),
       AVAIL - Inches(0.25), Inches(0.38),
       sz=Pt(14), bold=True, color=out_c, font=FH)
    _t(s, out.get("description", ""),
       CX + Inches(0.15), BY + Inches(3.1), AVAIL - Inches(0.25), Inches(0.9),
       sz=Pt(12), color=TEXT, font=FB)

    if bullets := sd.get("bullets"):
        _blt(s, bullets, CX, BY + Inches(4.15), AVAIL, Inches(1.2),
             sz=Pt(12), color=TEXT)

    _sd_footer(s, sd)


# ── split ─────────────────────────────────────────────────────────────────────
def render_split(prs, sd: dict):
    """
    Slide options
    -------------
    left         : { heading, heading_color, items: [...] }
    right        : { title, title_color, background, code }
                   Lines starting with  --  or  #  are rendered as comments (muted).
    footer_strip : string for a full-width coloured strip above the footer
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    left       = sd.get("left", {})
    right      = sd.get("right", {})
    has_fstrip = bool(sd.get("footer_strip"))

    blt_h = Inches(4.5) if has_fstrip else BH
    C1W   = Inches(4.75)
    C2X   = CX + Inches(5.1)
    C2W   = Inches(5.0)

    _blt(s, left.get("items", []), CX, BY, C1W, blt_h,
         sz=Pt(13), color=TEXT,
         hdg=left.get("heading"), hc=left.get("heading_color"))

    panel_bg = resolve_color(right.get("background", "#F5F8FF"))
    _box(s, C2X, BY, C2W, blt_h, fill=panel_bg)
    if rt := right.get("title"):
        _t(s, rt, C2X + Inches(0.15), BY + Inches(0.1), C2W - Inches(0.25), Inches(0.35),
           sz=Pt(13), bold=True,
           color=resolve_color(right.get("title_color", "deep_blue")), font=FH)

    code_lines = right.get("code", "").split("\n")
    for i, line in enumerate(code_lines):
        stripped = line.strip()
        is_cmt   = stripped.startswith("--") or stripped.startswith("#")
        lc       = MUTED if is_cmt else TEXT
        _t(s, line,
           C2X + Inches(0.15), BY + Inches(0.55) + i * Inches(0.225),
           C2W - Inches(0.25), Inches(0.225),
           sz=Pt(10), color=lc, font=FM)

    if has_fstrip:
        fstrip_y = BY + blt_h + Inches(0.1)
        _box(s, CX, fstrip_y, CW, Inches(0.46), fill=DEEP_BLUE)
        _t(s, sd["footer_strip"],
           CX + Inches(0.15), fstrip_y + Inches(0.08), CW - Inches(0.25), Inches(0.34),
           sz=Pt(14), bold=True, color=WHITE, align=PP_ALIGN.CENTER, font=FH)

    _sd_footer(s, sd)


# ── swimlanes ─────────────────────────────────────────────────────────────────
def render_swimlanes(prs, sd: dict):
    """
    Slide options
    -------------
    lanes : list of { title, color, items: [...] }
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    lanes = sd.get("lanes", [])
    n     = len(lanes)
    GAP   = Inches(0.2)
    AVAIL = GX - CX - Inches(0.1)
    lw    = (AVAIL - (n - 1) * GAP) / n if n else AVAIL

    for i, lane in enumerate(lanes):
        x     = CX + i * (lw + GAP)
        c     = resolve_color(lane.get("color", "deep_blue"))
        items = lane.get("items", [])
        _box(s, x, BY, lw, Inches(0.42), fill=c)
        _t(s, lane.get("title", ""),
           x + Inches(0.1), BY + Inches(0.06), lw - Inches(0.16), Inches(0.34),
           sz=Pt(12), bold=True, color=WHITE, font=FH)
        _box(s, x, BY + Inches(0.42), lw, BH - Inches(0.52),
             fill=LIGHT_BG, lc=c, lw=Pt(1))
        item_h = min(Inches(0.78), (BH - Inches(0.65)) / max(len(items), 1))
        for j, item in enumerate(items):
            _t(s, f"▸  {item}",
               x + Inches(0.12), BY + Inches(0.55) + j * item_h,
               lw - Inches(0.2), item_h - Inches(0.06),
               sz=Pt(12), color=TEXT, font=FB)

    _sd_footer(s, sd)


# ── checklist ─────────────────────────────────────────────────────────────────
def render_checklist(prs, sd: dict):
    """
    Slide options
    -------------
    title : large title (supports \\n for multi-line)
    items : list of { text, color }
    """
    s = _blank(prs); _white(s); _strip(s)
    _t(s, sd.get("title", ""), CX, Inches(0.42), Inches(9.5), Inches(1.8),
       sz=Pt(42), bold=True, color=CHARCOAL, font=FH)
    _rule(s, Inches(2.3), color=DEEP_BLUE, thick=Pt(3))
    for i, item in enumerate(sd.get("items", [])):
        ty = Inches(2.45) + i * Inches(0.62)
        c  = resolve_color(item.get("color", "deep_blue"))
        _box(s, CX, ty, Inches(0.32), Inches(0.46), fill=c)
        _t(s, "✅", CX, ty + Inches(0.04), Inches(0.32), Inches(0.4),
           sz=Pt(13), color=WHITE, align=PP_ALIGN.CENTER)
        _t(s, str(item.get("text", "")),
           CX + Inches(0.42), ty + Inches(0.05), Inches(9.5), Inches(0.46),
           sz=Pt(15), color=TEXT, font=FB)
    _sd_footer(s, sd)


# ── features ──────────────────────────────────────────────────────────────────
def render_features(prs, sd: dict):
    """
    Two-column layout where each item has a coloured sidebar bar,
    a bold title line, and a body description line.

    Slide options
    -------------
    left  : { title, title_color, items: [{ title, body }] }
    right : { title, title_color, items: [{ title, body }] }
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    left  = sd.get("left",  {})
    right = sd.get("right", {})
    C1X, C1W = CX, Inches(4.75)
    C2X, C2W = CX + Inches(5.2), Inches(4.75)
    _vline(s, CX + Inches(5.0), BY, BH)

    def _col(items, cx, cw, col_title, col_color_key):
        tc = resolve_color(col_color_key or "deep_blue")
        if col_title:
            _t(s, col_title, cx, BY, cw, Inches(0.35),
               sz=Pt(15), bold=True, color=tc, font=FH)
        item_h = Inches(0.88)
        for i, item in enumerate(items):
            ty = BY + Inches(0.42) + i * item_h
            _box(s, cx, ty, Inches(0.22), Inches(0.65), fill=tc)
            _t(s, str(item.get("title", "")),
               cx + Inches(0.3), ty + Inches(0.02), cw - Inches(0.32), Inches(0.28),
               sz=Pt(12.5), bold=True, color=tc, font=FH)
            _t(s, str(item.get("body", "")),
               cx + Inches(0.3), ty + Inches(0.32), cw - Inches(0.32), Inches(0.5),
               sz=Pt(12), color=TEXT, font=FB)

    _col(left.get("items", []),  C1X, C1W, left.get("title"),  left.get("title_color",  "deep_blue"))
    _col(right.get("items", []), C2X, C2W, right.get("title"), right.get("title_color", "orange"))
    _sd_footer(s, sd)


# ── architecture ──────────────────────────────────────────────────────────────
def render_architecture(prs, sd: dict):
    """
    Diagram layout:
      rows    — full-width horizontal rows (e.g. SOURCE SYSTEMS, ORCHESTRATION)
      layers  — three side-by-side layer boxes with arrows between them
      lambdas — row of Lambda function boxes at the bottom

    Slide options
    -------------
    rows    : list of { label, description, color, border_color }
    layers  : list of { label, description, color, border_color }
    lambdas : list of { label, color }
    """
    s = _blank(prs); _white(s); _strip(s)
    _header(s, sd.get("title", ""), sd.get("subtitle"))

    y = BY

    for row in sd.get("rows", []):
        fill = resolve_color(row.get("color", "lite_blue"))
        bc   = resolve_color(row.get("border_color", "deep_blue"))
        _box(s, CX, y, CW, Inches(0.72), fill=fill, lc=bc, lw=Pt(1.5))
        _t(s, row.get("label", ""), CX + Inches(0.12), y + Inches(0.07),
           CW - Inches(0.2), Inches(0.3), sz=Pt(10), bold=True, color=bc, font=FH)
        _t(s, row.get("description", ""), CX + Inches(0.12), y + Inches(0.37),
           CW - Inches(0.2), Inches(0.32), sz=Pt(11.5), color=TEXT, font=FB)
        y += Inches(0.78)

    layers = sd.get("layers", [])
    if layers:
        nl      = len(layers)
        GAP_L   = Inches(0.2)
        lay_w   = (CW - (nl - 1) * GAP_L) / nl
        lay_h   = Inches(2.55)
        for i, layer in enumerate(layers):
            lx   = CX + i * (lay_w + GAP_L)
            fill = resolve_color(layer.get("color", "lite_blue"))
            bc   = resolve_color(layer.get("border_color", "deep_blue"))
            _box(s, lx, y, lay_w, lay_h, fill=fill, lc=bc, lw=Pt(1.5))
            _t(s, layer.get("label", ""), lx + Inches(0.12), y + Inches(0.07),
               lay_w - Inches(0.2), Inches(0.3),
               sz=Pt(10), bold=True, color=bc, font=FH)
            _t(s, layer.get("description", ""), lx + Inches(0.12), y + Inches(0.42),
               lay_w - Inches(0.2), Inches(2.0),
               sz=Pt(12), color=TEXT, font=FB)
        # Arrows between layers
        for i in range(nl - 1):
            ax = CX + (i + 1) * (lay_w + GAP_L) - GAP_L / 2 - Inches(0.18)
            _t(s, "→", ax, y + lay_h / 2 - Inches(0.22), Inches(0.34), Inches(0.42),
               sz=Pt(22), bold=True, color=ORANGE, align=PP_ALIGN.CENTER)
        y += lay_h + Inches(0.12)

    lambdas = sd.get("lambdas", [])
    if lambdas:
        nla   = len(lambdas)
        GAP_A = Inches(0.15)
        lbw   = (CW - (nla - 1) * GAP_A) / nla
        for i, lam in enumerate(lambdas):
            lx = CX + i * (lbw + GAP_A)
            _box(s, lx, y, lbw, Inches(0.65),
                 fill=resolve_color(lam.get("color", "deep_blue")))
            _t(s, lam.get("label", ""),
               lx + Inches(0.08), y + Inches(0.05), lbw - Inches(0.12), Inches(0.58),
               sz=Pt(10), bold=True, color=WHITE, align=PP_ALIGN.CENTER, font=FH)

    _sd_footer(s, sd)


# ── Renderer registry ─────────────────────────────────────────────────────────
RENDERERS: dict = {
    "title":        render_title,
    "bullets":      render_bullets,
    "cards":        render_cards,
    "table":        render_table,
    "metrics":      render_metrics,
    "flow":         render_flow,
    "split":        render_split,
    "swimlanes":    render_swimlanes,
    "checklist":    render_checklist,
    "features":     render_features,
    "architecture": render_architecture,
}
