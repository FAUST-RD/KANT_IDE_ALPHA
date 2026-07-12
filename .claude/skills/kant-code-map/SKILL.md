---
name: kant-code-map
description: Generate or update the KANT structural code map (a KANT_*.md file at the project root) and its tag comments in source code. Only invoke this when the user types the exact command /kant-code-map. Do not invoke it for natural-language phrasing like "update the KANT map" or "generate the map" — wait for the literal command.
---

# KANT Code Map

Usage: `/kant-code-map` — no arguments.

When invoked, for the current project:
1. Create/update `KANT_<project-name>.md` at the project root.
2. Add matching tag comments above each tagged element in source code.

## KANT file structure

`KANT_<project-name>.md` is a real Markdown document, not raw bracket text
dumped into a `.md` file: it needs a title, a `##` section header, and the
map content itself wrapped in a fenced code block (so indentation and line
breaks render exactly as written — Markdown collapses plain indentation and
line breaks outside of code fences).

The map is canonical and has no line numbers — it never goes stale, and
exact positions are found by grepping the tag comment in source (e.g.
`grep -rn "\[FN\] login"`), not by reading a number that can drift.

```
[MOD auth/login.py] — login/logout endpoints
- [CLS] UserManager — creates and authenticates users
-- [FN] login — checks credentials, creates session
-- [FN] logout — invalidates current session
[CFG config/database.yaml] — DB connection settings
```

**Indentation is mandatory, not cosmetic — every element declared inside a
MOD (or inside a CLS) MUST be prefixed with one literal `-` per nesting
level, directly before the tag, followed by a single space (`- [FN] ...` at
depth 1, `-- [FN] ...` at depth 2, and so on). This applies even when the
module is flat (no classes, everything a direct child of MOD): those
elements still get a single `-` prefix under the MOD line. An element with
no `-` prefix, aligned with its own MOD line, is a hierarchy violation, full
stop — there is no such thing as a "top-level sibling" of MOD.**

**A `[CST]` (or `[VAR]`) entry corresponds to exactly one source statement —
not to how many names conceptually belong together.**

- If a single statement defines multiple constants (tuple/multi-target
  assignment), that statement is ONE entry listing all its names.
- If constants are defined in separate statements — even on adjacent lines,
  even conceptually related — each gets its OWN entry. Never merge separate
  statements into one just because they're related.

Same statement → ONE entry:
```python
W, H, P = 800, 500, 80  # field width, height, paddle height
```
```
[CST] W, H, P — field width, height, paddle height
```

Separate statements → THREE entries, even though related:
```python
W = 800  # field width
H = 500  # field height
P = 80   # paddle height
```
```
[CST] W — field width
[CST] H — field height
[CST] P — paddle height
```

Full template — exactly the file structure to write, including the
Markdown syntax itself (`#`, `##`, and the fences are literal characters in
the output file, not formatting of this instruction):

````markdown
# KANT Map — auth-service

## Structural map

```
[MOD auth/login.py] — login/logout endpoints
- [CLS] UserManager — creates and authenticates users
-- [FN] login — checks credentials, creates session
-- [FN] logout — invalidates current session
[CFG config/database.yaml] — DB connection settings
```
````

Fixed tag set — do not add others:

| Tag | Element | Path |
|---|---|---|
| MOD | file/module | yes (relative) |
| CFG | config file | yes (relative) |
| CLS | class | no (inherits MOD) |
| TYP | type/interface | no (inherits MOD) |
| FN | function/method | no (inherits MOD/CLS) |
| CST | constant | no |
| VAR | mutable global/state var | no |
| TST | test | path only if standalone file |

## Line format

`[TAG] Name — description, max 8 words`
`[TAG relative/path]` for MOD/CFG/TST files (the path is the name).

Nesting: one literal `-` per level, directly before the tag, plus a single
space (`- [TAG]` at depth 1, `-- [TAG]` at depth 2). Children inherit the
parent's path/context by position, not by repeating it.

## Rules

