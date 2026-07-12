# KANT IDE Project Map

## Files

- `kant_editor.py` - thin entry point: `_self_check()`, `main()`, and backward-compat re-exports (`MainWindow`, `CodeEdit`). All app code now lives in the `kant/` package.
- `kant/theme.py` - colors, styles, `set_theme()` (rebinds color globals live — read as `theme.<NAME>`), and top-level UI constants.
- `kant/model.py` - KANT model + parser: `Run`, `Node`, `parse_kant`, `serialize_kant`, `read_top_level_label`.
- `kant/fileio.py` - `write_file_atomic`, `detect_line_ending`, `is_safe_child_name`.
- `kant/dialogs.py` - reusable themed modal dialogs exposed through `IdeDialogsMixin`.
- `kant/projectops.py` - pure project scans: search/replace discovery, KANT map, validation, and local symbol lookup.
- `kant/workspace.py` - filesystem watching, create/rename/trash operations, external conflicts, and AI snapshot/diff/rollback.
- `kant/aipermissions.py` - authenticated localhost bridge between Claude permission requests and the AI chat.
- `kant/permission_mcp.py` - dependency-free stdio MCP permission tool launched by Claude Code.
- `kant/syntax.py` - `TOKEN_RE`, `check_syntax`, `check_file_syntax`, `run_command_for_path` (no Qt, no theme).
- `kant/xref.py` - `build_xref`: deterministic project-wide cross-reference graph (who references what) over all KANT-tagged elements, for the Incoming/Outgoing panels and the MAPPA graph (no Qt, no theme, no dependencies).
- `kant/lsp.py` - `LspClient` + per-language server config and URI helpers.
- `kant/gitutil.py` - `find_git_root`, `parse_git_status`, `git_status_map`.
- `kant/widgets.py` - Qt widgets: `KantHighlighter`, `CodeEdit`, `TerminalPane`, `ClaudePane`, `CollapsibleSection`, `LeafSection`, `ProjectTree`, `TitleBar`, `FileTab`, icons.
- `kant/mainwindow.py` - `MainWindow`: UI composition and orchestration; dialogs, scans, and workspace lifecycle live elsewhere.
- `DESIGN.md` - design overview and navigation guide for the codebase.
- `index.html` - legacy browser prototype kept for reference. Current development targets the Python app.
- `requirements.txt` - runtime dependency list.
- `CHANGELOG.txt` - manual history of visible changes.
- `install.sh` - setup helper.

## Main Flows

- Open project: `MainWindow._open_project_folder()` sets the root, closes old project tabs, rebuilds the tree, refreshes Git/KANT-map state, and watches directories.
- Open file: `MainWindow._open_file()` parses source into a KANT tree and creates a `FileTab`.
- Edit/save: each `CodeEdit` writes back to its `Run`; `FileTab` autosaves with `write_file_atomic()`.
- External edits: open files are fingerprinted and watched; clean tabs reload, dirty tabs ask whether to reload or overwrite.
- AI edits: tabs flush and the project is snapshotted; the review dialog summarizes changed files, exposes file/hunk checkboxes and an editable final result, then atomically keeps the chosen changes or restores the snapshot.
- Title menus: `TitleBar` exposes `File`, `Cerca`, `Git`, and `Aspetto` dropdowns beside the `KANT IDE` title.
- Tree view switch: `MainWindow._build_view_mode_bar()` now lives above the project tree and only switches `Codice` / `File`.
- Search/replace: `Ctrl+F` searches the active view; `Ctrl+Shift+F/H` search or replace across project text files. Project results open contextually and jump to files on double click; there is no manual Results button.
- Workspace: tree and editor share the upper area; the terminal spans their full width along the bottom, while the AI chat remains on the right. MAPPA is above the tree and the KANT-map status is below it.
- AI chat: prompts appear as right-aligned user bubbles and streamed Claude/Codex output as left-aligned response bubbles.
- Claude permissions: manual requests appear as actionable chat cards (deny, allow once, allow for the session); `Automatico` answers them immediately, while snapshot and final change review remain mandatory.
- First KANT setup: after contextual confirmation, `/kant-code-map` gets automatic permissions only for that single Claude/Codex run; normal permission handling resumes afterward.
- IO labels: Incoming/Outgoing are toggle panels below the editor and navigable lists of what references the selected element (`←`) and what it references (`→`). MAPPA uses directed module layers, cycle grouping, weighted barycenter ordering and local forces. Nodes drag freely, modules stay separated, positions persist per project, and `Riorganizza` restores the automatic layout. Hovering an edge opens a green-INCOMING/red-OUTGOING popup; clicking pins it.
- LSP diagnostics: `LspClient` starts a matching language server already on `PATH`, sends `initialize` + document sync, and shows published diagnostics in the syntax status label.
- Git: project tree badges come from `git status --short`; file context menus expose diff, stage, and unstage.
- Run file: `run_command_for_path()` maps file extensions to simple local commands and executes them in `TerminalPane`.
