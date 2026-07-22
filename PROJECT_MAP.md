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
| Generate or remove KANT marker skeletons | `skeleton.py`: `apply_skeleton`, `strip_kant_project` | KANT menu actions in `widgets.py`; orchestration in `mainwindow.py` |
| Atomic writes, fingerprints, filename safety | `fileio.py` | save callers in `widgets.py` and `workspace.py` |
| Local syntax checks, KANT marker audit/repair, or run commands | `syntax.py` | status/run orchestration in `mainwindow.py` |
| Project search, replace scan, map generation, validation | `projectops.py` | UI callbacks in `mainwindow.py` |
| Incoming/Outgoing reference detection | `xref.py`: `build_xref` | cache and panels in `mainwindow.py` |
| Saved element groups and member recovery after edits | `groupings.py`: `reconcile_groupings` | Groups tree and menus in `mainwindow.py` |
| MAPPA layout, filters, drill-down, graphics | `mappa.py`: `_force_layout_positions`, `XrefMapView`, `XrefMapDialog` | graph creation in `xref.py`; entry point (LOCAL/GLOBAL) in `mainwindow.py`'s `_build_io_tabs`/`_open_xref_window` |
| Editor blocks, tabs, autosave, undo/redo | `widgets.py`: `CodeEdit`, section widgets, `FileTab` | rendering and callbacks in `mainwindow.py` |
| Project tree drag/drop | `widgets.py`: `ProjectTree` routes role-agnostic callbacks | KANT reorder and File-view moves in `mainwindow.py`; safe path/tab/group migration in `workspace.py` `_move_tree_path` |
| Project tree, active tabs, rendering, menus, Git/LSP routing | `mainwindow.py`: `MainWindow` | the service module called by the relevant method |
| Snapshots, review/apply/rollback, file watching, create/rename/trash | `workspace.py` | live diff rendering in `mainwindow.py`: `_enter_ai_review_mode`/`_show_ai_review_diff`; review UI in `widgets.py`: `ClaudePane.offer_ai_review` |
| Claude/Codex process and inline review UI | `widgets.py`: `ClaudePane.offer_ai_review`, `_agent_command` | the diff itself lives in the coding board/tree, not a review widget — see `mainwindow.py`'s `_enter_ai_review_mode` |
| PURE AI layout and global grouped chat archive | `mainwindow.py`: `_set_pure_ai_mode`, `_switch_ai_project`, `_group_ai_conversation` | `widgets.py`: `ConversationSidebar`, `ClaudePane.conversation_state` |
| "Modifica import" (local, editable copy of a third-party import) | `mainwindow.py`: `_start_import_edit`, `_open_import_edit_dialog`, `_open_import_occurrence_picker`, `_apply_local_import_shadow` | context-menu detection in `widgets.py`'s `CodeEdit.contextMenuEvent`/`import_edit_provider`; Python-only (resolves via the project's active interpreter) |
| Permission requests from Claude Code | `aipermissions.py` and `permission_mcp.py` | `ClaudePane` in `widgets.py` |
| LSP transport/configuration | `lsp.py` | request and fallback handling in `mainwindow.py` |
| Theme or top-level visual constants | `theme.py` | each widget's `apply_style` method |
| SVG icon shapes and theme-aware icon color | `icons.py`: `draw_icon` | owning widget's `apply_style` method |
| Modal forms | `dialogs.py` | caller in `mainwindow.py` |
| Git status parsing | `gitutil.py` | Git actions in `gitops.py` |
| Git diff/stage/commit/branch actions | `gitops.py`: `GitOpsMixin` | status parsing in `gitutil.py`; commit dialog in `dialogs.py` |

## Files

