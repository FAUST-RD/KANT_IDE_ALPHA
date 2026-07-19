"""Deterministic KANT marker skeleton generation.

Given source that is unmarked (or only partially marked), finds every still-unmarked top-level
construct — module-level class/function/constant/mutable-global/test — and reports its tag, name,
and exact line span. ``insert_skeleton`` then writes OPEN/CATEGORY/tagline/CLOSED markers for those
spans, with the CATEGORY/tagline description left as an explicit blank (``Name —``, nothing after
the dash) for a human or an AI to fill in afterward.

Tag, name, nesting, span, and #id are never left to an AI to decide — they're facts about the code,
extracted deterministically. Only the description is left blank. Python gets exact results via the
stdlib ``ast`` module; every other language falls back to a brace/string-aware heuristic scanner
(the same rigor ``kant/projectops.py``'s ``definition_locations`` already uses for symbol lookup,
just applied to whole-file enumeration instead of one known name). ``EXTERNAL_SCANNERS`` is the
extension point for a real per-language toolchain integration (Go's own ``go/ast``, a project's own
``typescript`` compiler, Roslyn via ``dotnet``, ...) to slot in later without redesigning any of
this — none are wired in yet, since none could be verified end-to-end in this environment.
"""
import ast
import os
import re
from dataclasses import dataclass, field

from kant.model import ELEMENT_LANGUAGES, Node, parse_kant, KantParseError
from kant.fileio import write_file_atomic


# extension -> ELEMENT_LANGUAGES display name, extended with a few real-world extensions
# ELEMENT_LANGUAGES itself doesn't carry (.jsx/.tsx/.mjs/...)
LANGUAGE_BY_EXT = {info['ext']: name for name, info in ELEMENT_LANGUAGES.items()}
LANGUAGE_BY_EXT.update({
    '.pyw': 'Python',
    '.jsx': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
    '.tsx': 'TypeScript', '.mts': 'TypeScript', '.cts': 'TypeScript',
    '.hpp': 'C++', '.hh': 'C++', '.cc': 'C++', '.cxx': 'C++', '.h': 'C++',
})

# languages the brace-heuristic regex scanner below understands — real Algol-family declaration
# syntax with { }-delimited bodies. SQL/HTML/Generico have no equivalent notion of a top-level
# function/class body here, so they're left out rather than guessed at badly.
_BRACE_LANGUAGES = {'JavaScript', 'TypeScript', 'Go', 'Java', 'C++', 'C#', 'Rust'}


@dataclass
class SkeletonElement:
    tag: str
    name: str
    start_line: int   # 1-based, inclusive — the declaration's own first line (decorators included)
    end_line: int      # 1-based, inclusive — the construct's last line
    depth: int = 0


def language_for_path(path):
    return LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower())


# [FN CATEGORY] scan_python — exact, via the stdlib ast module: no guessing, Python's own grammar
# decides tag/name/span. CST vs VAR is decided by whether a module-level name is ever rebound
# (a second top-level assignment, or a `global NAME` anywhere in the file) — the same distinction
# this project's own theme.py already draws by hand (TAG_COLORS mutated in place stays CST; BG/
# PANEL/... rebound via `global` in set_theme are VAR), just made explicit and automatic. Nested
# defs/classes inside a function body are deliberately not tagged — KANT elements are module/class-
# level constructs, not every local closure.
# [FN] scan_python — exact tag/name/span extraction for Python source via ast
# [FN OPEN] scan_python
def scan_python(text, file_path=''):
    try:
        module = ast.parse(text)
    except SyntaxError:
        return []

    reassigned = set()
    seen_once = set()
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _assign_target_names(node):
                (reassigned if name in seen_once else seen_once).add(name)
    for node in ast.walk(module):
        if isinstance(node, ast.Global):
            reassigned.update(node.names)

    base = os.path.basename(file_path or '')
    is_test_file = base.startswith('test_') or base.endswith('_test.py')

    def end_line_of(node):
        return getattr(node, 'end_lineno', None) or node.lineno

    elements = []

    def walk(body, depth):
        for node in body:
            if isinstance(node, ast.ClassDef):
                tag = 'TYP' if any(_base_name(b) in ('Protocol', 'TypedDict', 'NamedTuple') for b in node.bases) else 'CLS'
                elements.append(SkeletonElement(tag, node.name, node.lineno, end_line_of(node), depth))
                walk(node.body, depth + 1)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tag = 'TST' if (is_test_file or node.name.startswith('test_')) else 'FN'
                start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                elements.append(SkeletonElement(tag, node.name, start, end_line_of(node), depth))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and depth == 0:
                for name in _assign_target_names(node):
                    tag = 'VAR' if name in reassigned else 'CST'
                    elements.append(SkeletonElement(tag, name, node.lineno, end_line_of(node), depth))

    walk(module.body, 0)
    return elements
# [FN CLOSED] scan_python


