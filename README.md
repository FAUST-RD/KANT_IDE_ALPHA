# KANT IDE

> **A cognitive interface for code.**

KANT IDE is a desktop environment for understanding, directing, and reviewing software—not only editing its text.

It adds explicit structure to ordinary source files and turns that structure into a navigable outline, focused editing views, dependency lenses, and an interactive project map.

```text
source code  ->  explicit structure  ->  cognitive interface  ->  informed action
```

No custom file format. No generated runtime code. No lock-in.

## Why a cognitive interface?

For most of software history, code was written manually, line by line. That work was slow, but it continuously produced knowledge. By writing, reading, debugging, and revising each part, developers formed a mental model of the system almost as a by-product of the labor itself.

Generative tools change that relationship. We can now create and transform code at a scale that was previously impossible for one person. We work at a higher level of abstraction: describing outcomes, delegating implementation, and reviewing results instead of micromanaging every line.

The magnitude of our actions has increased, while the manual contact that once created understanding has decreased. Production and comprehension can drift apart.

The answer is not to force people back into line-by-line supervision of everything a machine produces. That would discard the leverage of the new tools. Instead, we need new ways to know a codebase from this higher level of abstraction.

This changes what an IDE needs to be.

An IDE can no longer be only a better surface for manipulating text. It must become a **cognitive interface**: a place that exposes structure, responsibilities, boundaries, dependencies, and change in forms that help a person build and maintain an accurate mental model—even when they did not manually author every line.

As our tools expand the reach of our actions, our development environment must expand our **cognitive reach** with them.

## What KANT makes visible

KANT gives both people and tools stable coordinates inside a codebase.

| Cognitive need | KANT IDE |
| --- | --- |
| See what a system contains | A project outline organized by modules, classes, functions, constants, types, and tests |
| Focus on one responsibility | Section-level editing without losing the surrounding hierarchy |
| Understand relationships | Deterministic Incoming and Outgoing references |
| Move between detail and overview | An interactive **MAPPA** with filtering, clustering, and drill-down |
| Direct powerful tools | Explicit boundaries and descriptions that provide usable context |
| Verify delegated work | AI snapshots, file/hunk review, atomic application, and rollback |

The goal is not to make AI produce as much code as possible. It is to channel what these tools produce into forms that remain navigable, reviewable, and comprehensible to people.

## The KANT convention

KANT describes software structure with ordinary comments. Tagged files remain valid source code and continue to work with normal editors, compilers, formatters, and version control.

A section can use four markers:

```text
[TAG CATEGORY] detailed purpose or architectural context
[TAG] short human-readable description
[TAG OPEN #stable-id] exact-name
...section source code...
[TAG CLOSED #stable-id] exact-name
```

- `TAG` identifies the kind of element. Common tags are `MOD` (module), `CLS` (class), `FN` (function), `TYP` (type), `CST` (constant), `VAR` (variable), `CFG` (configuration), and `TST` (test).
- `CATEGORY` records responsibility, assumptions, or architectural context that is not obvious from the implementation.
- `[TAG]` supplies the concise label shown in the project tree and map.
- `OPEN` and `CLOSED` define the source span. Their tag, name, and optional ID must match.
- `#stable-id` preserves the identity of a section across reparses and name edits. KANT IDE adds one when it encounters an older marker without an ID.

Sections can be nested: functions inside classes, classes inside modules, and so on. They close in reverse order, so the most recently opened section always closes first.

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

Marker lines can use the host language's normal comment syntax, including `#`, `//`, `--`, `;`, `/* ... */`, and `<!-- ... -->`. KANT IDE preserves marker text and all unedited source while turning marked regions into its navigable model.

## One structure, several ways of knowing

The same KANT structure supports multiple views of the codebase:

### Outline

The **Codice** tree shows conceptual elements and their hierarchy. Switch to **File** whenever the physical folder layout is the more useful perspective.

### Focus

Open one tagged section as an editable unit. The IDE updates the original source through atomic autosaves, with undo/redo and external-change detection.

### Relationships

The **INCOMING** and **OUTGOING** panels show references crossing the selected section's boundary. Their graph is produced by deterministic source analysis rather than AI output.

### Map

**MAPPA** turns the project graph into a spatial overview. Filter it, rearrange it, change flow direction, isolate elements, or drill into the direct children of a component.

### Delegation and review

Claude Code and Codex can run inside the IDE. Permission prompts, project snapshots, file/hunk review, atomic application, and rollback keep delegated work visible and reversible.

KANT IDE also includes Git actions, a terminal, lightweight syntax checks, optional language-server integration, Python debugging, and day/night themes.

## Quick start

KANT IDE requires Python 3 and PySide6.

```powershell
git clone https://github.com/FAUST-RD/KANT_IDE.git
cd KANT_IDE
python -m pip install -r requirements.txt
python kant_editor.py
```

On Linux or macOS, `./install.sh` installs the Python dependency and prints the launch command.

Language-server features activate only when a compatible server is already available on `PATH`. The editor works without one.

## A first five-minute tour

1. Launch `kant_editor.py` and open a project folder.
2. Open any source file; untagged files remain editable as normal.
3. Add matching `OPEN` and `CLOSED` markers around one useful function or class.
4. Select **Codice** and open that section directly from the project tree.
5. Add a short `[TAG]` description and `CATEGORY` context.
6. Use **INCOMING**, **OUTGOING**, and **MAPPA** to move from local code to system-level understanding.

Start small. A project does not need to be fully tagged before KANT becomes useful.

## Development

- [`PROJECT_MAP.md`](PROJECT_MAP.md) shows where each feature lives and how the main flows connect.
- [`DESIGN.md`](DESIGN.md) records architectural decisions and safety invariants.
- [`AGENTS.md`](AGENTS.md) contains repository instructions for AI coding agents.

Run the regression check without opening a window:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python test_kant_smoke.py
```

On Linux or macOS, set `QT_QPA_PLATFORM=offscreen` before running the test.

The legacy `index.html` prototype remains in the repository for reference. Current development targets the Python/PySide6 application.

## License

[MIT](LICENSE)
