"""Per-project Python interpreter/venv detection and config — deterministic, no Qt.

AI navigation: venv discovery and the .kant/python.json config round-trip come first, then
version/module/dependency-file probes. mainwindow.py owns the UI (status label, picker dialog,
wiring run/debug/test/format/REPL to whichever interpreter this module resolves) — this module
never touches application state, matching model.py/fileio.py/syntax.py's own boundary.
"""
import json
import os
import subprocess
from pathlib import Path


CONFIG_DIRNAME = '.kant'
CONFIG_FILENAME = 'python.json'

# [CST] VENV_DIR_NAMES — conventional venv folder names checked directly under a project's root
VENV_DIR_NAMES = ('venv', '.venv', 'env', '.env')


def _venv_python_path(venv_dir):
    candidate = venv_dir / ('Scripts' if os.name == 'nt' else 'bin') / ('python.exe' if os.name == 'nt' else 'python')
    return str(candidate) if candidate.is_file() else None


def detect_venvs(project_root):
    """Interpreter paths for every conventional venv folder found directly under project_root."""
    root = Path(project_root)
    found = []
    for name in VENV_DIR_NAMES:
        python_path = _venv_python_path(root / name)
        if python_path:
            found.append(python_path)
    return found


def config_path(project_root):
    return Path(project_root) / CONFIG_DIRNAME / CONFIG_FILENAME


def load_interpreter(project_root):
    """The configured interpreter path for project_root, or None if unconfigured/stale."""
    try:
        data = json.loads(config_path(project_root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    stored = data.get('python')
    if not stored:
        return None
    resolved = stored if os.path.isabs(stored) else str(Path(project_root) / stored)
    return resolved if os.path.isfile(resolved) else None


# [FN CATEGORY] save_interpreter — stores a path relative to the project when the interpreter
# lives inside it (a project-local venv), absolute otherwise (a system/shared interpreter outside
# the tree) — a relative path is portable and safe to commit for a team to share; an absolute one
# to a machine-specific location wouldn't be.
# [FN] save_interpreter — writes the chosen interpreter to .kant/python.json
# [FN OPEN] save_interpreter
def save_interpreter(project_root, python_path):
    root = Path(project_root)
    python_path = Path(python_path)
    try:
        stored = python_path.relative_to(root).as_posix()
    except ValueError:
        stored = str(python_path)
    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'python': stored}, indent=2) + '\n', encoding='utf-8')
# [FN CLOSED] save_interpreter


def interpreter_version(python_path):
    """'3.11.4'-style version string for python_path, or None if it can't be run."""
    try:
        result = subprocess.run(
            [python_path, '-c', 'import sys; print("%d.%d.%d" % sys.version_info[:3])'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def interpreter_label(python_path):
    """Short display label: the venv/prefix folder name (e.g. '.venv'), or the executable's own
    stem when python_path isn't inside a Scripts/bin folder (a bare system interpreter)."""
    parts = Path(python_path).parts
    for marker in ('Scripts', 'bin'):
        if marker in parts:
            index = parts.index(marker)
            if index > 0:
                return parts[index - 1]
    return Path(python_path).stem


def has_module(python_path, module):
    """Whether `python_path -m module --version` succeeds — the module is importable/runnable
    under that specific interpreter (not just present somewhere on PATH)."""
    try:
        result = subprocess.run(
            [python_path, '-m', module, '--version'], capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def dependency_file(project_root):
    """'requirements.txt' or 'pyproject.toml' found at project_root, or None."""
    root = Path(project_root)
    if (root / 'requirements.txt').is_file():
        return 'requirements.txt'
    if (root / 'pyproject.toml').is_file():
        return 'pyproject.toml'
    return None


# [FN CATEGORY] is_python_majority_project — cheap heuristic (first sample_limit files by
# extension, not a full project scan) for deciding whether to default the terminal dock to the
# Python REPL tab on open — doesn't need to be exact, just directionally right for the common case
# [FN] is_python_majority_project — True when .py is the most common source extension found
# [FN OPEN] is_python_majority_project
def is_python_majority_project(project_root, sample_limit=200):
    counts = {}
    checked = 0
    for current, subdirs, files in os.walk(project_root):
        subdirs[:] = [d for d in subdirs if not d.startswith('.') and d not in ('node_modules', '__pycache__')]
        for name in files:
            ext = Path(name).suffix.lower()
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
                checked += 1
        if checked >= sample_limit:
            break
    return bool(counts) and max(counts, key=counts.get) == '.py'
# [FN CLOSED] is_python_majority_project
