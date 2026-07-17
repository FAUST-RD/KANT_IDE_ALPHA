"""Crash-safe file writes and filesystem-name safety helpers."""
import hashlib
import os
import tempfile
import time
from pathlib import Path


# [FN CATEGORY] write_file_atomic — writes to a temp file in the same directory then os.replace()s
# it over the target, which is atomic on both POSIX and Windows: a crash or kill mid-write leaves
# either the complete old file or the complete new one, never a half-written mix
# [FN] write_file_atomic — crash-safe file write
# [FN OPEN] write_file_atomic
def _write_atomic(path, mode, value):
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.kant-autosave-', dir=directory)
    try:
        kwargs = {'encoding': 'utf-8', 'newline': ''} if 'b' not in mode else {}
        with os.fdopen(fd, mode, **kwargs) as f:
            f.write(value)
        try:
            os.chmod(tmp_path, os.stat(path).st_mode)  # preserve mode (e.g. +x) across saves
        except OSError:
            pass  # new file — nothing to preserve
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))  # transient Windows reader/antivirus lock
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def write_file_atomic(path, text):
    _write_atomic(path, 'w', text)


def write_bytes_atomic(path, data):
    _write_atomic(path, 'wb', data)
# [FN CLOSED] write_file_atomic


# [FN CATEGORY] detect_line_ending — peeks the first chunk of a file's raw bytes for a literal
# CRLF sequence, purely for the status-bar indicator — doesn't affect reading/writing, which always
# goes through Python text mode with newline='' (whatever line endings are already in the file are
# preserved verbatim through parse_kant/serialize_kant regardless of what this reports)
# [FN] detect_line_ending — returns 'CRLF' or 'LF' for a file's line-ending style
# [FN OPEN] detect_line_ending
def detect_line_ending(path):
    try:
        with open(path, 'rb') as f:
            chunk = f.read(8192)
    except OSError:
        return 'LF'
    return 'CRLF' if b'\r\n' in chunk else 'LF'
# [FN CLOSED] detect_line_ending


# [FN CATEGORY] safe_mkstemp / safe_mkdtemp — observed on some macOS CI runners:
# tempfile.gettempdir()'s reported base directory can transiently not exist, failing
# mkstemp()/mkdtemp() outright (reproduced via a real macOS CI failure in
# write_permission_config, kant/aipermissions.py — every `claude` chat message creates one of
# these). Falls back to a directory under the user's own KANT IDE state folder (already used for
# crash logs), guaranteed to exist, instead of losing the temp file/dir entirely.
# [FN] safe_mkstemp / safe_mkdtemp — tempfile.mkstemp()/mkdtemp() with a guaranteed fallback base
# [FN OPEN] safe_mkstemp
def _fallback_tempdir():
    fallback = os.path.join(os.path.expanduser('~'), '.kant_ide', 'tmp')
    os.makedirs(fallback, exist_ok=True)
    return fallback


def safe_mkstemp(**kwargs):
    try:
        return tempfile.mkstemp(**kwargs)
    except FileNotFoundError:
        kwargs.pop('dir', None)
        return tempfile.mkstemp(dir=_fallback_tempdir(), **kwargs)


def safe_mkdtemp(**kwargs):
    try:
        return tempfile.mkdtemp(**kwargs)
    except FileNotFoundError:
        kwargs.pop('dir', None)
        return tempfile.mkdtemp(dir=_fallback_tempdir(), **kwargs)
# [FN CLOSED] safe_mkstemp


def is_safe_child_name(name):
    return bool(name) and name == os.path.basename(name) and not os.path.isabs(name) and name not in ('.', '..')


def file_fingerprint(path):
    try:
        return hashlib.blake2b(Path(path).read_bytes(), digest_size=16).digest()
    except OSError:
        return None
