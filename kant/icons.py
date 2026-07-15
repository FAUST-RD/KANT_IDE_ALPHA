"""Small drawn vector icons for toolbar/button chrome — QPainter shapes (not files, not emoji),
matching the same custom-painted-badge convention already used for MAPPA's pin/eye markers.
Kept in its own module (not theme.py) so theme.py stays importable by non-Qt modules
(projectops.py) without pulling PySide6 into a deterministic-scan code path.
"""
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from kant import theme


# [FN CATEGORY] draw_icon — every shape is drawn fresh into a small QPixmap at call time (cheap,
# no asset files, always matches the current theme's color); pen width scales with the icon's own
# `size` so it looks consistent whether it's rendered at 14px or 20px.
# [FN] draw_icon — renders one named icon shape as a QIcon
# [FN OPEN] draw_icon
def draw_icon(kind, size=16, color=None):
    color = color or theme.TEXT
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    m = size * 0.22
    mid = size / 2

    if kind in ('arrow-left', 'arrow-right', 'arrow-up', 'arrow-down'):
        path = QPainterPath()
        if kind == 'arrow-left':
            path.moveTo(size - m, m); path.lineTo(m, mid); path.lineTo(size - m, size - m)
        elif kind == 'arrow-right':
            path.moveTo(m, m); path.lineTo(size - m, mid); path.lineTo(m, size - m)
        elif kind == 'arrow-up':
            path.moveTo(m, size - m); path.lineTo(mid, m); path.lineTo(size - m, size - m)
        else:
            path.moveTo(m, m); path.lineTo(mid, size - m); path.lineTo(size - m, m)
        path.closeSubpath()
        p.drawPath(path)

    elif kind == 'run':
        path = QPainterPath()
        path.moveTo(m + 1, m * 0.7); path.lineTo(size - m + 1, mid); path.lineTo(m + 1, size - m * 0.7)
        path.closeSubpath()
        p.drawPath(path)

    elif kind == 'save':
        outer = m * 0.6
        p.drawRoundedRect(QRectF(outer, outer, size - 2 * outer, size - 2 * outer), 2, 2)
        p.setBrush(Qt.transparent)
        p.setPen(QColor(theme.BG))
        inner = size * 0.32
        p.drawRect(QRectF(mid - inner / 2, outer + 1, inner, size * 0.24))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BG))
        p.drawRect(QRectF(outer + 2, size - outer - size * 0.3, size - 2 * outer - 4, size * 0.18))

    elif kind in ('undo', 'redo'):
        radius = size * 0.32
        rect = QRectF(mid - radius, mid - radius, radius * 2, radius * 2)
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.13)
        p.setPen(pen)
        start_angle = 200 if kind == 'undo' else -20
        span = 220 if kind == 'undo' else -220
        p.drawArc(rect, start_angle * 16, span * 16)
        head_angle = math.radians(start_angle + span)
        hx, hy = mid + radius * math.cos(head_angle), mid - radius * math.sin(head_angle)
        tangent = head_angle + math.radians(90 if kind == 'undo' else -90)
        dx, dy = math.cos(tangent), -math.sin(tangent)
        hs = size * 0.16
        head = QPainterPath()
        head.moveTo(hx + dy * hs, hy + dx * hs)
        head.lineTo(hx - dy * hs, hy - dx * hs)
        head.lineTo(hx + dx * hs * 1.4, hy - dy * hs * 1.4)
        head.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawPath(head)

    elif kind == 'find':
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.14)
        p.setPen(pen)
        p.setBrush(Qt.transparent)
        r = size * 0.28
        cx, cy = mid - size * 0.08, mid - size * 0.08
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
        p.drawLine(int(cx + r * 0.7), int(cy + r * 0.7), int(size - m * 0.6), int(size - m * 0.6))

    elif kind == 'format':
        widths = (0.8, 0.55, 0.7)
        bar_h = size * 0.12
        gap = size * 0.2
        top = mid - (bar_h * 3 + gap * 2) / 2
        for i, w in enumerate(widths):
            p.drawRoundedRect(QRectF(m * 0.6, top + i * (bar_h + gap), (size - 2 * m * 0.6) * w, bar_h), 1, 1)

    elif kind == 'target':  # isolate
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.11)
        p.setPen(pen)
        p.drawEllipse(QRectF(m * 0.7, m * 0.7, size - 1.4 * m, size - 1.4 * m))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        r = size * 0.13
        p.drawEllipse(QRectF(mid - r, mid - r, r * 2, r * 2))

    elif kind == 'flame':  # heatmap
        path = QPainterPath()
        path.moveTo(mid, m * 0.6)
        path.cubicTo(size - m * 0.9, mid * 0.7, size - m * 0.7, size - m * 0.8, mid, size - m * 0.5)
        path.cubicTo(m * 0.7, size - m * 0.8, m * 0.9, mid * 0.7, mid, m * 0.6)
        p.drawPath(path)

    elif kind == 'swap':  # direction toggle
        head = size * 0.16
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.11)
        p.setPen(pen)
        p.drawLine(int(m * 0.6), int(mid - size * 0.12), int(size - m * 0.6), int(mid - size * 0.12))
        p.drawLine(int(m * 0.6), int(mid + size * 0.12), int(size - m * 0.6), int(mid + size * 0.12))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        left_head = QPainterPath()
        left_head.moveTo(m * 0.6 + head, mid - size * 0.12 - head * 0.7)
        left_head.lineTo(m * 0.6, mid - size * 0.12)
        left_head.lineTo(m * 0.6 + head, mid - size * 0.12 + head * 0.7)
        left_head.closeSubpath()
        p.drawPath(left_head)
        right_head = QPainterPath()
        right_head.moveTo(size - m * 0.6 - head, mid + size * 0.12 - head * 0.7)
        right_head.lineTo(size - m * 0.6, mid + size * 0.12)
        right_head.lineTo(size - m * 0.6 - head, mid + size * 0.12 + head * 0.7)
        right_head.closeSubpath()
        p.drawPath(right_head)

    elif kind == 'nest':  # containment
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.1)
        p.setPen(pen)
        p.drawRoundedRect(QRectF(m * 0.5, m * 0.5, size - m, size - m), 2, 2)
        inner = size * 0.32
        p.drawRoundedRect(QRectF(mid - inner / 2, mid - inner / 2, inner, inner), 1, 1)

    elif kind == 'globe':  # scope toggle (GLOBAL)
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.1)
        p.setPen(pen)
        r = size * 0.36
        p.drawEllipse(QRectF(mid - r, mid - r, r * 2, r * 2))
        p.drawEllipse(QRectF(mid - r * 0.45, mid - r, r * 0.9, r * 2))
        p.drawLine(int(mid - r), int(mid), int(mid + r), int(mid))

    elif kind in ('expand', 'collapse'):
        _corner_arrows(p, size, color, outward=(kind == 'expand'))

    elif kind == 'terminal':
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.1)
        p.setPen(pen)
        p.drawRoundedRect(QRectF(m * 0.4, m * 0.7, size - m * 0.8, size - m * 1.4), 2, 2)
        pen.setWidthF(size * 0.13)
        p.setPen(pen)
        p.drawLine(int(m * 1.1), int(mid - size * 0.05), int(mid - size * 0.05), int(mid + size * 0.08))
        p.drawLine(int(m * 1.1), int(mid + size * 0.21), int(mid - size * 0.05), int(mid + size * 0.08))
        p.drawLine(int(mid + size * 0.02), int(size - m * 1.1), int(size - m * 1.1), int(size - m * 1.1))

    elif kind == 'repl':  # python/interactive terminal — ">>>" prompt glyph
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.11)
        p.setPen(pen)
        p.setBrush(Qt.transparent)
        for i in range(3):
            x0 = m * 0.5 + i * size * 0.24
            p.drawLine(int(x0), int(mid - size * 0.16), int(x0 + size * 0.14), int(mid))
            p.drawLine(int(x0), int(mid + size * 0.16), int(x0 + size * 0.14), int(mid))

    elif kind == 'warning':
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.09)
        p.setPen(pen)
        p.setBrush(Qt.transparent)
        path = QPainterPath()
        path.moveTo(mid, m * 0.5)
        path.lineTo(size - m * 0.5, size - m * 0.6)
        path.lineTo(m * 0.5, size - m * 0.6)
        path.closeSubpath()
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(mid - size * 0.05, mid - size * 0.12, size * 0.1, size * 0.24), 1, 1)
        r = size * 0.055
        p.drawEllipse(QRectF(mid - r, size - m * 0.95, r * 2, r * 2))

    elif kind == 'debug':
        # a simple ladybug glyph (oval body + antennae + three legs per side) — the common
        # "debug" shorthand across IDEs, distinct enough from 'run' (a plain play triangle) at a
        # glance even at small sizes
        p.setBrush(QColor(color))
        body_w, body_h = size * 0.42, size * 0.56
        p.drawEllipse(QRectF(mid - body_w / 2, mid - body_h / 2, body_w, body_h))
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(max(1.0, size * 0.07))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        top = mid - body_h / 2
        for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (1, -1), (1, 0), (1, 1)):
            p.drawLine(
                int(mid + dx * body_w * 0.45), int(mid + dy * body_h * 0.28),
                int(mid + dx * body_w * 0.9), int(mid + dy * body_h * 0.42),
            )
        p.drawLine(int(mid - body_w * 0.18), int(top), int(mid - body_w * 0.35), int(top - size * 0.12))
        p.drawLine(int(mid + body_w * 0.18), int(top), int(mid + body_w * 0.35), int(top - size * 0.12))

    elif kind == 'home':
        # outline-only (like target/nest/warning above), not a solid fill — a filled silhouette's
        # door would need to be cut out in the exact backdrop color, which varies with theme/hover
        # state; an outline never has that problem
        p.setBrush(Qt.transparent)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.1)
        p.setPen(pen)
        house = QPainterPath()
        house.moveTo(mid, m * 0.4)
        house.lineTo(size - m * 0.4, mid * 0.9)
        house.lineTo(size - m * 0.4, size - m * 0.5)
        house.lineTo(m * 0.4, size - m * 0.5)
        house.lineTo(m * 0.4, mid * 0.9)
        house.closeSubpath()
        p.drawPath(house)
        door_w, door_h = size * 0.22, size * 0.32
        p.drawRect(QRectF(mid - door_w / 2, size - m * 0.5 - door_h, door_w, door_h))

    p.end()
    return QIcon(pm)
# [FN CLOSED] draw_icon


def _corner_arrows(p, size, color, outward):
    m = size * 0.2
    arm = size * 0.22
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    corners = [(m, m, 1, 1), (size - m, m, -1, 1), (m, size - m, 1, -1), (size - m, size - m, -1, -1)]
    for x, y, sx, sy in corners:
        path = QPainterPath()
        if outward:
            path.moveTo(x, y); path.lineTo(x + sx * arm, y); path.lineTo(x, y + sy * arm)
        else:
            tx, ty = x - sx * arm * 0.6, y - sy * arm * 0.6
            path.moveTo(tx, ty); path.lineTo(tx + sx * arm, ty); path.lineTo(tx, ty + sy * arm)
        path.closeSubpath()
        p.drawPath(path)
