"""Deterministic KANT marker skeleton generation.

Given source that is unmarked (or only partially marked), finds every still-unmarked top-level
construct — module-level class/function/constant/mutable-global/test — and reports its tag, name,
and exact line span. ``insert_skeleton`` then writes OPEN/CATEGORY/tagline/CLOSED markers for those
spans, with the CATEGORY/tagline description left as an explicit blank (``Name —``, nothing after
the dash) for a human or an AI to fill in afterward. ``apply_skeleton``/``apply_skeleton_to_project``
also add the file's own MOD (or TST, for a standalone test file) wrapper whenever one doesn't
already exist — the individual-construct scanners only ever find things INSIDE a file, never the
file as a KANT element in its own right, so without this every tagged file would end up with its
top-level constructs as loose, unwrapped siblings instead of children of the file's own element.

Tag, name, nesting, span, and #id are never left to an AI to decide — they're facts about the code,
extracted deterministically. Only the description is left blank. Python gets exact results via the
stdlib ``ast`` module. Go, TypeScript/JavaScript, C#, Java, and C++ each first try a real toolchain
scanner (``kant/toolchains.py`` — the target language's own compiler: ``go/ast``, an installed
``typescript`` package, Roslyn via the .NET SDK's own bundled assemblies, the JDK's Compiler Tree
API, ``clang -ast-dump=json``), falling back to a brace/string-aware heuristic scanner below (the
same rigor ``kant/projectops.py``'s ``definition_locations`` already uses for symbol lookup, just
applied to whole-file enumeration instead of one known name) whenever the toolchain isn't
installed or its output doesn't parse as expected. C#/TypeScript were verified end-to-end against
a real local install; Go/Java/C++ were written with the same care but their toolchains weren't
available to run in the environment this was built in — see kant/toolchains.py's own docstring.
"""
import ast
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from kant.model import ELEMENT_LANGUAGES, Node, parse_kant, KantParseError
from kant.fileio import write_file_atomic
from kant import toolchains


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

# languages apply_skeleton/apply_skeleton_to_project will also add a file-level MOD/TST wrapper
# for — anything scan_source can actually find real constructs in. SQL/HTML/Generico (an explicit
# "Generico" -> .txt catch-all meant only for the "+ Aggiungi elemento" dialog's manual language
# picker, not for auto-detection here) have no scanner at all, so there's no reliable notion of
# "this file's own top-level construct" for them — wrapping their whole content in a MOD shell
# would just be a guess, exactly the kind _BRACE_LANGUAGES' own exclusion already avoids.
_WRAPPABLE_LANGUAGES = {'Python', 'SQL', 'HTML'} | _BRACE_LANGUAGES


@dataclass
class SkeletonElement:
    tag: str
    name: str
    start_line: int   # 1-based, inclusive — the declaration's own first line (decorators included)
    end_line: int      # 1-based, inclusive — the construct's last line
    depth: int = 0
    # override the file's own comment leader/suffix for just this element's markers — needed for
    # JavaScript dissected out of an HTML <script> block: its markers sit inside JS, not HTML, so
    # they need `//`, never the file's own `<!-- -->` (which JS would read as broken syntax, not
    # a comment). None (the default) means "use the file's own language leader," same as before.
    comment: str = None
    comment_suffix: str = ''


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


# same CREATE-statement tag mapping the "+ Aggiungi elemento" dialog's own SQL skeletons already
# use (kant/model.py's _ELEMENT_SKELETONS['SQL']) — TABLE/VIEW as CLS, FUNCTION/PROCEDURE as FN,
# TYPE as TYP — so a scanned element and a manually-added one land on the identical convention
_SQL_DECL_RE = re.compile(
    r'(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW|FUNCTION|PROCEDURE|TYPE)\s+'
    r'(?:IF\s+NOT\s+EXISTS\s+)?["\[`]?(\w+)["\]`]?'
)
_SQL_TAG_BY_KIND = {
    'TABLE': 'CLS', 'VIEW': 'CLS', 'FUNCTION': 'FN', 'PROCEDURE': 'FN', 'TYPE': 'TYP',
}


