"""
theme.py  —  Shared theme layer.
Colours, fonts, layout constants, and primitive drawing helpers.
All presentation files import from here; nothing content-specific lives here.
"""
from __future__ import annotations
import os
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

# ── Named colour palette ──────────────────────────────────────────────────────
# Use these names in JSON/YAML content files.
# Hex strings like '#156082' are also accepted by resolve_color().
NAMED_COLORS: dict = {
    # Core palette
    "charcoal":     RGBColor(0x38, 0x3E, 0x48),   # title text
    "dark_blue":    RGBColor(0x0E, 0x28, 0x41),   # dark accent
    "deep_blue":    RGBColor(0x15, 0x60, 0x82),   # primary accent
    "orange":       RGBColor(0xE9, 0x71, 0x32),   # secondary accent
    "sky":          RGBColor(0x0F, 0x9E, 0xD5),   # tertiary accent
    "green":        RGBColor(0x19, 0x6B, 0x24),   # success / green
    "green_ok":     RGBColor(0x19, 0x6B, 0x24),   # alias
    "green_lite":   RGBColor(0x4E, 0xA7, 0x2E),
    "purple":       RGBColor(0x6A, 0x3F, 0xC0),
    "amber":        RGBColor(0xFF, 0xA5, 0x00),
    "red":          RGBColor(0xC0, 0x20, 0x20),
    # Neutrals
    "white":        RGBColor(0xFF, 0xFF, 0xFF),
    "light_bg":     RGBColor(0xF8, 0xF8, 0xF8),
    "rule_grey":    RGBColor(0xD0, 0xD0, 0xD0),
    "mid_grey":     RGBColor(0xE8, 0xE8, 0xE8),
    "text":         RGBColor(0x22, 0x22, 0x22),
    "muted":        RGBColor(0x66, 0x66, 0x66),
    # Light tints (card / box backgrounds)
    "lite_blue":    RGBColor(0xE3, 0xEE, 0xF8),
    "lite_green":   RGBColor(0xE8, 0xF5, 0xE9),
    "lite_purple":  RGBColor(0xF3, 0xEC, 0xFF),
    "lite_orange":  RGBColor(0xFF, 0xF3, 0xE8),
    "lite_red":     RGBColor(0xFF, 0xEE, 0xEE),
}

# Direct references (use in Python code for clarity)
CHARCOAL  = NAMED_COLORS["charcoal"]
DARK_BLUE = NAMED_COLORS["dark_blue"]
DEEP_BLUE = NAMED_COLORS["deep_blue"]
ORANGE    = NAMED_COLORS["orange"]
SKY       = NAMED_COLORS["sky"]
GREEN     = NAMED_COLORS["green"]
PURPLE    = NAMED_COLORS["purple"]
WHITE     = NAMED_COLORS["white"]
LIGHT_BG  = NAMED_COLORS["light_bg"]
RULE_C    = NAMED_COLORS["rule_grey"]
MID_GREY  = NAMED_COLORS["mid_grey"]
TEXT      = NAMED_COLORS["text"]
MUTED     = NAMED_COLORS["muted"]
GREEN_OK  = NAMED_COLORS["green_ok"]
AMBER     = NAMED_COLORS["amber"]
RED       = NAMED_COLORS["red"]


def resolve_color(c) -> RGBColor:
    """
    Accept any of:
      - Named string  : 'deep_blue', 'orange', 'green_ok' …
      - Hex string    : '#156082'  or  '156082'
      - RGBColor      : passed through unchanged
      - None / empty  : returns TEXT (#222222)
    """
    if isinstance(c, RGBColor):
        return c
    if not c:
        return TEXT
    s = str(c).strip()
    # Try hex
    h = s[1:] if s.startswith("#") else (s if len(s) == 6 else None)
    if h and len(h) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in h):
        try:
            return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            pass
    # Try named
    return NAMED_COLORS.get(s.lower(), TEXT)


# ── Fonts ─────────────────────────────────────────────────────────────────────
FH = "Open Sauce Bold"   # headings / bold labels
FB = "Open Sauce"        # body text
FM = "Courier New"       # code / monospace


# ── Slide geometry ────────────────────────────────────────────────────────────
SW  = Inches(13.33)   # slide width
SH  = Inches(7.5)     # slide height
GX  = Inches(10.9)    # gradient strip left edge
GW  = Inches(2.43)    # gradient strip width
CX  = Inches(0.55)    # content left margin
CW  = Inches(10.1)    # safe content width (stops before strip)
TY  = Inches(0.42)    # title y
TH  = Inches(0.82)    # title height
RY  = Inches(1.30)    # rule y (below title)
STY = Inches(1.38)    # subtitle y
STH = Inches(0.35)    # subtitle height
BY  = Inches(1.82)    # body start y (below subtitle)
BH  = Inches(5.15)    # full body height
FTY = Inches(7.10)    # footer y


# ── Gradient strip (orange → pale lime) ──────────────────────────────────────
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_STRIP  = os.path.join(_ASSETS, "gradient_strip.png")


