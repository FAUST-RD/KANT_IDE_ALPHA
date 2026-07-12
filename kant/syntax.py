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

from kant.model import Node, parse_kant, KantParseError


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


# [FN CATEGORY] check_kant_markers — re-parses the file's current (possibly just-edited) text with
# parse_kant, which already enforces strict OPEN/CLOSED nesting and #id matching and raises
# KantParseError on any mismatch; on top of that this only needs to add the one check parse_kant
# can't do on its own — #id uniqueness across the whole file, since two non-overlapping OPEN/CLOSED
# pairs elsewhere in the same file could reuse an id without ever tripping the stack-matching check
# [FN] check_kant_markers — validates KANT marker nesting and #id uniqueness for one file's text
# [FN OPEN] check_kant_markers
def check_kant_markers(text):
    try:
        tree = parse_kant(text)
    except KantParseError as e:
        return {'ok': False, 'line': e.line, 'message': e.message}

    seen = set()

    def find_dupe(node):
        for item in node.body:
            if isinstance(item, Node):
                if item.uid in seen:
                    return item
                seen.add(item.uid)
                dupe = find_dupe(item)
                if dupe is not None:
                    return dupe
        return None

    dupe = find_dupe(tree)
    if dupe is not None:
        return {'ok': False, 'line': 1, 'message': f'#id duplicato nel file: #{dupe.uid} ({dupe.tag} {dupe.name})'}
    return {'ok': True, 'message': 'Marcatori KANT OK'}
# [FN CLOSED] check_kant_markers


# ponytail: broad syntax support is delegated to compilers already on PATH; unknown or missing tools
# fall back to the cheap bracket check above instead of bundling parsers for every language.
def check_file_syntax(path, text):
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
        '.py': (sys.executable, ['-m', 'py_compile']),
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


def run_command_for_path(path):
    ext = Path(path).suffix.lower()
    quoted = _quote_arg(path)
    commands = {
        '.py': f'{_quote_arg(sys.executable)} {quoted}',
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
