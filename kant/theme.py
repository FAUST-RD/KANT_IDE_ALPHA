"""Theme colors, styles, and top-level UI constants.

set_theme() rebinds the module-level color globals in place; read them live
as theme.<NAME> (never `from kant.theme import <NAME>`) so a theme switch is
visible to widgets built afterward. The *_style() builder functions below
follow the same rule automatically — they read the current globals at call
time, so no separate rebuild step is needed for them inside set_theme().
"""

NIGHT = False
BG = '#ffffff'; PANEL = '#fbfcff'; PANEL2 = '#eef1f6'; BORDER = '#d7dce5'; BORDER_WEAK = '#e5e9f0'
TEXT = '#111827'; DIM = '#64748b'; TEXT_DISABLED = '#9aa4b2'; ACCENT = '#f3bd27'; CODE_BG = '#f3f5f9'
# HOT is deliberately NOT gold: mappa.py's EdgeFlowPopup picks it specifically so a "pinned" state
# reads as a distinct color from ACCENT, not a tint of the same one (see its own apply_style
# comment) — HOT was already close to the old blue ACCENT's warm-contrast pairing, and since ACCENT
# is gold now too, an unchanged HOT would have collapsed both into near-identical golds
HOT = '#ea580c'; OK = '#15803d'; WARN = '#7c3aed'; DANGER = '#dc2626'
HL_COMMENT = '#7a7f87'; HL_STRING = '#067d17'; HL_NUMBER = '#1750eb'; HL_KEYWORD = '#cf5b00'

TAG_COLORS = {
    'MOD': '#7c3aed', 'CLS': '#0f766e', 'FN': '#b45309', 'TYP': '#2563eb',
    'CST': '#be123c', 'VAR': '#475569', 'CFG': '#9333ea', 'TST': '#dc2626',
}
TAG_BACKGROUNDS = {
    'MOD': '#f3e8ff', 'CLS': '#ccfbf1', 'FN': '#fef3c7', 'TYP': '#dbeafe',
    'CST': '#ffe4e6', 'VAR': '#e2e8f0', 'CFG': '#f3e8ff', 'TST': '#fee2e2',
}

DAY_TAG_COLORS = dict(TAG_COLORS)
DAY_TAG_BACKGROUNDS = dict(TAG_BACKGROUNDS)
NIGHT_TAG_COLORS = {
    'MOD': '#c084fc', 'CLS': '#2dd4bf', 'FN': '#fbbf24', 'TYP': '#60a5fa',
    'CST': '#fb7185', 'VAR': '#cbd5e1', 'CFG': '#d8b4fe', 'TST': '#f87171',
}
NIGHT_TAG_BACKGROUNDS = {
    'MOD': '#312e81', 'CLS': '#134e4a', 'FN': '#451a03', 'TYP': '#172554',
    'CST': '#4c0519', 'VAR': '#334155', 'CFG': '#3b0764', 'TST': '#450a0a',
}

# Design tokens — single source for spacing/radius/control metrics, so widgets.py/mainwindow.py/
# dialogs.py stop hand-rolling one-off pixel values per control. Values themselves never change
# with the theme (only colors do); kept as plain module constants, not part of set_theme()'s
# rebuild.
RADIUS = 4  # single corner-radius tier app-wide; pill shapes (toggle switches) opt out explicitly
SPACE_1, SPACE_2, SPACE_3, SPACE_4 = 4, 8, 12, 16
ICON_BTN = 28       # square icon-only buttons
CONTROL_H = 28      # text buttons / inputs / tab height
TOPBAR_H = 38       # single consolidated top bar (menu + primary actions)
CONTEXTBAR_H = 32   # file-tab / context bar under the top bar
STATUSBAR_H = 23

APP_STYLE = f'''
QWidget {{ background:{BG}; color:{TEXT}; selection-background-color:{ACCENT}; selection-color:#ffffff; }}
QSplitter::handle {{ background:#e2e8f0; }}
QSplitter::handle:hover {{ background:{ACCENT}; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background:{BG}; border:1px solid #e2e8f0; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background:#94a3b8; border-radius:4px; min-height:24px; min-width:24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; width:0; }}
'''

