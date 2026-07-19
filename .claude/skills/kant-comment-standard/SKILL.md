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

**Tag, name, nesting, OPEN/CLOSED placement, and `#id` are not yours to
decide.** `kant/skeleton.py` derives all of that deterministically from the
code itself — exactly (via Python's own `ast` module) for `.py` files, by a
tested heuristic scanner for other languages. Your job after writing or
editing code is: run the tool below on the file(s) you touched, then write
the two description lines it leaves blank. Never hand-author an
OPEN/CLOSED/CATEGORY-tag/`#id` block yourself unless the tool genuinely can't
help (see "When the tool can't run" below) — that's exactly the class of
mistake (wrong tag, mismatched name, bad nesting, ID collision) this process
exists to remove.

## After writing or editing code in a tagged file

Run this once per file you touched, with its real path:

```
python -c "
from pathlib import Path
from kant.skeleton import apply_skeleton
path = 'PATH/TO/FILE'
text = Path(path).read_text(encoding='utf-8')
result = apply_skeleton(text, path)
if result:
    new_text, count = result
    Path(path).write_text(new_text, encoding='utf-8')
    print(f'{count} elementi taggati')
else:
    print('nulla da taggare')
"
```

Then list what still needs a description in that same file:

```
python -c "
from pathlib import Path
from kant.syntax import audit_kant_headers
text = Path('PATH/TO/FILE').read_text(encoding='utf-8')
for w in audit_kant_headers(text)['warnings']:
    if w['message'] in ('CATEGORY mancante', 'CATEGORY vuota', 'tagline mancante', 'tagline vuota'):
        print(f'{w[\"line\"]}: [{w[\"tag\"]}] {w[\"name\"]} — {w[\"message\"]}')
"
```

For each location listed, fill in only its CATEGORY line and its tagline —
two different questions, never merged into one:

1. **Tagline** (`[TAG] Name — ...`, max 8 words): what that piece of code does.
2. **CATEGORY** (no length cap): how it works — the mechanism, not a
   restatement of what it is.

```python
# [FN CATEGORY] list_users — paginates using offset = (page-1) * MAX_PAGE_SIZE, capped server-side
# [FN] list_users — GET /users, paginated list
# [FN OPEN] list_users
def list_users(page: int = 1):
    offset = (page - 1) * MAX_PAGE_SIZE
    return db.query(User).limit(MAX_PAGE_SIZE).offset(offset).all()
# [FN CLOSED] list_users
```

Never touch OPEN/CLOSED, the tag, `#id`, or nesting while doing this — those
lines are already correct once the tool has run; rewriting them yourself is
exactly the risk this whole process removes.

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

There is no INCOMING/OUTGOING line to write. Who calls/uses an element and
what it calls/uses is computed deterministically from the code by KANT IDE's
cross-reference system, not hand-written here — a stale or wrong data-flow
comment is worse than none, and half of it just duplicated the signature one
line below anyway. Never add `[TAG INCOMING]`/`[TAG OUTGOING]` lines to new
or edited code.

## When the tool can't run

Two situations mean you're back to hand-authoring markers, the way this
skill used to work for everything:

- **The language isn't one `kant/skeleton.py` recognizes**, or the construct
  is nested in a way its top-level-only scanner won't reach (a method inside
  a class, in a non-Python file). Check with the IDE's own "+ Aggiungi
  elemento" dialog first if you're working inside `kant_editor.py`'s UI —
  otherwise, write the block by hand, matching the shape below exactly.
- **The file's existing markers don't parse** (`apply_skeleton` above prints
  nothing and the audit step raises, or reports a hard error rather than a
  warning). Don't add more structure on top of a file that's already broken
  — fix the existing mismatch first (or point the user at "Verifica KANT").

Hand-written shape, when you must — three lines opening, one line closing:

**Opening**, in this fixed order:
1. Category line — how it works, no length cap: `[TAG CATEGORY] Name — how it works`
2. Tag line — max 8 words, this is the grep anchor: `[TAG] Name — description`
3. Open marker — no description: `[TAG OPEN] Name`

**Closing**, one line:
1. `[TAG CLOSED] Name`

A `[CST]`/`[VAR]` entry corresponds to one source statement, not to how many
names conceptually belong together — a tuple/multi-target assignment is one
entry listing all its names; separate statements each get their own entry
even when related and adjacent.

Binding rules for hand-written markers:
- Every comment includes the element's name, matching the declaration it
  delimits — unambiguous even out of context.
- The tag line matches the corresponding KANT map entry (if this project has
  a `KANT_*.md`) byte-for-byte, minus indentation.
- Every OPEN has exactly one matching CLOSED (same tag + name) — an
  unmatched or mismatched marker is a hard parse error, not something the
  tool recovers from silently.
- Nesting is strict LIFO: a CLOSED always closes the most recently opened
  element. Never leave markers in an order that would cross spans.
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
