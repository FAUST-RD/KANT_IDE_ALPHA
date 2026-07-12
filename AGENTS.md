# Agent Guide

Read `PROJECT_MAP.md` first. It is the authoritative routing table for this repository; use `DESIGN.md` only when a change touches an architectural invariant.

## Navigation

1. Search for the public class/function named in `PROJECT_MAP.md`.
2. In large files, search for `AI-NAV`, `[FN CATEGORY]`, or the nearest `# ----` region heading.
3. Trace callers before editing shared parser, save, workspace, or xref code.
4. Ignore `index.html` unless the task explicitly targets the legacy browser prototype.

## Boundaries

- Pure behavior belongs in `model.py`, `fileio.py`, `syntax.py`, `xref.py`, `gitutil.py`, or `projectops.py`.
- Stateful filesystem and AI-review lifecycle belongs in `workspace.py`.
- Reusable Qt components belong in `widgets.py`; application orchestration belongs in `mainwindow.py`.
- Keep `kant_editor.py` a thin entry point and compatibility surface.

## Invariants

- Parsing and serialization must round-trip untouched source text.
- Writes that replace project files must remain atomic.
- Workspace paths must stay below the selected project root; AI snapshots must remain rollback-safe.
- Cross-references are deterministic text analysis, not LSP or AI output.
- Theme colors are read through `kant.theme` at use time because `set_theme()` rebinds globals.
- A legacy KANT file without persisted `#id` markers may receive new UIDs on reparse; navigation must retain its document-order fallback.

## Verification

For documentation-only edits, compile the package. For behavior changes, run the offscreen smoke test:

```powershell
python -m compileall -q kant kant_editor.py test_kant_smoke.py
$env:QT_QPA_PLATFORM='offscreen'
python test_kant_smoke.py
```

Do not add a new test framework: `test_kant_smoke.py` is the runnable regression check.
