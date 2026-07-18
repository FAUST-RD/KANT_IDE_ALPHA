"""MAPPA: the project cross-reference map — layout algorithm, graphics items, and the dialog.

AI navigation: layout helpers (_module_flow_seeds through _force_layout_positions) come first,
then the QGraphicsItem subclasses (XrefNodeItem, PinBadgeItem, EyeBadgeItem, XrefEdgeItem), then
XrefMapView (camera/interaction/rendering) and EdgeFlowPopup, then XrefMapDialog (toolbar, filters,
drill-down, persistence). Split out of widgets.py — this subsystem alone was roughly half that
file's line count. The graph itself (XrefElement, build_xref) stays in xref.py; this module only
lays it out and draws it.
"""
import hashlib
import json
import math
import os
import re
import time
from html import escape as html_escape

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QSettings, QSize, Signal, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF,
)
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from kant import theme
from kant.groupings import remap_member_key
from kant.icons import draw_icon
from kant.model import Node
from kant.xref import XrefElement


# [FN CATEGORY] _kant_mentions_html — MAPPA flow popups reuse the same tag palette and monospace
# visual cue as the KANT tree instead of flattening references into unstyled prose.
# [FN] _kant_mentions_html — colors and underlines KANT tag mentions in popup text
# [FN OPEN] _kant_mentions_html
def _kant_mentions_html(text):
    html = html_escape(text).replace('\n', '<br>')
    for tag, color in theme.TAG_COLORS.items():
        html = html.replace(
            f'[{tag}]',
            f'<span style="color:{color}; font-family:Consolas; font-weight:700; '
            f'text-decoration:underline">[{tag}]</span>',
        )
    return html
# [FN CLOSED] _kant_mentions_html


# [FN] _position_settings_key — the QSettings key MAPPA's node coordinates are stored under for a
# project; one key per project (not per node). Factored out of XrefMapDialog.set_graph so
# migrate_position_keys below can compute the identical key without a live dialog instance.
# [FN OPEN] _position_settings_key
def _position_settings_key(project_path, project_name=''):
    identity = os.path.normcase(os.path.abspath(project_path or project_name or '.'))
    return 'xrefPositionsV2/' + hashlib.sha1(identity.encode('utf-8')).hexdigest()
# [FN CLOSED] _position_settings_key


