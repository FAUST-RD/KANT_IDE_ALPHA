"""Synchronous project scans with no Qt widgets or mutable application state.

Search, replace discovery, KANT-map generation, validation, and local symbol lookup live here.
Callers own threading, confirmation, display, and writes; ``MainWindow`` runs expensive scans in
the background and ``WorkspaceMixin`` owns mutations.
"""
import os
import re
from pathlib import Path

from kant import theme
from kant.fileio import file_fingerprint
from kant.model import Node, read_top_level_label
from kant.syntax import check_kant_markers


def iter_project_text_files(root):
    if not root:
        return
    for current, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in theme.IGNORE_DIRS]
        for name in files:
            path = os.path.join(current, name)
            try:
                if os.path.getsize(path) > theme.SEARCH_MAX_BYTES:
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


def validate_kant_project(root, map_path):
    errors, visual_errors, tagged = [], [], []
    checked_markers = 0
    map_text = ''
    if map_path is None:
        errors.append('manca KANT_*.md nella radice del progetto')
    else:
        try:
            map_text = Path(map_path).read_text(encoding='utf-8')
        except OSError as error:
            errors.append(f'{os.path.basename(map_path)} non leggibile: {error}')
    for path in iter_kant_tagged_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, '/')
        try:
            text = Path(path).read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        except OSError as error:
            errors.append(f'{rel}: non leggibile: {error}')
            continue
        checked_markers += 1
        result = check_kant_markers(text)
        if not result['ok']:
            line = result.get('line', 1)
            message = result.get('message', 'marker KANT non valido')
            errors.append(f'{rel}:{line} {message}')
            visual_errors.append((path, rel, line, message))
        label = read_top_level_label(path)
        if label is not None:
            tagged.append((rel, label[0]))
    map_out_of_sync = False
    if map_text:
        missing = [rel for rel, tag in tagged if f'[{tag} {rel}]' not in map_text]
        if missing:
            map_out_of_sync = True
            sample = ', '.join(missing[:5])
            extra = f' (+{len(missing) - 5})' if len(missing) > 5 else ''
            errors.append(f'KANT map non coerente: mancano {sample}{extra}')
    map_name = os.path.basename(map_path) if map_path else 'KANT_*.md'
    if errors:
        sample = '\n'.join(f'- {error}' for error in errors[:8])
        extra = f'\n- ... altri {len(errors) - 8} errori' if len(errors) > 8 else ''
        summary = f'# KANT verifica: ERRORI\n{sample}{extra}'
    else:
        summary = f'# KANT verifica: OK ({map_name}, {checked_markers} file con marker)'
    return summary, errors, visual_errors, map_out_of_sync


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