BUTTON_STYLE = f'''
QPushButton {{
    background:{PANEL}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:{RADIUS}px; padding:7px 13px; font-weight:700;
}}
QPushButton:hover {{ background:#fdf3d8; color:{ACCENT}; border-color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT}; color:#ffffff; }}
QPushButton:disabled {{ color:{DIM}; border-color:#e2e8f0; background:#f1f5f9; }}
'''

# NOTE: every on/off toggle in the app used to be styled here via a QSS qradialgradient thumb
# trick (CHECKBOX_STYLE) — removed. That gradient's coordinates are scaled against the checkbox's
# FULL bounding rect (indicator + label text), not just the ::indicator sub-control's own rect, a
# real Qt stylesheet limitation with no per-instance fix possible from a single shared QSS string
# (a short label and a long one need different coordinate fractions for the same indicator size).
# kant/widgets.py's ToggleSwitch replaces it — a QCheckBox subclass that paints its own track/
# thumb directly instead of relying on that gradient.


def set_theme(night=False):
    global NIGHT, BG, PANEL, PANEL2, BORDER, BORDER_WEAK, TEXT, DIM, TEXT_DISABLED, ACCENT, CODE_BG
    global HOT, OK, WARN, DANGER, HL_COMMENT, HL_STRING, HL_NUMBER, HL_KEYWORD
    global APP_STYLE, BUTTON_STYLE

    NIGHT = night
    if night:
        # neutral-black dark palette: BG is the deepest surface (editor/content), PANEL one
        # step up (chrome bars, side panels), PANEL2 a further step up (hover states, nested
        # surfaces inside a panel) — exactly three tonal levels, no more. CODE_BG stays pure black
        # so the coding area still recesses below PANEL the same way it does in day mode.
        BG = '#090909'; PANEL = '#111111'; PANEL2 = '#1a1a1a'; BORDER = '#303030'; BORDER_WEAK = '#242424'
        TEXT = '#e5e5e5'; DIM = '#a3a3a3'; TEXT_DISABLED = '#666666'; ACCENT = '#f3bd27'; CODE_BG = '#000000'
        # a lighter, more saturated orange than the day value — needs to read clearly against the
        # dark BG/CODE_BG while staying just as distinct from gold ACCENT (see the top-level HOT
        # comment for why this can't just be a tint of ACCENT)
        HOT = '#fb923c'; OK = '#4ade80'; WARN = '#c084fc'; DANGER = '#f87171'
        HL_COMMENT = '#8b93a3'; HL_STRING = '#86efac'; HL_NUMBER = '#93c5fd'; HL_KEYWORD = '#f59e0b'
        TAG_COLORS.clear(); TAG_COLORS.update(NIGHT_TAG_COLORS)
        TAG_BACKGROUNDS.clear(); TAG_BACKGROUNDS.update(NIGHT_TAG_BACKGROUNDS)
        hover = '#1a1a1c'; disabled_border = '#2a2a2e'; disabled_bg = '#0a0a0c'
    else:
        BG = '#ffffff'; PANEL = '#fbfcff'; PANEL2 = '#eef1f6'; BORDER = '#d7dce5'; BORDER_WEAK = '#e5e9f0'
        TEXT = '#111827'; DIM = '#64748b'; TEXT_DISABLED = '#9aa4b2'; ACCENT = '#f3bd27'; CODE_BG = '#f3f5f9'
        HOT = '#ea580c'; OK = '#15803d'; WARN = '#7c3aed'; DANGER = '#dc2626'
        HL_COMMENT = '#7a7f87'; HL_STRING = '#067d17'; HL_NUMBER = '#1750eb'; HL_KEYWORD = '#cf5b00'
        TAG_COLORS.clear(); TAG_COLORS.update(DAY_TAG_COLORS)
        TAG_BACKGROUNDS.clear(); TAG_BACKGROUNDS.update(DAY_TAG_BACKGROUNDS)
        hover = '#fdf3d8'; disabled_border = '#e2e8f0'; disabled_bg = '#f1f5f9'

    APP_STYLE = f'''
QWidget {{ background:{BG}; color:{TEXT}; selection-background-color:{ACCENT}; selection-color:#ffffff; }}
QSplitter::handle {{ background:{BORDER}; }}
QSplitter::handle:hover {{ background:{ACCENT}; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background:{BG}; border:1px solid {BORDER}; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background:{DIM}; border-radius:4px; min-height:24px; min-width:24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; width:0; }}
'''
    BUTTON_STYLE = f'''
QPushButton {{
    background:{PANEL}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:{RADIUS}px; padding:7px 13px; font-weight:700;
}}
QPushButton:hover {{ background:{hover}; color:{ACCENT}; border-color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT}; color:#ffffff; }}
QPushButton:disabled {{ color:{DIM}; border-color:{disabled_border}; background:{disabled_bg}; }}
'''


