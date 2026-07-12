"""KANT source model and its round-trip boundary.

``parse_kant`` converts source into an ordered ``Node``/``Run`` tree; editors mutate only ``Run``
text; ``serialize_kant`` reconstructs the file. Preserve ordering and raw marker lines unless the
user explicitly edits metadata. Missing marker IDs are stamped during parsing, so callers that
reparse legacy files must retain a document-order navigation fallback.
"""
import os
import re
import secrets
from dataclasses import dataclass, field


# [FN CATEGORY] parse_kant — single pass over source lines with an explicit stack: every OPEN
# pushes a frame, every CLOSED pops the top frame and asserts it matches (tag, name, and — once a
# marker carries one — #id). No backward search across the stack: a CLOSED always closes exactly
# the most recently opened frame, which is what makes nesting strictly LIFO (no crossing spans). A
# mismatch, or a CLOSED with nothing open to close, is a hard KantParseError with the offending line
# number — silent best-effort recovery is exactly what produced ambiguous trees before.
# [FN] parse_kant — parses KANT-tagged source into a section tree
# [FN OPEN] parse_kant
MARKER_PREFIX = r'^\s*(?:(?:#|//|--|;|/\*+|\*)\s*|<!--\s*)'
MARKER_SUFFIX = r'\s*(?:\*/|-->)?\s*$'
OPEN_RE = re.compile(MARKER_PREFIX + r'\[(\w+)\s+OPEN(?:\s+#(\S+))?\]\s+(\S.*?)' + MARKER_SUFFIX)
CLOSED_RE = re.compile(MARKER_PREFIX + r'\[(\w+)\s+CLOSED(?:\s+#(\S+))?\]\s+(\S.*?)' + MARKER_SUFFIX)
CATEGORY_RE = re.compile(MARKER_PREFIX + r'\[(\w+)\s+CATEGORY\]\s+(\S.*?)' + MARKER_SUFFIX)
TAGLINE_RE = re.compile(
    MARKER_PREFIX + r'\[(\w+)(?:\s+(?!OPEN\b|CLOSED\b|CATEGORY\b|INCOMING\b|OUTGOING\b)([^\]]+))?\]\s+(\S.*?)' + MARKER_SUFFIX
)
# legacy: [TAG INCOMING/OUTGOING] Name — data, comma-separated. Dropped from the convention —
# kept only so old files that still have these round-trip losslessly.
IO_RE = re.compile(MARKER_PREFIX + r'\[(\w+)\s+(INCOMING|OUTGOING)\]\s+(\S+)\s*(?:—\s*(.*?))?' + MARKER_SUFFIX)

# marks the exact spot an id-less OPEN/CLOSED gets a freshly generated #id stamped into its raw line
_ID_INSERT_RE = re.compile(r'\[(\w+)\s+(OPEN|CLOSED)\]')


def _short_desc(text):
    text = (text or '').strip()
    for sep in (' — ', ' - ', ' -- ', ': '):
        if sep in text:
            return text.split(sep, 1)[1].strip() or text
    if text.startswith(('—', '-')):
        return text[1:].strip() or text
    return text


class KantParseError(Exception):
    """Malformed OPEN/CLOSED nesting or a stale/mismatched #id. Carries line numbers so the UI
    can point at the exact spot instead of failing generically."""

    def __init__(self, message, line, open_line=None):
        location = f'line {line}' if open_line is None else f'line {line} (opened at line {open_line})'
        super().__init__(f'{location}: {message}')
        self.message = message
        self.line = line
        self.open_line = open_line


@dataclass
class Run:
    lines: list


@dataclass
class Node:
    tag: str
    name: str
    open_raw: str
    closed_raw: str = None
    category_raw: str = None
    tag_raw: str = None
    desc: str = ''
    category_desc: str = None
    body: list = field(default_factory=list)  # list[Run | Node]
    uid: str = None  # the KANT `#id` — read off the source marker, or generated once (by
    # _assign_uids) and stamped back into open_raw/closed_raw for legacy files that predate it.
    # Stable across parses/saves — never regenerate one that already exists. Doubles as the
    # widget-lookup key (dict keys, Qt item-data role) since a string works fine for both.
    # legacy fields — INCOMING/OUTGOING comment lines were dropped from the convention: a
    # hand-written, unverified data-flow comment can drift from what the code does. Still parsed
    # here so old files with them round-trip losslessly, but nothing writes new ones and the
    # Incoming/Outgoing panel no longer reads them — it uses the deterministic cross-reference
    # graph (kant/xref.py) instead.
    incoming_raw: str = None
    outgoing_raw: str = None
    incoming: str = None  # data used as input (FN/TST only) — comma-separated, from IO_RE
    outgoing: str = None  # data produced as output (FN/TST only) — comma-separated, from IO_RE


