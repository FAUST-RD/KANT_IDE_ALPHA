# KANT IDE Design

Use `PROJECT_MAP.md` to locate code. This document explains the invariants and decisions that should survive refactors.

## Purpose

KANT IDE is a desktop editor for source files containing structural comment markers:

```text
[TAG CATEGORY] explanatory text
[TAG] short label
[TAG OPEN #stable-id] name
...
[TAG CLOSED #stable-id] name
```

The editor turns those markers into an outline, editable section views, Incoming/Outgoing lists, and a project graph. The Python/PySide6 application is current; `index.html` is a legacy prototype.

## Architecture

The code is a small dependency DAG:

```text
entry point
  -> application orchestration
     -> Qt components and workspace lifecycle
        -> deterministic services
```

- Deterministic services (`model`, `fileio`, `syntax`, `xref`, `gitutil`, `projectops`) do not depend on application UI state.
- `workspace.py` is the filesystem trust boundary and is mixed into `MainWindow`.
- `widgets.py` owns reusable Qt components and exposes signals/callbacks.
- `mainwindow.py` owns application state, connects components, schedules background work, and invalidates derived state.
- `kant_editor.py` starts the app and retains compatibility re-exports; feature code does not belong there.

Do not create new layers unless code has a second real consumer. The existing split is by responsibility, not by one-class-per-file.

## Source round trip

The central data flow is deterministic:

```text
source text -> parse_kant -> Node/Run tree -> edit Run.text -> serialize_kant -> atomic replace
```

Required invariants:

- Untouched source round-trips byte-for-byte apart from intentionally stamped missing `#id` markers.
- KANT nesting is strict LIFO. Mismatched, crossing, or unclosed markers are errors, not best-effort recovery.
- Marker raw text and ordering remain authoritative for serialization.
- A legacy file without stable IDs can receive new UIDs on each parse. Tree and xref navigation therefore fall back to pre-order document position when a stored UID misses.
- `FileTab` owns dirty/undo/autosave state; `MainWindow` renders and routes edits but does not duplicate the source model.

## Filesystem and AI safety

Edits made by an external AI process are a transaction:

```text
snapshot -> run agent -> remove unsafe symlinks -> build review -> apply selection | rollback
```

Required invariants:

- File replacement is atomic and preserves an existing file's mode when possible.
- User-provided relative paths are resolved with `safe_project_path` and cannot escape the project root.
- Snapshots exclude symlinks. Symlinks created by an agent are removed before review or rollback.
- Cancel restores the complete snapshot; partial review reconstructs accepted hunks deterministically with `difflib`.
- Snapshot metadata survives a crash so startup recovery can finish or roll back the interrupted transaction.
- Permission automation never bypasses final change review.

## Derived state and invalidation

The project tree, syntax status, Git badges, generated KANT map, and xref graph are derived from disk or open-tab state.

- `MainWindow` owns the xref cache and generation counter. Save, external changes, project switch, and relevant tab operations invalidate it.
- Expensive project scans run through `_run_background`; Qt objects are updated only in the completion callback.
- Filesystem watcher events distinguish clean tabs (reload) from dirty tabs (explicit conflict decision).
- Theme values are mutable module globals. Consumers read `theme.<NAME>` at use time; importing individual colors would make runtime theme changes stale.

## Cross-references and MAPPA

`build_xref` is deliberately heuristic and deterministic. It tokenizes each KANT element's own code, ignores strings/comments, and creates edges when identifier tokens match other element names. It does not use an LSP, language grammar, or AI.

`XrefMapDialog` transforms the full graph into the displayed subset by applying tag/file/search/collapse/isolation or drill-down state. `XrefMapView` draws that subset and owns camera interaction, pins, edge popups, and layout.

Layout uses directed module ranks followed by local forces. It supports left-to-right and right-to-left seeds. Persisted coordinates are fixed until `Riorganizza`; ordinary filtering preserves the camera where possible. Drill-down excludes the parent from the scene, shows only its direct children and mutual references, and represents the parent as a fixed viewport title card.

## LSP and local fallback

`LspClient` implements only the transport needed by the IDE: process startup, JSON-RPC framing, document versions, requests, and diagnostics. `MainWindow` owns UI actions, applies returned edits, and uses deterministic local symbol operations when no supported server is available.

## Commenting convention

Comments should help future contributors and coding agents decide where to look or why an invariant exists.

- Module docstrings state ownership, boundaries, and the order of major regions.
- In large modules, `AI-NAV` or `# ----` headings are stable search anchors.
- `[FN CATEGORY]` explains non-obvious intent or coupling; `[FN]` is the short searchable label; `[FN OPEN/CLOSED]` delimits the span consumed by KANT tooling.
- Do not duplicate marker sets for one function.
- Do not narrate obvious assignments or Qt plumbing. Prefer caller/callee names and invalidation rules over prose that merely restates the next line.
- A deliberate simplification with a known ceiling uses a `ponytail:` comment naming both the ceiling and the upgrade trigger.

## Verification strategy

There is one runnable offscreen regression check rather than a large test hierarchy:

```powershell
python -m compileall -q kant kant_editor.py test_kant_smoke.py
$env:QT_QPA_PLATFORM='offscreen'
python test_kant_smoke.py
```

Add the smallest assertion that fails for the behavior being changed. Parser, persistence, security, and non-trivial graph logic require a check; documentation and trivial presentation changes only require compilation.