- Only run on the exact `/kant-code-map` command — never as a side effect of another task, never from paraphrased natural language.
- Update the existing KANT file in place, never duplicate it.
- Cover all real code files; skip assets, binaries, deps (node_modules, venv, .git, dist, build).
- Order elements as they appear in source (top-to-bottom), not alphabetically.
- The source code is the single source of truth. On any mismatch, regenerate
  the KANT file from the code, never the other way around.
- Before finishing, re-check the output against these most-violated rules:
  the file is genuine Markdown (title, `##` header, map content inside a
  fenced code block); every non-MOD/CFG element has the correct number of
  `-` prefixes for its nesting depth (never flush left with MOD).

## Code comments

Every tagged element is delimited by an opening marker and a closing marker,
paired by tag + name. These markers are structural bookkeeping only — they
never carry a description and never merge with the category line or the
8-word tag line.

**Opening** (immediately above the element), three lines in this fixed order:

1. **Category line** — general explanation of how the element works:
   `[TAG CATEGORY] Name — how it works`. No length cap.
2. **Tag line** — matches the KANT file exactly:
   `[TAG] Name — description, max 8 words`. This line is the grep anchor.
3. **Open marker** — pure boundary start, no description: `[TAG OPEN] Name`

**Closing** (immediately after the element's last line), up to three lines:

1. `[TAG CLOSED] Name`
2. `[TAG INCOMING] Name — data used as input, comma-separated`
3. `[TAG OUTGOING] Name — data produced as output, comma-separated`

Tag and name in OPEN and CLOSED must match, so the exact span of every
element is recoverable by grep alone, even with nesting.

**Nesting must be strictly well-formed**, like balanced brackets: an OPEN's
matching CLOSED must appear before the CLOSED of whatever element contains
it. Crossing spans are invalid and must be fixed, not left for a tool to
guess at.

RIGHT (properly nested):
```
[CLS OPEN] UserManager
- [FN OPEN] login
- [FN CLOSED] login
[CLS CLOSED] UserManager
```

WRONG (crossing spans — the class closes before its own child does):
```
[CLS OPEN] UserManager
- [FN OPEN] login
[CLS CLOSED] UserManager
- [FN CLOSED] login
```

INCOMING/OUTGOING apply only to `FN` and `TST` — units with actual data
flow. `CST`, `VAR`, `TYP`, `CLS`, `MOD`, `CFG` are declarative: skip both
lines entirely for them, don't write empty ones.

For `FN`: INCOMING is parameters plus any external names read (globals,
constants, other functions' return values consumed). OUTGOING is the return
value plus any external state mutated (side effects — DB writes, file I/O,
mutated globals). If an `FN` genuinely has neither (rare — a pure no-arg
function with no return and no side effect), write `none` rather than
omitting the line, so its absence is never mistaken for "forgot to add it."

Example (Python):
```python
# [FN CATEGORY] list_users — paginates using offset = (page-1) * MAX_PAGE_SIZE, capped server-side
# [FN] list_users — GET /users, paginated list
# [FN OPEN] list_users
def list_users(page: int = 1):
    offset = (page - 1) * MAX_PAGE_SIZE
    return db.query(User).limit(MAX_PAGE_SIZE).offset(offset).all()
# [FN CLOSED] list_users
# [FN INCOMING] list_users — page, MAX_PAGE_SIZE
# [FN OUTGOING] list_users — paginated user list

# [FN CATEGORY] create_user — validates payload, hashes password, persists row
# [FN] create_user — POST /users, creates new user
# [FN OPEN] create_user
def create_user(payload: UserCreate):
    ...
# [FN CLOSED] create_user
# [FN INCOMING] create_user — payload
# [FN OUTGOING] create_user — created user record, db write
```

Binding rules:

- Every comment must include the element's name, matching the declaration
  it delimits — unambiguous even out of context.
- The tag line must be byte-identical to the corresponding entry in the
  KANT file (minus indentation), so grep on either one finds the other.
- Every OPEN must have exactly one CLOSED with the same tag and name;
  an unmatched marker is invalid.
- When renaming an FN/TST element, rename it in all seven places in the same
  edit: declaration, category line, tag line, open marker, closed marker,
  incoming line, outgoing line, and the KANT entry (for other tags, skip the
  incoming/outgoing places — five places, as before).
