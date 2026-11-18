#!/usr/bin/env bash
set -euo pipefail
umask 077

LLMIRROR_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$LLMIRROR_ROOT"
LLMIRROR_PYTHON="${LLMIRROR_PYTHON:-python3}"
if ! command -v "$LLMIRROR_PYTHON" >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.11+ is required. Install Python and its venv module first.' >&2
    exit 1
fi
if ! "$LLMIRROR_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    printf '%s\n' 'Python 3.11+ is required. Set LLMIRROR_PYTHON to a supported interpreter.' >&2
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    if ! "$LLMIRROR_PYTHON" -m venv .venv; then
        printf '%s\n' 'Could not create .venv. Check that python3-venv is installed and this folder is writable.' >&2
        exit 1
    fi
fi
if ! cmp -s requirements.txt .venv/llmirror-requirements.txt; then
    .venv/bin/python -m pip install --disable-pip-version-check --only-binary=:all: -r requirements.txt
    cp requirements.txt .venv/llmirror-requirements.txt
fi
export PYTHONUNBUFFERED=1
exec .venv/bin/python launcher.py --no-browser "$@"
