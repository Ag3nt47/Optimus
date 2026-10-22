# Adapted from Ag3nt47/AngeronaSuite (MIT); see bundled NOTICE and LICENSE-Angerona.txt.
"""Theming engine — multiple appearance packages + custom accent tint.

Themes are palette dicts fed into one QSS template, so adding a theme is just
adding a palette. Two presets ship:

  • cyber : Modern Cyber/Infosec — dark, sharp geometric edges, neon accents.
  • crt   : Retro 1980s CRT — green phosphor on black, monospace.

A custom accent colour can override either theme's accent for easy tinting.
"""
from __future__ import annotations

from enum import IntEnum

class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

SEVERITY_COLOR = {
    Severity.INFO: "#6b7280",
    Severity.LOW: "#3b82f6",
    Severity.MEDIUM: "#f97316",   # orange — was amber; easier to read on dark bg
    Severity.HIGH: "#ef4444",     # red    — was orange
    Severity.CRITICAL: "#b91c1c", # deep red, visually distinct from HIGH
}

# Font stacks — each theme picks either a UI-style or mono-style stack.
# Segoe UI: Windows system font — crisp, readable, zero-install.
# Fira Code: beautiful ligature monospace for code / console areas.
_CYBER_FONT = "'Segoe UI','Helvetica Neue','Arial','Noto Sans','DejaVu Sans','Liberation Sans',sans-serif"
_CRT_FONT   = "'Cascadia Mono','Consolas','Courier New',monospace"
# Slate theme: Segoe UI for the UI surface, Fira Code in code panels.
# The QSS `font-family` on * sets the default; code-area widgets
# (CommandConsolePanel, etc.) override with their own QFont/setStyleSheet.
_SLATE_FONT = _CYBER_FONT

THEMES = {
    "cyber": {
        "label": "Modern Cyber",
        "bg": "#0a0e14", "panel": "#0f141c", "panel2": "#141b26",
        "border": "#263345", "text": "#d6e2f0", "dim": "#94a3b8",
        "accent": "#1f9cff", "accent2": "#ff7a1a", "font": _CYBER_FONT,
        "radius": "10px",
        # Qt stylesheets parse eight-digit hex as #AARRGGBB, not CSS
        # #RRGGBBAA. Keep the low-opacity byte first.
        "alt_row": "#08ffffff",      # alternating table row tint
        "chip_h": "44px",
    },
    "crt": {
        "label": "Retro CRT Terminal",
        "bg": "#000a00", "panel": "#001200", "panel2": "#001a00",
        "border": "#00b347", "text": "#33ff66", "dim": "#1f8a3a",
        "accent": "#39ff14", "accent2": "#00ff66", "font": _CRT_FONT,
        "radius": "2px",
        "alt_row": "#0800ff00",
        "chip_h": "44px",
    },
    # ── Dark Slate ───────────────────────────────────────────────────────
    # A professional, calm palette for extended analyst sessions.
    # Inspired by Tailwind slate — bg=slate-950, panel=slate-900,
    # panel2=slate-800, accent=sky-400.  The accent is desaturated enough
    # to stay readable for hours without eye strain.
    "slate": {
        "label": "Dark Slate",
        "bg": "#020617", "panel": "#0f172a", "panel2": "#1e293b",
        "border": "#334155", "text": "#f1f5f9", "dim": "#94a3b8",
        "accent": "#38bdf8", "accent2": "#fb923c", "font": _SLATE_FONT,
        "radius": "8px",
        "alt_row": "#06ffffff",
        "chip_h": "44px",
    },
}


def available_themes():
    """[(key, label), ...] for the settings dropdown."""
    return [(k, v["label"]) for k, v in THEMES.items()]


def clamp_scale(scale: float) -> float:
    """Clamp a raw UI-scale factor into a readable band.

    The window feeds in a factor derived from its current size; we never let it
    drop so far that text becomes illegible, nor blow up so large that buttons
    stop fitting. 1.0 == the original design size. Kept as a module function so
    the main window and the QSS builder agree on the exact same band."""
    try:
        s = float(scale)
    except (TypeError, ValueError):
        s = 1.0
    return max(0.75, min(1.35, s))


def _qt_alpha(color: str, alpha: str) -> str:
    """Return a Qt-QSS #AARRGGBB tint for a six-digit hex colour."""
    raw = str(color).strip()
    if len(raw) == 7 and raw.startswith("#"):
        return f"#{alpha}{raw[1:]}"
    return raw


