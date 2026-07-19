"""Language-agnostic syntax checks and run/token helpers (no Qt, no theme)."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from kant.model import CATEGORY_RE, TAGLINE_RE, Node, Run, _short_desc, parse_kant, KantParseError


# [CST] KEYWORDS — cross-language keyword set for the lightweight syntax highlighter below
KEYWORDS = set((
    'def class function return if elif else for while do switch case break continue import from as '
    'export default const let var public private protected static final void int float double long short '
    'byte char bool boolean string String True False None null nil undefined true false self this new '
    'delete try except catch finally throw throws raise yield async await lambda with in is not and or '
    'typeof instanceof extends implements interface enum struct namespace using package fn pub mut impl match'
).split())


# [CST] TOKEN_RE — the tokenizer shared by KantHighlighter and check_syntax; treats
# comments/strings as opaque so bracket-like chars inside them are ignored
TOKEN_RE = re.compile(
    r'(#[^\n]*|//[^\n]*)'
    r'|(/\*[\s\S]*?\*/)'
    r'|("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:[^"\n\\]|\\.)*"|\'(?:[^\'\n\\]|\\.)*\'|`(?:[^`\\]|\\.)*`)'
    r'|(\b\d+(?:\.\d+)?\b)'
    r'|(\b[A-Za-z_]\w*\b)'
)


BRACKET_PAIRS = {'(': ')', '[': ']', '{': '}'}
BRACKET_CLOSERS = {')': '(', ']': '[', '}': '{'}


# ponytail: a real syntax checker needs a grammar per language; check_syntax instead validates the
# one thing that's true of nearly every language — brackets/parens/braces must balance — and skips
# comments/strings (reusing TOKEN_RE) so bracket-like characters inside them don't
# misfire. It catches unbalanced/misplaced brackets in any language but not, say, a missing colon.
# [FN CATEGORY] check_syntax — scans the full reconstructed file text for unbalanced brackets,
# treating comment/string tokens (from the same tokenizer used for highlighting) as opaque so
# brackets mentioned inside them don't produce false positives
# [FN] check_syntax — reports the first bracket-balance error in a source string, or ok=True
# [FN OPEN] check_syntax
def check_syntax(text):
    stack = []
    line = [1]

    def scan_plain(segment):
        for ch in segment:
            if ch == '\n':
                line[0] += 1
                continue
            if ch in BRACKET_PAIRS:
                stack.append((ch, line[0]))
                continue
            if ch in BRACKET_CLOSERS:
                if not stack or stack[-1][0] != BRACKET_CLOSERS[ch]:
                    return {'ok': False, 'line': line[0], 'message': f'"{ch}" senza corrispondenza'}
                stack.pop()
        return None

    last_index = 0
    for m in TOKEN_RE.finditer(text):
        err = scan_plain(text[last_index:m.start()])
        if err:
            return err
        comment, block_comment, string, _number, _word = m.groups()
        if comment or block_comment or string:
            line[0] += m.group().count('\n')
        else:
            err = scan_plain(m.group())
            if err:
                return err
        last_index = m.end()
    err = scan_plain(text[last_index:])
    if err:
        return err
    if stack:
        ch, at_line = stack[-1]
        return {'ok': False, 'line': at_line, 'message': f'"{ch}" non chiuso'}
    return {'ok': True}
# [FN CLOSED] check_syntax


def _quote_arg(arg):
    # list2cmdline only quotes when the string contains a space/tab/quote, leaving characters
    # like & | ^ % <> bare when none of those trigger it — and Windows filenames legally allow
    # them, so a path such as "file&calc.py" would inject an extra command into cmd.exe's
    # unquoted metacharacter scan. Unconditional quoting closes that: Windows paths can never
    # contain '"' and a file path never ends in a bare backslash, so simple wrapping is safe.
    return f'"{arg}"' if os.name == 'nt' else shlex.quote(arg)


# [FN CATEGORY] find_duplicate_uid — walks a parsed tree looking for the first #id reused by two
# different nodes; shared by check_kant_markers (live check) and audit_kant_headers (full project
# validation) so the reused-id detection logic exists in exactly one place.
# [FN] find_duplicate_uid — first Node whose #id was already seen elsewhere in the tree, or None
# [FN OPEN] find_duplicate_uid
def find_duplicate_uid(tree):
    seen = set()

    def walk(node):
        for item in node.body:
            if isinstance(item, Node):
                if item.uid in seen:
                    return item
                seen.add(item.uid)
                dupe = walk(item)
                if dupe is not None:
                    return dupe
        return None

    return walk(tree)
# [FN CLOSED] find_duplicate_uid


# [FN CATEGORY] check_kant_markers — re-parses the file's current (possibly just-edited) text with
# parse_kant, which already enforces strict OPEN/CLOSED nesting and #id matching and raises
# KantParseError on any mismatch; on top of that this only needs to add the one check parse_kant
# can't do on its own — #id uniqueness across the whole file, since two non-overlapping OPEN/CLOSED
# pairs elsewhere in the same file could reuse an id without ever tripping the stack-matching check.
# Deliberately cheap and single-verdict (first problem found, stop) — this runs on every keystroke's
# live syntax check (see check_file_syntax); the fuller multi-issue audit is audit_kant_headers below,
# run only from the background full-project validation.
# [FN] check_kant_markers — validates KANT marker nesting and #id uniqueness for one file's text
# [FN OPEN] check_kant_markers
def check_kant_markers(text):
    try:
        tree = parse_kant(text)
    except KantParseError as e:
        return {'ok': False, 'line': e.line, 'message': e.message}

    dupe = find_duplicate_uid(tree)
    if dupe is not None:
        line = dupe.open_line or 1
        return {'ok': False, 'line': line, 'message': f'#id duplicato nel file: #{dupe.uid} ({dupe.tag} {dupe.name})'}
    return {'ok': True, 'message': 'Marcatori KANT OK'}
# [FN CLOSED] check_kant_markers


# [CST] _HEADER_NAME_SEPARATORS — same separators _short_desc (kant/model.py) already uses to split
# a CATEGORY/tagline's "Name — description" text; reused here so audit_kant_headers extracts the
# "name" portion the identical way the rest of the codebase already defines it, not a new heuristic.
_HEADER_NAME_SEPARATORS = (' — ', ' - ', ' -- ', ': ')


# shared by both the CATEGORY and tagline empty-description checks below: strip the element's own
# name (already confirmed present by _header_name_part's caller) plus one separator, leaving just
# the description part — "" if there wasn't one, i.e. the line is only "Name —" or bare "Name"
def _strip_name_prefix(text, name):
    remainder = text[len(name):].strip() if text.startswith(name) else text
    for sep in _HEADER_NAME_SEPARATORS:
        marker = sep.strip()
        if remainder.startswith(marker):
            return remainder[len(marker):].strip()
    return remainder


def _header_name_part(text):
    text = (text or '').strip()
    for sep in _HEADER_NAME_SEPARATORS:
        if sep in text:
            return text.split(sep, 1)[0].strip()
    if text.startswith(('—', '-')):
        return ''
    for sep in _HEADER_NAME_SEPARATORS:
        # trailing separator with no description after it ("Name —") — sep itself never matches as
        # an infix above since there's nothing past it to form the closing space, but the name part
        # is still recoverable by stripping the separator's own (unspaced) marker off the end
        marker = sep.strip()
        if text.endswith(marker):
            return text[:-len(marker)].strip()
    return text


# [FN CATEGORY] audit_kant_headers — the fuller, multi-issue counterpart to check_kant_markers: walks
# the whole parsed tree (not just the first problem) and separates hard errors (nesting/pair/#id
# problems already caught by parse_kant plus header/tag/name coherence and orphaned pending headers)
# from warnings (missing/empty headers, over-length taglines, tag outside the fixed 8-tag set,
# unconfirmed marker-to-declaration linkage). Only ever called from the background full-project
# validation (kant/projectops.py:validate_kant_project) — never from the live per-keystroke path,
# which stays on the cheaper single-verdict check_kant_markers above.
# [FN] audit_kant_headers — full error/warning audit of one file's KANT markers
# [FN OPEN] audit_kant_headers
_FIXED_TAGS = {'MOD', 'CFG', 'CLS', 'TYP', 'FN', 'CST', 'VAR', 'TST'}
# same per-name declaration templates kant/projectops.py:definition_locations already uses to find a
# symbol's definition — reused here (formatted with the escaped element name) instead of a new set
_DECLARATION_TEMPLATES = [
    r'\b(?:async\s+def|def|class)\s+{name}\b',
    r'\bfunction\s+{name}\b',
    r'\b(?:const|let|var|type|interface|enum|struct|fn)\s+{name}\b',
    r'^\s*{name}\s*[:=]',
]


def audit_kant_headers(text):
    try:
        tree = parse_kant(text)
    except KantParseError as e:
        return {'errors': [{'line': e.line, 'message': e.message, 'tag': None, 'name': None}], 'warnings': []}

    errors, warnings = [], []

    dupe = find_duplicate_uid(tree)
    if dupe is not None:
        errors.append({
            'line': dupe.open_line or 1, 'tag': dupe.tag, 'name': dupe.name,
            'message': f'#id duplicato nel file: #{dupe.uid} ({dupe.tag} {dupe.name})',
        })

    for line_no, tag, kind in tree.orphaned:
        marker = f'[{tag} CATEGORY]' if kind == 'category' else f'[{tag}]'
        errors.append({
            'line': line_no, 'tag': tag, 'name': None,
            'message': f'intestazione {marker} pendente, non associata a un OPEN successivo',
        })

    def walk(node):
        for item in node.body:
            if not isinstance(item, Node):
                continue
            if item.category_raw:
                m = CATEGORY_RE.match(item.category_raw)
                cat_tag, cat_text = (m.group(1), m.group(2)) if m else (None, '')
                if cat_tag is not None and cat_tag != item.tag:
                    errors.append({
                        'line': item.category_line, 'tag': item.tag, 'name': item.name,
                        'message': f'tag CATEGORY ({cat_tag}) incoerente con OPEN ({item.tag})',
                    })
                elif _header_name_part(cat_text) not in (item.name, ''):
                    errors.append({
                        'line': item.category_line, 'tag': item.tag, 'name': item.name,
                        'message': f'nome in CATEGORY ("{_header_name_part(cat_text)}") incoerente con OPEN ("{item.name}")',
                    })
                elif not _strip_name_prefix(cat_text, item.name):
                    # CATEGORY has no length cap, but a placeholder like "Name —" with nothing
                    # after the dash is still not a real "how it works" explanation — same class
                    # of gap the tagline check below already catches
                    warnings.append({'line': item.category_line, 'tag': item.tag, 'name': item.name, 'message': 'CATEGORY vuota'})
            else:
                warnings.append({'line': item.open_line, 'tag': item.tag, 'name': item.name, 'message': 'CATEGORY mancante'})
            if item.tag_raw:
                m = TAGLINE_RE.match(item.tag_raw)
                tl_tag, tl_text = (m.group(1), m.group(3)) if m else (None, '')
                if tl_tag is not None and tl_tag != item.tag:
                    errors.append({
                        'line': item.tagline_line, 'tag': item.tag, 'name': item.name,
                        'message': f'tag riga descrittiva ({tl_tag}) incoerente con OPEN ({item.tag})',
                    })
                elif _header_name_part(tl_text) not in (item.name, ''):
                    errors.append({
                        'line': item.tagline_line, 'tag': item.tag, 'name': item.name,
                        'message': f'nome nella riga descrittiva incoerente con OPEN ("{item.name}")',
                    })
                else:
                    # tl_text is known to start with item.name (or have no name prefix at all) at
                    # this point — strip that known prefix plus one separator to get the actual
                    # description, rather than _short_desc's generic search (which can't tell "name
                    # followed by a separator and nothing else" from "no separator present at all")
                    desc = _strip_name_prefix(tl_text, item.name)
                    if not desc:
                        warnings.append({'line': item.tagline_line, 'tag': item.tag, 'name': item.name, 'message': 'tagline vuota'})
                    elif len(desc.split()) > 8:
                        warnings.append({
                            'line': item.tagline_line, 'tag': item.tag, 'name': item.name,
                            'message': 'descrizione oltre le 8 parole previste dalla convenzione',
                        })
            else:
                warnings.append({'line': item.open_line, 'tag': item.tag, 'name': item.name, 'message': 'tagline mancante'})
            if item.tag not in _FIXED_TAGS:
                warnings.append({
                    'line': item.open_line, 'tag': item.tag, 'name': item.name,
                    'message': f'tag "{item.tag}" non appartiene all\'insieme previsto (MOD/CFG/CLS/TYP/FN/CST/VAR/TST)',
                })
            first_code_line = next(
                (ln for run in item.body if isinstance(run, Run) for ln in run.lines if ln.strip()), None,
            )
            if first_code_line is not None:
                escaped = re.escape(item.name)
                linked = any(re.search(template.format(name=escaped), first_code_line)
                             for template in _DECLARATION_TEMPLATES)
                if not linked:
                    warnings.append({
                        'line': item.open_line, 'tag': item.tag, 'name': item.name,
                        'message': 'impossibile confermare il collegamento del marker alla dichiarazione — verifica manuale',
                    })
            walk(item)

    walk(tree)
    return {'errors': errors, 'warnings': warnings}
# [FN CLOSED] audit_kant_headers


# ponytail: broad syntax support is delegated to compilers already on PATH; unknown or missing tools
# fall back to the cheap bracket check above instead of bundling parsers for every language.
def check_file_syntax(path, text, python_exe=None):
    marker_result = check_kant_markers(text)
    if not marker_result['ok']:
        return marker_result
    ext = Path(path).suffix.lower()
    if ext == '.json':
        try:
            json.loads(text)
            return {'ok': True, 'message': 'JSON OK'}
        except json.JSONDecodeError as e:
            return {'ok': False, 'line': e.lineno, 'message': e.msg}
    if ext in ('.xml', '.svg'):
        try:
            ElementTree.fromstring(text)
            return {'ok': True, 'message': 'XML OK'}
        except ElementTree.ParseError as e:
            return {'ok': False, 'line': e.position[0], 'message': str(e)}

    checkers = {
        '.py': (python_exe or sys.executable, ['-m', 'py_compile']),
        '.js': ('node', ['--check']),
        '.mjs': ('node', ['--check']),
        '.cjs': ('node', ['--check']),
        '.ts': ('tsc', ['--noEmit', '--pretty', 'false']),
        '.sh': ('sh', ['-n']),
        '.bash': ('bash', ['-n']),
        '.php': ('php', ['-l']),
        '.rb': ('ruby', ['-c']),
        '.pl': ('perl', ['-c']),
        '.pm': ('perl', ['-c']),
        '.lua': ('luac', ['-p']),
        '.go': ('gofmt', ['-e']),
        '.c': ('gcc', ['-fsyntax-only']),
        '.h': ('gcc', ['-fsyntax-only']),
        '.cpp': ('g++', ['-fsyntax-only']),
        '.cc': ('g++', ['-fsyntax-only']),
        '.cxx': ('g++', ['-fsyntax-only']),
        '.hpp': ('g++', ['-fsyntax-only']),
        '.java': ('javac', []),
    }
    checker = checkers.get(ext)
    if checker is not None:
        tool, args = checker
        executable = tool if os.path.isabs(tool) else shutil.which(tool)
        if executable:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp) / Path(path).name
                tmp_path.write_text(text, encoding='utf-8', newline='')
                try:
                    result = subprocess.run(
                        [executable, *args, str(tmp_path)],
                        cwd=os.path.dirname(path) or None,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                except subprocess.TimeoutExpired:
                    return {'ok': False, 'line': 1, 'message': f'{tool}: controllo scaduto'}
            if result.returncode == 0:
                return {'ok': True, 'message': f'{tool} OK'}
            output = (result.stderr or result.stdout or '').strip().splitlines()
            return {'ok': False, 'line': 1, 'message': output[0] if output else f'{tool}: errore sintattico'}

    result = check_syntax(text)
    result['message'] = 'Controllo base OK' if result['ok'] else result['message']
    return result


def run_command_for_path(path, python_exe=None):
    ext = Path(path).suffix.lower()
    quoted = _quote_arg(path)
    commands = {
        '.py': f'{_quote_arg(python_exe or sys.executable)} {quoted}',
        '.js': f'node {quoted}',
        '.mjs': f'node {quoted}',
        '.cjs': f'node {quoted}',
        '.ts': f'ts-node {quoted}',
        '.sh': f'sh {quoted}',
        '.bash': f'bash {quoted}',
        '.php': f'php {quoted}',
        '.rb': f'ruby {quoted}',
        '.pl': f'perl {quoted}',
        '.lua': f'lua {quoted}',
        '.go': f'go run {quoted}',
        '.java': f'javac {quoted} && java -cp {_quote_arg(os.path.dirname(path) or ".")} {Path(path).stem}',
        '.bat': quoted,
        '.cmd': quoted,
        '.ps1': f'powershell -ExecutionPolicy Bypass -File {quoted}',
    }
    return commands.get(ext)
