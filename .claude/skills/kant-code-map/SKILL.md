---
name: kant-code-map
description: Analyze the repository and add/fix KANT tag comments in source code so the IDE can deterministically regenerate the structural code map (a KANT_*.md file at the project root). Only invoke this when the user types the exact command /kant-code-map. Do not invoke it for natural-language phrasing like "update the KANT map" or "generate the map" — wait for the literal command.
---

# KANT Code Map

Usage: `/kant-code-map` — no arguments.

This command runs in two phases. **Phase 1 is not your job to improvise** —
tag, name, nesting, OPEN/CLOSED placement, and `#id` are facts about the
code, computed deterministically by `kant/skeleton.py`, never guessed at by
reading the file. **Phase 2 is the only phase you actually write anything
for**: filling in the two description lines a blank marker leaves behind.
Never do Phase 2's job by re-deriving structure yourself, and never do
Phase 1's job by hand-writing OPEN/CLOSED/CATEGORY-tag/#id — if either phase
looks wrong, that's a bug in `kant/skeleton.py` to report, not something to
silently work around by writing markers yourself.

## Phase 1 — deterministic skeleton (run this, don't write it)

Run this from the project root before touching any source file:

```
python -c "from kant.skeleton import apply_skeleton_to_project; changed, skipped = apply_skeleton_to_project('.'); print(f'{len(changed)} file aggiornati, {len(skipped)} saltati'); [print(f'  {f}: +{n} elementi') for f, n in changed]; [print(f'  SALTATO (marker non validi): {f}') for f in skipped]"
```

This inserts OPEN/CATEGORY/tagline/CLOSED markers for every still-unmarked
module-level construct (class, function, constant, mutable global, test) in
every recognized source file, with the CATEGORY and tagline description left
as an explicit blank (`Name —`, nothing after the dash) — that shape is
exactly what Phase 2 below finds and fills. Elements that already have a
marker (any tag) are left untouched, so this is safe to run on a project
that's already partially tagged. A file whose *existing* markers don't even
parse is skipped, not guessed at — list any `SALTATO` files in your final
summary as needing a manual look (or "Verifica KANT" in the IDE) instead of
attempting to fix their nesting yourself.

Python gets exact tag/name/nesting/span extraction (via the stdlib `ast`
module — no guessing). Every other language uses a tested but heuristic
brace/string-aware scanner, top-level constructs only — nested methods in
non-Python files may still need a tag added by hand via the IDE's own
"+ Aggiungi elemento" dialog, since reliably matching a method to its
containing class by regex alone isn't something this can promise.

## Phase 2 — fill the blanks (this is your actual job)

List every element that still needs a description:

```
python -c "
from kant.projectops import iter_project_text_files
from kant.syntax import audit_kant_headers
for path, text in iter_project_text_files('.'):
    audit = audit_kant_headers(text)
    for w in audit['warnings']:
        if w['message'] in ('CATEGORY mancante', 'CATEGORY vuota', 'tagline mancante', 'tagline vuota'):
            print(f'{path}:{w[\"line\"]}: [{w[\"tag\"]}] {w[\"name\"]} — {w[\"message\"]}')
"
```

For each listed location, edit *only* its CATEGORY line and its tagline —
never the OPEN/CLOSED lines, never the tag, never the `#id`, never anything
about nesting or ordering. Two-part format, and they ask different
questions:

1. **Tagline** (the line with just `[TAG] Name — ...`, max 8 words):
   what that piece of code does — `[FN] list_users — GET /users, paginated list`.
2. **CATEGORY** (no length cap): how it works — the mechanism, not a
   restatement of what it is. `[FN CATEGORY] list_users — paginates using
   offset = (page-1) * MAX_PAGE_SIZE, capped server-side`, not `[FN CATEGORY]
   list_users — a function that lists users`.

Do not create, edit, or hand-compose `KANT_<project-name>.md` yourself —
KANT IDE regenerates that file deterministically from the source markers
after your changes are reviewed and applied. Writing it yourself only
produces a version the IDE immediately overwrites.

## KANT file structure (reference only — the IDE generates this, you don't)

`KANT_<project-name>.md` is a real Markdown document: a title, a `##`
section header, and the map content wrapped in a fenced code block. This is
what the IDE's generator produces from your source markers — shown here so
you understand what your tag comments turn into, not as something to author:

```
[MOD auth/login.py] — login/logout endpoints
- [CLS] UserManager — creates and authenticates users
-- [FN] login — checks credentials, creates session
-- [FN] logout — invalidates current session
[CFG config/database.yaml] — DB connection settings
```

The map is canonical and has no line numbers — it never goes stale, and
exact positions are found by grepping the tag comment in source (e.g.
`grep -rn "\[FN\] login"`), not by reading a number that can drift.

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

## Rules

- Only run on the exact `/kant-code-map` command — never as a side effect of another task, never from paraphrased natural language.
- Phase 1 first, always — never write a CATEGORY/tagline for an element that doesn't have OPEN/CLOSED yet; run the skeleton command instead of hand-adding the marker.
- Phase 2 touches CATEGORY/tagline text only. Never OPEN/CLOSED, never the tag, never `#id`, never reorders or renames anything.
- There is no INCOMING/OUTGOING line to write. Who calls/uses an element and what it calls/uses is computed deterministically from the code by KANT IDE's cross-reference system, not hand-written here. Never add `[TAG INCOMING]`/`[TAG OUTGOING]` lines.
- Cover all real code files; skip assets, binaries, deps (node_modules, venv, .git, dist, build) — Phase 1's command already does this for you.
- The source code is the single source of truth. The generated map always reflects it — never edit the map to "fix" a mismatch, fix or add the source markers instead.
- Before finishing, confirm every location Phase 2 listed now has real text (not `Name —` still empty), and list any Phase-1 `SALTATO` files in your summary.
