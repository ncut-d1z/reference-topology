#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Keep global pip settings untouched. An explicit PIP_INDEX_URL takes precedence.
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

is_python312() {
    [[ -x "$1" ]] && "$1" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1
}

if is_python312 .venv/bin/python; then
    python_exe=.venv/bin/python
elif is_python312 .venv-py312/bin/python; then
    python_exe=.venv-py312/bin/python
else
    if [[ -e .venv ]]; then
        venv_dir=.venv-py312
        echo 'Existing .venv is not Python 3.12; using .venv-py312 without modifying it.'
    else
        venv_dir=.venv
    fi

    if command -v python3.12 >/dev/null 2>&1; then
        python3.12 -m venv "$venv_dir"
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
        python3 -m venv "$venv_dir"
    else
        echo 'Python 3.12 is required; existing virtual environments have not been deleted.' >&2
        exit 2
    fi

    python_exe="$venv_dir/bin/python"
    if ! is_python312 "$python_exe"; then
        echo 'Failed to create a Python 3.12 virtual environment.' >&2
        exit 2
    fi
fi

if ! "$python_exe" -c 'import fastapi, uvicorn, pypdf, pdfminer, multipart' >/dev/null 2>&1; then
    echo "Installing Python dependencies from $PIP_INDEX_URL"
    "$python_exe" -m pip install --index-url "$PIP_INDEX_URL" -e .
fi

export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_exe" -m reference_topology "$@"
