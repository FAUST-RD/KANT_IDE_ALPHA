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

from kant.model import CATEGORY_RE, CLOSED_RE, TAGLINE_RE, Node, Run, _short_desc, parse_kant, KantParseError


# [CST] KEYWORDS — cross-language keyword set for the lightweight syntax highlighter below
KEYWORDS = set((
    'def class function return if elif else for while do switch case break continue import from as '
    'export default const let var public private protected static final void int float double long short '
    'byte char bool boolean string String True False None null nil undefined true false self this new '
    'delete try except catch finally throw throws raise yield async await lambda with in is not and or '
    'typeof instanceof extends implements interface enum struct namespace using package fn pub mut impl match'
).split())

# [CST CATEGORY] KEYWORD_DOCS — one entry per KEYWORDS token, for the coding board's hover-to-
# explain popup (see mainwindow.py's _request_hover). KEYWORDS is deliberately one flat set across
# every supported language rather than per-language, and this mirrors that: each explanation is
# written to be accurate wherever the keyword actually appears (several mean almost the same thing
# in every language that has them — a loop is a loop), with the specific language named only where
# the meaning genuinely differs by language (e.g. Python's `except` vs C-family `catch`).
# [CST] KEYWORD_DOCS — hover explanation text for each cross-language keyword
# [CST OPEN] KEYWORD_DOCS
def _keyword_doc(language, syntax, example):
    """Build the same scan-friendly Markdown card for every keyword hover."""
    return (
        f'**{language}**\n'
        f'**Sintassi**\n```text\n{syntax}\n```\n'
        f'**Esempio**\n```text\n{example}\n```'
    )