def _assign_target_names(node):
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = []
    for target in targets:
        names.extend(_names_in(target))
    return names


def _names_in(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for elt in target.elts:
            names.extend(_names_in(elt))
        return names
    return []


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ''


# same declaration-keyword vocabulary kant/projectops.py:definition_locations and
# kant/syntax.py:audit_kant_headers already use for this language family — reused rather than a
# third slightly-different pattern set
_DECL_RE = re.compile(
    r'^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|final\s+|async\s+)*'
    r'(class|struct|interface|enum)\s+(\w+)'
    r'|^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|final\s+|async\s+)*'
    r'function\s+(\w+)'
    r'|^\s*func\s+(?:\([^)]*\)\s*)?(\w+)'
    r'|^\s*fn\s+(\w+)'
)
_TEST_NAME_RE = re.compile(r'^test_?|_test$|^Test', re.IGNORECASE)


# [FN CATEGORY] scan_regex — the fallback scanner for every non-Python brace language: finds
# top-level declarations by keyword (same vocabulary already shared by definition_locations/
# audit_kant_headers), then walks braces from the declaration line to find where the body closes,
# skipping over string/char literals and line comments so a stray brace inside one doesn't end the
# scan early. Deliberately top-level only — reliably matching a method to its containing class by
# regex/indentation alone is not something this project's "no full parser" constraint can promise,
# so nested members are left for the "+ Aggiungi elemento" dialog or a human/AI pass instead of
# being guessed at. Same rigor as this project's existing definition_locations, just enumerating
# every declaration instead of searching for one known name.
# [FN] scan_regex — best-effort top-level tag/name/span extraction for non-Python brace languages
# [FN OPEN] scan_regex
def scan_regex(text, language, file_path=''):
    if language not in _BRACE_LANGUAGES:
        return []
    lines = text.split('\n')
    base = os.path.basename(file_path or '')
    elements = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and (line[0] in ' \t'):
            i += 1
            continue  # top-level only: a declaration at column 0
        m = _DECL_RE.match(line)
        if not m:
            i += 1
            continue
        kind, name = (m.group(1), m.group(2)) if m.group(1) else \
            ('function', m.group(3) or m.group(4) or m.group(5))
        if not name:
            i += 1
            continue
        tag = {'class': 'CLS', 'struct': 'CLS', 'interface': 'TYP', 'enum': 'TYP'}.get(kind, 'FN')
        if tag == 'FN' and (base.startswith('test') or _TEST_NAME_RE.search(name)):
            tag = 'TST'
        end = _brace_span_end(lines, i)
        if end is None:
            i += 1
            continue
        elements.append(SkeletonElement(tag, name, i + 1, end + 1, 0))
        i = end + 1
    return elements
# [FN CLOSED] scan_regex


def _brace_span_end(lines, start_index):
    depth = 0
    opened = False
    in_string = None  # '"', "'", '`', or None
    for idx in range(start_index, len(lines)):
        line = lines[idx]
        j = 0
        while j < len(line):
            ch = line[j]
            if in_string:
                if ch == '\\':
                    j += 2
                    continue
                if ch == in_string:
                    in_string = None
                j += 1
                continue
            if ch in ('"', "'", '`'):
                in_string = ch
            elif ch == '/' and j + 1 < len(line) and line[j + 1] == '/':
                break  # rest of line is a comment
            elif ch == '{':
                depth += 1
                opened = True
            elif ch == '}':
                depth -= 1
                if opened and depth == 0:
                    return idx
            j += 1
        if opened and depth == 0:
            return idx
    return None  # never closed — don't guess a span that might be wrong


# [FN] scan_source — dispatches to the exact Python scanner or the regex fallback by extension
# [FN OPEN] scan_source
def scan_source(text, file_path):
    language = language_for_path(file_path)
    if language == 'Python':
        return scan_python(text, file_path), 'ast'
    if language in _BRACE_LANGUAGES:
        return scan_regex(text, language, file_path), 'regex'
    return [], 'unsupported'
# [FN CLOSED] scan_source


# [FN CATEGORY] unmarked_elements — cross-references freshly scanned elements against the file's
# EXISTING KANT tree so a construct that already has a marker (any tag, even one this scanner
# wouldn't itself have chosen) is left untouched rather than double-wrapped. Refuses to propose
# anything if the file's current markers don't even parse — inserting more structure on top of
# already-broken markers would only compound the problem; that file needs "Verifica KANT" first.
# [FN] unmarked_elements — elements not already covered by an existing OPEN...CLOSED span
# [FN OPEN] unmarked_elements
def unmarked_elements(text, elements):
    if not elements:
        return []
    try:
        tree = parse_kant(text)
    except KantParseError:
        return None
    covered = []

    def collect(node):
        for item in node.body:
            if isinstance(item, Node):
                if item.open_line is not None and item.closed_line is not None:
                    covered.append((item.open_line, item.closed_line))
                collect(item)

    collect(tree)
    return [
        e for e in elements
        if not any(open_l <= e.start_line and e.end_line <= closed_l for open_l, closed_l in covered)
    ]
# [FN CLOSED] unmarked_elements


# [FN CATEGORY] insert_skeleton — writes OPEN/CATEGORY/tagline/CLOSED markers for each given
# element, with the description left as an explicit blank ("Name —", nothing after the dash) —
# that exact shape is what audit_kant_headers's "tagline vuota"/"CATEGORY vuota" checks already
# recognize as needing content, so nothing new has to re-detect these blanks later; the same audit
# already used for full-project validation lists them. Two elements can need markers spliced at the
# very same original line with no blank line to separate them — a class ending on the same line as
# its last method, or two adjacent module-level constants with no gap between them — so insertions
# at a shared index need a real order, not just "whatever order they were appended in": a CLOSED
# always sorts before an OPEN-block at the same index (finish what's ending before starting what's
# next), and among ties in the same direction, CLOSED goes deepest-first (innermost closes before
# its container) while OPEN-block goes shallowest-first (a container's own header before whatever
# starts right after it) — plain LIFO nesting, expressed as a sort key instead of a real stack.
# [FN] insert_skeleton — writes marker skeletons for the given elements into source text
# [FN OPEN] insert_skeleton
def insert_skeleton(text, elements, language):
    if not elements:
        return text, 0
    leader = ELEMENT_LANGUAGES.get(language, ELEMENT_LANGUAGES['Generico'])
    prefix, suffix = leader['comment'], leader['suffix']
    tail = f' {suffix}' if suffix else ''

    def marker(bracket_text):
        return f'{prefix} {bracket_text}{tail}'

    lines = text.split('\n')
    # (index, category, tie, block) — category 0 (close) sorts before 1 (open) at a shared index
    entries = []
    for elem in elements:
        header = f'{elem.name} —'
        entries.append((elem.start_line - 1, 1, elem.depth, [
            marker(f'[{elem.tag} CATEGORY] {header}'),
            marker(f'[{elem.tag}] {header}'),
            marker(f'[{elem.tag} OPEN] {elem.name}'),
        ]))
        entries.append((elem.end_line, 0, -elem.depth, [marker(f'[{elem.tag} CLOSED] {elem.name}')]))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    inserts = {}
    for index, _category, _tie, block in entries:
        inserts.setdefault(index, []).extend(block)

    for index in sorted(inserts, reverse=True):
        lines[index:index] = inserts[index]

    return '\n'.join(lines), len(elements)
# [FN CLOSED] insert_skeleton


# [FN CATEGORY] apply_skeleton — the single entry point the IDE and the AI-facing tooling both use:
# scan, filter out what's already marked, insert skeletons for the rest. Returns None (no change)
# rather than raising when the file's language isn't recognized or its existing markers don't
# parse — callers decide how to surface that, this stays a pure best-effort transform.
# [FN] apply_skeleton — scans a file's text and returns (new_text, inserted_count) or None
# [FN OPEN] apply_skeleton
def apply_skeleton(text, file_path):
    language = language_for_path(file_path)
    if language is None:
        return None
    elements, _method = scan_source(text, file_path)
    unmarked = unmarked_elements(text, elements)
    if not unmarked:
        return None
    new_text, count = insert_skeleton(text, unmarked, language)
    return new_text, count
# [FN CLOSED] apply_skeleton


# [FN CATEGORY] apply_skeleton_to_project — the whole-project counterpart apply_skeleton's caller
# (kant/mainwindow.py's Aggiorna KANT AI button) uses for one open file — /kant-code-map's own
# deterministic first phase runs this across every recognized source file before the AI touches
# anything, so tag/name/nesting/span/#id are never something it has to get right by reading code;
# it only ever fills in the blanks this leaves. Skips any file whose existing markers don't parse
# rather than risk compounding an already-broken one — that file surfaces in the returned skip list
# instead, for a human/Verifica KANT to look at.
# [FN] apply_skeleton_to_project — runs apply_skeleton over every real source file in a project
# [FN OPEN] apply_skeleton_to_project
def apply_skeleton_to_project(root):
    from kant.projectops import iter_project_text_files

    changed, skipped = [], []
    for path, text in iter_project_text_files(root):
        language = language_for_path(path)
        if language is None:
            continue
        elements, _method = scan_source(text, path)
        unmarked = unmarked_elements(text, elements)
        if unmarked is None:
            skipped.append(os.path.relpath(path, root))
            continue
        if not unmarked:
            continue
        new_text, count = insert_skeleton(text, unmarked, language)
        try:
            write_file_atomic(path, new_text)
        except OSError:
            skipped.append(os.path.relpath(path, root))
            continue
        changed.append((os.path.relpath(path, root), count))
    return changed, skipped
# [FN CLOSED] apply_skeleton_to_project