# [FN CATEGORY] migrate_position_keys — after KANT IDE renames a file or folder, remaps MAPPA's
# persisted node coordinates the same way kant/groupings.py:migrate_member_paths remaps Grouping
# members — same remap_member_key rule, same key format, so a renamed node keeps its manually
# dragged position instead of silently losing it the next time set_graph filters positions down to
# elements it can still find (see set_graph's `self._positions = {... if key in elements}`, which is
# exactly where an unmigrated rename's coordinates would otherwise vanish). Not a dialog method —
# the coordinates persist in QSettings whether or not MAPPA is currently open.
# [FN] migrate_position_keys — remaps MAPPA's saved coordinates after a rename; True if changed
# [FN OPEN] migrate_position_keys
def migrate_position_keys(project_path, old_rel, new_rel, is_dir):
    settings = QSettings('KANT', 'KANT Editor')
    key = _position_settings_key(project_path)
    try:
        data = json.loads(settings.value(key, '{}'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    changed = False
    remapped = {}
    for member_key, value in data.items():
        new_key = remap_member_key(member_key, old_rel, new_rel, is_dir)
        changed = changed or new_key != member_key
        remapped[new_key] = value
    if changed:
        settings.setValue(key, json.dumps(remapped))
    return changed
# [FN CLOSED] migrate_position_keys



# [FN CATEGORY] XrefMapView — QGraphicsView drawing an Obsidian-style force-directed project graph:
# connected nodes attract, all nodes repel, and every node can be dragged while its curved arrows
# follow live. Selecting still dims non-neighbours; wheel zooms and background drag pans the scene.
# [FN] XrefMapView — interactive graph view of the KANT cross-reference map
# [FN OPEN] XrefMapView
def _module_flow_seeds(elements, rtl=False):
    """Place file clusters left-to-right (or right-to-left when rtl) along the condensed directed
    dependency graph. Named `rtl`, not `reverse`, to avoid shadowing the unrelated reverse-adjacency
    dict already local to this function."""
    files = sorted({element.file for element in elements.values()})
    adjacency = {name: set() for name in files}
    flow_weights = {name: {} for name in files}
    for element in elements.values():
        for target in element.outgoing:
            if target not in elements or elements[target].file == element.file:
                continue
            target_file = elements[target].file
            adjacency[element.file].add(target_file)
            flow_weights[element.file][target_file] = flow_weights[element.file].get(target_file, 0) + 1

    visited, finish = set(), []
    for root in files:
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            file_name, closing = stack.pop()
            if closing:
                finish.append(file_name)
                continue
            if file_name in visited:
                continue
            visited.add(file_name)
            stack.append((file_name, True))
            stack.extend((target, False) for target in sorted(adjacency[file_name], reverse=True))
    reverse = {name: set() for name in files}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    assigned, components = set(), []
    for root in reversed(finish):
        if root in assigned:
            continue
        component, stack = [], [root]
        assigned.add(root)
        while stack:
            file_name = stack.pop(); component.append(file_name)
            for source in sorted(reverse[file_name], reverse=True):
                if source not in assigned:
                    assigned.add(source); stack.append(source)
        components.append(sorted(component))
    component_of = {file_name: number for number, component in enumerate(components) for file_name in component}
    dag = {number: set() for number in range(len(components))}
    for source, targets in adjacency.items():
        for target in targets:
            if component_of[source] != component_of[target]:
                dag[component_of[source]].add(component_of[target])
    indegree = {number: 0 for number in dag}
    for targets in dag.values():
        for target in targets:
            indegree[target] += 1
    rank = {number: 0 for number in dag}
    queue = sorted((number for number, degree in indegree.items() if degree == 0), key=lambda n: components[n])
    while queue:
        source = queue.pop(0)
        for target in sorted(dag[source], key=lambda n: components[n]):
            rank[target] = max(rank[target], rank[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target); queue.sort(key=lambda n: components[n])
    file_rank = {file_name: rank[component_of[file_name]] for file_name in files}
    layers = {level: [name for name in files if file_rank[name] == level] for level in sorted(set(file_rank.values()))}
    order = {name: index for names in layers.values() for index, name in enumerate(names)}
    neighbour_weights = {name: dict(flow_weights[name]) for name in files}
    for source in files:
        for target, weight in flow_weights[source].items():
            neighbour_weights[target][source] = neighbour_weights[target].get(source, 0) + weight
    for sweep in range(4):
        levels = list(layers) if sweep % 2 == 0 else list(reversed(layers))
        for level in levels:
            layers[level].sort(key=lambda name: (
                sum(order[n] * weight for n, weight in neighbour_weights[name].items())
                / sum(neighbour_weights[name].values()) if neighbour_weights[name] else order[name],
                name,
            ))
            for position, name in enumerate(layers[level]):
                order[name] = position

    by_file = {name: sorted((e for e in elements.values() if e.file == name), key=lambda e: (e.order, e.key)) for name in files}
    radii = {name: max(180.0, 130.0 * math.sqrt(len(by_file[name]))) for name in files}
    layer_gap = max(760.0, max((radius * 2 for radius in radii.values()), default=0) + 420.0)
    seeds = {}
    for level, names in layers.items():
        total_height = sum(radii[name] * 2 + 180 for name in names) - (180 if names else 0)
        cursor = -total_height / 2
        for name in names:
            center_y = cursor + radii[name]
            center_x = level * layer_gap * (-1 if rtl else 1)
            _seed_file_cluster(by_file[name], center_x, center_y, radii[name], seeds, rtl)
            cursor += radii[name] * 2 + 180
    return seeds


def _seed_file_cluster(elements_in_file, center_x, center_y, radius, seeds, rtl=False):
    """Seed one file's elements around (center_x, center_y), ranking left-to-right (or
    right-to-left when rtl) by intra-file call depth (source before target) instead of an
    arbitrary spiral, so the starting position already reads as logical flow before the force
    simulation even runs — the simulation's own seed-pull is strong enough that a direction-blind
    seed never recovers. Shared by the normal multi-file map and MAPPA's drill-down (there, the
    "file" is really the drilled element's own children, ranked by their mutual references)."""
    keys = {e.key for e in elements_in_file}
    local_targets = {e.key: [t for t in e.outgoing if t in keys and t != e.key] for e in elements_in_file}
    rank = {e.key: 0 for e in elements_in_file}
    for _ in range(len(elements_in_file)):
        changed = False
        for e in elements_in_file:
            for target in local_targets[e.key]:
                if rank[target] < rank[e.key] + 1:
                    rank[target] = rank[e.key] + 1
                    changed = True
        if not changed:
            break
    groups = {}
    for element in elements_in_file:
        groups.setdefault(rank[element.key], []).append(element)
    max_rank = max(groups) if groups else 0
    span = radius * 1.6
    step = span / (max_rank + 1) if max_rank else 0.0
    for level, group in groups.items():
        offset = level * step
        x = center_x + span / 2 - offset if rtl else center_x - span / 2 + offset
        group_height = len(group) * 60.0
        y = center_y - group_height / 2 + 30.0
        for element in group:
            seeds[element.key] = (x, y)
            y += 60.0


# [FN] _element_degree — connectivity count driving node size, heatmap intensity, and layout weight.
# Node-tag visibility already narrows incoming/outgoing before this ever runs (_display_elements
# only aggregates references between currently-active tags) — passing `elements`/`active_edge_tags`
# additionally narrows by the connections filter, the same rule that decides whether an edge is
# drawn, so size/heatmap/geography react to that filter too, not just to node visibility.
def _element_degree(el, elements=None, active_edge_tags=None):
    incoming = el.incoming_detail or el.incoming
    outgoing = el.outgoing_detail or el.outgoing
    if active_edge_tags is None or elements is None:
        return len(incoming) + len(outgoing)
    if el.tag not in active_edge_tags:
        return 0
    incoming = [k for k in incoming if k in elements and elements[k].tag in active_edge_tags]
    outgoing = [k for k in outgoing if k in elements and elements[k].tag in active_edge_tags]
    return len(incoming) + len(outgoing)


# [CST] MIN_NODE_W/MAX_NODE_W/MIN_NODE_H/MAX_NODE_H — node box size bounds; an element scales
# between these by how much code traffic it carries (see _element_size). Shared by the force
# layout (so real box footprints never overlap) and XrefMapView's rendering (so what's drawn
# matches what was laid out).
MIN_NODE_W, MAX_NODE_W = 170, 320
MIN_NODE_H, MAX_NODE_H = 22, 42
MIN_NODE_GAP = 30.0
ANCHOR_SIZE = 16  # small unmarked "common origin" circle footprint — fixed, not traffic-scaled


def _element_size(el, max_degree, elements=None, active_edge_tags=None):
    """Box (width, height) for one element, scaled by its share of the busiest node's traffic.
    A common-origin anchor is always the small fixed circle size regardless of how many
    siblings connect to it — it's a marker, not a traffic hub."""
    if el.is_anchor:
        return ANCHOR_SIZE, ANCHOR_SIZE
    degree = _element_degree(el, elements, active_edge_tags)
    t = (degree / max_degree) if max_degree else 0.0
    width = MIN_NODE_W + (MAX_NODE_W - MIN_NODE_W) * t
    height = MIN_NODE_H + (MAX_NODE_H - MIN_NODE_H) * t
    return width, height


def _force_layout_positions(elements, fixed=None, seed=None, active_edge_tags=None, use_parent_attraction=True, rtl=False):
    """Directed module seeding plus local attraction/repulsion for readable organic spacing.
    `fixed` positions are pinned and never moved by the simulation (persisted/dragged nodes).
    `seed` positions are only a starting point the simulation is free to adjust — used to warm-start
    a re-layout after a filter change so nodes drift to their new spot instead of jumping.
    Nodes with no incoming/outgoing edges at all get a much stronger pull toward their module's
    seed position, since nothing else anchors them and unopposed repulsion would otherwise push
    them far from their cluster. Repulsion strength is derived per-pair from each node's own
    (traffic-scaled) box size, so two large hubs never overlap just because a single global spacing
    constant happened to fit smaller nodes. Individual connections — not just module rank — read
    left-to-right as the logical flow of the code, because `_module_flow_seeds` ranks each file's
    own elements by intra-file call depth before the simulation starts (the seed-pull below is
    strong enough that a direction-blind starting position never recovers into one afterward).

    `fixed`/`seed` are given, and the result is returned, as top-left corners (what callers store
    and what QGraphicsItem.setPos expects) — but since nodes now vary in size, the physics itself
    runs in center coordinates internally: comparing top-left corners directly would make two
    differently-sized nodes look closer or farther apart than their real edges actually are,
    letting boxes overlap. `sizes`/half-size are computed once up front and used to convert at the
    boundary in both directions."""
    max_degree = max((_element_degree(e, elements, active_edge_tags) for e in elements.values()), default=0)
    sizes = {key: _element_size(element, max_degree, elements, active_edge_tags) for key, element in elements.items()}

    def to_center(pos, key):
        w, h = sizes[key]
        return (pos[0] + w / 2, pos[1] + h / 2)

    fixed = {key: to_center(tuple(value), key) for key, value in (fixed or {}).items() if key in elements}
    seed = {key: to_center(tuple(value), key) for key, value in (seed or {}).items() if key in elements}
    keys = sorted(elements)
    count = len(keys)
    if not count:
        return {}
    seeds = _module_flow_seeds(elements, rtl)  # already centers
    radius = max(320.0, 140.0 * math.sqrt(count))
    positions = {
        key: list(fixed[key] if key in fixed else seed.get(key, seeds[key]))
        for key in keys
    }
    if len(fixed) != count:
        # nodes are wide-and-short (roughly 170-320 x 22-42, scaled per-node by traffic): an
        # isotropic (circular) force wastes vertical room since a ~30px-tall box needs far less
        # vertical clearance than its width does. Shrinking dy's contribution to the
        # repulsion/attraction distance (not the real dy itself) lets rows pack tighter vertically
        # while keeping horizontal spacing intact.
        y_squash = 0.55
        # different modules repel harder than elements of the same module, so a cluster's own
        # boundary reads clearly against its neighbours instead of blending into them.
        cross_module_boost = 1.35
        # common-origin (same immediate KANT parent, or the same anchor for orphaned siblings —
        # see _add_common_origin_anchors) pulls elements toward each other too, but deliberately
        # weaker than the reference-edge attraction (1.0x) below: this is a secondary clustering
        # cue, not a replacement for the hierarchy-rank seeding or call-direction flow that already
        # dominate node placement.
        origin_weight = 0.35
        # a floor high enough that the common case (dozens to a couple hundred elements) always
        # gets to settle fully, not just whatever `1200 // count` happened to leave it — a map
        # that stops refining early reads as visibly less organized than one that's converged.
        # Very large graphs (hundreds+) still get fewer passes since O(n^2) repulsion cost grows
        # quadratically (see the ponytail note below), but never fewer than 20.
        iterations = max(20, min(80, 4000 // count))
        temperature = radius * 0.22
        cooling = 0.94  # geometric decay — smoother settling than a linear ramp to zero
        # each node's own half-diagonal, in the same squashed metric used for `distance` below, so
        # a required minimum separation compares like with like
        half_diag = {key: math.hypot(w / 2, (h / 2) * y_squash) for key, (w, h) in sizes.items()}
        edges = {(min(source, target), max(source, target))
                 for source, element in elements.items() for target in element.outgoing if target in elements}
        connected = {key for pair in edges for key in pair}
        # a node with no edges at all has nothing pulling it back in — only repulsion pushes it —
        # so it drifts far from its cluster over enough iterations. Anchoring it to its seed much
        # more strongly keeps it near its module instead of stranded at the edge of the canvas.
        isolated = elements.keys() - connected
        parent_pairs = [
            (key, element.parent) for key, element in elements.items()
            if use_parent_attraction and element.parent and element.parent in elements
        ]
        # ponytail: O(n²) repulsion is simplest; use Barnes-Hut only if thousand-node maps lag.
        for step in range(iterations):
            movement = {key: [0.0, 0.0] for key in keys}
            for index, left in enumerate(keys):
                for right in keys[index + 1:]:
                    dx = positions[left][0] - positions[right][0]
                    dy = positions[left][1] - positions[right][1]
                    distance = max(1.0, math.hypot(dx, dy * y_squash))
                    boost = cross_module_boost if elements[left].file != elements[right].file else 1.0
                    min_sep = half_diag[left] + half_diag[right] + 40.0  # real footprints + a visible gap
                    force = min_sep * min_sep * boost / distance
                    fx, fy = dx / distance * force, dy / distance * force
                    movement[left][0] += fx; movement[left][1] += fy
                    movement[right][0] -= fx; movement[right][1] -= fy
            for left, right in edges:
                dx = positions[left][0] - positions[right][0]
                dy = positions[left][1] - positions[right][1]
                distance = max(1.0, math.hypot(dx, dy * y_squash))
                force = distance * distance / 280.0
                fx, fy = dx / distance * force, dy / distance * force
                movement[left][0] -= fx; movement[left][1] -= fy
                movement[right][0] += fx; movement[right][1] += fy
            for child, parent in parent_pairs:
                dx = positions[child][0] - positions[parent][0]
                dy = positions[child][1] - positions[parent][1]
                distance = max(1.0, math.hypot(dx, dy * y_squash))
                force = distance * distance / 280.0 * origin_weight
                fx, fy = dx / distance * force, dy / distance * force
                movement[child][0] -= fx; movement[child][1] -= fy
                movement[parent][0] += fx; movement[parent][1] += fy
            limit = temperature * (cooling ** step)
            for key in keys:
                if key in fixed:
                    continue
                dx, dy = movement[key]
                length = max(1.0, math.hypot(dx, dy))
                positions[key][0] += dx / length * min(length, limit) - positions[key][0] * 0.008
                positions[key][1] += dy / length * min(length, limit) - positions[key][1] * 0.008
                pull_x, pull_y = (0.35, 0.22) if key in isolated else (0.12, 0.05)
                positions[key][0] += (seeds[key][0] - positions[key][0]) * pull_x
                positions[key][1] += (seeds[key][1] - positions[key][1]) * pull_y

        # the repulsion above is a soft, elliptical approximation (via y_squash) — it makes overlap
        # rare but can't guarantee zero in every configuration, particularly when two boxes end up
        # nearly aligned on one axis. Directly resolve any real axis-aligned overlap left over,
        # using each box's true (unsquashed) width/height — a hard constraint layered on top of the
        # soft aesthetic preference above, so two boxes never actually overlap on screen. Defined as
        # a closure (not inlined once) because the median-Y pass below moves nodes again afterward
        # and can reintroduce overlaps — it needs re-running, not just running once.
        margin = MIN_NODE_GAP

        def resolve_overlaps():
            for _ in range(40):
                moved = False
                for index, left in enumerate(keys):
                    for right in keys[index + 1:]:
                        move_left, move_right = left not in fixed, right not in fixed
                        if not move_left and not move_right:
                            continue
                        lw, lh = sizes[left]
                        rw, rh = sizes[right]
                        dx = positions[left][0] - positions[right][0]
                        dy = positions[left][1] - positions[right][1]
                        overlap_x = (lw + rw) / 2 + margin - abs(dx)
                        overlap_y = (lh + rh) / 2 + margin - abs(dy)
                        if overlap_x <= 0 or overlap_y <= 0:
                            continue
                        moved = True
                        share = 0.5 if (move_left and move_right) else 1.0
                        if overlap_x < overlap_y:
                            sign = 1.0 if dx >= 0 else -1.0
                            push = overlap_x * share
                            if move_left:
                                positions[left][0] += sign * push
                            if move_right:
                                positions[right][0] -= sign * push
                        else:
                            sign = 1.0 if dy >= 0 else -1.0
                            push = overlap_y * share
                            if move_left:
                                positions[left][1] += sign * push
                            if move_right:
                                positions[right][1] -= sign * push
                if not moved:
                    break

        resolve_overlaps()

        # Sugiyama-style median-Y refinement: nudge each free node's y toward the median y of its
        # directly connected neighbours (edge and parent-origin alike) — the spring attraction above
        # already pulls toward neighbours in both x and y, but a few dedicated median passes at the
        # very end straighten near-vertical runs and reduce edge crossings beyond what pairwise
        # springs alone settle into, a standard technique from layered graph drawing adapted to this
        # continuous (non-layered) layout. X is left untouched so the module-flow/call-depth rank
        # established by seeding — and the source.x < target.x guarantee that gives a simple chain —
        # can never be disturbed by this pass. Unlike a real layered Sugiyama layout (where X is
        # fixed per rank and only sibling order within a layer changes), nodes here span a
        # continuous X range, so pulling several toward a shared neighbour's Y can cram unrelated,
        # X-overlapping nodes into the same Y band — resolve_overlaps() runs after EVERY sweep (not
        # just once at the end) so that crowding gets separated in small doses before it compounds
        # into a tangle too dense for one pass to untangle.
        neighbors = {key: [] for key in keys}
        for left, right in edges:
            neighbors[left].append(right)
            neighbors[right].append(left)
        for child, parent in parent_pairs:
            neighbors[child].append(parent)
            neighbors[parent].append(child)
        for _ in range(4):
            for key in keys:
                if key in fixed or not neighbors[key]:
                    continue
                ys = sorted(positions[other][1] for other in neighbors[key])
                median_y = ys[len(ys) // 2]
                positions[key][1] += (median_y - positions[key][1]) * 0.12
            resolve_overlaps()
        # Pairwise pushes can cycle in dense graphs; one ordered sweep makes the gap a hard bound.
        # Persisted/dragged positions are fixed by design and are never silently rearranged.
        if not fixed:
            placed = []
            for key in sorted(keys, key=lambda k: (positions[k][1], positions[k][0], k)):
                width, height = sizes[key]
                positions[key][1] = max((
                    positions[other][1] + (sizes[other][1] + height) / 2 + margin
                    for other in placed
                    if abs(positions[key][0] - positions[other][0])
                    < (width + sizes[other][0]) / 2 + margin
                ), default=positions[key][1])
                placed.append(key)
    if not fixed and not seed:
        min_x = min(point[0] for point in positions.values())
        min_y = min(point[1] for point in positions.values())
        for point in positions.values():
            point[0] -= min_x
            point[1] -= min_y
    # convert back from the internal center representation to the top-left corners callers expect
    return {
        key: (round(point[0] - sizes[key][0] / 2, 2), round(point[1] - sizes[key][1] / 2, 2))
        for key, point in positions.items()
    }


# [CST] _HEAT_STOPS — classic heat-spectrum gradient (cold blue to hot red), interpolated by
# _heat_color; used by MAPPA's heatmap color mode instead of per-tag coloring
_HEAT_STOPS = (
    (0.0, (59, 130, 246)), (0.25, (34, 211, 238)), (0.5, (34, 197, 94)),
    (0.75, (250, 204, 21)), (1.0, (239, 68, 68)),
)


def _heat_color(t):
    t = max(0.0, min(1.0, t))
    for (t0, c0), (t1, c1) in zip(_HEAT_STOPS, _HEAT_STOPS[1:]):
        if t0 <= t <= t1:
            span = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return QColor(*(round(a + (b - a) * span) for a, b in zip(c0, c1)))
    return QColor(*_HEAT_STOPS[-1][1])


class XrefNodeItem(QGraphicsPathItem):
    def __init__(self, key, path, moved, hovered=None):
        super().__init__(path)
        self.key, self._moved, self._hovered = key, moved, hovered
        self.setData(0, key)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setCursor(Qt.OpenHandCursor)
        self.setZValue(1)
        self.setAcceptHoverEvents(True)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            self._moved(self.key)
        return result

    def hoverEnterEvent(self, event):
        if self._hovered:
            self._hovered(self.key, event.scenePos(), True)
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        if self._hovered:
            self._hovered(self.key, event.scenePos(), True)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        if self._hovered:
            self._hovered(self.key, event.scenePos(), False)
        super().hoverLeaveEvent(event)


# [FN CATEGORY] PinBadgeItem — the pin-sequence marker shown above a pinned node: a filled circle
# with the sequence number painted directly (not a font glyph/emoji), so its click/hover area is
# the whole circle — predictable and generously sized — instead of whatever a "📌" glyph's actual
# ink happens to occupy in a given font.
# [FN] PinBadgeItem — drawn (not emoji) pin-sequence badge
# [FN OPEN] PinBadgeItem
class PinBadgeItem(QGraphicsItem):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._number = 1
        self._diameter = 40.0
        self.setAcceptedMouseButtons(Qt.NoButton)  # decorative only; the node itself owns pin-toggling

    def set_appearance(self, number, diameter):
        self._number = number
        self._diameter = diameter
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, self._diameter, self._diameter)

    def paint(self, painter, _option, _widget=None):
        d = self._diameter
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.HOT))
        painter.drawEllipse(QRectF(0, 0, d, d))
        painter.setPen(QColor('#ffffff'))
        painter.setFont(QFont('Consolas', max(7, round(d * 0.42)), QFont.Bold))
        painter.drawText(QRectF(0, 0, d, d), Qt.AlignCenter, str(self._number))
# [FN CLOSED] PinBadgeItem


# [FN CATEGORY] EyeBadgeItem — the clickable drill-down icon next to a single pinned node's
# sequence badge, shown only when that element has a complex enough internal structure to be
# worth drilling into. A drawn eye (outline + pupil), not an emoji glyph — same reasoning as
# PinBadgeItem: the whole circle is the hit area, not an unpredictable font glyph's ink extent.
# Swallows its own press so clicking it doesn't also re-toggle the node's pin underneath.
# [FN] EyeBadgeItem — clickable drill-down icon, child of one node's XrefNodeItem
# [FN OPEN] EyeBadgeItem
class EyeBadgeItem(QGraphicsItem):
    def __init__(self, key, clicked, parent=None):
        super().__init__(parent)
        self.key = key
        self._clicked = clicked
        self._diameter = 40.0
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(4)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def set_diameter(self, diameter):
        self._diameter = diameter
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, self._diameter, self._diameter)

    def paint(self, painter, _option, _widget=None):
        d = self._diameter
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.HOT))
        painter.drawEllipse(QRectF(0, 0, d, d))
        painter.setPen(QPen(QColor('#ffffff'), max(1.2, d * 0.08)))
        painter.setBrush(Qt.NoBrush)
        eye_w, eye_h = d * 0.62, d * 0.34
        painter.drawEllipse(QRectF(d / 2 - eye_w / 2, d / 2 - eye_h / 2, eye_w, eye_h))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#ffffff'))
        pupil = d * 0.18
        painter.drawEllipse(QRectF(d / 2 - pupil / 2, d / 2 - pupil / 2, pupil, pupil))

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.contains(event.pos()):
            self._clicked(self.key)
        event.accept()
