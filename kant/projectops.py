"""Synchronous project scans with no Qt widgets or mutable application state.

Search, replace discovery, KANT-map generation, validation, and local symbol lookup live here.
Callers own threading, confirmation, display, and writes; ``MainWindow`` runs expensive scans in
the background and ``WorkspaceMixin`` owns mutations.
"""
import csv
import io
import os
import re
from pathlib import Path

from kant import theme
from kant.fileio import file_fingerprint
from kant.model import Node, read_top_level_label, read_top_level_label_result
from kant.syntax import audit_kant_headers
from kant.xref import build_xref

_SEARCH_SIZE_LIMIT = object()


def iter_project_text_files(root, max_bytes=_SEARCH_SIZE_LIMIT):
    if max_bytes is _SEARCH_SIZE_LIMIT:
        max_bytes = theme.SEARCH_MAX_BYTES
    if not root:
        return
    for current, subdirs, files in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in theme.IGNORE_DIRS)
        for name in sorted(files):
            path = os.path.join(current, name)
            try:
                if max_bytes is not None and os.path.getsize(path) > max_bytes:
                    continue
                data = Path(path).read_bytes()
                if b'\0' not in data:
                    yield path, data.decode('utf-8')
            except (OSError, UnicodeDecodeError):
                continue


def search_project(root, needle, limit=200):
    matches = []
    for path, text in iter_project_text_files(root):
        rel = os.path.relpath(path, root)
        for lineno, line in enumerate(text.splitlines(), 1):
            if needle in line:
                matches.append((path, rel, lineno, line.strip()))
                if len(matches) >= limit:
                    return matches
    return matches


def scan_project_replace(root, needle, replacement):
    changes = []
    for path, text in iter_project_text_files(root):
        count = text.count(needle)
        if count:
            changes.append((path, text.replace(needle, replacement), count, file_fingerprint(path)))
    return changes


def iter_kant_tagged_files(root):
    for current, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in theme.IGNORE_DIRS]
        for name in files:
            yield os.path.join(current, name)


def has_any_kant_tags(root):
    return any(read_top_level_label(path) is not None for path in iter_kant_tagged_files(root))


def _map_line(depth, tag, name, desc):
    prefix = '-' * depth + (' ' if depth else '')
    label = f'[{tag} {name}]' if depth == 0 else f'[{tag}] {name}'
    return f'{prefix}{label} — {desc or name}'


def _append_map_children(lines, node, depth):
    for child in node.body:
        if isinstance(child, Node):
            lines.append(_map_line(depth, child.tag, child.name, child.desc))
            _append_map_children(lines, child, depth + 1)


def build_kant_map(root, project_name):
    entries = []
    for path in iter_kant_tagged_files(root):
        label = read_top_level_label(path)
        if label is not None:
            tag, desc, _tree, top_node = label
            entries.append((os.path.relpath(path, root), tag, desc, top_node))
    entries.sort(key=lambda entry: entry[0])
    lines = [f'# KANT Code Map - {project_name}', '', '## Struttura', '', '```']
    for rel, tag, desc, top_node in entries:
        lines.append(_map_line(0, tag, rel.replace(os.sep, '/'), desc))
        _append_map_children(lines, top_node, 1)
    return '\n'.join([*lines, '```', ''])


# [FN CATEGORY] build_kant_flow_csv — same file-discovery pass as build_kant_map (every KANT-
# tagged file's already-parsed tree, keyed by project-relative path), fed into build_xref for the
# incoming/outgoing graph. A plain flat CSV, not a real .xlsx — this project has no dependency
# beyond PySide6/pytest and stdlib csv already opens fine as a spreadsheet in Excel/Sheets without
# adding one. One row per KANT element; Incoming/Outgoing cells list "file::name" for each
# referencing/referenced element, semicolon-separated, resolved through the same graph rather than
# left as raw '<rel_path>::<uid>' keys.
# [FN] build_kant_flow_csv — CSV of every element's incoming/outgoing cross-references
# [FN OPEN] build_kant_flow_csv
def build_kant_flow_csv(root):
    trees = {}
    for path in iter_kant_tagged_files(root):
        label = read_top_level_label(path)
        if label is not None:
            _tag, _desc, tree, _top_node = label
            trees[os.path.relpath(path, root).replace(os.sep, '/')] = tree
    graph = build_xref(trees)

    def label_for(key):
        el = graph.get(key)
        return f'{el.file}::{el.name}' if el is not None else key

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['File', 'Tag', 'Nome', 'Descrizione', 'Incoming', 'Outgoing'])
    for key in sorted(graph, key=lambda k: (graph[k].file, graph[k].order)):
        el = graph[key]
        incoming = '; '.join(sorted(label_for(k) for k in el.incoming))
        outgoing = '; '.join(sorted(label_for(k) for k in el.outgoing))
        writer.writerow([el.file, el.tag, el.name, el.desc, incoming, outgoing])
    return buf.getvalue()
# [FN CLOSED] build_kant_flow_csv


def _canonical_map_text(text):
    # equivalent only across line-ending and trailing-newline differences — used both to decide
    # whether the map is in sync (validate_kant_project) and whether _sync_kant_map needs to write
    return text.replace('\r\n', '\n').rstrip('\n')


def _collect_uids(tree, rel, into):
    for item in tree.body:
        if isinstance(item, Node):
            if item.uid is not None:
                into.setdefault(item.uid, []).append((rel, item.tag, item.name))
            _collect_uids(item, rel, into)


