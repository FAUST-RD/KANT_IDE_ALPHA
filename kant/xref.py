"""Deterministic cross-reference index over KANT-tagged elements.

Answers "who references this element, and what does this element reference" for every tagged
element in the project (FN, CLS, CST, VAR, TYP, TST — anything with a KANT marker), by pure
text analysis of the parsed trees: an element's own code lines are tokenized with the same
TOKEN_RE the highlighter and check_syntax use (so comments and strings are opaque), and every
identifier token that matches another element's name becomes a directed edge. No AI, no
language server, no per-language grammar — same input always produces the same graph.
"""
import re
from dataclasses import dataclass, field

from kant.model import Node, Run, _short_desc
from kant.syntax import TOKEN_RE

# [CST] _NAME_ID_RE — identifier extractor for element names ("W, H, P" → W, H, P)
_NAME_ID_RE = re.compile(r'[A-Za-z_]\w*')


# [CST] _FILE_LEVEL_TAGS — tags whose Name is a file path, not a code identifier; they are
# indexed as graph nodes (so the map shows modules) but their names are never matched against
# code tokens ("a.py" would otherwise index the identifiers "a" and "py" and produce junk edges)
_FILE_LEVEL_TAGS = {'MOD', 'CFG'}


# [TYP] XrefElement — one KANT-tagged element in the cross-reference graph
# [TYP OPEN] XrefElement
@dataclass
class XrefElement:
    key: str    # '<rel_path>::<uid>' — globally unique across the project
    uid: str
    tag: str
    name: str   # the raw code identifier(s) — used only for reference matching, not display
    desc: str   # short tag-line description — what the left project tree shows, and the map label
    file: str   # project-relative path, '/' separators
    order: int  # document order within its file, for stable map layout
    category_desc: str = ''  # long [TAG CATEGORY] explanation, shown as the map node's hover tooltip
    outgoing: list = field(default_factory=list)  # keys of elements this one references
    incoming: list = field(default_factory=list)  # keys of elements that reference this one
# [TYP CLOSED] XrefElement


def _walk_nodes(node):
    for item in node.body:
        if isinstance(item, Node):
            yield item
            yield from _walk_nodes(item)


def _own_code(node):
    """The element's own code lines — Runs directly in its body. Lines of nested tagged
    children belong to the children, so a CLS doesn't inherit every reference its methods make."""
    return '\n'.join('\n'.join(item.lines) for item in node.body if isinstance(item, Run))


# [FN CATEGORY] build_xref — two passes over the parsed trees: first index every tagged element
# by each identifier in its Name (a multi-constant entry like "W, H, P" indexes all three), then
# tokenize each element's own code with TOKEN_RE — skipping comment/string tokens so a name
# mentioned in prose never counts — and add an edge for every identifier token that resolves to
# another element. Resolution is name-based, not scope-resolved: if two elements share a name
# (two classes with a "process" method), a reference links to all of them.
# ponytail: name-based matching, no semantic resolution — deterministic and language-agnostic,
# at the cost of ambiguity on duplicate names; scope-aware resolution would need per-language
# parsing, which is exactly what this module exists to avoid.
# [FN] build_xref — builds the project cross-reference graph from parsed KANT trees
# [FN OPEN] build_xref
def build_xref(trees):
    elements = {}
    name_index = {}  # identifier -> [element keys]

    for rel_path, root in trees.items():
        for order, node in enumerate(_walk_nodes(root)):
            key = f'{rel_path}::{node.uid}'
            elements[key] = XrefElement(
                key=key, uid=node.uid, tag=node.tag, name=node.name,
                desc=node.desc or node.name, file=rel_path, order=order,
                category_desc=node.category_desc or '',
            )
            if node.tag not in _FILE_LEVEL_TAGS:
                # node.name may inline "Name — description" on the OPEN line itself; strip that
                # the same way desc already is, or every word of the description becomes a
                # phantom identifier and produces false-positive edges
                for ident in _NAME_ID_RE.findall(_short_desc(node.name)):
                    name_index.setdefault(ident, []).append(key)

    for rel_path, root in trees.items():
        for node in _walk_nodes(root):
            source_key = f'{rel_path}::{node.uid}'
            source = elements[source_key]
            seen = set()
            for m in TOKEN_RE.finditer(_own_code(node)):
                comment, block_comment, string, _number, word = m.groups()
                if comment or block_comment or string or not word:
                    continue
                for target_key in name_index.get(word, ()):
                    if target_key == source_key or target_key in seen:
                        continue
                    seen.add(target_key)
                    source.outgoing.append(target_key)
                    elements[target_key].incoming.append(source_key)

    return elements
# [FN CLOSED] build_xref