# [FN CLOSED] EyeBadgeItem


class XrefEdgeItem(QGraphicsPathItem):
    def __init__(self, source, target, hovered, pinned):
        super().__init__()
        self.source, self.target = source, target
        self._hovered, self._pinned = hovered, pinned
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(-1)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        return stroker.createStroke(self.path())

    def hoverEnterEvent(self, event):
        self._hovered(self.source, self.target, event.scenePos(), True)
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        self._hovered(self.source, self.target, event.scenePos(), True)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered(self.source, self.target, event.scenePos(), False)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pinned(self.source, self.target, event.scenePos())
            event.accept()
            return
        super().mousePressEvent(event)


class XrefMapView(QGraphicsView):
    FONT_PT_SPAN = 6  # label font grows this many points from quietest to busiest node

    nodesPinned = Signal(list)     # ordered list of pinned element keys (sequence order = pin order)
    nodeActivated = Signal(str)     # element key, on double click
    nodeMoved = Signal(str, float, float)
    nodeHovered = Signal(str, object, bool)
    edgeHovered = Signal(str, str, object, bool)
    edgePinned = Signal(str, str, object)
    drillRequested = Signal(str)   # element key, from clicking its eye icon

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # panning is drag-based; scrollbars are clutter
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._elements = {}       # currently drawn subset: key -> element
        self._edges = []          # (source_key, target_key, path_item, arrow_item, base_color)
        self._containment_edges = []  # (parent_key, child_key, path_item) — neutral, arrowless hierarchy lines
        self._active_edge_tags = None  # None = no filter yet (show all); set by the dialog's edge-tag row
        self._show_containment = True  # whether the neutral "belonging" connections draw/pull at all
        self._rtl = False  # False = code flow reads left-to-right (default); True = right-to-left
        self._node_items = {}     # key -> rect item
        self._label_items = {}    # key -> text item
        self._pin_badges = {}     # key -> small "📌N" label shown above a pinned node
        self._eye_badges = {}     # key -> clickable "👁" drill-down icon, next to the pin badge
        self._pinned = []         # ordered list of pinned element keys; order = pin sequence number
        self._drillable_key = None  # key eligible for the eye icon right now (dialog decides eligibility)
        self._laying_out = False
        self._heatmap = False    # False: color by tag; True: color by connectivity heat
        self._max_degree = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate_tick)
        self._anim_start = {}
        self._anim_target = {}
        self._anim_clock = QElapsedTimer()
        self._anim_duration = 280

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.CODE_BG}; border:none;')
        self.scene().setBackgroundBrush(QColor(theme.CODE_BG))

    def wheelEvent(self, event):
        self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)

    def zoom(self, factor):
        current = self.transform().m11()
        target = max(0.15, min(3.0, current * factor))
        self.scale(target / current, target / current)
        self._update_node_scale()

    # [FN CATEGORY] _update_node_scale — bubble size follows the camera but damped: on-screen size
    # scales with zoom**0.5 instead of zoom**1, so zooming way out to see the whole graph doesn't
    # shrink labels to illegible specks, and zooming in close doesn't blow a single node up past
    # the screen. Scaled around each box's own center (not its top-left corner) so nodes don't
    # visually drift as the factor changes; edges re-anchor to the live (scaled) box afterward.
    # [FN] _update_node_scale — applies zoom-damped LOD scaling to every displayed node
    # [FN OPEN] _update_node_scale
    def _update_node_scale(self):
        zoom = self.transform().m11()
        if zoom <= 0:
            return
        factor = max(0.55, min(2.2, zoom ** -0.5))
        for item in self._node_items.values():
            item.setScale(factor)
        self._redraw_edges()
    # [FN CLOSED] _update_node_scale

    def fit(self):
        rect = self.scene().itemsBoundingRect()
        if not rect.isNull():
            self.resetTransform()
            self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
            if self.transform().m11() > 1.25:
                self.resetTransform()
                self.scale(1.25, 1.25)
                self.centerOn(rect.center())
            self._update_node_scale()

    # [FN CATEGORY] set_data — redraws the filtered graph using persisted coordinates as fixed
    # anchors and force-layout positions for new nodes, then creates every visible live edge.
    # [FN] set_data — renders a subset of the cross-reference graph
    # [FN OPEN] set_data
    def set_data(self, elements, saved_positions=None):
        scene = self.scene()
        scene.clear()
        self._elements = elements
        self._edges, self._node_items, self._label_items, self._pin_badges = [], {}, {}, {}
        self._eye_badges = {}
        self._containment_edges = []
        self._pinned = [key for key in self._pinned if key in elements]
        if not elements:
            empty = scene.addSimpleText('Nessun elemento da mostrare')
            empty.setBrush(QColor(theme.DIM))
            return

        self._max_degree = max((self._node_degree(e) for e in elements.values()), default=0)
        positions = _force_layout_positions(
            elements, saved_positions,
            active_edge_tags=self._active_edge_tags, use_parent_attraction=self._show_containment,
            rtl=self._rtl,
        )
        self._laying_out = True
        for el in elements.values():
            width, height, font_pt = self._node_dims(el)
            is_anchor = el.is_anchor
            path = QPainterPath()
            if is_anchor:
                path.addEllipse(0, 0, width, height)
            else:
                path.addRoundedRect(0, 0, width, height, 7, 7)
            rect = XrefNodeItem(
                el.key, path, self._node_moved,
                lambda key, point, entered: self.nodeHovered.emit(key, point, entered),
            )
            rect.setTransformOriginPoint(width / 2, height / 2)  # scale from center, not corner
            pen_color, fill_color = self._node_colors(el)
            rect.setPen(QPen(pen_color, 1.4))
            rect.setBrush(QBrush(fill_color))
            rect.setPos(*positions[el.key])
            if is_anchor:
                # unmarked: no text, no tag/category tooltip — just a reminder these elements
                # share an origin that isn't drawn right now
                rect.setToolTip('Origine comune (elemento radice non visualizzato)')
                label = QGraphicsSimpleTextItem('', rect)
            else:
                collapsed = el.collapsed
                prefix = '▸ ' if collapsed is True else ('▾ ' if collapsed is False else '')
                tooltip = f'{el.file}\n{el.category_desc or el.desc or el.name}'
                rect.setToolTip(tooltip)
                label_font = QFont('Consolas', round(font_pt))
                label_font.setUnderline(True)
                available_width = max(10, int(width) - 16)  # 8px margin each side
                elided = QFontMetrics(label_font).elidedText(f'{prefix}[{el.tag}] {el.desc}', Qt.ElideRight, available_width)
                label = QGraphicsSimpleTextItem(elided, rect)
                label.setFont(label_font)
                label.setBrush(QColor(theme.TAG_COLORS.get(el.tag, theme.TEXT)))
                label.setPos(8, (height - label.boundingRect().height()) / 2)
                label.setData(0, el.key)
                label.setToolTip(tooltip)
                label.setAcceptedMouseButtons(Qt.NoButton)
            pin_badge = PinBadgeItem(rect)
            pin_badge.set_appearance(1, 40.0)
            pin_badge.setPos(2, -47)  # a real gap above the node, not touching its top edge
            pin_badge.setZValue(3)
            pin_badge.setVisible(False)
            eye_badge = EyeBadgeItem(el.key, lambda key: self.drillRequested.emit(key), rect)
            eye_badge.set_diameter(40.0)
            eye_badge.setPos(2, -47)
            eye_badge.setZValue(4)
            eye_badge.setVisible(False)
            scene.addItem(rect)
            self._node_items[el.key] = rect
            self._label_items[el.key] = label
            self._pin_badges[el.key] = pin_badge
            self._eye_badges[el.key] = eye_badge
        self._laying_out = False

        for el in elements.values():
            for target_key in el.outgoing:
                if el.key not in self._node_items or target_key not in self._node_items:
                    continue
                target_el = elements.get(target_key)
                if self._active_edge_tags is not None and (
                    el.tag not in self._active_edge_tags
                    or (target_el is not None and target_el.tag not in self._active_edge_tags)
                ):
                    continue
                # a direct parent-child pair already has the neutral containment line — a colored
                # reference arrow between the very same two nodes (e.g. a class referencing its
                # own method) would just be a redundant second connection for one relationship
                if target_el is not None and (target_el.parent == el.key or el.parent == target_key):
                    continue
                base_color = self._node_colors(el)[0]
                color = QColor(base_color)
                color.setAlpha(80)
                path_item = XrefEdgeItem(
                    el.key, target_key,
                    lambda source, target, point, entered: self.edgeHovered.emit(source, target, point, entered),
                    lambda source, target, point: self.edgePinned.emit(source, target, point),
                )
                path_item.setPen(QPen(color, 1.4))
                scene.addItem(path_item)
                arrow = scene.addPolygon(
                    QPolygonF(), QPen(Qt.NoPen), QBrush(color),
                )
                arrow.setZValue(-1)
                arrow.setAcceptedMouseButtons(Qt.NoButton)
                self._edges.append((el.key, target_key, path_item, arrow, base_color))

        # hierarchy lines: a plain, arrowless black edge from each element to its immediate
        # container (module/class, or a common-origin anchor standing in for one that isn't
        # currently displayed) — independent of the colored directed reference arrows above, this
        # is what makes containment/belonging readable on the map, not just call flow. Toggled off
        # entirely via the "Appartenenza" connections selector.
        if self._show_containment:
            for el in elements.values():
                if el.parent and el.parent in self._node_items and el.key in self._node_items:
                    line_item = QGraphicsPathItem()
                    line_item.setZValue(-2)
                    line_item.setAcceptedMouseButtons(Qt.NoButton)
                    line_item.setPen(QPen(QColor(0, 0, 0, 90), 1.2))
                    scene.addItem(line_item)
                    self._containment_edges.append((el.parent, el.key, line_item))

        self._update_node_scale()  # newly built nodes must reflect the current zoom's LOD factor
        # An Obsidian-like canvas needs breathing room; generous stable bounds prevent scrollbar
        # recentering when a module expands or a node is dragged near the current graph edge.
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-1600, -1200, 1600, 1200))
        self._apply_highlight()
    # [FN CLOSED] set_data

    def _node_degree(self, el):
        return _element_degree(el, self._elements, self._active_edge_tags)

    def _traffic_t(self, el):
        """0..1: how connected this element is relative to the busiest one on screen right now."""
        return (self._node_degree(el) / self._max_degree) if self._max_degree else 0.0

    # [FN CATEGORY] _node_colors — a node's pen/fill colors: normally per-tag (TAG_COLORS/
    # TAG_BACKGROUNDS), but in heatmap mode a classic cold-to-hot gradient keyed on how connected
    # the node is (incoming + outgoing reference count), normalized against the busiest node
    # currently on screen — so the map reads as "where the activity is" instead of "what kind of
    # thing this is".
    # [FN] _node_colors — returns (pen_color, fill_color) for one displayed node
    # [FN OPEN] _node_colors
    def _node_colors(self, el):
        if el.is_anchor:
            black = QColor(0, 0, 0)
            return black, black  # unmarked: same black whether heatmap or tag coloring is active
        if self._heatmap:
            color = _heat_color(self._traffic_t(el))
            fill = QColor(color)
            fill.setAlpha(130)
            return color, fill
        return QColor(theme.TAG_COLORS.get(el.tag, theme.DIM)), QColor(theme.TAG_BACKGROUNDS.get(el.tag, theme.PANEL))
    # [FN CLOSED] _node_colors

    # [FN CATEGORY] _node_dims — box width/height and label font size scale with how much code
    # traffic this element carries (incoming + outgoing reference count, same metric as heatmap
    # coloring): a busy hub gets a bigger, more legible box, a quiet leaf stays compact — always
    # on, independent of whether heatmap coloring itself is enabled.
    # [FN] _node_dims — returns (width, height, font_pt) for one displayed node
    # [FN OPEN] _node_dims
    def _node_dims(self, el):
        width, height = _element_size(el, self._max_degree, self._elements, self._active_edge_tags)
        font_pt = (theme.CODE_FONT_PT - 3) + self.FONT_PT_SPAN * self._traffic_t(el)
        return width, height, font_pt
    # [FN CLOSED] _node_dims

    def recolor(self, heatmap):
        """Re-applies tag or heatmap coloring to the already-drawn graph, without touching
        positions, the camera, or the selected/pinned state — a pure appearance toggle."""
        self._heatmap = heatmap
        self._max_degree = max((self._node_degree(e) for e in self._elements.values()), default=0) if heatmap else 0
        for key, rect in self._node_items.items():
            pen_color, fill_color = self._node_colors(self._elements[key])
            rect.setPen(QPen(pen_color, 1.4))
            rect.setBrush(QBrush(fill_color))
        for index, (source, target, path_item, arrow, _old_base) in enumerate(self._edges):
            base_color = self._node_colors(self._elements[source])[0]
            self._edges[index] = (source, target, path_item, arrow, base_color)
        self._apply_highlight()

    def positions(self):
        return {key: (item.pos().x(), item.pos().y()) for key, item in self._node_items.items()}

    # [FN CATEGORY] relayout_to — recomputes positions for a changed element set (tag/file filter,
    # module expand/collapse, isolate) using current on-screen coordinates as a warm start rather
    # than hard pins, then renders at the final layout and animates surviving nodes back from their
    # old spot to it — so filtering reads as the graph settling into place, not a jump cut.
    # [FN] relayout_to — recomputes and smoothly animates node positions for a new element set
    # [FN OPEN] relayout_to
    def relayout_to(self, elements, seed_positions):
        old_on_screen = {key: item.pos() for key, item in self._node_items.items()}
        new_positions = _force_layout_positions(
            elements, seed=seed_positions,
            active_edge_tags=self._active_edge_tags, use_parent_attraction=self._show_containment, rtl=self._rtl,
        )
        self.set_data(elements, new_positions)
        self._laying_out = True
        for key, pos in old_on_screen.items():
            item = self._node_items.get(key)
            if item is not None:
                item.setPos(pos)
        self._laying_out = False
        self._redraw_edges()
        self._animate_positions(old_on_screen, new_positions)
        return new_positions
    # [FN CLOSED] relayout_to

    def _animate_positions(self, start, target, duration_ms=280):
        self._anim_timer.stop()
        self._anim_start = {key: QPointF(pos) for key, pos in start.items() if key in target and key in self._node_items}
        self._anim_target = {key: QPointF(*target[key]) for key in self._anim_start}
        if not self._anim_start:
            return
        self._anim_duration = duration_ms
        self._laying_out = True
        self._anim_clock.start()
        self._anim_timer.start(16)

    def _animate_tick(self):
        elapsed = self._anim_clock.elapsed()
        t = min(1.0, elapsed / self._anim_duration)
        eased = 1 - (1 - t) ** 3  # ease-out cubic
        for key, start_pos in self._anim_start.items():
            item = self._node_items.get(key)
            if item is not None:
                item.setPos(start_pos + (self._anim_target[key] - start_pos) * eased)
        # one redraw for the whole batch, not one per node moved this frame (see _node_moved)
        self._redraw_edges()
        if t >= 1.0:
            self._anim_timer.stop()
            self._laying_out = False

    def _anchor(self, source_key, target_key):
        source = self._node_items[source_key].sceneBoundingRect()
        target = self._node_items[target_key].sceneBoundingRect()
        start, end = source.center(), target.center()
        dx, dy = end.x() - start.x(), end.y() - start.y()
        if not dx and not dy:
            return start, end

        def boundary(rect, center, vx, vy):
            scale = min(
                (rect.width() / 2) / abs(vx) if vx else float('inf'),
                (rect.height() / 2) / abs(vy) if vy else float('inf'),
            )
            return QPointF(center.x() + vx * scale, center.y() + vy * scale)

        return boundary(source, start, dx, dy), boundary(target, end, -dx, -dy)

    def _redraw_edges(self):
        for source, target, path_item, arrow, _base in self._edges:
            if source not in self._node_items or target not in self._node_items:
                continue
            p1, p2 = self._anchor(source, target)
            dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
            length = math.hypot(dx, dy) or 1.0
            bend = min(34.0, length * 0.08) * (-1 if source < target else 1)
            control = QPointF((p1.x() + p2.x()) / 2 - dy / length * bend,
                              (p1.y() + p2.y()) / 2 + dx / length * bend)
            path = QPainterPath(p1)
            path.quadTo(control, p2)
            path_item.setPath(path)
            direction = p2 - control
            direction_length = math.hypot(direction.x(), direction.y()) or 1.0
            ux, uy = direction.x() / direction_length, direction.y() / direction_length
            back = QPointF(p2.x() - 9 * ux, p2.y() - 9 * uy)
            normal = QPointF(-uy * 4, ux * 4)
            arrow.setPolygon(QPolygonF([p2, back + normal, back - normal]))
        for parent_key, child_key, line_item in self._containment_edges:
            if parent_key not in self._node_items or child_key not in self._node_items:
                continue
            p1, p2 = self._anchor(parent_key, child_key)
            path = QPainterPath(p1)
            path.lineTo(p2)  # straight, not bent — reads as structural, not a call
            line_item.setPath(path)

    def _node_moved(self, key):
        if self._laying_out:
            # a programmatic relayout (or the initial set_data build) moves every node in one
            # batch; redrawing all edges once per node here is O(nodes) redundant redraws per
            # frame — the animation tick (or set_data itself) does the single redraw that's
            # actually needed once the whole batch has moved
            return
        self._redraw_edges()
        node_rect = self._node_items[key].sceneBoundingRect().adjusted(-80, -80, 80, 80)
        self.scene().setSceneRect(self.scene().sceneRect().united(node_rect))
        point = self._node_items[key].pos()
        self.nodeMoved.emit(key, point.x(), point.y())

    def _key_at(self, viewport_pos):
        item = self.itemAt(viewport_pos)
        return item.data(0) if item is not None else None

    def mousePressEvent(self, event):
        # the eye badge sits on top of its node (higher Z) with no item-data of its own, so
        # itemAt() here would return it and _key_at would read back None — read as "clicked empty
        # canvas" and clear every pin before Qt ever delivers the press to the eye's own handler.
        # Recognize it explicitly and let the normal event dispatch below reach it untouched.
        if isinstance(self.itemAt(event.position().toPoint()), EyeBadgeItem):
            super().mousePressEvent(event)
            return
        key = self._key_at(event.position().toPoint())
        if key is not None:
            pinned = list(self._pinned)
            if key in pinned:
                pinned.remove(key)  # clicking an already-pinned node's own pin ends its highlight
            else:
                pinned.append(key)  # new pin goes to the end of the sequence
            self.set_pinned(pinned)
            self.nodesPinned.emit(list(self._pinned))
        elif self._pinned:
            self.set_pinned([])  # clicking empty canvas clears every pin at once
            self.nodesPinned.emit([])
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        key = self._key_at(event.position().toPoint())
        if key is not None:
            self.nodeActivated.emit(key)
        super().mouseDoubleClickEvent(event)

    def select(self, key):
        """Pin exactly this node, clearing any others — for single-target jumps (isolate, search)."""
        self.set_pinned([key] if key in self._node_items else [])

    def set_pinned(self, keys):
        """Replace the whole pin set (e.g. restoring it after a relayout) and redraw highlighting."""
        self._pinned = [key for key in keys if key in self._node_items]
        self._apply_highlight()

    def set_active_edge_tags(self, tags):
        """Which tags' reference edges to draw at all — independent of node visibility (self can
        show a function node while hiding every edge that touches a function, or vice versa).
        Takes effect on the next set_data(), not retroactively."""
        self._active_edge_tags = set(tags)

    def set_show_containment(self, show):
        """Whether the neutral "belonging" (containment/common-origin) connections draw and pull
        at all. Takes effect on the next set_data(), not retroactively."""
        self._show_containment = show

    def set_direction(self, rtl):
        """False (default) = code flow reads left-to-right; True = right-to-left. Takes effect
        on the next set_data()/relayout_to(), not retroactively."""
        self._rtl = rtl

    def set_drillable(self, key):
        """Which key (if any) is eligible for the eye icon right now — the dialog decides
        eligibility (needs the full, undisplayed graph to check for internal cross-references)
        and pushes it down here; this just redraws to reflect it."""
        self._drillable_key = key
        self._apply_highlight()

    def focus_on(self, key):
        if key in self._node_items:
            self.select(key)
            self.centerOn(self._node_items[key])

    # [FN CATEGORY] _apply_highlight — multi-pin focus: every pinned node and its direct neighbours
    # stay full-strength while everything else drops to low opacity, so several elements' connections
    # can be compared at once in a dense graph; each pinned node also gets a small numbered "📌N"
    # badge above it showing its position in the pin sequence. When exactly one node is pinned and
    # it's the current drillable key (dialog-decided eligibility), that badge and a "👁" drill-down
    # icon next to it both enlarge — the eye opens the internal-only view for that element. No pins
    # resets all to the default resting look.
    # [FN] _apply_highlight — dims everything except pinned nodes and their neighbours
    # [FN OPEN] _apply_highlight
    def _apply_highlight(self):
        show_all = not self._pinned
        neighbours = set()
        for key in self._pinned:
            if key in self._elements:
                el = self._elements[key]
                neighbours |= {key} | set(el.incoming) | set(el.outgoing)
        for parent, child, _line in self._containment_edges:
            if parent in self._pinned or child in self._pinned:
                neighbours |= {parent, child}
        for k, rect in self._node_items.items():
            active = show_all or k in neighbours
            rect.setOpacity(1.0 if active else 0.18)
            self._label_items[k].setOpacity(1.0)  # child inherits the node's dimming
            badge = self._pin_badges.get(k)
            eye = self._eye_badges.get(k)
            if badge is not None:
                if k in self._pinned:
                    drillable = len(self._pinned) == 1 and k == self._drillable_key
                    diameter = 60.0 if drillable else 40.0
                    badge.set_appearance(self._pinned.index(k) + 1, diameter)
                    badge.setPos(2, -diameter - 7)
                    badge.setVisible(True)
                    badge.setOpacity(1.0)
                    if eye is not None:
                        eye.setVisible(drillable)
                        if drillable:
                            eye.set_diameter(diameter)
                            eye.setOpacity(1.0)
                            eye.setPos(badge.pos().x() + diameter + 8, badge.pos().y())
                else:
                    badge.setVisible(False)
                    if eye is not None:
                        eye.setVisible(False)
            pen = rect.pen()
            pen.setWidthF(2.8 if k in self._pinned else 1.4)
            rect.setPen(pen)
        for src, dst, path_item, arrow, base in self._edges:
            touches = show_all or src in self._pinned or dst in self._pinned
            color = QColor(base)
            color.setAlpha((210 if not show_all else 80) if touches else 16)
            pen = path_item.pen()
            pen.setColor(color)
            pen.setWidthF(2.2 if (not show_all and touches) else 1.4)
            path_item.setPen(pen)
            arrow.setBrush(QBrush(color))
        for parent_key, child_key, line_item in self._containment_edges:
            touches = show_all or parent_key in self._pinned or child_key in self._pinned
            pen = line_item.pen()
            pen.setColor(QColor(0, 0, 0, (140 if not show_all else 90) if touches else 18))
            line_item.setPen(pen)
    # [FN CLOSED] _apply_highlight