def build_qss(name: str = "cyber", accent: str | None = None,
              scale: float = 1.0) -> str:
    p = dict(THEMES.get(name, THEMES["cyber"]))
    # Fill in optional keys that older theme dicts may not have.
    p.setdefault("alt_row", "#08ffffff")
    p.setdefault("chip_h",  "44px")
    if accent:
        p["accent"] = accent
    r = p["radius"]

    # ── Responsive scale ──────────────────────────────────────────────────
    # Every font-size and padding below is derived from the design value times
    # a clamped scale factor, so the whole surface — buttons, labels, table
    # text — grows and shrinks together with the window while staying readable.
    s = clamp_scale(scale)

    def px(v: float) -> str:
        return f"{max(1, round(v * s))}px"

    fs_base   = px(13)   # global default text
    fs_brand  = px(26)
    fs_tag    = px(11)
    fs_page   = px(20)
    fs_sect   = px(13)
    fs_card_v = px(30)
    fs_card_l = px(11)
    fs_hdr    = px(11)   # table header sections
    # Button paddings (vertical, horizontal) for normal + pressed states.
    pad_pri   = f"{px(9)} {px(16)}"
    pad_pri_p = f"{px(11)} {px(16)} {px(7)} {px(16)}"
    pad_btn   = f"{px(7)} {px(14)}"
    pad_btn_p = f"{px(9)} {px(14)} {px(5)} {px(14)}"
    pad_pill  = f"{px(5)} {px(14)}"
    pad_input = px(7)
    ind_wh    = px(18)   # checkbox indicator
    # Status-strip chip height scales too so chips don't clip larger text.
    try:
        chip_px = int(str(p['chip_h']).replace("px", "").strip())
    except (TypeError, ValueError):
        chip_px = 44
    chip_h = px(chip_px)
    accent_15 = _qt_alpha(p["accent"], "15")
    accent_22 = _qt_alpha(p["accent"], "22")
    accent_33 = _qt_alpha(p["accent"], "33")
    accent_44 = _qt_alpha(p["accent"], "44")
    accent_55 = _qt_alpha(p["accent"], "55")
    # A painted translucent surface gives depth without a platform compositor,
    # blur effect, extra backing buffers, or continuously animated shadows.
    surface = p["panel"] if name == "crt" else (
        "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
        f"stop:0 {_qt_alpha(p['panel2'], 'ed')}, "
        f"stop:1 {_qt_alpha(p['panel'], 'f5')})"
    )

    return f"""
/* ── Base ─────────────────────────────────────────────────────────────── */
* {{ font-family: {p['font']}; font-size: {fs_base}; color: {p['text']}; }}
QMainWindow, QWidget {{ background: {p['bg']}; }}
/* Labels inherit their panel surface instead of painting dark rectangles. */
QLabel {{ background: transparent; }}
QCheckBox, QRadioButton {{ background: transparent; }}
QScrollArea#DashboardViewport {{ border: none; }}

/* ── Typography helpers ────────────────────────────────────────────────── */
#Brand     {{ font-size: {fs_brand}; font-weight: 800; letter-spacing: 6px; color: {p['accent']}; }}
#Tagline   {{ color: {p['dim']}; font-size: {fs_tag}; }}
#PageTitle {{ font-size: {fs_page}; font-weight: 800; letter-spacing: 2px; color: {p['text']}; }}
#SectionTitle {{ font-size: {fs_sect}; font-weight: 700; letter-spacing: 1px;
                 color: {p['accent']}; padding: 2px 2px 6px 2px; }}
#Pill {{ border-radius: {r}; padding: {pad_pill}; font-weight: 700; }}

/* ── Panels / cards ────────────────────────────────────────────────────── */
#Panel {{ background: {surface}; border: 1px solid {p['border']};
          border-radius: {r}; }}
#Card  {{ background: {surface}; border: 1px solid {p['border']};
          border-left: 3px solid {p['accent']}; border-radius: {r}; }}
#Card[alert="true"] {{ border-color: #88ef4444; border-left: 3px solid #ef4444; }}
#CardValue {{ font-size: {fs_card_v}; font-weight: 800; color: {p['text']}; }}
#CardLabel {{ color: {p['dim']}; font-size: {fs_card_l}; letter-spacing: 1px; }}

/* ── Tables — alternating zebra, compact header, hover row ────────────── */
QTableWidget {{
    background: {p['panel']};
    border: 1px solid {p['border']};
    border-radius: {r};
    gridline-color: {p['border']};
    alternate-background-color: {p['alt_row']};
}}
QTableWidget::item {{
    padding: 4px 6px;
    border: none;
}}
QTableWidget::item:alternate {{
    background: {p['alt_row']};
}}
QTableWidget::item:hover {{
    background: {accent_15};
}}
QTableWidget::item:selected {{
    background: {accent_33};
    color: {p['text']};
}}
QHeaderView::section {{
    background: {p['panel2']};
    color: {p['dim']};
    border: none;
    border-bottom: 2px solid {accent_55};
    padding: 6px 8px;
    font-weight: 700;
    letter-spacing: 1px;
    font-size: {fs_hdr};
    text-transform: uppercase;
}}

/* ── Buttons ───────────────────────────────────────────────────────────── */
QPushButton#Primary {{
    background: {p['accent']}; color: {p['bg']};
    border: none; border-radius: {r};
    padding: {pad_pri}; font-weight: 700;
    border-bottom: 2px solid {p['accent2']};   /* subtle raised edge */
}}
QPushButton#Primary:hover {{ background: {p['accent2']}; }}
/* Pressed: content dips ~2px and the raised edge collapses → tactile "push". */
QPushButton#Primary:pressed {{
    background: {p['accent2']};
    padding: {pad_pri_p};
    border-bottom: 0px;
}}
/* Danger / Critical — the two red header actions (Red Team sim, Stop). Styled
   here (not inline) so they scale with the rest of the surface. */
QPushButton#Danger {{
    background: #dc2626; color: white; font-weight: 800;
    border: none; border-radius: {r}; padding: {pad_pri};
}}
QPushButton#Danger:hover {{ background: #ef4444; }}
QPushButton#Danger:pressed {{ background: #b91c1c; padding: {pad_pri_p}; }}
QPushButton#Critical {{
    background: #ef4444; color: white; font-weight: 800;
    border: none; border-radius: {r}; padding: {pad_pri};
}}
QPushButton#Critical:hover {{ background: #f87171; }}
QPushButton#Critical:pressed {{ background: #dc2626; padding: {pad_pri_p}; }}
QPushButton {{
    background: {p['panel2']};
    border: 1px solid {p['border']};
    border-bottom: 2px solid {p['border']};    /* raised edge for depth */
    border-radius: {r};
    padding: {pad_btn};
    color: {p['text']};
}}
QPushButton:hover {{
    background: {accent_22};
    border-color: {p['accent']};
}}
/* Every button visibly depresses on click: darker fill, label shifts down, and
   the raised bottom edge collapses so it reads as physically pushed in. */
QPushButton:pressed {{
    background: {accent_44};
    border: 1px solid {p['accent']};
    border-bottom: 0px;
    padding: {pad_btn_p};
}}
QPushButton:checked {{
    background: {accent_33};
    border: 1px solid {p['accent']};
}}
QPushButton:focus {{ border-color: {p['accent']}; background: {accent_22}; }}
QPushButton#Primary:focus {{ border: 2px solid {p['text']}; }}
QPushButton:disabled, QPushButton#Primary:disabled {{
    background: {p['panel']}; color: {p['dim']}; border-color: {p['border']};
}}
QGroupBox {{
    border: 1px solid {p['border']}; border-radius: {r};
    margin-top: 12px; padding: 14px 12px 12px 12px; background: {p['panel']};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px;
    color: {p['text']}; font-weight: 700; }}
QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: 6px; }}
QTabBar::tab {{ background: {p['panel']}; color: {p['dim']}; padding: 9px 12px;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {p['text']}; background: {p['panel2']};
    border-bottom-color: {p['accent']}; }}
QProgressBar#WorkflowProgress {{ min-height: 24px; max-height: 24px;
    text-align: center; font-weight: 700; border-radius: 7px; }}
QProgressBar#WorkflowProgress::chunk {{ background: {accent_55}; border-radius: 6px; }}
QLabel#BackgroundStatus {{ color: {p['dim']}; padding: 4px 0; }}
QScrollArea {{ border: none; }}

/* ── Inputs ────────────────────────────────────────────────────────────── */
QLineEdit, QComboBox, QPlainTextEdit {{
    background: {p['bg']};
    border: 1px solid {p['border']};
    border-radius: {r};
    padding: {pad_input};
    color: {p['text']};
}}
QComboBox QAbstractItemView {{
    background: {p['panel']};
    color: {p['text']};
    selection-background-color: {accent_44};
}}
QCheckBox::indicator {{ width: {ind_wh}; height: {ind_wh}; }}
QProgressBar {{
    background: {p['bg']};
    border: 1px solid {p['border']};
    border-radius: 3px;
}}
QProgressBar::chunk {{
    background: {p['accent']};
    border-radius: 2px;
}}
QScrollBar:vertical  {{ background: {p['panel']}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 5px; }}
QScrollBar:horizontal {{ background: {p['panel']}; height: 8px; }}
QScrollBar::handle:horizontal {{ background: {p['border']}; border-radius: 4px; }}

/* ── StatusStrip — background; chips get per-chip inline QSS each tick. */
#StatusStrip {{
    background: {p['panel']};
    border-top: 1px solid {p['border']};
    border-radius: 0px;
    min-height: {chip_h};
}}

/* ── Splitter handles: 3px visible grab lines ─────────────────────────── */
QSplitter::handle:horizontal {{ background: {p['border']}; width: 3px; }}
QSplitter::handle:vertical   {{ background: {p['border']}; height: 3px; }}

/* ── ToolTips ──────────────────────────────────────────────────────────── */
QToolTip {{
    background: {p['panel2']};
    color: {p['text']};
    border: 1px solid {p['border']};
    padding: 4px 8px;
    border-radius: 4px;
}}
"""


# Monospace font string for code/console panels (overrides theme base font).
# Import this wherever a QFont or setStyleSheet needs a coding font.
MONO_FONT_FAMILY = "'Fira Code','Cascadia Mono','JetBrains Mono','Consolas',monospace"

# Back-compat: some modules import DARK_QSS directly.
DARK_QSS = build_qss("cyber")