# [FN CATEGORY] scan_sql — top-level CREATE TABLE/VIEW/FUNCTION/PROCEDURE/TYPE statements, by the
# same keyword-declaration approach as scan_regex, adapted for SQL's actual terminator: a `;` not
# inside a string, a `--` line comment, or a dollar-quoted body (`$$...$$`/`$tag$...$tag$`, the
# common Postgres way to write a function body containing its own semicolons) — SQL statements
# don't nest the way brace languages do, so no depth tracking is needed, only finding the right
# terminating semicolon.
# [FN] scan_sql — top-level tag/name/span extraction for SQL source
# [FN OPEN] scan_sql
def scan_sql(text, file_path=''):
    lines = text.split('\n')
    elements = []
    i = 0
    while i < len(lines):
        m = _SQL_DECL_RE.match(lines[i])
        if not m:
            i += 1
            continue
        tag = _SQL_TAG_BY_KIND[m.group(1).upper()]
        name = m.group(2)
        end = _sql_statement_end(lines, i)
        if end is None:
            i += 1
            continue
        elements.append(SkeletonElement(tag, name, i + 1, end + 1, 0))
        i = end + 1
    return elements
# [FN CLOSED] scan_sql


def _sql_statement_end(lines, start_index):
    in_string = None  # "'" or a dollar-tag like '$$'/'$tag$'
    for idx in range(start_index, len(lines)):
        line = lines[idx]
        j = 0
        while j < len(line):
            ch = line[j]
            if in_string:
                if in_string == "'":
                    if line[j:j + 2] == "''":
                        j += 2
                        continue
                    if ch == "'":
                        in_string = None
                    j += 1
                    continue
                if line[j:j + len(in_string)] == in_string:
                    j += len(in_string)
                    in_string = None
                    continue
                j += 1
                continue
            if ch == '-' and j + 1 < len(line) and line[j + 1] == '-':
                break  # rest of line is a comment
            if ch == "'":
                in_string = "'"
                j += 1
                continue
            if ch == '$':
                m = re.match(r'\$\w*\$', line[j:])
                if m:
                    in_string = m.group(0)
                    j += len(in_string)
                    continue
            if ch == ';':
                return idx
            j += 1
    return None  # never terminated — don't guess a span that might be wrong


# tags matching the "+ Aggiungi elemento" dialog's own HTML skeletons (_ELEMENT_SKELETONS['HTML']):
# a <section id> is CLS, everything else with an id defaults to FN, <style id> is CFG (styling is
# configuration data, no further dissection — CSS has no function/class concept to find inside
# it), and <script id> is FN too but its BODY gets dissected with the real JavaScript scanner —
# real "on par with other languages" treatment for embedded JS, not just a single opaque blob
_HTML_CLASS_TAGS = {'section'}


# [FN CATEGORY] scan_html — uses the stdlib html.parser (no dependency) to walk real element
# nesting instead of guessing at regex/braces, which HTML's own tag soup doesn't reliably support
# (attributes can contain '>', tags can be unclosed, etc.). Only elements with an id attribute are
# tagged — id is HTML's own stable, human-chosen name, the same role a KANT element's name always
# plays; untagged markup stays untouched, same as any code with no name to hang a marker on.
# [FN] scan_html — dissects HTML into id-named elements, recursing into <script> as real JS
# [FN OPEN] scan_html
def scan_html(text, file_path=''):
    parser = _HtmlElementParser()
    try:
        parser.feed(text)
    except Exception:
        return []
    return parser.elements
# [FN CLOSED] scan_html


