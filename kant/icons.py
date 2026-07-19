"""Central monochrome SVG icons used by KANT IDE chrome."""

from html import escape

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from kant import theme


# [CST CATEGORY] _SVG_BODIES — compact Lucide-style paths keep every IDE icon on one visual grid;
# the wrapper in draw_icon supplies the live theme color, stroke and dimensions.
# [CST] _SVG_BODIES — SVG fragments keyed by the public draw_icon names
# [CST OPEN] _SVG_BODIES
_SVG_BODIES = {
    'arrow-left': '<path d="M15 18l-6-6 6-6"/>',
    'arrow-right': '<path d="M9 18l6-6-6-6"/>',
    'arrow-up': '<path d="M18 15l-6-6-6 6"/>',
    'arrow-down': '<path d="M6 9l6 6 6-6"/>',
    'run': '<path d="M6 4l14 8-14 8z"/>',
    'save': '<path d="M5 3h12l2 2v16H5z"/><path d="M8 3v6h8V3M8 21v-7h8v7"/>',
    'undo': '<path d="M9 7L4 12l5 5"/><path d="M5 12h8a6 6 0 0 1 6 6"/>',
    'redo': '<path d="M15 7l5 5-5 5"/><path d="M19 12h-8a6 6 0 0 0-6 6"/>',
    'find': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/>',
    'format': '<path d="M4 6h16M4 12h11M4 18h14"/>',
    'target': '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
    'flame': '<path d="M12 22c4 0 7-3 7-7 0-3-2-6-5-10 0 4-2 5-3 7-1-2-2-3-2-5-3 3-4 6-4 9 0 3 3 6 7 6z"/>',
    'swap': '<path d="M4 7h14l-3-3M20 17H6l3 3"/>',
    'nest': '<rect x="3" y="3" width="18" height="18" rx="2"/><rect x="8" y="8" width="8" height="8" rx="1"/>',
    'globe': '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    'expand': '<path d="M9 3H3v6M15 3h6v6M9 21H3v-6M15 21h6v-6"/>',
    'collapse': '<path d="M3 9h6V3M21 9h-6V3M3 15h6v6M21 15h-6v6"/>',
    'terminal': '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 16h4"/>',
    'repl': '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M6 10l2 2-2 2M10 10l2 2-2 2M14 15h4"/>',
    'warning': '<path d="M12 3L2.5 20h19z"/><path d="M12 9v5M12 17h.01"/>',
    'debug': '<path d="M8 8h8v9a4 4 0 0 1-8 0zM9 8a3 3 0 0 1 6 0M5 13h3M16 13h3M9 4L7 2M15 4l2-2"/>',
    'home': '<path d="M3 11l9-8 9 8v10h-6v-6H9v6H3z"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    'moon': '<path d="M20 15.5A9 9 0 0 1 8.5 4 9 9 0 1 0 20 15.5z"/>',
    'attach': '<path d="M21 11.5l-8.8 8.8a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
    'model': '<rect x="5" y="5" width="14" height="14" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    'effort': '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-7M7 18h10"/>',
    'kant': '<path d="M7 3v18M18 3L7 13M11 10l8 11"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    'pin': '<path d="M9 3h6l-1 6 3 3H7l3-3zM12 12v9"/>',
    'close': '<path d="M6 6l12 12M18 6L6 18"/>',
    'tokens': '<circle cx="9" cy="15" r="5"/><circle cx="15" cy="9" r="5"/>',
    'sparkle': '<path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><path d="M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18"/>',
}
# [CST CLOSED] _SVG_BODIES


# [FN CATEGORY] draw_icon — renders one central SVG fragment into a transparent QIcon; omitted
# colors follow normal text by day and the requested gold accent in night mode.
# [FN] draw_icon — renders one named, theme-aware SVG icon
# [FN OPEN] draw_icon
def draw_icon(kind, size=16, color=None):
    color = color or (theme.ACCENT if theme.NIGHT else theme.TEXT)
    body = _SVG_BODIES.get(kind, _SVG_BODIES['warning'])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{escape(color)}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode('utf-8'))).render(painter)
    painter.end()
    return QIcon(pixmap)
# [FN CLOSED] draw_icon
