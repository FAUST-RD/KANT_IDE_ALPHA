# KANT IDE

KANT IDE is a lightweight desktop editor that turns structured comments into a visual outline of a codebase.

Instead of replacing your language or build tools, it adds a navigable layer on top of ordinary source files: edit code by section, inspect incoming and outgoing references, and explore the project through an interactive map.

## What it offers

- A project tree built from KANT markers such as `[FN OPEN]` and `[CLS CLOSED]`.
- Focused editing of individual functions, classes, constants, and other tagged sections.
- Automatic saving with atomic file replacement and external-change detection.
- Deterministic Incoming/Outgoing references and an interactive project map.
- Lightweight syntax checks, optional language-server integration, Git actions, terminal, and Python debugging.
- Built-in Claude Code and Codex terminals with permission prompts, snapshots, change review, and rollback.
- Day and night themes.

## How the KANT convention works

KANT describes the structure of a codebase with ordinary comments. The source remains valid for its language and continues to work with normal editors, compilers, formatters, and version control.

A section can use four markers:

```text
[TAG CATEGORY] detailed purpose or architectural context
[TAG] short description shown in the IDE
[TAG OPEN #stable-id] exact-name
...section source code...
[TAG CLOSED #stable-id] exact-name
```

- `TAG` identifies the kind of element. Common tags are `MOD` (module), `CLS` (class), `FN` (function), `TYP` (type), `CST` (constant), `VAR` (variable), `CFG` (configuration), and `TST` (test).
- `CATEGORY` is optional long-form context. It is useful for explaining responsibility, assumptions, or relationships that are not obvious from the code.
- `[TAG]` is an optional concise description used as the human-readable label in the project tree and map.
- `OPEN` starts the section and `CLOSED` ends it. Their tag, name, and optional ID must match.
- `#stable-id` lets the IDE recognize the same section across reparses and name edits. KANT IDE automatically adds an ID when an older marker does not have one.

Sections can be nested. For example, functions can live inside a class and classes inside a module. They must close in reverse order: the most recently opened section is always closed first.

```python
# [MOD] User service
# [MOD OPEN #users-module] users.py

# [CLS CATEGORY] Coordinates user retrieval without owning persistence
# [CLS] User service
# [CLS OPEN #user-service] UserService
class UserService:
    # [FN CATEGORY] Reads one user through the injected repository
    # [FN] Fetch user by ID
    # [FN OPEN #load-user] load_user
    def load_user(self, user_id):
        return self.repository.get(user_id)
    # [FN CLOSED #load-user] load_user
# [CLS CLOSED #user-service] UserService

# [MOD CLOSED #users-module] users.py
```

Marker lines may use the host language's normal comment syntax, including `#`, `//`, `--`, `;`, `/* ... */`, and `<!-- ... -->`. KANT IDE preserves marker text and all unedited source while converting the marked regions into its navigable outline.

## Installation

KANT IDE requires Python 3 and PySide6.

```powershell
git clone https://github.com/FAUST-RD/KANT_IDE.git
cd KANT_IDE
python -m pip install -r requirements.txt
python kant_editor.py
```

On Linux or macOS, `./install.sh` installs the Python dependency and prints the launch command.

Language-server features are enabled only when a compatible server is already available on `PATH`. The editor itself works without one.

## Using the editor

1. Launch `kant_editor.py` and open a project folder.
2. Select **Codice** to browse KANT sections or **File** for a conventional file tree.
3. Open a section and edit its code block; changes are autosaved.
4. Use **INCOMING**, **OUTGOING**, or **MAPPA** to explore relationships.

Files without KANT markers can still be opened and edited. The legacy `index.html` prototype is kept for reference; current development targets the Python application.

## Development

- [`PROJECT_MAP.md`](PROJECT_MAP.md) explains where each feature lives and how the main flows connect.
- [`DESIGN.md`](DESIGN.md) records architectural decisions and safety invariants.
- [`AGENTS.md`](AGENTS.md) contains repository instructions for AI coding agents.

Run the regression check without opening a window:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python test_kant_smoke.py
```

On Linux or macOS, use `export QT_QPA_PLATFORM=offscreen` before the test command.

## License

[MIT](LICENSE)