class _HtmlElementParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.elements = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        line, _col = self.getpos()
        self._stack.append({
            'tag': tag, 'name': dict(attrs).get('id'), 'start_line': line,
            'depth': len(self._stack), 'text': [],
        })

    def handle_data(self, data):
        if self._stack and self._stack[-1]['tag'] in ('script', 'style'):
            top = self._stack[-1]
            top.setdefault('content_start_line', self.getpos()[0])
            top['text'].append(data)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]['tag'] == tag:
                entry = self._stack.pop(i)
                del self._stack[i:]  # anything still open above it is malformed — drop, don't guess
                self._finish(entry, self.getpos()[0])
                return
        # no matching open tag at all — malformed HTML, ignore rather than guess

    def _finish(self, entry, end_line):
        name, tag, depth = entry['name'], entry['tag'], entry['depth']
        if tag == 'style':
            if name:
                self.elements.append(SkeletonElement('CFG', name, entry['start_line'], end_line, depth))
            return
        if tag == 'script':
            if name:
                self.elements.append(SkeletonElement('FN', name, entry['start_line'], end_line, depth))
            body = ''.join(entry['text'])
            if body.strip():
                offset = entry.get('content_start_line', entry['start_line'] + 1) - 1
                sub_elements, _method = scan_source(body, 'inline.js')
                for e in sub_elements:
                    self.elements.append(SkeletonElement(
                        e.tag, e.name, offset + e.start_line, offset + e.end_line,
                        depth + 1 + e.depth, comment='//', comment_suffix='',
                    ))
            return
        if name:
            kant_tag = 'CLS' if tag in _HTML_CLASS_TAGS else 'FN'
            self.elements.append(SkeletonElement(kant_tag, name, entry['start_line'], end_line, depth))


# [FN CATEGORY] scan_source — dispatches by extension: Python always gets the exact ast-based
# scanner; Go/TS/JS/C#/Java/C++ try their real toolchain scanner first (kant/toolchains.py) and
# only fall back to the regex heuristic if that toolchain isn't installed or its output doesn't
# come back as the expected shape — never on a language it doesn't recognize at all.
# [FN] scan_source — dispatches to the exact scanner (ast or toolchain) or the regex fallback
# [FN OPEN] scan_source
def scan_source(text, file_path):
    language = language_for_path(file_path)
    if language == 'Python':
        return scan_python(text, file_path), 'ast'
    if language == 'SQL':
        return scan_sql(text, file_path), 'sql'
    if language == 'HTML':
        return scan_html(text, file_path), 'html'
    if language in toolchains.SCANNERS:
        result = toolchains.SCANNERS[language](text, file_path)
        if result is not None:
            return result, 'toolchain'
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
    default_prefix, default_suffix = leader['comment'], leader['suffix']

    def marker(elem, bracket_text):
        # an element can override the file's own comment leader (JS dissected out of an HTML
        # <script> block needs `//`, never the file's `<!-- -->` — see SkeletonElement.comment)
        prefix = elem.comment or default_prefix
        suffix = elem.comment_suffix if elem.comment else default_suffix
        tail = f' {suffix}' if suffix else ''
        return f'{prefix} {bracket_text}{tail}'

    lines = text.split('\n')
    # (index, category, tie, block) — category 0 (close) sorts before 1 (open) at a shared index
    entries = []
    for elem in elements:
        header = f'{elem.name} —'
        entries.append((elem.start_line - 1, 1, elem.depth, [
            marker(elem, f'[{elem.tag} CATEGORY] {header}'),
            marker(elem, f'[{elem.tag}] {header}'),
            marker(elem, f'[{elem.tag} OPEN] {elem.name}'),
        ]))
        entries.append((elem.end_line, 0, -elem.depth, [marker(elem, f'[{elem.tag} CLOSED] {elem.name}')]))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    inserts = {}
    for index, _category, _tie, block in entries:
        inserts.setdefault(index, []).extend(block)

    for index in sorted(inserts, reverse=True):
        lines[index:index] = inserts[index]

    return '\n'.join(lines), len(elements)
# [FN CLOSED] insert_skeleton