KEYWORD_DOCS = {
    'def': _keyword_doc('Python', 'def nome(parametro: Tipo = valore) -> Tipo:\n    corpo', 'def area(r: float) -> float:\n    return 3.14 * r * r'),
    'class': _keyword_doc('Python / JS / Java', 'class Nome(Base): ...\nclass Nome extends Base { ... }', 'class Utente(Persona):\n    pass'),
    'function': _keyword_doc('JavaScript', 'function nome(parametro = valore) {\n  corpo\n}', 'function somma(a, b) {\n  return a + b;\n}'),
    'return': _keyword_doc('Python / C-family / JS', 'return [valore]', 'return totale;'),
    'if': _keyword_doc('Python / C-family / JS', 'if condizione:\n    corpo\nif (condizione) { corpo }', 'if eta >= 18:\n    abilita()'),
    'elif': _keyword_doc('Python', 'elif condizione:\n    corpo', 'elif voto >= 6:\n    promuovi()'),
    'else': _keyword_doc('Python / C-family / JS', 'else:\n    corpo\nelse { corpo }', 'else:\n    mostra_errore()'),
    'for': _keyword_doc('Python / C-family / JS', 'for elemento in iterabile:\n    corpo\nfor (init; condizione; passo) { corpo }', 'for item in elementi:\n    stampa(item)'),
    'while': _keyword_doc('Python / C-family / JS', 'while condizione:\n    corpo\nwhile (condizione) { corpo }', 'while coda:\n    elabora(coda.pop())'),
    'do': _keyword_doc('C-family / JavaScript', 'do {\n  corpo\n} while (condizione);', 'do { tentativi++; } while (!pronto);'),
    'switch': _keyword_doc('C-family / JavaScript', 'switch (valore) {\n  case valore: ...; break;\n  default: ...;\n}', 'switch (stato) {\n  case "ok": salva(); break;\n}'),
    'case': _keyword_doc('C-family / JavaScript', 'case valore:\n  istruzioni\n  break;', 'case 404:\n  mostraErrore();\n  break;'),
    'break': _keyword_doc('Python / C-family / JS', 'break', 'for item in elementi:\n    if item == target: break'),
    'continue': _keyword_doc('Python / C-family / JS', 'continue', 'for item in elementi:\n    if not item: continue'),
    'import': _keyword_doc('Python / JavaScript', 'import modulo\nfrom modulo import nome\nimport nome from "modulo";', 'from pathlib import Path'),
    'from': _keyword_doc('Python / JavaScript', 'from modulo import nome\nimport nome from "modulo";', 'from collections import deque'),
    'as': _keyword_doc('Python / TypeScript', 'import nome as alias\nvalore as Tipo', 'import numpy as np'),
    'export': _keyword_doc('JavaScript / TypeScript', 'export [default] dichiarazione;\nexport { nome };', 'export const VERSION = "1.0";'),
    'default': _keyword_doc('JavaScript / switch', 'export default valore;\ndefault:\n  istruzioni', 'export default App;'),
    'const': _keyword_doc('JavaScript / C-family', 'const nome = valore;\nconst Tipo nome = valore;', 'const MAX_RETRY = 3;'),
    'let': _keyword_doc('JavaScript / TypeScript', 'let nome[: Tipo] = valore;', 'let totale: number = 0;'),
    'var': _keyword_doc('JavaScript / Go', 'var nome = valore;\nvar nome Tipo = valore', 'var count = 0;'),
    'public': _keyword_doc('Java / C# / TypeScript', 'public Tipo nome(...) { ... }', 'public void salva() { ... }'),
    'private': _keyword_doc('Java / C# / TypeScript', 'private Tipo nome;', 'private String token;'),
    'protected': _keyword_doc('Java / C# / TypeScript', 'protected Tipo nome;', 'protected int tentativi;'),
    'static': _keyword_doc('Java / C# / TypeScript', 'static Tipo nome = valore;\nstatic Tipo nome(...) { ... }', 'static int count = 0;'),
    'final': _keyword_doc('Java', 'final Tipo nome = valore;\nfinal class Nome { ... }', 'final int MAX = 10;'),
    'void': _keyword_doc('C-family / Java / TypeScript', 'void nome(parametri) { ... }', 'void reset() { count = 0; }'),
    'int': _keyword_doc('C-family / Java / C#', 'int nome = valore;', 'int count = 0;'),
    'float': _keyword_doc('C-family / Java / C#', 'float nome = valore;', 'float prezzo = 9.99f;'),
    'double': _keyword_doc('C-family / Java / C#', 'double nome = valore;', 'double media = 12.5;'),
    'long': _keyword_doc('C-family / Java / C#', 'long nome = valore;', 'long timestamp = 1720000000L;'),
    'short': _keyword_doc('C-family / Java / C#', 'short nome = valore;', 'short porta = 8080;'),
    'byte': _keyword_doc('Java / C#', 'byte nome = valore;', 'byte canale = 127;'),
    'char': _keyword_doc('C-family / Java / C#', "char nome = 'x';", "char iniziale = 'K';"),
    'bool': _keyword_doc('C++ / C# / Rust', 'bool nome = true;', 'bool attivo = true;'),
    'boolean': _keyword_doc('Java', 'boolean nome = true;', 'boolean pronto = false;'),
    'string': _keyword_doc('C# / TypeScript', 'string nome = "testo";', 'string titolo = "KANT";'),
    'String': _keyword_doc('Java / Rust', 'String nome = "testo";\nlet nome: String = String::from("testo");', 'String titolo = "KANT";'),
    'True': _keyword_doc('Python', 'True', 'attivo = True'),
    'False': _keyword_doc('Python', 'False', 'completato = False'),
    'None': _keyword_doc('Python', 'None', 'risultato = None'),
    'null': _keyword_doc('JavaScript / Java / C#', 'null', 'const utente = null;'),
    'nil': _keyword_doc('Go / Ruby / Lua', 'nil', 'if err != nil { return err }'),
    'undefined': _keyword_doc('JavaScript / TypeScript', 'undefined', 'let risultato = undefined;'),
    'true': _keyword_doc('C-family / JS / Rust', 'true', 'const attivo = true;'),
    'false': _keyword_doc('C-family / JS / Rust', 'false', 'let pronto = false;'),
    'self': _keyword_doc('Python / Rust', 'self.attributo\nself::nome', 'def salva(self):\n    self.pronto = True'),
    'this': _keyword_doc('JavaScript / Java / C#', 'this.membro', 'this.nome = nome;'),
    'new': _keyword_doc('JavaScript / Java / C# / C++', 'new Classe(argomenti)', 'const utente = new Utente(nome);'),
    'delete': _keyword_doc('JavaScript / C++', 'delete oggetto.proprieta;\ndelete puntatore;', 'delete cache.chiave;'),
    'try': _keyword_doc('Python / C-family / JS', 'try:\n    codice_rischioso\nexcept Errore: ...\ntry { ... } catch (Errore e) { ... }', 'try:\n    carica()\nexcept OSError:\n    recupera()'),
    'except': _keyword_doc('Python', 'except TipoErrore as errore:\n    gestisci', 'except ValueError as errore:\n    log(errore)'),
    'catch': _keyword_doc('JavaScript / Java / C#', 'catch (errore) {\n  gestisci\n}', 'catch (error) {\n  console.error(error);\n}'),
    'finally': _keyword_doc('Python / C-family / JS', 'finally:\n    pulizia\nfinally { pulizia }', 'finally:\n    file.close()'),
    'throw': _keyword_doc('JavaScript / Java / C#', 'throw espressione;', 'throw new Error("dato non valido");'),
    'throws': _keyword_doc('Java', 'Tipo nome(...) throws TipoErrore { ... }', 'void carica() throws IOException { ... }'),
    'raise': _keyword_doc('Python', 'raise TipoErrore(messaggio)\nraise', 'raise ValueError("dato non valido")'),
    'yield': _keyword_doc('Python / JavaScript', 'yield valore\nyield* iterabile', 'for item in elementi:\n    yield item.id'),
    'async': _keyword_doc('Python / JavaScript', 'async def nome(...): ...\nasync function nome(...) { ... }', 'async def carica():\n    return await fetch()'),
    'await': _keyword_doc('Python / JavaScript', 'risultato = await operazione_async', 'const dati = await fetch(url);'),
    'lambda': _keyword_doc('Python', 'lambda parametri: espressione', 'raddoppia = lambda x: x * 2'),
    'with': _keyword_doc('Python', 'with gestore as nome:\n    corpo', 'with open(path) as file:\n    testo = file.read()'),
    'in': _keyword_doc('Python / JavaScript', 'elemento in collezione\nfor elemento in iterabile', 'if chiave in dizionario:\n    usa(dizionario[chiave])'),
    'is': _keyword_doc('Python', 'sinistra is destra\nsinistra is not destra', 'if valore is None:\n    return'),
    'not': _keyword_doc('Python', 'not espressione\nvalore not in collezione', 'if not pronto:\n    attendi()'),
    'and': _keyword_doc('Python', 'sinistra and destra', 'if pronto and connesso:\n    avvia()'),
    'or': _keyword_doc('Python', 'sinistra or destra', 'nome = input_nome or "Anonimo"'),
    'typeof': _keyword_doc('JavaScript / TypeScript', 'typeof espressione', 'if (typeof id === "string") { ... }'),
    'instanceof': _keyword_doc('JavaScript / Java', 'oggetto instanceof Classe', 'if (error instanceof TypeError) { ... }'),
    'extends': _keyword_doc('JavaScript / Java / TypeScript', 'class Figlia extends Base { ... }', 'class Admin extends Utente { ... }'),
    'implements': _keyword_doc('Java / TypeScript', 'class Nome implements Interfaccia { ... }', 'class FileStore implements Store { ... }'),
    'interface': _keyword_doc('Java / TypeScript', 'interface Nome {\n  metodo(...): Tipo;\n}', 'interface Store {\n  save(id: string): void;\n}'),
    'enum': _keyword_doc('C-family / Java / TypeScript / Rust', 'enum Nome { ValoreA, ValoreB }', 'enum Stato { Attivo, Inattivo }'),
    'struct': _keyword_doc('C / C++ / Rust', 'struct Nome {\n  Tipo campo;\n}', 'struct Punto {\n  int x;\n  int y;\n};'),
    'namespace': _keyword_doc('C++ / C#', 'namespace Nome {\n  dichiarazioni\n}', 'namespace Kant { class Editor { }; }'),
    'using': _keyword_doc('C# / C++', 'using Namespace;\nusing Alias = Tipo;', 'using System.Text;'),
    'package': _keyword_doc('Java / Go', 'package nome;', 'package com.example.app;'),
    'fn': _keyword_doc('Rust', 'fn nome(parametro: Tipo) -> Tipo {\n    corpo\n}', 'fn somma(a: i32, b: i32) -> i32 {\n    a + b\n}'),
    'pub': _keyword_doc('Rust', 'pub elemento\npub(crate) elemento', 'pub fn salva() { ... }'),
    'mut': _keyword_doc('Rust', 'let mut nome: Tipo = valore;\n&mut valore', 'let mut count = 0;'),
    'impl': _keyword_doc('Rust', 'impl Tipo {\n    metodi\n}\nimpl Trait for Tipo { ... }', 'impl Utente {\n    fn nome(&self) -> &str { ... }\n}'),
    'match': _keyword_doc('Rust', 'match valore {\n    pattern => espressione,\n    _ => fallback,\n}', 'match stato {\n    Ok(v) => usa(v),\n    Err(e) => log(e),\n}'),
}
# [CST CLOSED] KEYWORD_DOCS


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
            # MOD/CFG (kant/xref.py's own _FILE_LEVEL_TAGS, duplicated here rather than imported —
            # xref.py already imports FROM this module, so the reverse import would be circular):
            # their Name is the file's own path, not a code identifier, so there is no in-file
            # "declaration line" for it to ever match against. Before this guard, virtually any
            # real file starting with a docstring or import line ahead of its first def/class
            # (i.e. most real Python files) failed this check on its own MOD wrapper every single
            # time — not a real signal, just permanent unfixable noise.
            if item.tag not in ('MOD', 'CFG'):
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