# icon_button_style — the one square icon-only button variant (toolbar icons, titlebar chrome,
# panel-header actions). Replaces the old
# `theme.BUTTON_STYLE.replace('padding:7px 13px;', 'padding:4px;')` string-patch pattern, which
# silently broke if BUTTON_STYLE's literal padding text ever changed.
def icon_button_style(selected=False):
    bg = PANEL2 if selected else 'transparent'
    border = ACCENT if selected else 'transparent'
    return f'''
QPushButton, QToolButton {{
    background:{bg}; color:{TEXT}; border:1px solid {border};
    border-radius:{RADIUS}px; padding:4px;
}}
QPushButton:hover, QToolButton:hover {{ background:{PANEL2}; border-color:{BORDER}; }}
QPushButton:pressed, QToolButton:pressed {{ background:{ACCENT}; border-color:{ACCENT}; }}
QPushButton:checked, QToolButton:checked {{ background:{PANEL2}; border-color:{ACCENT}; }}
QPushButton:disabled, QToolButton:disabled {{ color:{TEXT_DISABLED}; border-color:transparent; background:transparent; }}
'''


# tab_style — flat tab-row buttons (view-mode KANT/File/Gruppi, panel-header tabs, INCOMING/
# OUTGOING/MAPPA, bottom-dock Terminale/Problemi/Output): active state is a gold underline, never
# a filled background — the one place accent-as-large-fill is explicitly avoided.
def tab_style():
    return f'''
QPushButton {{
    background:transparent; color:{DIM}; border:none; border-bottom:2px solid transparent;
    padding:6px 10px; font-weight:600;
}}
QPushButton:hover {{ color:{TEXT}; }}
QPushButton:checked {{ color:{TEXT}; border-bottom:2px solid {ACCENT}; }}
QPushButton:disabled {{ color:{TEXT_DISABLED}; }}
'''


# input_style — QLineEdit/QTextEdit/QComboBox/QPlainTextEdit: one flat-bordered surface with a
# gold focus ring, used everywhere text or choices are entered.
def input_style():
    return f'''
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
    background:{PANEL}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:{RADIUS}px; padding:4px 8px; selection-background-color:{ACCENT}; selection-color:#ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color:{ACCENT}; }}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ color:{TEXT_DISABLED}; background:{PANEL2}; }}
QComboBox::drop-down {{ border:none; width:18px; }}
QComboBox QAbstractItemView {{ background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; selection-background-color:{PANEL2}; selection-color:{ACCENT}; }}
'''


# panel_header_style — the one compact header-bar treatment (left-panel tab strip container,
# AI-pane header, bottom-dock header, INCOMING/OUTGOING label bar): a PANEL surface with a single
# thin bottom border, never a doubled/decorative border.
def panel_header_style():
    return f'background:{PANEL}; border:none; border-bottom:1px solid {BORDER_WEAK};'


IGNORE_DIRS = {'.git', 'node_modules', 'venv', '.venv', '__pycache__', 'dist', 'build', '.idea', '.vscode', '.kant-trash'}
COORDINATED_LEAF_TAGS = {'CST'}
SEARCH_MAX_BYTES = 2_000_000

TREE_FONT_PT = 10
# the left tree's own description/"comment" line (under each row's tag+name) — deliberately its own
# constant, so resizing the coding board never drags this along with it; kept small for a dense outline
TREE_DETAIL_FONT_PT = 8
CODE_FONT_PT = 11
CODING_FONT_PT = 10  # code blocks and KANT labels inside the central coding board only
TREE_MIN_WIDTH = 420