# [CST] _TEST_FILE_RE — a whole FILE counts as a test file (gets TST instead of MOD as its own
# wrapper tag) by the same naming convention already used for individual test functions/methods
_TEST_FILE_RE = re.compile(r'(?i)^test_|_test$|\.test$|\.spec$')


def _file_wrapper_tag(file_path):
    stem = os.path.splitext(os.path.basename(file_path))[0]
    return 'TST' if _TEST_FILE_RE.search(stem) else 'MOD'


# [FN] _needs_file_wrapper — True unless the file already has exactly one top-level KANT element
# (its own MOD/CFG/TST wrapper) — zero (untagged) or more than one (individually-tagged elements
# with nothing enclosing them, e.g. everything insert_skeleton has tagged so far) both need one
# [FN OPEN] _needs_file_wrapper
def _needs_file_wrapper(text):
    try:
        tree = parse_kant(text)
    except KantParseError:
        return False
    return len([item for item in tree.body if isinstance(item, Node)]) != 1
# [FN CLOSED] _needs_file_wrapper


# [FN CATEGORY] _wrap_whole_file — wraps already-processed text (individual elements already
# tagged, still lacking a file-level wrapper) in one MOD/TST OPEN...CLOSED pair spanning the whole
# file — the parent every KANT-tagged file is supposed to have, per the convention's own "MOD |
# file/module" row, that scan_python/scan_regex/the toolchain scanners never produce themselves
# (they only ever find constructs INSIDE a file, never the file as a construct in its own right).
# Named by its path relative to project_root when known (matching the map's own path convention),
# basename otherwise — e.g. a lone file with no project context yet.
# [FN] _wrap_whole_file — adds the missing file-level MOD/TST wrapper around already-tagged text
# [FN OPEN] _wrap_whole_file
def _wrap_whole_file(text, language, file_path, project_root=None):
    leader = ELEMENT_LANGUAGES.get(language, ELEMENT_LANGUAGES['Generico'])
    prefix, suffix = leader['comment'], leader['suffix']
    tail = f' {suffix}' if suffix else ''

    def marker(bracket_text):
        return f'{prefix} {bracket_text}{tail}'

    tag = _file_wrapper_tag(file_path)
    name = (
        os.path.relpath(file_path, project_root).replace(os.sep, '/')
        if project_root else os.path.basename(file_path)
    )
    header = f'{name} —'
    return '\n'.join([
        marker(f'[{tag} CATEGORY] {header}'),
        marker(f'[{tag}] {header}'),
        marker(f'[{tag} OPEN] {name}'),
        text,
        marker(f'[{tag} CLOSED] {name}'),
    ])
# [FN CLOSED] _wrap_whole_file


# [FN CATEGORY] apply_skeleton — the single entry point the IDE and the AI-facing tooling both use:
# scan, filter out what's already marked, insert skeletons for the rest, then add the file's own
# MOD/TST wrapper if it doesn't already have exactly one. Returns None (no change) rather than
# raising when the file's language isn't recognized or its existing markers don't parse — callers
# decide how to surface that, this stays a pure best-effort transform.
# [FN] apply_skeleton — scans a file's text and returns (new_text, inserted_count) or None
# [FN OPEN] apply_skeleton
def apply_skeleton(text, file_path, project_root=None):
    language = language_for_path(file_path)
    if language is None:
        return None
    elements, _method = scan_source(text, file_path)
    unmarked = unmarked_elements(text, elements)
    if unmarked is None:
        return None
    new_text, count = text, 0
    if unmarked:
        new_text, count = insert_skeleton(new_text, unmarked, language)
    # checked against new_text, AFTER inserting — a file with one already-tagged element and one
    # still-bare one looks like it only has a single top-level node until that second element
    # actually becomes one; checking the pre-insertion text here would need a second call to
    # notice the wrapper is needed once both are real nodes, instead of converging in one pass
    if language in _WRAPPABLE_LANGUAGES and _needs_file_wrapper(new_text):
        new_text = _wrap_whole_file(new_text, language, file_path, project_root)
        count += 1
    elif count == 0:
        return None
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
    for path, text in iter_project_text_files(root, max_bytes=None):
        language = language_for_path(path)
        if language is None:
            continue
        elements, _method = scan_source(text, path)
        unmarked = unmarked_elements(text, elements)
        if unmarked is None:
            skipped.append(os.path.relpath(path, root))
            continue
        new_text, count = text, 0
        if unmarked:
            new_text, count = insert_skeleton(new_text, unmarked, language)
        # checked against new_text, AFTER inserting — see apply_skeleton's own comment on why
        if language in _WRAPPABLE_LANGUAGES and _needs_file_wrapper(new_text):
            new_text = _wrap_whole_file(new_text, language, path, root)
            count += 1
        elif count == 0:
            continue
        try:
            write_file_atomic(path, new_text)
        except OSError:
            skipped.append(os.path.relpath(path, root))
            continue
        changed.append((os.path.relpath(path, root), count))
    return changed, skipped
