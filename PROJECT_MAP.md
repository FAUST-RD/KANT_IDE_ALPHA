# KANT IDE Project Map

This is the contributor's navigation index: use it to find where a change belongs and which neighboring code must be checked. Architectural rationale lives in `DESIGN.md`; AI-specific working rules live in `AGENTS.md`.

## Runtime path

```text
kant_editor.py
  -> MainWindow (mainwindow.py)
     -> reusable UI and file tabs (widgets.py)
     -> workspace mutation/recovery (workspace.py)
     -> deterministic services (model.py, syntax.py, xref.py, projectops.py, ...)
```

The dependency direction is toward the deterministic service modules. `mainwindow.py` orchestrates them; service modules must not import the main window.

## Change routing

| Change | Start here | Then inspect |
| --- | --- | --- |
| KANT marker parsing, IDs, round-trip serialization | `model.py`: `parse_kant`, `_assign_uids`, `serialize_kant` | parser assertions in `kant_editor.py`; smoke cases in `test_kant_smoke.py` |
| Atomic writes, fingerprints, filename safety | `fileio.py` | save callers in `widgets.py` and `workspace.py` |
| Local syntax checks or run commands | `syntax.py` | status/run orchestration in `mainwindow.py` |
| Project search, replace scan, map generation, validation | `projectops.py` | UI callbacks in `mainwindow.py` |
| Incoming/Outgoing reference detection | `xref.py`: `build_xref` | cache and panels in `mainwindow.py` |
| MAPPA layout, filters, drill-down, graphics | `widgets.py`: `_force_layout_positions`, `XrefMapView`, `XrefMapDialog` | graph creation in `xref.py`; dialog opening in `mainwindow.py` |
| Editor blocks, tabs, autosave, undo/redo | `widgets.py`: `CodeEdit`, section widgets, `FileTab` | rendering and callbacks in `mainwindow.py` |
| Project tree, active tabs, rendering, menus, Git/LSP routing | `mainwindow.py`: `MainWindow` | the service module called by the relevant method |
| Snapshots, review/apply/rollback, file watching, create/rename/trash | `workspace.py` | review UI in `widgets.py`; orchestration in `mainwindow.py` |
| Claude/Codex process and inline review UI | `widgets.py`: `ClaudePane`, `_AiReviewCard`, `_agent_command` | permission bridge modules; workspace snapshot lifecycle |
| Permission requests from Claude Code | `aipermissions.py` and `permission_mcp.py` | `ClaudePane` in `widgets.py` |
| LSP transport/configuration | `lsp.py` | request and fallback handling in `mainwindow.py` |
| Theme or top-level visual constants | `theme.py` | each widget's `apply_style` method |
| Modal forms | `dialogs.py` | caller in `mainwindow.py` |
| Git status parsing | `gitutil.py` | Git commands/UI in `mainwindow.py` |

## Files

- `kant_editor.py` — thin executable entry point, compatibility re-exports, and small deterministic self-check.
- `kant/model.py` — `Node`/`Run` tree, strict marker parser, UID stamping, serialization, cached top-level labels.
- `kant/fileio.py` — atomic text/byte replacement, line endings, fingerprints, safe child names.
- `kant/syntax.py` — shared tokenizer, lightweight checks, KANT validation, per-extension run commands.
- `kant/xref.py` — deterministic project-wide graph of KANT elements and identifier references.
- `kant/projectops.py` — pure project scans and generated KANT map validation.
- `kant/workspace.py` — trust boundary for filesystem watching/mutation and AI snapshot review/rollback.
- `kant/widgets.py` — reusable Qt UI, agent terminal, review card, file tabs, and all MAPPA graphics.
- `kant/mainwindow.py` — application state and orchestration. Search its region headings or `AI-NAV` before reading linearly.
- `kant/lsp.py` — JSON-RPC/LSP process lifecycle and document synchronization.
- `kant/gitutil.py` — small read-only Git helpers.
- `kant/dialogs.py` — themed modal forms mixed into `MainWindow`.
- `kant/aipermissions.py` — authenticated localhost permission bridge owned by the IDE.
- `kant/permission_mcp.py` — dependency-free stdio MCP process used by Claude Code.
- `kant/theme.py` — live theme globals, styles, tag colors, limits, ignored directories.
- `test_kant_smoke.py` — single offscreen integration/regression check.
- `DESIGN.md` — invariants, data flow, and rationale; not a second file index.
- `index.html` — legacy browser prototype, outside the Python runtime.

## End-to-end flows

### Open and edit a file

`MainWindow._open_file` reads and parses the file, creates a `FileTab`, and calls `_render_view`. Each `CodeEdit` updates its owning `Run`; `FileTab.save` serializes the entire tree and atomically replaces the file.

### Navigate a KANT section

The project tree stores path, UID, and document order. `_on_tree_item_clicked` opens the file and resolves the node by UID; legacy files whose generated UID changed on reparse fall back to document order. `_render_view` then isolates that node.

### Build Incoming/Outgoing and MAPPA

`MainWindow._schedule_xref_build` parses project files in the background and calls `build_xref`. `_update_io_tabs` aggregates boundary-crossing edges for the selected subtree. `XrefMapDialog` filters the same graph; `XrefMapView` owns drawing, zoom, drag, and layout.

### Run an AI edit safely

`WorkspaceMixin._prepare_ai_snapshot` snapshots the project before `ClaudePane.run_prompt` starts a CLI process. On completion, `build_ai_review` computes deterministic file/hunk choices, `_AiReviewCard` collects the decision, and `apply_ai_review` or `rollback_snapshot` finishes the transaction.

### React to external filesystem changes

`WorkspaceMixin` owns watchers and conflict handling. Clean tabs reload; dirty tabs ask whether to reload or overwrite. Successful saves refresh watchers, invalidate derived xref state, and update project UI through `MainWindow` callbacks.

## Search anchors in large files

- `mainwindow.py`: `# ---- project tree`, `# ---- tabs`, `# ---- file open/save`, `# ---- section view`, and `# ---- workspace mutations`.
- `widgets.py`: class names are the stable anchors. MAPPA starts at `[FN CATEGORY] XrefMapView`; AI UI starts at `class ClaudePane`.
- KANT comments use `[FN CATEGORY]` for rationale, `[FN]` for the searchable summary, and `[FN OPEN/CLOSED]` for structural spans. Do not duplicate those markers for the same function.

## Verification

```powershell
python -m compileall -q kant kant_editor.py test_kant_smoke.py
$env:QT_QPA_PLATFORM='offscreen'
python test_kant_smoke.py
```