- `kant_editor.py` — thin executable entry point, compatibility re-exports, and small deterministic self-check.
- `kant/model.py` — `Node`/`Run` tree, strict marker parser, UID stamping, serialization, cached top-level labels.
- `kant/skeleton.py` — deterministic marker generation and project-wide KANT-marker removal.
- `kant/fileio.py` — atomic text/byte replacement, line endings, fingerprints, safe child names.
- `kant/syntax.py` — shared tokenizer, lightweight checks, KANT validation, per-extension run commands.
- `kant/xref.py` — deterministic project-wide graph of KANT elements and identifier references.
- `kant/groupings.py` — persisted groups, stable member hints, and deterministic stale-key recovery.
- `kant/projectops.py` — pure project scans and generated KANT map validation.
- `kant/workspace.py` — trust boundary for filesystem watching/mutation and AI snapshot review/rollback.
- `kant/widgets.py` — reusable Qt UI, agent terminal (`ClaudePane`, including the accept/cancel review card — the diff itself renders in `mainwindow.py`'s coding board/tree, not here), file tabs.
- `docs/PURE_AI_DESIGN.md` — panel composition and persistence contract for PURE AI mode.
- `docs/AUDIT_BUG_PERFORMANCE_2026-07-22.md` — latest verified bug/performance audit and inactive-feature inventory.
- `kant/mappa.py` — MAPPA: layout algorithm, graph graphics items, `XrefMapView`, `XrefMapDialog`.
- `kant/mainwindow.py` — application state and orchestration. Search its region headings or `AI-NAV` before reading linearly.
- `kant/lsp.py` — JSON-RPC/LSP process lifecycle and document synchronization.
- `kant/gitutil.py` — small read-only Git helpers.
- `kant/gitops.py` — `GitOpsMixin`: status refresh, diff/stage, commit, branch switch, mixed into `MainWindow`.
- `kant/dialogs.py` — themed modal forms mixed into `MainWindow`.
- `kant/aipermissions.py` — authenticated localhost permission bridge owned by the IDE.
- `kant/permission_mcp.py` — dependency-free stdio MCP process used by Claude Code.
- `kant/theme.py` — live theme globals, styles, tag colors, limits, ignored directories.
- `kant/icons.py` — central monochrome SVG icon set; defaults to gold in night mode.
- `test_kant_smoke.py` — offscreen integration/regression checks, one `test_*` method per feature area.
- `DESIGN.md` — invariants, data flow, and rationale; not a second file index.

## End-to-end flows

### Open and edit a file

`MainWindow._open_file` reads and parses the file, creates a `FileTab`, and calls `_render_view`. Each `CodeEdit` updates its owning `Run`; `FileTab.save` serializes the entire tree and atomically replaces the file.

### Navigate a KANT section

The project tree stores path, UID, and document order. `_on_tree_item_clicked` opens the file and resolves the node by UID; legacy files whose generated UID changed on reparse fall back to document order. Files and elements share one visible unpinned preview slot; only pinning makes a tab persistent. While an element preview is visible, its parent `FileTab` may remain hidden as the single shared tree/undo/save owner. Sibling KANT elements can be dragged to reorder within their shared parent (`ProjectTree`'s `InternalMove` drag mode, KANT view only); `_kant_reorder_apply` rewrites `parent_node.body`'s child order and re-serializes — the default order, before any manual reordering, is document order as parsed from source.

### Build Incoming/Outgoing and MAPPA

`MainWindow._schedule_xref_build` parses project files in the background and calls `build_xref`. `_update_io_tabs` aggregates boundary-crossing edges for the selected subtree. MAPPA's entry point is the MAPPA button in the INCOMING/OUTGOING bar (`_build_io_tabs`), which expands two lateral options: GLOBAL opens the classic full-project `XrefMapDialog` (and explicitly exits drill mode via `_exit_drill_mode`, in case a previous LOCAL session left the dialog drilled in); LOCAL calls `_current_map_local_key` to find the container currently open in the coding board (walking up to its nearest parent via the xref graph if the active element is itself a leaf with no children) and opens straight into that element's drilled-in view (`XrefMapDialog._enter_drill_mode`). `XrefMapView` owns drawing, zoom, drag, and layout for both.

### Fork a third-party import locally ("Modifica import")

Right-clicking a Python `import`/`from ... import` line offers "Modifica import" (`CodeEdit.contextMenuEvent`, detected via a small regex pair — Python only, matching what can actually be resolved). `MainWindow._start_import_edit` resolves the module's real source file via `importlib.util.find_spec` run against the project's active interpreter (`_active_python`), copies that one file into `kant_local_imports/<flattened-module-name>.py` (reused as-is on a later edit of the same module, never re-copied over local changes), and opens a dialog with that copy in a `CodeEdit` plus a `ClaudePane` scoped to just that file (`context_hint`/`focus_hint` set to the local copy's path). Confirming opens a second dialog listing every project file that both mentions the imported symbol (`reference_locations`) and has its own matching import line to insert after; each checked file gets exactly one new shadow-import line placed immediately after its original (`from kant_local_imports.<module> import <symbol>`, or `import kant_local_imports.<module> as <symbol>` for a plain `import` line) — the original import is never touched, and Python's own name shadowing means every later use of that symbol in the file resolves to the local copy with no other line rewritten.

### Run an AI edit safely

`MainWindow._build_ai_context_hint` sends Claude/Codex one compact hidden line containing the active project-relative path and real code symbol names; variable groups use their assigned identifiers. Within a parent module tab, the focused or most-visible inner code block refines that context automatically. GLOBAL adds the absolute project root while retaining the active view as the immediate focus. `WorkspaceMixin._prepare_ai_snapshot` snapshots the project before `ClaudePane.run_prompt` starts a CLI process. On completion, `build_ai_review` computes deterministic file/hunk data, `MainWindow._enter_ai_review_mode` re-opens every changed file as one merged, read-only, green/red-underlined diff block (and colors that file's project-tree row the same way — half-and-half via a `qlineargradient` background when a file has both additions and deletions, strikethrough for a deleted file), and `ClaudePane.offer_ai_review` posts a small Accetta/Annulla chat card — there is no separate review window and no per-hunk selection. `apply_ai_review` (always all hunks accepted) or `rollback_snapshot` finishes the transaction; `_exit_ai_review_mode` clears the tree coloring and every affected tab is force-reloaded from disk (not the usual fingerprint-gated watcher path — an accepted review often rewrites a file with byte-identical content, which wouldn't otherwise trip the fingerprint check and would leave the stale read-only diff view stuck).

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
