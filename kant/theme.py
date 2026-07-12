"""Theme colors, styles, and top-level UI constants.

set_theme() rebinds the module-level color globals in place; read them live
as theme.<NAME> (never `from kant.theme import <NAME>`) so a theme switch is
visible to widgets built afterward.
"""

BG = '#ffffff'; PANEL = '#fbfcff'; BORDER = '#d7dce5'; TEXT = '#111827'
DIM = '#64748b'; ACCENT = '#2563eb'; CODE_BG = '#f3f5f9'
HOT = '#f2b705'; OK = '#15803d'; WARN = '#7c3aed'
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
    border-radius:8px; padding:7px 13px; font-weight:700;
}}
QPushButton:hover {{ background:#eef4ff; color:{ACCENT}; border-color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT}; color:#ffffff; }}
QPushButton:disabled {{ color:{DIM}; border-color:#e2e8f0; background:#f1f5f9; }}
'''


def set_theme(night=False):
    global BG, PANEL, BORDER, TEXT, DIM, ACCENT, CODE_BG, HOT, OK, WARN
    global HL_COMMENT, HL_STRING, HL_NUMBER, HL_KEYWORD, APP_STYLE, BUTTON_STYLE

    if night:
        BG = '#0f172a'; PANEL = '#111827'; BORDER = '#334155'; TEXT = '#e5e7eb'
        DIM = '#94a3b8'; ACCENT = '#60a5fa'; CODE_BG = '#0b1120'
        HOT = '#facc15'; OK = '#4ade80'; WARN = '#c084fc'
        HL_COMMENT = '#94a3b8'; HL_STRING = '#86efac'; HL_NUMBER = '#93c5fd'; HL_KEYWORD = '#f59e0b'
        TAG_COLORS.clear(); TAG_COLORS.update(NIGHT_TAG_COLORS)
        TAG_BACKGROUNDS.clear(); TAG_BACKGROUNDS.update(NIGHT_TAG_BACKGROUNDS)
        hover = '#1e293b'; disabled_border = '#334155'; disabled_bg = '#111827'
    else:
        BG = '#ffffff'; PANEL = '#fbfcff'; BORDER = '#d7dce5'; TEXT = '#111827'
        DIM = '#64748b'; ACCENT = '#2563eb'; CODE_BG = '#f3f5f9'
        HOT = '#f2b705'; OK = '#15803d'; WARN = '#7c3aed'
        HL_COMMENT = '#7a7f87'; HL_STRING = '#067d17'; HL_NUMBER = '#1750eb'; HL_KEYWORD = '#cf5b00'
        TAG_COLORS.clear(); TAG_COLORS.update(DAY_TAG_COLORS)
        TAG_BACKGROUNDS.clear(); TAG_BACKGROUNDS.update(DAY_TAG_BACKGROUNDS)
        hover = '#eef4ff'; disabled_border = '#e2e8f0'; disabled_bg = '#f1f5f9'

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
    border-radius:8px; padding:7px 13px; font-weight:700;
}}
QPushButton:hover {{ background:{hover}; color:{ACCENT}; border-color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT}; color:#ffffff; }}
QPushButton:disabled {{ color:{DIM}; border-color:{disabled_border}; background:{disabled_bg}; }}
'''


IGNORE_DIRS = {'.git', 'node_modules', 'venv', '.venv', '__pycache__', 'dist', 'build', '.idea', '.vscode', '.kant-trash'}
COORDINATED_LEAF_TAGS = {'CST'}
SEARCH_MAX_BYTES = 2_000_000

TREE_FONT_PT = 11
CODE_FONT_PT = 13
TREE_MIN_WIDTH = 420