def parse_kant(source: str) -> Node:
    root = Node(tag='ROOT', name='', open_raw=None)
    stack = [root]
    open_lines = [0]  # lockstep with `stack`, for error messages
    pending_category = pending_tag = None  # (raw_line, tag, text)
    last_closed = None  # node closed by the immediately preceding line, for INCOMING/OUTGOING

    def current_body():
        return stack[-1].body

    def push_line(text):
        body = current_body()
        if body and isinstance(body[-1], Run):
            body[-1].lines.append(text)
        else:
            body.append(Run(lines=[text]))

    lines = source.split('\n')
    for line_no, line in enumerate(lines, start=1):
        m = CATEGORY_RE.match(line)
        if m:
            if pending_category:  # unresolved header line, never reached an OPEN — keep it as text
                push_line(pending_category[0])
            pending_category = (line, m.group(1), m.group(2))
            last_closed = None
            continue
        m = OPEN_RE.match(line)
        if m:
            tag, id_, name = m.group(1), m.group(2), m.group(3)
            if pending_tag:
                desc = _short_desc(pending_tag[2])
            elif pending_category:
                desc = _short_desc(pending_category[2])
            else:
                desc = _short_desc(name)
            node = Node(
                tag=tag, name=name, open_raw=line,
                category_raw=pending_category[0] if pending_category else None,
                tag_raw=pending_tag[0] if pending_tag else None,
                desc=desc,
                category_desc=_short_desc(pending_category[2]) if pending_category else None,
                uid=id_,
            )
            pending_category = pending_tag = None
            current_body().append(node)
            stack.append(node)
            open_lines.append(line_no)
            last_closed = None
            continue
        m = CLOSED_RE.match(line)
        if m:
            tag, id_, name = m.group(1), m.group(2), m.group(3)
            # flush any header line that never resolved into an OPEN, before popping the stack,
            # so it stays inside the node being closed instead of landing after it once popped
            if pending_category:
                push_line(pending_category[0]); pending_category = None
            if pending_tag:
                push_line(pending_tag[0]); pending_tag = None
            last_closed = None
            if len(stack) <= 1:
                raise KantParseError(f'[{tag} CLOSED] {name} has no matching OPEN', line_no)
            top = stack[-1]
            if top.tag != tag or top.name != name:
                raise KantParseError(
                    f'[{tag} CLOSED] {name} does not match the open element '
                    f'[{top.tag} OPEN] {top.name}', line_no, open_lines[-1],
                )
            if (top.uid is not None or id_ is not None) and top.uid != id_:
                raise KantParseError(
                    f'[{tag} CLOSED{" #" + id_ if id_ else ""}] {name} id does not match '
                    f'its OPEN (#{top.uid})', line_no, open_lines[-1],
                )
            top.closed_raw = line
            last_closed = top
            stack.pop()
            open_lines.pop()
            continue
        m = IO_RE.match(line)
        if m:
            tag, kind, name, data = m.group(1), m.group(2), m.group(3), m.group(4)
            if last_closed is not None and last_closed.tag == tag and last_closed.name == name:
                if kind == 'INCOMING':
                    last_closed.incoming_raw = line
                    last_closed.incoming = (data or '').strip()
                else:
                    last_closed.outgoing_raw = line
                    last_closed.outgoing = (data or '').strip()
                    last_closed = None  # nothing else is expected after OUTGOING
                continue
            last_closed = None  # mismatched marker — falls through and is treated as plain text
        m = TAGLINE_RE.match(line)
        if m:
            if pending_tag:  # unresolved header line, never reached an OPEN — keep it as text
                push_line(pending_tag[0])
            pending_tag = (line, m.group(1), m.group(3))
            last_closed = None
            continue
        # flush any tentative header lines that never resolved into an OPEN (plain comment lines)
        if pending_category:
            push_line(pending_category[0]); pending_category = None
        if pending_tag:
            push_line(pending_tag[0]); pending_tag = None
        push_line(line)
        last_closed = None
    if pending_category:
        push_line(pending_category[0])
    if pending_tag:
        push_line(pending_tag[0])
    if len(stack) > 1:
        unclosed = stack[-1]
        raise KantParseError(
            f'[{unclosed.tag} OPEN] {unclosed.name} was never closed', len(lines), open_lines[-1],
        )
    _assign_uids(root)
    return root
# [FN CLOSED] parse_kant


# [FN CATEGORY] _assign_uids — two passes over a freshly parsed tree: first collect every #id
# already present in the source, then stamp a freshly generated one onto any OPEN/CLOSED pair that
# doesn't have one yet (a legacy file predating this convention). A generated id is written straight
# into open_raw/closed_raw so serialize_kant carries it back to disk on the next save — held only in
# memory, it would just regenerate a different id every time the file is reopened.
# [FN] _assign_uids — reads existing #ids and generates+stamps missing ones
# [FN OPEN] _assign_uids
def _new_id(existing):
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in existing:
            return candidate


