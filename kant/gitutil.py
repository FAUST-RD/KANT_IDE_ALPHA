"""Git status helpers: repo root discovery and `git status --short` parsing."""
import os
import shutil
import subprocess


def find_git_root(path):
    if not path or not shutil.which('git'):
        return None
    try:
        result = subprocess.run(
            ['git', '-C', path, 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return os.path.abspath(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def parse_git_status(output):
    status = {}
    for line in output.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip() or line[:2]
        path = line[3:]
        if ' -> ' in path:
            path = path.rsplit(' -> ', 1)[1]
        status[path.replace('/', os.sep)] = code
    return status


def git_status_map(root):
    if not root:
        return {}
    try:
        result = subprocess.run(
            ['git', '-C', root, 'status', '--short', '--untracked-files=normal'],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_git_status(result.stdout) if result.returncode == 0 else {}
