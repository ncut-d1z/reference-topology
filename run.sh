#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Affect only this process. Respect an explicit user-provided index.
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# Match pyproject.toml (Python >= 3.12), without pinning a minor version.
compatible_python() {
    [[ -x "$1" ]] && "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1
}

dependencies_available() {
    "$1" -c 'import fastapi, uvicorn, pypdf, pdfminer, multipart' >/dev/null 2>&1
}

prepare_environment() {
    local executable=$1
    if dependencies_available "$executable"; then
        return 0
    fi
    echo "Installing dependencies using $executable from $PIP_INDEX_URL"
    if "$executable" -m pip install --index-url "$PIP_INDEX_URL" -e . && dependencies_available "$executable"; then
        return 0
    fi
    echo "Dependencies unavailable for $executable; trying another installed Python." >&2
    return 1
}

# Reuse working environments from previous runs, including newer Python.
chosen_python=''
shopt -s nullglob
for executable in .venv/bin/python .venv-py*/bin/python; do
    if compatible_python "$executable" && prepare_environment "$executable"; then
        chosen_python=$executable
        break
    fi
done
shopt -u nullglob

if [[ -z "$chosen_python" ]]; then
    # Discover installed interpreters from PATH rather than hardcoding versions.
    interpreters=()
    IFS=: read -r -a search_dirs <<< "$PATH"
    for directory in "${search_dirs[@]}"; do
        [[ -d "$directory" ]] || continue
        for executable in "$directory"/python3 "$directory"/python "$directory"/python3.[0-9]*; do
            [[ -x "$executable" && ! -d "$executable" ]] || continue
            if compatible_python "$executable"; then
                interpreters+=("$executable")
            fi
        done
    done

    attempted_versions=':'
    for interpreter in "${interpreters[@]}"; do
        version_tag=$("$interpreter" -c 'import sys; print("%d%d" % sys.version_info[:2])')
        if [[ "$attempted_versions" == *":$version_tag:"* ]]; then
            continue
        fi
        attempted_versions+="$version_tag:"

        if [[ ! -e .venv ]]; then
            environment=.venv
        else
            environment=".venv-py$version_tag"
            suffix=2
            while [[ -e "$environment" ]]; do
                environment=".venv-py$version_tag-$suffix"
                ((suffix+=1))
            done
        fi

        echo "Trying Python $version_tag in $environment (existing environments are retained)"
        if ! "$interpreter" -m venv "$environment"; then
            echo "Unable to create $environment; trying another interpreter." >&2
            continue
        fi
        executable="$environment/bin/python"
        if compatible_python "$executable" && prepare_environment "$executable"; then
            chosen_python=$executable
            break
        fi
    done
fi

if [[ -z "$chosen_python" ]]; then
    echo 'No usable Python >= 3.12 environment could be prepared.' >&2
    echo 'Check installed interpreters, pip package availability and PIP_INDEX_URL.' >&2
    echo 'All pre-existing virtual environments have been retained.' >&2
    exit 2
fi

export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$chosen_python" -m reference_topology "$@"