def _stamp_id(raw_line, new_id):
    return _ID_INSERT_RE.sub(lambda m: f'[{m.group(1)} {m.group(2)} #{new_id}]', raw_line, count=1)


def _assign_uids(root):
    existing = set()

    def collect(node):
        for item in node.body:
            if isinstance(item, Node):
                if item.uid is not None:
                    existing.add(item.uid)
                collect(item)

    collect(root)

    def assign(node):
        for item in node.body:
            if isinstance(item, Node):
                if item.uid is None:
                    new_id = _new_id(existing)
                    existing.add(new_id)
                    item.uid = new_id
                    item.open_raw = _stamp_id(item.open_raw, new_id)
                    if item.closed_raw:
                        item.closed_raw = _stamp_id(item.closed_raw, new_id)
                assign(item)

    assign(root)
# [FN CLOSED] _assign_uids


# [FN CATEGORY] serialize_kant — walks the tree in original order, using edited run text where
# present, to reconstruct the full source text byte-for-byte. Marker lines are never rebuilt from
# (tag, name, uid) — they're emitted verbatim from open_raw/closed_raw, which already carry
# whatever #id they had (or were stamped with) at parse time, so an id is never regenerated across
# a rename or move.
# [FN] serialize_kant — reconstructs full source text from a (possibly edited) tree
# [FN OPEN] serialize_kant
def serialize_kant(node: Node) -> str:
    out = []
    for item in node.body:
        if isinstance(item, Run):
            out.append('\n'.join(item.lines))
        else:
            if item.open_raw is None:
                # ponytail: no caller builds a Node with no open_raw today (structural "create
                # section" is future work) — this just keeps such a Node serializable once that
                # caller exists. The comment-leader style isn't known at this layer, so the marker
                # is emitted bare; create-section should supply a real, language-correct raw line
                # instead of relying on this fallback.
                if item.uid is None:
                    item.uid = secrets.token_hex(4)
                item.open_raw = f'[{item.tag} OPEN #{item.uid}] {item.name}'
                item.closed_raw = f'[{item.tag} CLOSED #{item.uid}] {item.name}'
            if item.category_raw:
                out.append(item.category_raw)
            if item.tag_raw:
                out.append(item.tag_raw)
            out.append(item.open_raw)
            out.append(serialize_kant(item))
            if item.closed_raw:
                out.append(item.closed_raw)
            if item.incoming_raw:
                out.append(item.incoming_raw)
            if item.outgoing_raw:
                out.append(item.outgoing_raw)
    return '\n'.join(out)
# [FN CLOSED] serialize_kant


# [CST] _label_cache — abspath -> ((mtime_ns, size), result) for read_top_level_label_result.
# Every caller (project tree rebuild, xref rebuild, has_any_kant_tags, the KANT map) re-scans every
# project file on its own schedule; without this, a large project re-reads and re-parses every
# unchanged file's full text on each of those, repeatedly. Keyed on mtime+size so an edited file is
# always re-read, never on content hash (a stat() is one syscall; hashing still means reading the
# whole file, which is exactly the cost this exists to avoid).
_label_cache = {}


# [FN CATEGORY] read_top_level_label — reads a file and parses it just to find its first top-level
# KANT node (the file's MOD/CFG/TST), so the project tree can label the file by convention instead
# of by filename. Returns None for files with no KANT tags, that fail to decode as text, or whose
# markers are malformed (KantParseError) — those are left out of the tree entirely rather than
# crashing the whole project scan over one bad file. Skips the read+reparse entirely when the
# file's (mtime, size) matches what was last cached for it.
# [FN] read_top_level_label — extracts a file's top-level tag+desc and its parsed tree
# [FN OPEN] read_top_level_label
def read_top_level_label_result(path):
    abspath = os.path.abspath(path)
    try:
        stat = os.stat(path)
    except OSError:
        _label_cache.pop(abspath, None)
        return None, None
    stat_key = (stat.st_mtime_ns, stat.st_size)
    cached = _label_cache.get(abspath)
    if cached is not None and cached[0] == stat_key:
        return cached[1]

    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        result = (None, None)
        _label_cache[abspath] = (stat_key, result)
        return result
    try:
        tree = parse_kant(text)
    except KantParseError as e:
        result = (None, e)
        _label_cache[abspath] = (stat_key, result)
        return result
    top = next((c for c in tree.body if isinstance(c, Node)), None)
    result = (None, None) if top is None else ((top.tag, (top.desc or top.name), tree, top), None)
    _label_cache[abspath] = (stat_key, result)
    return result


def read_top_level_label(path):
    label, _error = read_top_level_label_result(path)
    return label
# [FN CLOSED] read_top_level_label