# [FN CATEGORY] validate_kant_project — single project-wide scan: per-file marker validation (hard
# errors and soft warnings, both from audit_kant_headers) plus cross-file duplicate #id detection,
# feeding one of five mutually exclusive map states. Structural
# marker errors take precedence over the map comparison — a broken source file means the canonical
# map can't be trusted to represent it, so map sync is never even evaluated in that case.
# [FN] validate_kant_project — full marker + canonical map-sync validation for a project
# [FN OPEN] validate_kant_project
def validate_kant_project(root, map_path):
    errors, visual_errors, warnings, tagged = [], [], [], []
    checked_markers = 0
    map_text = None
    map_read_error = None
    if map_path is not None:
        try:
            map_text = Path(map_path).read_text(encoding='utf-8')
        except OSError as error:
            map_read_error = str(error)

    # the map's absolute path, so it can be skipped below — build_kant_map's own output uses
    # "[TAG Name]"/"[TAG] Name" summary lines (_map_line) that happen to match TAGLINE_RE's shape
    # (a bracketed tag + name) with no real OPEN/CLOSED markers anywhere in the file, since it's
    # generated documentation, not source. Scanning it here as if it were taggable source produced
    # a wall of bogus "intestazione [TAG] pendente" errors — one per summary line — on every run.
    map_abspath = os.path.abspath(map_path) if map_path else None

    uid_locations = {}
    for path in iter_kant_tagged_files(root):
        if map_abspath is not None and os.path.abspath(path) == map_abspath:
            continue
        rel = os.path.relpath(path, root).replace(os.sep, '/')
        try:
            text = Path(path).read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        except OSError as error:
            errors.append(f'{rel}: non leggibile: {error}')
            continue
        checked_markers += 1
        # audit_kant_headers's hard-error set is a strict superset of check_kant_markers's (parse
        # errors + duplicate #id, plus tag/name coherence and orphaned headers) — calling both here
        # reported every parse/duplicate-id error twice; check_kant_markers stays the live/cheap
        # per-keystroke check elsewhere, this full-project scan only needs the richer one.
        audit = audit_kant_headers(text)
        for entry in audit['errors']:
            errors.append(f"{rel}:{entry['line']} {entry['message']}")
            visual_errors.append((path, rel, entry['line'], entry['message']))
        for entry in audit['warnings']:
            warnings.append(f"{rel}:{entry['line']} {entry['message']}")
        label, parse_error = read_top_level_label_result(path)
        if label is not None:
            tag, _desc, tree, _top = label
            tagged.append((rel, tag))
            _collect_uids(tree, rel, uid_locations)

    for uid, locations in uid_locations.items():
        files = sorted({rel for rel, _tag, _name in locations})
        if len(files) > 1:
            sample = ', '.join(files[:5])
            extra = f' (+{len(files) - 5})' if len(files) > 5 else ''
            warnings.append(f'UID duplicato #{uid} in {sample}{extra}')

    if map_path is None:
        map_state = 'assente'
        errors.append('manca KANT_*.md nella radice del progetto')
    elif map_read_error is not None:
        map_state = 'errore_generazione'
        errors.append(f'{os.path.basename(map_path)} non leggibile: {map_read_error}')
    elif errors:
        map_state = 'marker_invalidi'
    else:
        try:
            canonical = build_kant_map(root, os.path.basename(root))
        except OSError as error:
            map_state = 'errore_generazione'
            errors.append(f'generazione mappa fallita: {error}')
        else:
            map_state = 'sincronizzata' if _canonical_map_text(canonical) == _canonical_map_text(map_text) else 'non_sincronizzata'
            if map_state == 'non_sincronizzata':
                errors.append('KANT map non coerente con il sorgente')

    map_name = os.path.basename(map_path) if map_path else 'KANT_*.md'
    if errors:
        sample = '\n'.join(f'- {error}' for error in errors[:8])
        extra = f'\n- ... altri {len(errors) - 8} errori' if len(errors) > 8 else ''
        summary = f'# KANT verifica: ERRORI\n{sample}{extra}'
    else:
        summary = f'# KANT verifica: OK ({map_name}, {checked_markers} file con marker)'
    return summary, errors, visual_errors, map_state, warnings
# [FN CLOSED] validate_kant_project


def definition_locations(root, symbol, limit=200):
    escaped = re.escape(symbol)
    patterns = [
        re.compile(rf'\b(?:async\s+def|def|class)\s+{escaped}\b'),
        re.compile(rf'\bfunction\s+{escaped}\b'),
        re.compile(rf'\b(?:const|let|var|type|interface|enum|struct|fn)\s+{escaped}\b'),
        re.compile(rf'^\s*{escaped}\s*[:=]'),
    ]
    matches = []
    for path, text in iter_project_text_files(root):
        rel = os.path.relpath(path, root)
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in patterns):
                matches.append((path, rel, lineno, line.strip()))
                if len(matches) >= limit:
                    return matches
    return matches


def reference_locations(root, symbol, limit=200):
    pattern = re.compile(rf'\b{re.escape(symbol)}\b')
    matches = []
    for path, text in iter_project_text_files(root):
        rel = os.path.relpath(path, root)
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                matches.append((path, rel, lineno, line.strip()))
                if len(matches) >= limit:
                    return matches
    return matches
