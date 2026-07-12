---
name: kant-comment-standard
description: Keep KANT tag comments intact and correctly formed in any code you write or edit in this project — modules, classes, types, functions, constants, mutable globals, tests, config files. Always active for code changes in this repository, not just when explicitly asked.
---

# KANT comment standard

This project's source files are annotated with KANT tag comments — structural
markers that a companion tool (`kant_editor.py`) parses to render the code as
a navigable, foldable outline. Whenever you add or modify code here, keep
every element correctly tagged. Do not leave new functions, classes,
constants, or files untagged in a codebase that already uses this convention.

## Fixed tag set

| Tag | Element |
|---|---|
| MOD | file/module |
| CFG | config file |
| CLS | class |
| TYP | type/interface |
| FN | function/method |
| CST | constant |
| VAR | mutable global/state var |
| TST | test |

## Comment shape

Every tagged element is delimited by an opening block (immediately above it)
and a closing block (immediately after its last line). These markers are
structural bookkeeping — they never carry a description themselves and never
merge with each other.

**Opening**, three lines in this fixed order:
1. Category line — how it works, no length cap: `[TAG CATEGORY] Name — how it works`
2. Tag line — max 8 words, this is the grep anchor: `[TAG] Name — description`
3. Open marker — no description: `[TAG OPEN] Name`

**Closing**, one line:
1. `[TAG CLOSED] Name`

There is no INCOMING/OUTGOING line to write. Who calls/uses an element and
what it calls/uses is computed deterministically from the code by KANT IDE's
cross-reference system, not hand-written here — a stale or wrong data-flow
comment is worse than none, and half of it just duplicated the signature one
line below anyway. Never add `[TAG INCOMING]`/`[TAG OUTGOING]` lines to new
or edited code.

Example (Python):
```python
# [FN CATEGORY] list_users — paginates using offset = (page-1) * MAX_PAGE_SIZE, capped server-side
# [FN] list_users — GET /users, paginated list
# [FN OPEN] list_users
def list_users(page: int = 1):
    offset = (page - 1) * MAX_PAGE_SIZE
    return db.query(User).limit(MAX_PAGE_SIZE).offset(offset).all()
# [FN CLOSED] list_users
```

A `[CST]`/`[VAR]` entry corresponds to one source statement, not to how many
names conceptually belong together — a tuple/multi-target assignment is one
entry listing all its names; separate statements each get their own entry
even when related and adjacent.

## Binding rules

- Every comment includes the element's name, matching the declaration it
  delimits — unambiguous even out of context.
- The tag line matches the corresponding KANT map entry (if this project has
  a `KANT_*.md`) byte-for-byte, minus indentation.
- Every OPEN has exactly one matching CLOSED (same tag + name) — an
  unmatched or mismatched marker is a hard parse error, not something the
  tool recovers from silently.
- Nesting is strict LIFO: a CLOSED always closes the most recently opened
  element. Never leave markers in an order that would cross spans (closing
  an outer element before an inner one still open) — move/reorder code as
  whole OPEN…CLOSED blocks, never split one apart from its matching marker.
- When renaming an element, rename it everywhere in the same edit:
  declaration, category line, tag line, open marker, and closed marker.
- New files in an already-KANT-tagged project get a top-level `[MOD]` (or
  `[CFG]`/`[TST]`) wrapping the whole file, per the pattern already used by
  sibling files in the same project.

## When this doesn't apply

If the project has no existing KANT tags anywhere (a fresh, untagged
codebase), don't retrofit the whole thing unasked — just don't break an
existing convention if one is already present. Never invent a different tag
than the eight above.