def _ensure_strip() -> None:
    """Generate the gradient strip PNG if it does not already exist."""
    if os.path.exists(_STRIP):
        return
    os.makedirs(_ASSETS, exist_ok=True)
    try:
        from PIL import Image
        W, H = 280, 900
        img  = Image.new("RGB", (W, H))
        px   = img.load()
        for y in range(H):
            t = y / H
            r = int(0xFF * (1 - t) + 0xD3 * t)
            g = int(0x66 * (1 - t) + 0xFE * t)
            b = int(0x00 * (1 - t) + 0xCC * t)
            for x in range(W):
                px[x, y] = (r, g, b)
        img.save(_STRIP)
        print(f"  [theme] Gradient strip generated: {_STRIP}")
    except ImportError:
        print("  [theme] Pillow not installed — install with: pip install Pillow")


# ── Primitive drawing helpers ─────────────────────────────────────────────────

def _blank(prs):
    """Add a blank slide (no placeholders)."""
    return prs.slides.add_slide(prs.slide_layouts[6])


def _white(s):
    """Set slide background to pure white."""
    f = s.background.fill
    f.solid()
    f.fore_color.rgb = WHITE


def _strip(s):
    """Add the right-side gradient strip to a slide."""
    _ensure_strip()
    if os.path.exists(_STRIP):
        s.shapes.add_picture(_STRIP, GX, Inches(0), GW, SH)


def _box(s, left, top, width, height, fill=None, lc=None, lw=Pt(0)):
    """Add a solid-filled rectangle. fill/lc accept colour names, hex, or RGBColor."""
    shp = s.shapes.add_shape(1, left, top, width, height)
    shp.line.width = lw
    if fill:
        shp.fill.solid()
        shp.fill.fore_color.rgb = resolve_color(fill)
    else:
        shp.fill.background()
    if lc:
        shp.line.color.rgb = resolve_color(lc)
    else:
        shp.line.fill.background()
    return shp


def _t(s, text: str, left, top, width, height,
       sz=Pt(14), bold=False, italic=False,
       color=None, align=PP_ALIGN.LEFT, wrap=True, font=None):
    """Add a text box with a single run."""
    c   = resolve_color(color) if color else TEXT
    txb = s.shapes.add_textbox(left, top, width, height)
    tf  = txb.text_frame
    tf.word_wrap = wrap
    p   = tf.paragraphs[0]
    p.alignment = align
    r   = p.add_run()
    r.text          = str(text)
    r.font.size     = sz
    r.font.bold     = bold
    r.font.italic   = italic
    r.font.color.rgb = c
    r.font.name     = font or (FH if bold else FB)
    return txb


def _blt(s, items: list, left, top, width, height,
         sz=Pt(13), color=None, bullet="•  ", numbered=False,
         hdg=None, hc=None, hsz=None):
    """
    Bullet or numbered list, with an optional bold heading on the first line.

    Parameters
    ----------
    items    : list of strings
    bullet   : bullet prefix string  (ignored when numbered=True)
    numbered : if True, prefix with  1.  2.  3. …
    hdg      : optional heading text (rendered bold above the list)
    hc       : heading colour (name/hex/RGBColor)
    hsz      : heading font size (default: sz + 2pt)
    """
    c   = resolve_color(color) if color else TEXT
    txb = s.shapes.add_textbox(left, top, width, height)
    tf  = txb.text_frame
    tf.word_wrap = True
    first = True
    if hdg:
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text           = str(hdg)
        r.font.size      = hsz or (sz + Pt(2))
        r.font.bold      = True
        r.font.color.rgb = resolve_color(hc) if hc else DEEP_BLUE
        r.font.name      = FH
        first = False
    for i, item in enumerate(items):
        p = tf.add_paragraph() if not first else tf.paragraphs[0]
        first = False
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        prefix   = f"{i + 1}.  " if numbered else bullet
        r.text           = f"{prefix}{item}"
        r.font.size      = sz
        r.font.color.rgb = c
        r.font.name      = FB
    return txb


def _rule(s, y, color=None, thick=Pt(1.5)):
    """Horizontal rule that stops before the gradient strip."""
    c = resolve_color(color) if color else RULE_C
    _box(s, CX, y, GX - CX - Inches(0.15), thick, fill=c)


def _vline(s, x, y_top, height, color=None, w=Pt(1)):
    """Thin vertical divider line."""
    c = resolve_color(color) if color else RULE_C
    _box(s, x, y_top, w, height, fill=c)


def _header(s, title: str, subtitle: str = None):
    """
    Standard slide header: large charcoal title + deep-blue rule + optional subtitle.
    Used by most content slide types.
    """
    _t(s, title, CX, TY, CW, TH, sz=Pt(36), bold=True, color=CHARCOAL, font=FH)
    _rule(s, RY, color=DEEP_BLUE, thick=Pt(2))
    if subtitle:
        _t(s, subtitle, CX, STY, CW, STH, sz=Pt(14), color=DEEP_BLUE, font=FB)


def _footer(s, txt: str = ""):
    """Small footer text at the bottom of the slide."""
    if txt:
        _t(s, txt, CX, FTY, CW, Inches(0.32), sz=Pt(10), color=MUTED, font=FB)
