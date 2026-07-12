#!/usr/bin/env bash
# Installs everything needed to run kant_editor.py (just Python 3 + PySide6, the rest is stdlib).
set -euo pipefail

PYTHON="$(command -v python3 || command -v python)"
if [ -z "$PYTHON" ]; then
  echo "Python non trovato. Installa Python 3 e riprova (https://www.python.org/downloads/)." >&2
  exit 1
fi

echo "Uso $PYTHON ($("$PYTHON" --version))"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$(dirname "$0")/requirements.txt"

echo "Fatto. Avvia con: $PYTHON kant_editor.py"