# [FN CLOSED] XrefMapView


class EdgeFlowPopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('edgeFlowPopup')
        self._base_min_width, self._base_max_width = 340, 480
        self.setMinimumWidth(self._base_min_width)
        self.setMaximumWidth(self._base_max_width)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._pinned = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(7)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.state = QLabel('Hover · clicca l’arco per fissare')
        self.incoming = QLabel()
        self.outgoing = QLabel()
        for label in (self.incoming, self.outgoing):
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.title)
        layout.addWidget(self.state)
        layout.addWidget(self.incoming)
        layout.addWidget(self.outgoing)
        self.apply_style()
        self.hide()

    # [FN CATEGORY] apply_style — re-applies theme colors, plus a stronger accent border and bolder
    # state line while pinned, so a fixed popup clearly reads as "stuck" rather than a passing hover.
    # Pinned uses HOT (not ACCENT) since ACCENT is the same blue as every button/hover/selection in
    # the app — a pinned popup needs a border that doesn't blend into that everywhere-blue.
    # [FN] apply_style — re-applies theme (and pinned-state) styling to the popup
    # [FN OPEN] apply_style
    def apply_style(self):
        border_color = theme.HOT if self._pinned else theme.BORDER
        border_width = 2 if self._pinned else 1
        self.setStyleSheet(
            f'#edgeFlowPopup {{ background:{theme.PANEL}; border:{border_width}px solid {border_color}; border-radius:12px; }}'
        )
        self.title.setStyleSheet(f'color:{theme.TEXT}; font-weight:700; border:none;')
        self.state.setStyleSheet(
            f'color:{theme.HOT if self._pinned else theme.DIM}; font-weight:{700 if self._pinned else 400}; border:none;'
        )
        self.incoming.setStyleSheet(f'color:{theme.OK}; border:none;')
        self.outgoing.setStyleSheet('color:#ef4444; border:none;')
    # [FN CLOSED] apply_style

    def set_flow(self, title, incoming, outgoing, pinned):
        self._pinned = pinned
        self.title.setText(_kant_mentions_html(title))
        self.title.setTextFormat(Qt.RichText)
        self.state.setText('Fissato · clicca di nuovo l’arco per chiudere' if pinned else 'Hover · clicca l’arco per fissare')
        self.incoming.setText(_kant_mentions_html('INCOMING\n' + ('\n'.join(f'← {item}' for item in incoming) if incoming else '← nessuno')))
        self.outgoing.setText(_kant_mentions_html('OUTGOING\n' + ('\n'.join(f'→ {item}' for item in outgoing) if outgoing else '→ nessuno')))
        self.apply_style()
        self.adjustSize()

    # [FN CATEGORY] set_zoom_scale — the popup is a fixed screen-space overlay, not a scene item, so
    # it never zooms with the canvas on its own; this makes its size track the camera's current
    # zoom level (clamped to a sane range) instead of staying identical regardless of how far in or
    # out the map is.
    # [FN] set_zoom_scale — scales the popup's width bounds and font sizes with the given factor
    # [FN OPEN] set_zoom_scale
    def set_zoom_scale(self, scale):
        scale = max(0.7, min(1.6, scale))
        self.setMinimumWidth(round(self._base_min_width * scale))
        self.setMaximumWidth(round(self._base_max_width * scale))
        base_pt = theme.CODE_FONT_PT
        title_font = self.title.font()
        title_font.setPointSizeF(max(7.0, (base_pt + 1) * scale))
        self.title.setFont(title_font)
        for label in (self.state, self.incoming, self.outgoing):
            font = label.font()
            font.setPointSizeF(max(6.5, base_pt * scale))
            label.setFont(font)
        self.adjustSize()
    # [FN CLOSED] set_zoom_scale


