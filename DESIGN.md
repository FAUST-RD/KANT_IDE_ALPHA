# KANT IDE Design

## Purpose

KANT IDE is a small desktop editor for projects that use KANT comment markers:

- `[TAG OPEN] name`
- `[TAG CLOSED] name`
- optional `[TAG CATEGORY]`, `[TAG]`, `[TAG INCOMING]`, `[TAG OUTGOING]`

The editor turns those markers into a navigable code outline. It is not trying to replace a full IDE yet; its useful niche is structural navigation and editing of KANT-tagged code.

## Repository Layout

- `kant_editor.py` - thin entry point (`main()`, `_self_check()`, compat re-exports).
- `kant/` - the application package (see Main Areas below).
- `PROJECT_MAP.md` - short map of files and main flows.
- `DESIGN.md` - this document.
- `requirements.txt` - Python dependency list, currently PySide6.
- `CHANGELOG.txt` - manual change history.
- `index.html` - old browser prototype kept as reference.
- `install.sh` - install helper.
- `_scratch_test.py` - scratch/local test file, not core design.

## Core Design

The app is intentionally deterministic:

1. Read source text.
2. Parse KANT markers with `parse_kant()`.
3. Store the result as a tree of `Node` and `Run`.
4. Render that tree into editable Qt widgets.
5. On edit, write text back into the affected `Run`.
6. Serialize with `serialize_kant()`.
7. Save atomically with `write_file_atomic()`.

The key invariant is round-trip safety: parse and serialize should preserve source text except for the code runs the user edits.

## Main Areas (the `kant/` package)

The package is a layered DAG: lower modules never import upward.

- `theme.py` - colors, styles, `set_theme()`, and UI sizing constants. `set_theme()`
  rebinds the color globals in place, so consumers read them live as `theme.<NAME>`
  rather than importing them by name (otherwise a theme switch goes stale).
- `model.py` - KANT model/parser: `Run`, `Node`, `parse_kant()`, `_assign_uids()`,
  `serialize_kant()`, `read_top_level_label()`.
- `fileio.py` - file safety: `write_file_atomic()`, `detect_line_ending()`, `is_safe_child_name()`.
- `dialogs.py` - themed modal dialog mixin, with no application workflow.
- `projectops.py` - pure project search, replace scan, KANT-map generation, validation, and symbol lookup.
- `workspace.py` - safe filesystem lifecycle: watchers, conflicts, create/rename/trash, and AI snapshot/diff/rollback.
- `syntax.py` - `TOKEN_RE` (shared tokenizer), `check_syntax()`, `check_file_syntax()`,
  `run_command_for_path()`. No Qt, no theme.
- `xref.py` - `build_xref()`: deterministic cross-reference graph over every KANT-tagged
  element (who references what, project-wide), by tokenizing each element's own code with
  the shared TOKEN_RE and matching identifiers against other elements' names. No Qt, no
  theme, no external dependency.
- `lsp.py` - `LspClient`, `lsp_config_for_path()`, URI helpers.
- `gitutil.py` - `find_git_root()`, `parse_git_status()`, `git_status_map()`.
- `widgets.py` - Qt widgets: `KantHighlighter`, `CodeEdit`/`LineNumberArea`,
  `TerminalPane`, `ClaudePane`, `CollapsibleSection`/`LeafSection`, `ProjectTree`,
  `TitleBar`, `FileTab`, and the icon builders.
- `mainwindow.py` - UI composition and orchestration: project/editor panels, menus, Git/LSP callbacks, and section rendering.
- `kant_editor.py` - entry point and smoke check `_self_check()`.

## Main Flows

### Open Project

`MainWindow._open_project_folder()` sets the project root, closes tabs from a previous project, refreshes Git state, checks for a KANT map, watches directories, and shows the main screen.

### Open File

`MainWindow._open_file()` parses the file into a KANT tree, creates a `FileTab`, renders its sections, and starts LSP diagnostics when a matching language server is available.

### Edit And Save

Each visible code block is a `CodeEdit`. When text changes, `_on_code_changed()` updates the related `Run`, marks the tab dirty, and schedules autosave. `FileTab.save()` serializes the whole tree and calls `write_file_atomic()`.

### Search And Replace

`Ctrl+F` searches visible code blocks in the active tab. `Ctrl+Shift+F` scans project text files and writes clickable rows into the `RISULTATI` panel. `Ctrl+Shift+H` does confirmed project-wide replacement.

### Git

Git state is read with `git status --short`. File context menus and the top `Git` menu expose refresh, diff, stage, and unstage. Diff output currently goes to the terminal pane.

### Workspace Layout