def repair_kant_error(text, line, message):
    """Repair one unambiguous header/CLOSED mismatch; return None when judgment is required."""
    lines = text.split('\n')
    if not 1 <= line <= len(lines):
        return None
    raw = lines[line - 1]
    repaired = None

    if 'incoerente con OPEN' in message:
        try:
            tree = parse_kant(text)
        except KantParseError:
            return None

        def nodes(node):
            for item in node.body:
                if isinstance(item, Node):
                    yield item
                    yield from nodes(item)

        node = next(
            (item for item in nodes(tree) if line in (item.category_line, item.tagline_line)), None,
        )
        if node is None:
            return None
        category = line == node.category_line
        match = (CATEGORY_RE if category else TAGLINE_RE).match(raw)
        if match is None:
            return None
        payload = match.group(2 if category else 3)
        old_name = _header_name_part(payload)
        new_payload = node.name + payload[len(old_name):] if old_name else node.name
        start = raw.find(payload, raw.find(']') + 1)
        if start < 0:
            return None
        repaired = raw[:start] + new_payload + raw[start + len(payload):]
        marker = f'[{node.tag} CATEGORY]' if category else f'[{node.tag}]'
        repaired = re.sub(r'\[[^\]]+\]', marker, repaired, count=1)

    elif 'does not match the open element' in message:
        expected = re.search(r'\[(\w+) OPEN\]\s+(.+)$', message)
        current = CLOSED_RE.match(raw)
        if expected is None or current is None:
            return None
        expected_tag, expected_name = expected.groups()
        repaired = re.sub(r'\[\w+\s+CLOSED', f'[{expected_tag} CLOSED', raw, count=1)
        name_start = repaired.find(current.group(3), repaired.find(']') + 1)
        if name_start < 0:
            return None
        repaired = repaired[:name_start] + expected_name + repaired[name_start + len(current.group(3)):]

    elif 'id does not match its OPEN' in message:
        expected = re.search(r'its OPEN \(#([^\)]+)\)', message)
        current = CLOSED_RE.match(raw)
        if expected is None or current is None:
            return None
        expected_uid = expected.group(1)
        repaired = re.sub(
            r'(\[\w+\s+CLOSED)(?:\s+#\S+)?\]',
            lambda match: f'{match.group(1)} #{expected_uid}]', raw, count=1,
        )

    if repaired is None or repaired == raw:
        return None
    lines[line - 1] = repaired
    result = '\n'.join(lines)
    try:
        parse_kant(result)
    except KantParseError as error:
        # A later independent error is allowed: this repair still made deterministic progress.
        if error.line <= line:
            return None
    return result


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