# [FN CATEGORY] XrefMapDialog — the cross-reference map as a frameless dialog INTERNAL to the IDE
# (a QDialog parented to the main window, not a separate OS window with its own taskbar entry): it
# floats over the editor, closes with the app, and is centered over the main window on first show.
# Wraps XrefMapView with the aids that make a large graph usable. Two defaults matter: every module
# starts fully EXPANDED on each open (set_graph() re-expands every file, regardless of how a
# previous session left it) — double-click a module to collapse it back to its file-level MOD/CFG
# node, aggregating references between whole files onto it — and the TST tag starts OFF so tests
# are hidden. Also: a name/description search that expands+focuses a hidden match, tag
# toggle buttons (also the colour legend), a file selector that isolates one file plus its
# neighbours, an "isolate selected" mode, expand/collapse-all, and zoom/fit. Owns the full graph and
# recomputes the displayed (filtered + collapsed) node set on every change; the view only renders
# that. Double-clicking a leaf node emits nodeActivated so the main window can open it in the editor.
# [FN] XrefMapDialog — filterable, searchable, collapsible cross-reference map dialog
# [FN OPEN] XrefMapDialog
class XrefMapDialog(QDialog):
    TAG_ORDER = ('MOD', 'CFG', 'CLS', 'TYP', 'FN', 'CST', 'VAR', 'TST')

    nodeActivated = Signal(str)
    resized = Signal()  # lets the main window keep the close-tab (reparented onto this dialog) positioned

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(False)
        self._elements = {}
        self._display = {}                              # last rendered display nodes
        self._active_tags = set(self.TAG_ORDER) - {'TST'}   # tests hidden by default
        # MOD edges include the aggregated module-to-module connections a collapsed file's node
        # carries (the sum of every underlying element's own in/out, via _display_elements' dkey
        # remap) — start those hidden so the map opens showing individual element-level references,
        # not a wall of module-to-module summary arrows; the "Connessioni: MOD" toggle re-enables them.
        self._active_edge_tags = set(self.TAG_ORDER) - {'MOD'}
        self._show_containment = True    # the neutral "belonging" connections, toggled alongside the tags
        self._rtl = False    # False = code flow reads left-to-right (default); True = right-to-left
        self._expanded = set()                          # files shown expanded; set_graph() fills this with every file on each open
        self._focus_file = None
        self._isolate = False
        self._selected = None       # most-recently-pinned key — read by selected_key() for map-close navigation
        self._pinned_nodes = []     # full ordered multi-pin set (mirrors XrefMapView._pinned)
        self._positioned = False
        self._position_key = None
        self._positions = {}
        self._position_timer = QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.timeout.connect(self._save_positions)
        self._pinned_edge = None
        self._edge_hide_timer = QTimer(self)
        self._edge_hide_timer.setSingleShot(True)
        self._edge_hide_timer.timeout.connect(self._hide_edge_popup)
        self._hover_show_timer = QTimer(self)     # delay between hover and the popup appearing
        self._hover_show_timer.setSingleShot(True)
        self._hover_show_timer.timeout.connect(self._show_pending_hover)
        self._pending_hover = None                # ('edge', (source, target, point)) or ('node', (key, point))
        self._drill_key = None    # element whose internal-only view we're showing, or None for the full map
        self._project_name = ''

        self.view = XrefMapView()
        self.view.set_active_edge_tags(self._active_edge_tags)
        self.view.set_show_containment(self._show_containment)
        self.view.set_direction(self._rtl)
        self.view.nodesPinned.connect(self._on_nodes_pinned)
        self.view.nodeActivated.connect(self._on_node_activated)
        self.view.nodeMoved.connect(self._on_node_moved)
        self.view.nodeHovered.connect(self._on_node_hovered)
        self.view.edgeHovered.connect(self._on_edge_hovered)
        self.view.edgePinned.connect(self._on_edge_pinned)
        self.view.drillRequested.connect(self._enter_drill_mode)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_toolbar())
        outer.addWidget(self.view, 1)
        outer.addWidget(self._build_footer())
        self.edge_popup = EdgeFlowPopup(self)

        # drill mode's detached "title card": a widget overlay (not a scene item, so it never
        # scales with zoom) pinned to the view's top-right corner at a constant on-screen size
        self.drill_title_card = QFrame(self)
        self.drill_title_card.setObjectName('drillTitleCard')
        drill_card_layout = QVBoxLayout(self.drill_title_card)
        drill_card_layout.setContentsMargins(16, 10, 16, 12)
        drill_card_layout.setSpacing(2)
        self.drill_title_tag = QLabel('')
        self.drill_title_tag.setFont(QFont('Consolas', 10, QFont.DemiBold))
        self.drill_title_name = QLabel('')
        self.drill_title_name.setFont(QFont('Consolas', 20, QFont.Bold))
        self.drill_title_name.setWordWrap(True)
        drill_card_layout.addWidget(self.drill_title_tag)
        drill_card_layout.addWidget(self.drill_title_name)
        self.drill_title_card.setMaximumWidth(320)
        self.drill_title_card.hide()
        self.resized.connect(self._position_drill_title_card)

    def _build_toolbar(self):
        bar = QWidget()
        bar.setObjectName('mapToolbar')
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(10, 8, 10, 8)
        rows.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        # only visible while drilled into one element's internals — takes the header's old spot
        # now that the window has no header row (fixed position, no title/close-button chrome)
        self.drill_back_btn = QPushButton(' Torna alla mappa completa')
        self.drill_back_btn.setIcon(draw_icon('arrow-left', 14))
        self.drill_back_btn.setIconSize(QSize(14, 14))
        self.drill_back_btn.setFixedHeight(28)
        self.drill_back_btn.setToolTip("Esce dalla vista concentrata su un elemento e torna alla mappa completa")
        self.drill_back_btn.clicked.connect(self._exit_drill_mode)
        self.drill_back_btn.hide()
        top.addWidget(self.drill_back_btn)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText('Cerca per nome o descrizione…')
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setToolTip('Filtra i nodi mostrati per nome o descrizione')
        self.search_box.textChanged.connect(self._on_search)
        self.search_box.returnPressed.connect(self._on_search_enter)
        self.search_box.setMaximumWidth(300)
        top.addWidget(self.search_box)

        top.addWidget(QLabel('File:'))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(200)
        self.file_combo.setToolTip('Mostra solo i nodi del file scelto (o tutti i file)')
        self.file_combo.currentIndexChanged.connect(self._on_file_filter)
        top.addWidget(self.file_combo)

        self.expand_all_btn = QPushButton(' Espandi tutti')
        self.expand_all_btn.setIcon(draw_icon('expand', 14))
        self.expand_all_btn.setIconSize(QSize(14, 14))
        self.expand_all_btn.setToolTip('Espande tutti i moduli/classi raggruppati nella mappa')
        self.collapse_all_btn = QPushButton(' Comprimi tutti')
        self.collapse_all_btn.setIcon(draw_icon('collapse', 14))
        self.collapse_all_btn.setIconSize(QSize(14, 14))
        self.collapse_all_btn.setToolTip('Comprime tutti i moduli/classi in un singolo nodo ciascuno')
        self.expand_all_btn.clicked.connect(self._expand_all)
        self.collapse_all_btn.clicked.connect(self._collapse_all)
        top.addWidget(self.expand_all_btn)
        top.addWidget(self.collapse_all_btn)
        self.relayout_btn = QPushButton(' Riorganizza')
        self.relayout_btn.setIcon(draw_icon('undo', 14))
        self.relayout_btn.setIconSize(QSize(14, 14))
        self.relayout_btn.setToolTip('Ricalcola la disposizione dei nodi, scartando le posizioni trascinate a mano')
        self.relayout_btn.clicked.connect(self._reorganize)
        top.addWidget(self.relayout_btn)

        self.isolate_btn = QPushButton(' Isola selezionato')
        self.isolate_btn.setIcon(draw_icon('target', 14))
        self.isolate_btn.setIconSize(QSize(14, 14))
        self.isolate_btn.setCheckable(True)
        self.isolate_btn.setToolTip('Mostra solo il nodo selezionato e i suoi collegamenti diretti')
        self.isolate_btn.toggled.connect(self._on_isolate)
        top.addWidget(self.isolate_btn)

        self.heatmap_btn = QPushButton(' Heatmap')
        self.heatmap_btn.setIcon(draw_icon('flame', 14))
        self.heatmap_btn.setIconSize(QSize(14, 14))
        self.heatmap_btn.setCheckable(True)
        self.heatmap_btn.setToolTip('Colora i nodi per connettività (caldo = molti riferimenti) invece che per tag')
        self.heatmap_btn.toggled.connect(self._on_heatmap_toggle)
        top.addWidget(self.heatmap_btn)

        # direction of the code flow (module rank + intra-cluster call depth): left-to-right by
        # default, this button flips the whole layout to right-to-left
        self.direction_btn = QPushButton(' Direzione: Sx → Dx')
        self.direction_btn.setIcon(draw_icon('swap', 14))
        self.direction_btn.setIconSize(QSize(14, 14))
        self.direction_btn.setCheckable(True)
        self.direction_btn.setToolTip('Inverte la direzione del flusso logico del codice nella mappa')
        self.direction_btn.toggled.connect(self._on_direction_toggle)
        top.addWidget(self.direction_btn)

        top.addStretch(1)
        zoom_out = QPushButton('−')
        zoom_in = QPushButton('+')
        fit = QPushButton('Adatta')
        zoom_out.setToolTip('Rimpicciolisci')
        zoom_in.setToolTip('Ingrandisci')
        fit.setToolTip('Adatta lo zoom per mostrare tutta la mappa')
        zoom_out.clicked.connect(lambda: self.view.zoom(1 / 1.2))
        zoom_in.clicked.connect(lambda: self.view.zoom(1.2))
        fit.clicked.connect(self.view.fit)
        for b in (zoom_out, zoom_in, fit):
            b.setFixedHeight(28)
            top.addWidget(b)
        rows.addLayout(top)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(6)
        tag_row.addWidget(QLabel('Tag:'))
        self.tag_buttons = {}
        for tag in self.TAG_ORDER:
            btn = QPushButton(tag)
            btn.setCheckable(True)
            btn.setChecked(tag in self._active_tags)
            btn.setFixedHeight(26)
            btn.setToolTip(f'Mostra/nascondi i nodi con tag {tag}')
            btn.toggled.connect(lambda checked, t=tag: self._on_tag_toggle(t, checked))
            self.tag_buttons[tag] = btn
            tag_row.addWidget(btn)
        tag_row.addStretch(1)
        self.count_label = QLabel('')
        self.count_label.setStyleSheet(f'color:{theme.DIM};')
        tag_row.addWidget(self.count_label)
        rows.addLayout(tag_row)

        # separate from node visibility above: which tags' reference connections are drawn at all
        # (e.g. show FN nodes but hide every edge touching a function) — same tag set, own state
        edge_tag_row = QHBoxLayout()
        edge_tag_row.setSpacing(6)
        edge_tag_row.addWidget(QLabel('Connessioni:'))
        self.edge_tag_buttons = {}
        for tag in self.TAG_ORDER:
            btn = QPushButton(tag)
            btn.setCheckable(True)
            btn.setChecked(tag in self._active_edge_tags)
            btn.setFixedHeight(26)
            btn.setToolTip(f'Mostra/nascondi le connessioni verso nodi con tag {tag}')
            btn.toggled.connect(lambda checked, t=tag: self._on_edge_tag_toggle(t, checked))
            self.edge_tag_buttons[tag] = btn
            edge_tag_row.addWidget(btn)
        edge_tag_row.addSpacing(10)
        self.containment_btn = QPushButton(' Appartenenza')
        self.containment_btn.setIcon(draw_icon('nest', 14))
        self.containment_btn.setIconSize(QSize(14, 14))
        self.containment_btn.setCheckable(True)
        self.containment_btn.setChecked(self._show_containment)
        self.containment_btn.setFixedHeight(26)
        self.containment_btn.setToolTip('Connessione neutra che collega un elemento alla sua origine comune (modulo/classe)')
        self.containment_btn.toggled.connect(self._on_containment_toggle)
        edge_tag_row.addWidget(self.containment_btn)
        edge_tag_row.addStretch(1)
        rows.addLayout(edge_tag_row)

        self._toolbar = bar
        return bar

    def _build_footer(self):
        bar = QWidget()
        bar.setObjectName('mapFooter')
        bar.setFixedHeight(20)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.hint_label = QLabel('  Trascina i nodi per disporli · doppio clic per espandere o aprire · Riorganizza ripristina il layout')
        self.hint_label.setStyleSheet(f'color:{theme.DIM};')
        row.addWidget(self.hint_label)
        row.addStretch(1)
        self._footer = bar
        return bar

    def apply_style(self):
        self.setStyleSheet(
            f'XrefMapDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}'
        )
        self.drill_back_btn.setStyleSheet(theme.BUTTON_STYLE)
        self._toolbar.setStyleSheet(
            f'#mapToolbar {{ background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER}; }} '
            f'QLabel {{ color:{theme.TEXT}; }}'
        )
        self._footer.setStyleSheet(f'#mapFooter {{ background:{theme.PANEL}; }}')
        self.search_box.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:5px 8px;'
        )
        self.file_combo.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:4px 8px;'
        )
        for b in (self.isolate_btn, self.heatmap_btn, self.direction_btn, self.expand_all_btn, self.collapse_all_btn, self.relayout_btn):
            b.setStyleSheet(theme.BUTTON_STYLE)
        for button, kind in (
            (self.drill_back_btn, 'arrow-left'), (self.expand_all_btn, 'expand'),
            (self.collapse_all_btn, 'collapse'), (self.relayout_btn, 'undo'),
            (self.isolate_btn, 'target'), (self.heatmap_btn, 'flame'),
            (self.direction_btn, 'swap'), (self.containment_btn, 'nest'),
        ):
            button.setIcon(draw_icon(kind, 14))
        for tag, btn in self.tag_buttons.items():
            color = theme.TAG_COLORS.get(tag, theme.DIM)
            bg = theme.TAG_BACKGROUNDS.get(tag, theme.PANEL)
            # checked = colour-filled legend chip; unchecked = muted, so the toggle doubles as legend
            btn.setStyleSheet(
                f'QPushButton {{ border:1px solid {color}; border-radius:6px; padding:3px 10px; '
                f'font-weight:700; color:{color}; background:{theme.PANEL}; }} '
                f'QPushButton:checked {{ background:{bg}; color:{color}; }} '
                f'QPushButton:!checked {{ color:{theme.DIM}; border-color:{theme.BORDER}; }}'
            )
        for tag, btn in self.edge_tag_buttons.items():
            color = theme.TAG_COLORS.get(tag, theme.DIM)
            bg = theme.TAG_BACKGROUNDS.get(tag, theme.PANEL)
            btn.setStyleSheet(
                f'QPushButton {{ border:1px solid {color}; border-radius:6px; padding:3px 10px; '
                f'font-weight:700; color:{color}; background:{theme.PANEL}; }} '
                f'QPushButton:checked {{ background:{bg}; color:{color}; }} '
                f'QPushButton:!checked {{ color:{theme.DIM}; border-color:{theme.BORDER}; }}'
            )
        # black, not a tag color — this toggle is the neutral belonging connection itself
        self.containment_btn.setStyleSheet(
            f'QPushButton {{ border:1px solid #000000; border-radius:6px; padding:3px 10px; '
            f'font-weight:700; color:#000000; background:{theme.PANEL}; }} '
            f'QPushButton:checked {{ background:#00000022; color:#000000; }} '
            f'QPushButton:!checked {{ color:{theme.DIM}; border-color:{theme.BORDER}; }}'
        )
        self.view.apply_style()
        self.edge_popup.apply_style()
        self.drill_title_card.setStyleSheet(
            f'#drillTitleCard {{ background:{theme.PANEL}; border:2px solid {theme.ACCENT}; border-radius:10px; }}'
        )
        self.drill_title_tag.setStyleSheet(f'color:{theme.ACCENT}; letter-spacing:1px; border:none;')
        self.drill_title_name.setStyleSheet(f'color:{theme.TEXT}; border:none;')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def showEvent(self, event):
        super().showEvent(event)
        # generic fallback sizing only — precise full-page alignment (flush with the main
        # window's own action toolbar/status bar) is MainWindow's job, not this dialog's: it
        # needs mainwindow-specific coordination ("Application-wide coordination stays in
        # mainwindow.py", per this module's own docstring), and MainWindow already has the
        # established pattern for it (see _position_map_dialog, called after every .show() —
        # not just the first — so it can't go stale like a one-shot flag here would)
        parent = self.parentWidget()
        if not self._positioned and parent is not None:
            self.resize(int(parent.width() * 0.96), int(parent.height() * 0.94))
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            self.move(geo.topLeft())
            self._positioned = True

    # [FN] set_graph — loads a fresh full graph and renders it (keeping the user's filter state)
    # [FN OPEN] set_graph
    def set_graph(self, elements, project_name='', project_path=''):
        position_key = _position_settings_key(project_path, project_name)
        new_project = position_key != self._position_key
        if position_key != self._position_key:
            self._position_key = position_key
            self._positions = self._load_positions()
        self._elements = elements
        self._positions = {key: value for key, value in self._positions.items() if key in elements}
        self._selected = None
        self._pinned_nodes = []
        self._project_name = project_name
        files = sorted({el.file for el in elements.values()})
        # every open starts fully expanded, regardless of how it was left last time
        self._expanded = set(files)
        current = self.file_combo.currentData()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.addItem('Tutti i file', None)
        for f in files:
            self.file_combo.addItem(f, f)
        idx = self.file_combo.findData(current)
        self.file_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.file_combo.blockSignals(False)
        self._focus_file = self.file_combo.currentData()
        # a same-project refresh (e.g. a newly detected element after an xref rebuild) should settle
        # into place like every other filter/isolate trigger already does (relayout_to's animated
        # transition), not snap instantly via set_data — that path is reserved for a genuinely new
        # project or the graph's very first render, where there's nothing on screen yet to animate from
        has_nodes = bool(self.view._node_items)
        self._refresh(fit=new_project or not has_nodes, relayout=has_nodes and not new_project)
    # [FN CLOSED] set_graph

    def _load_positions(self):
        try:
            raw = QSettings('KANT', 'KANT Editor').value(self._position_key, '{}')
            data = json.loads(raw)
            return {
                key: (float(value[0]), float(value[1]))
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, list) and len(value) == 2
                and all(isinstance(number, (int, float)) and math.isfinite(number) for number in value)
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _save_positions(self):
        if self._position_key:
            QSettings('KANT', 'KANT Editor').setValue(
                self._position_key,
                json.dumps({key: [round(x, 2), round(y, 2)] for key, (x, y) in self._positions.items()}),
            )

    def _on_node_moved(self, key, x, y):
        self._positions[key] = (x, y)
        self._position_timer.start(250)

    def _reorganize(self):
        self._positions.clear()
        self._refresh(relayout=True, fit=True)
        self._save_positions()

    def closeEvent(self, event):
        self._position_timer.stop()
        self._save_positions()
        super().closeEvent(event)

    def _element_label(self, key):
        element = self._display.get(key) or self._elements.get(key)
        return f'[{element.tag}] {element.desc} — {element.file}' if element else key

    def _position_popup(self, scene_point):
        viewport_point = self.view.mapFromScene(scene_point)
        point = self.view.viewport().mapTo(self, viewport_point)
        x = min(max(8, point.x() + 14), max(8, self.width() - self.edge_popup.width() - 8))
        # above the hover point by default (smaller y = higher on screen); only falls back below
        # when there isn't room above, e.g. hovering near the view's own top edge
        y = point.y() - self.edge_popup.height() - 14
        if y < 8:
            y = min(point.y() + 14, self.height() - self.edge_popup.height() - 8)
        self.edge_popup.move(x, y)
        self.edge_popup.raise_()
        self.edge_popup.show()

    def _show_edge_popup(self, source, target, scene_point, pinned):
        source_element = self._display.get(source)
        target_element = self._display.get(target)
        if source_element is None or target_element is None:
            return
        incoming = [self._element_label(key) for key in target_element.incoming_detail]
        outgoing = [self._element_label(key) for key in source_element.outgoing_detail]
        self.edge_popup.set_zoom_scale(self.view.transform().m11())
        self.edge_popup.set_flow(
            f'{self._element_label(source)}  →  {self._element_label(target)}',
            incoming, outgoing, pinned,
        )
        self._position_popup(scene_point)

    # [FN] _show_node_popup — same explanatory popup as an edge's, but for one node's own
    # incoming/outgoing rather than a source→target flow
    # [FN OPEN] _show_node_popup
    def _show_node_popup(self, key, scene_point, pinned):
        element = self._display.get(key)
        if element is None or element.is_anchor:
            return  # a common-origin anchor has no real data of its own to explain
        incoming = [self._element_label(k) for k in (element.incoming_detail or element.incoming)]
        outgoing = [self._element_label(k) for k in (element.outgoing_detail or element.outgoing)]
        self.edge_popup.set_zoom_scale(self.view.transform().m11())
        self.edge_popup.set_flow(self._element_label(key), incoming, outgoing, pinned)
        self._position_popup(scene_point)
    # [FN CLOSED] _show_node_popup

    # [FN CATEGORY] hover popups — a short delay (_hover_show_timer) between hovering and the popup
    # actually appearing, so a mouse simply passing over an edge or node on the way elsewhere
    # doesn't pop a window open; leaving before the delay elapses just cancels the pending show.
    # Shared by both edges (source→target flow) and nodes (one element's own incoming/outgoing).
    # [FN] _on_edge_hovered / _on_node_hovered / _show_pending_hover
    # [FN OPEN] hover popups
    def _on_edge_hovered(self, source, target, scene_point, entered):
        if self._pinned_edge is not None:
            return
        if entered:
            self._edge_hide_timer.stop()
            self._pending_hover = ('edge', (source, target, scene_point))
            self._hover_show_timer.start(450)
        else:
            self._hover_show_timer.stop()
            self._pending_hover = None
            self._edge_hide_timer.start(120)

    def _on_node_hovered(self, key, scene_point, entered):
        if self._pinned_edge is not None:
            return
        if entered:
            self._edge_hide_timer.stop()
            self._pending_hover = ('node', (key, scene_point))
            self._hover_show_timer.start(450)
        else:
            self._hover_show_timer.stop()
            self._pending_hover = None
            self._edge_hide_timer.start(120)

    def _show_pending_hover(self):
        if self._pending_hover is None:
            return
        kind, args = self._pending_hover
        if kind == 'edge':
            self._show_edge_popup(*args, False)
        else:
            self._show_node_popup(*args, False)
    # [FN CLOSED] hover popups

    def _on_edge_pinned(self, source, target, scene_point):
        edge = (source, target)
        if self._pinned_edge == edge:
            self._pinned_edge = None
            self.edge_popup.hide()
            return
        self._hover_show_timer.stop()
        self._pending_hover = None
        self._edge_hide_timer.stop()
        self._pinned_edge = edge
        self._show_edge_popup(source, target, scene_point, True)

    def _hide_edge_popup(self):
        if self._pinned_edge is None:
            self.edge_popup.hide()

    def _file_roots(self):
        roots = {}
        for k, e in self._elements.items():
            if e.file not in roots or e.order < self._elements[roots[e.file]].order:
                roots[e.file] = k
        return roots

    def _file_counts(self):
        counts = {}
        for e in self._elements.values():
            counts[e.file] = counts.get(e.file, 0) + 1
        return counts

    # [FN CATEGORY] _display_elements — turns the full graph into the node/edge set actually drawn,
    # applying (1) the tag filter, (2) module collapse — every element of a collapsed file is remapped
    # onto that file's root node so cross-file references aggregate into module-to-module edges —
    # then (3) the file-focus restriction and (4) isolate-selected. Synthesises fresh XrefElement
    # nodes (never mutating the originals) and tags each file-root with `.collapsed` so the view can
    # draw the ▸/▾ affordance. `.outgoing`/`.incoming` are deduped per target module (one arrow per
    # module pair); `.outgoing_detail`/`.incoming_detail` keep every underlying real element key
    # that feeds into that arrow, so the edge popup can list all of them, not just one.
    # [FN] _display_elements — computes the filtered + collapsed node set for the view
    # [FN OPEN] _display_elements
    def _display_elements(self):
        if self._drill_key is not None:
            return self._drill_display()
        roots = self._file_roots()
        counts = self._file_counts()
        expanded = set(self._expanded)
        if self._focus_file is not None:
            expanded.add(self._focus_file)  # focusing a file always shows its contents

        def dkey(k):
            e = self._elements[k]
            return k if e.file in expanded else roots.get(e.file, k)

        base = {k: e for k, e in self._elements.items() if e.tag in self._active_tags}
        disp = {}

        def ensure(dk):
            de = self._elements.get(dk)
            if de is None or de.tag not in self._active_tags:
                return None
            if dk not in disp:
                node = XrefElement(
                    key=dk, uid=de.uid, tag=de.tag, name=de.name, desc=de.desc,
                    file=de.file, order=de.order, category_desc=de.category_desc,
                    parent=de.parent,
                )
                collapsible = dk == roots.get(de.file) and counts.get(de.file, 0) > 1
                node.collapsed = (de.file not in expanded) if collapsible else None
                # real (uncollapsed) element keys behind this node's aggregated incoming/outgoing —
                # .incoming/.outgoing are deduped per target module (one arrow per module pair), but
                # the edge popup needs every individual reference, not just "some module points here"
                node.outgoing_detail = []
                node.incoming_detail = []
                disp[dk] = node
            return disp[dk]

        for k in base:
            ensure(dkey(k))
        for k, e in base.items():
            a = dkey(k)
            if a not in disp:
                continue
            for tk in e.outgoing:
                if tk not in base:
                    continue
                b = dkey(tk)
                if b == a or b not in disp:
                    continue
                if b not in disp[a].outgoing:
                    disp[a].outgoing.append(b)
                if a not in disp[b].incoming:
                    disp[b].incoming.append(a)
                disp[a].outgoing_detail.append(tk)
                disp[b].incoming_detail.append(k)

        if self._focus_file is not None:
            keep = {k for k, e in disp.items() if e.file == self._focus_file}
            for k in list(keep):
                keep |= set(disp[k].incoming) | set(disp[k].outgoing)
            disp = {k: e for k, e in disp.items() if k in keep}
            self._prune_edges(disp)
        if self._isolate and self._pinned_nodes:
            keep = set()
            for pinned_key in self._pinned_nodes:
                if pinned_key in disp:
                    keep |= {pinned_key} | set(disp[pinned_key].incoming) | set(disp[pinned_key].outgoing)
            if keep:
                disp = {k: e for k, e in disp.items() if k in keep}
                self._prune_edges(disp)
        if self._show_containment:
            self._add_common_origin_anchors(disp)
        return disp
    # [FN CLOSED] _display_elements

    # [FN CATEGORY] _drill_display — the internal-only view for one element: ONLY its direct
    # children, with only the reference edges where BOTH ends are children (no outside callers/
    # callees, no sibling clusters, no grandchildren) — just the geography of how the children
    # relate to each other. The parent itself is deliberately excluded from this set: it's not a
    # node in this graph at all, it's rendered as a fixed title card instead (see _enter_drill_mode).
    # Node-visibility/edge-tag/isolate filters don't apply here; drilling in is its own lens.
    # [FN] _drill_display — builds the children-only node set for drill-down mode
    # [FN OPEN] _drill_display
    def _drill_display(self):
        if self._drill_key not in self._elements:
            return {}
        children = {key for key, element in self._elements.items() if element.parent == self._drill_key}
        disp = {}
        for key in children:
            e = self._elements[key]
            node = XrefElement(
                key=e.key, uid=e.uid, tag=e.tag, name=e.name, desc=e.desc,
                file=e.file, order=e.order, category_desc=e.category_desc,
                parent=None,  # the real parent is excluded from this graph entirely
            )
            node.outgoing_detail = []
            node.incoming_detail = []
            disp[key] = node
        for key in children:
            for target in self._elements[key].outgoing:
                if target in children and target != key:
                    if target not in disp[key].outgoing:
                        disp[key].outgoing.append(target)
                    if key not in disp[target].incoming:
                        disp[target].incoming.append(key)
                    disp[key].outgoing_detail.append(target)
                    disp[target].incoming_detail.append(key)
        return disp
    # [FN CLOSED] _drill_display

    # [FN CATEGORY] _enter_drill_mode / _exit_drill_mode — switches the map between the full
    # project graph and one element's internal-only view. The drilled element is detached from
    # the graph entirely: it becomes a fixed title card (drill_title_card) pinned to the
    # viewport's top-right corner at a constant on-screen size, independent of zoom/pan — a
    # widget overlay, not a scene item, since scene items scale with the view's transform.
    # [FN] _enter_drill_mode / _exit_drill_mode
    # [FN OPEN] drill mode
    def _enter_drill_mode(self, key):
        if key not in self._elements:
            return
        self._drill_key = key
        self.view.set_pinned([])
        element = self._elements[key]
        self.drill_title_tag.setText(f'[{element.tag}]')
        self.drill_title_name.setText(element.desc or element.name)
        self.drill_title_card.show()
        self._position_drill_title_card()
        self._refresh(relayout=True, fit=True)
        self.drill_back_btn.show()

    def _exit_drill_mode(self):
        self._drill_key = None
        self.drill_back_btn.hide()
        self.drill_title_card.hide()
        self._refresh(fit=True, relayout=True)
    # [FN CLOSED] drill mode

    def _position_drill_title_card(self):
        if not self.drill_title_card.isVisible():
            return
        self.drill_title_card.adjustSize()
        view_geo = self.view.geometry()
        x = view_geo.right() - self.drill_title_card.width() - 20
        y = view_geo.top() + 20
        self.drill_title_card.move(x, y)
        self.drill_title_card.raise_()

    # [FN CATEGORY] _add_common_origin_anchors — a filter/collapse can remove an element's own
    # parent from the displayed set while leaving several of its siblings visible; without this
    # they'd show no trace of coming from the same place. Synthesizes one small unmarked "common
    # origin" anchor node per orphaned parent (only when 2+ siblings survive together — a single
    # orphan has no sibling to visually connect with) and re-parents those children onto it, so the
    # existing containment-edge drawing in XrefMapView.set_data — and the secondary origin-clustering
    # force in _force_layout_positions — both apply to it exactly like a real parent, no new code
    # paths needed there.
    # [FN] _add_common_origin_anchors — synthesizes anchor nodes for orphaned sibling groups
    # [FN OPEN] _add_common_origin_anchors
    def _add_common_origin_anchors(self, disp):
        orphans = {}
        for key, element in disp.items():
            if element.parent and element.parent not in disp:
                orphans.setdefault(element.parent, []).append(key)
        for parent_key, children in orphans.items():
            if len(children) < 2:
                continue
            anchor_key = f'__anchor__::{parent_key}'
            anchor = XrefElement(
                key=anchor_key, uid=anchor_key, tag='', name='', desc='',
                file=disp[children[0]].file, order=-1,
            )
            anchor.is_anchor = True
            disp[anchor_key] = anchor
            for child_key in children:
                disp[child_key].parent = anchor_key
    # [FN CLOSED] _add_common_origin_anchors

    @staticmethod
    def _prune_edges(disp):
        for e in disp.values():
            e.outgoing = [k for k in e.outgoing if k in disp]
            e.incoming = [k for k in e.incoming if k in disp]

    # [FN CATEGORY] _refresh — rebuilds self._display from current filters, then hands it to the
    # view. Plain refresh (fit=False, relayout=False) keeps every node exactly where it already is —
    # used for things that don't change which nodes are visible (selection, drag persistence).
    # relayout=True recomputes positions for the new element set instead, warm-started from where
    # nodes already are, and animates the transition — used whenever the filtered/collapsed set
    # itself changes (tag toggle, file focus, isolate, expand/collapse, search), so removing or
    # adding nodes lets the layout actually resettle instead of leaving survivors pinned to spots
    # chosen for a different set of neighbours.
    # [FN] _refresh — recomputes the displayed graph and (optionally) relays it out
    # [FN OPEN] _refresh
    def _refresh(self, fit=False, relayout=False):
        old_transform = self.view.transform()
        old_center = self.view.mapToScene(self.view.viewport().rect().center())
        self._display = self._display_elements()
        if relayout and self.view._node_items:
            self._positions.update(self.view.relayout_to(self._display, self._positions))
        else:
            self.view.set_data(self._display, self._positions)
            self._positions.update(self.view.positions())
        self._position_timer.start(250)
        visible_edges = {(source, target) for source, target, *_rest in self.view._edges}
        if self._pinned_edge not in visible_edges:
            self._pinned_edge = None
            self.edge_popup.hide()
        self.view.set_pinned([k for k in self._pinned_nodes if k in self._display])
        modules = sum(1 for e in self._display.values() if e.collapsed is not None)
        self.count_label.setText(
            f'{len(self._display)} nodi · {len(self.view._edges)} collegamenti'
            + (f' · {modules} moduli comprimibili' if modules else '')
        )
        if fit:
            QTimer.singleShot(0, self.view.fit)
        else:
            self.view.setTransform(old_transform)
            self.view.centerOn(old_center)

    def _on_tag_toggle(self, tag, checked):
        if checked:
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        self._refresh(relayout=True)

    def _on_edge_tag_toggle(self, tag, checked):
        if checked:
            self._active_edge_tags.add(tag)
        else:
            self._active_edge_tags.discard(tag)
        self.view.set_active_edge_tags(self._active_edge_tags)
        # node size/heatmap intensity and the layout itself are also driven by (filtered) degree
        # now, not just which edges get drawn — so this needs a real relayout, not just a redraw
        self._refresh(relayout=True)

    def _on_containment_toggle(self, checked):
        self._show_containment = checked
        self.view.set_show_containment(checked)
        self._refresh(relayout=True)

    def _on_direction_toggle(self, checked):
        self._rtl = checked
        self.direction_btn.setText('Direzione: Dx → Sx' if checked else 'Direzione: Sx → Dx')
        self.view.set_direction(checked)
        self._refresh(relayout=True)

    def _on_file_filter(self, _index):
        self._focus_file = self.file_combo.currentData()
        self._refresh(relayout=True)

    def _on_isolate(self, checked):
        self._isolate = checked
        self._refresh(relayout=True)
        if checked and self._pinned_nodes:
            # multiple nodes may be pinned; fit the whole isolated neighbourhood rather than
            # re-centering (and re-pinning) on just one of them
            self.view.fit()

    def _on_heatmap_toggle(self, checked):
        self.view.recolor(checked)

    def _expand_all(self):
        self._expanded = {e.file for e in self._elements.values()}
        self._refresh(relayout=True)

    def _collapse_all(self):
        self._expanded = set()
        self._refresh(relayout=True)

    # [FN] _on_nodes_pinned — mirrors the view's multi-pin set at the dialog level
    # [FN OPEN] _on_nodes_pinned
    def _on_nodes_pinned(self, keys):
        self._pinned_nodes = list(keys)
        self._selected = keys[-1] if keys else None
        drillable = keys[0] if len(keys) == 1 and self._is_drillable(keys[0]) else None
        self.view.set_drillable(drillable)
        if self._isolate:
            self._refresh(relayout=True)
    # [FN CLOSED] _on_nodes_pinned

    # [FN] _is_drillable — a real KANT tree parent has ≥2 direct children AND at least one child
    # references another child directly; only then is drilling into it more informative than the
    # main map already is. Uses the full undisplayed graph, not the filtered/collapsed view.
    # [FN OPEN] _is_drillable
    def _is_drillable(self, key):
        children = [k for k, e in self._elements.items() if e.parent == key]
        if len(children) < 2:
            return False
        child_set = set(children)
        return any(target in child_set for child in children for target in self._elements[child].outgoing)
    # [FN CLOSED] _is_drillable

    def selected_key(self):
        """Most-recently-pinned element key, or None — read when the map closes so the coding
        panel underneath can open whatever was last pinned."""
        return self._selected

    # [FN CATEGORY] _on_node_activated — double-click routing: on a collapsible module root it toggles
    # that file's expansion; on any other node it re-emits nodeActivated so the editor jumps to the
    # element. (Single click always just pins/highlights, via _on_nodes_pinned.)
    # [FN] _on_node_activated — expands a module or opens an element on double-click
    # [FN OPEN] _on_node_activated
    def _on_node_activated(self, key):
        node = self._display.get(key)
        if node is not None and node.collapsed is not None:
            self._expanded.symmetric_difference_update({node.file})
            self._refresh(relayout=True)
        else:
            self.nodeActivated.emit(key)
    # [FN CLOSED] _on_node_activated

    # [FN CATEGORY] _on_search — locates the first element whose name or short description matches;
    # if it sits inside a collapsed module the module is expanded first so the node exists to focus on
    # [FN] _on_search — expands if needed and focuses the first matching element
    # [FN OPEN] _on_search
    def _on_search(self, text):
        text = text.strip().lower()
        if not text:
            return
        for key, el in self._elements.items():
            if text in el.name.lower() or text in el.desc.lower():
                if el.file not in self._expanded and self._focus_file != el.file:
                    self._expanded.add(el.file)
                    self._refresh(relayout=True)
                self._selected = key
                self._pinned_nodes = [key]
                self.view.focus_on(key)
                return
    # [FN CLOSED] _on_search

    def _on_search_enter(self):
        self._on_search(self.search_box.text())
# [FN CLOSED] XrefMapDialog