# [FN CLOSED] apply_skeleton_to_project


def strip_kant_project(root):
    """Remove every KANT marker project-wide, including markers in malformed trees."""
    from kant.projectops import iter_project_text_files
    from kant.model import strip_kant_marker_lines

    changed, skipped = [], []
    for path, text in iter_project_text_files(root, max_bytes=None):
        bare = strip_kant_marker_lines(text)
        if bare == text:
            continue
        try:
            write_file_atomic(path, bare)
        except OSError:
            skipped.append(os.path.relpath(path, root))
            continue
        changed.append(os.path.relpath(path, root))
    return changed, skipped


# [FN CATEGORY] wipe_and_reskeleton_project — the "wipe and rebuild deterministically" project
# action (kant/mainwindow.py's KANT menu): strips every existing KANT marker (model.py's
# strip_kant_markers — CATEGORY/tagline/OPEN/CLOSED and any legacy INCOMING/OUTGOING) back to bare
# code, then runs the ordinary skeleton pass on that bare code as if it were never tagged at all.
# Hand-written CATEGORY/tagline text is genuinely discarded here, not preserved — that's the whole
# point of "wipe." A file whose existing markers don't even parse is skipped rather than stripped
# blind, same caution apply_skeleton_to_project already takes.
# [FN] wipe_and_reskeleton_project — strips all markers, then re-tags every file from scratch
# [FN OPEN] wipe_and_reskeleton_project
def wipe_and_reskeleton_project(root):
    from kant.projectops import iter_project_text_files
    from kant.model import parse_kant, strip_kant_markers, KantParseError

    changed, skipped = [], []
    for path, text in iter_project_text_files(root, max_bytes=None):
        language = language_for_path(path)
        # SQL/HTML/Generico have no scanner and so no reliable way to re-tag anything at all —
        # wiping their markers without being able to rebuild would just be a loss, so they're left
        # alone entirely, same as an unrecognized language
        if language not in _WRAPPABLE_LANGUAGES:
            continue
        try:
            tree = parse_kant(text)
        except KantParseError:
            skipped.append(os.path.relpath(path, root))
            continue
        bare = strip_kant_markers(tree)
        if not bare.strip():
            continue  # genuinely empty file — nothing worth wrapping either
        elements, _method = scan_source(bare, path)
        new_text, count = bare, 0
        if elements:
            new_text, count = insert_skeleton(new_text, elements, language)
        # stripped text never has a wrapper of its own left — always add a fresh one
        new_text = _wrap_whole_file(new_text, language, path, root)
        count += 1
        try:
            write_file_atomic(path, new_text)
        except OSError:
            skipped.append(os.path.relpath(path, root))
            continue
        changed.append((os.path.relpath(path, root), count))
    return changed, skipped
# [FN CLOSED] wipe_and_reskeleton_project