The outer horizontal splitter keeps the main workspace beside `ClaudePane`. Inside the workspace, tree and editor occupy the upper horizontal splitter and `TerminalPane` spans their combined width at the bottom. `ClaudePane` renders prompts and streaming agent output as chat bubbles while retaining the existing CLI process and log flow.

### AI Change Review

Before an agent starts, `workspace.py` snapshots the project. When it finishes, the chat-styled `AiReviewDialog` first shows an assistant change card, then expands into a rounded inspection bubble with file/hunk choices and an editable final result. Checked hunks are rebuilt deterministically from `difflib` opcodes; unchecked hunks come from the snapshot, manual final text overrides the selected file, and cancel restores the entire snapshot.

Claude stays in print/streaming mode and delegates permission prompts to the local `kant_permissions` MCP tool. `permission_mcp.py` forwards each request over an authenticated localhost socket owned by `PermissionBridge`; `ClaudePane` renders the decision card and returns allow/deny. Automatic mode uses the same path instead of bypassing Claude's permission system. The first confirmed KANT setup enables this automatic path for one run only; Codex receives its corresponding `--full-auto` flag for that one run.

### Incoming / Outgoing / Mappa

`_update_io_tabs()` looks the selected element up in the deterministic cross-reference graph (`kant/xref.py`, cached on `MainWindow` and invalidated on save/filesystem change/project switch/tab open). `INCOMING` lists what references the element and from which file (`←`), `OUTGOING` what it references and to which file (`→`) — for any tag, not just functions. Each row shows the element's short description (what the left tree shows) and its `[TAG CATEGORY]` text on hover. Both are navigable lists: double-click a row and `_navigate_to_element()` opens that element's file and scrolls to it (matching by `#id`, falling back to document order for legacy files whose ephemeral uid changes on reparse).

The `MAPPA` button above the project tree opens `XrefMapDialog` (`widgets.py`) — a frameless dialog **internal to the IDE**. `XrefMapView` condenses cyclic module dependencies into strongly connected components, ranks the resulting DAG from outgoing source to target, orders modules within each layer by weighted barycenter, seeds each module as an organic spiral cluster, then applies local attraction/repulsion. Nodes can be dragged directly, curved arrows update live, and coordinates are stored in `QSettings` under a versioned project-path hash; `Riorganizza` clears them and reruns the deterministic layout. Hovering a widened interactive edge opens an anchored popup listing the target's incoming references in green and the source's outgoing references in red; clicking pins or unpins it. The stable canvas preserves zoom and center during ordinary changes. Module collapse, TST filtering, search, tag/file filters, isolation and navigation remain available.

## Where To Change Things

- Add a new KANT tag color: edit `TAG_COLORS`/`TAG_BACKGROUNDS` in `theme.py`.
- Change parser behavior: edit `parse_kant()` in `model.py` and add an assertion in `_self_check()`.
- Change save behavior: edit `FileTab.save()` (`widgets.py`) or `write_file_atomic()` (`fileio.py`).
- Add a run command: edit `run_command_for_path()` in `syntax.py`.
- Add syntax support: edit `check_file_syntax()` in `syntax.py`.
- Add an LSP server: edit `LSP_SERVERS_BY_EXT`/`LSP_SERVER_ARGS`/`LSP_LANGUAGE_BY_EXT` in `lsp.py`.
- Change top menus: edit `TitleBar` in `widgets.py`.
- Change project tree layout: edit `_build_project_tree()`, `_build_plain_project_tree()`, and `_build_view_mode_bar()` in `mainwindow.py`.
- Change Incoming/Outgoing/Results/Mappa UI: edit `_build_io_tabs()`, `_toggle_info_popup()`, and `_update_io_tabs()` in `mainwindow.py`.
- Change how references are detected: edit `build_xref()` in `xref.py`.
- Change the map's layout/appearance: edit `XrefMapView` in `widgets.py`.
- Change the map dialog's filters/search/collapse/navigation: edit `XrefMapDialog` in `widgets.py`.

## Current Design Debt

- `mainwindow.py` is down to ~2.3k lines after extracting dialogs, pure project operations,
  and workspace lifecycle; section rendering is the next boundary only if it grows again.
- Long-running syntax, Git, search, KANT-map, and xref work runs outside the Qt UI thread.
- Git diff is terminal text, not a dedicated diff view.
- KANT structural editing is still mostly manual text editing.
- Tests consist of `_self_check()` plus the discoverable offscreen smoke test, including conflict, rollback, path-safety, LSP-error, and multi-file regressions.

## Next Essential Direction

The next useful step is not more UI chrome. It is safe structural KANT editing:

- create section
- rename section
- move section
- delete section
- preserve round-trip serialization

That would make the project more than a viewer/editor and turn it into a real KANT-aware tool.
