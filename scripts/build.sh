#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
BUILD_VENV="$REPO_ROOT/.venv-build"
BUILD_DIR="$REPO_ROOT/build"
DIST_DIR="$REPO_ROOT/dist"
SPEC_PATH="$REPO_ROOT/WorkNetConnector.spec"
VENV_PYTHON="$BUILD_VENV/bin/python"
PYINSTALLER="$BUILD_VENV/bin/pyinstaller"
PYINSTALLER_ARGUMENTS=(
    --noconfirm
    --clean
    --onefile
    --windowed
    --name
    WorkNetConnector
    --collect-all
    keyring
    --collect-all
    pystray
    --hidden-import
    PIL._tkinter_finder
    src/net_connector/__main__.py
)

assert_allowed_removal() {
    local path="$1"
    case "$path" in
        "$BUILD_VENV"|"$BUILD_DIR"|"$DIST_DIR"|"$SPEC_PATH") ;;
        *)
            printf 'Refusing to remove a path outside the build allowlist: %s\n' "$path" >&2
            return 1
            ;;
    esac
}

safe_remove() {
    local path
    for path in "$@"; do
        assert_allowed_removal "$path"
        if [[ -e "$path" || -L "$path" ]]; then
            rm -rf -- "$path"
        fi
    done
}

is_python312() {
    local executable="$1"
    [[ -x "$executable" ]] &&
        "$executable" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
            >/dev/null 2>&1
}

find_python312() {
    local name candidate
    for name in python3.12 python3; do
        if candidate="$(command -v "$name" 2>/dev/null)" && is_python312 "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

case "${1:-}" in
    --validate-only)
        printf 'Repository root: %s\n' "$REPO_ROOT"
        printf 'PyInstaller arguments:'
        printf ' %s' "${PYINSTALLER_ARGUMENTS[@]}"
        printf '\n'
        exit 0
        ;;
    "") ;;
    *)
        printf 'Unknown argument: %s\n' "$1" >&2
        exit 2
        ;;
esac

cd -- "$REPO_ROOT"

if [[ -e "$BUILD_VENV" ]] && ! is_python312 "$VENV_PYTHON"; then
    safe_remove "$BUILD_VENV"
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    if ! BASE_PYTHON="$(find_python312)"; then
        printf 'Python 3.12 is required to build WorkNetConnector.\n' >&2
        exit 1
    fi
    "$BASE_PYTHON" -m venv "$BUILD_VENV"
fi

if ! is_python312 "$VENV_PYTHON"; then
    printf 'The repository-local build environment is not a valid Python 3.12 environment.\n' >&2
    exit 1
fi

BASE_PREFIX="$("$VENV_PYTHON" -c 'import sys; print(sys.base_prefix)')"
if [[ -d "$BASE_PREFIX/Library/lib/tcl8.6" && -d "$BASE_PREFIX/Library/lib/tk8.6" ]]; then
    export TCL_LIBRARY="$BASE_PREFIX/Library/lib/tcl8.6"
    export TK_LIBRARY="$BASE_PREFIX/Library/lib/tk8.6"
fi

"$VENV_PYTHON" -m pip install '.[dev]'
"$VENV_PYTHON" -m pytest

safe_remove "$BUILD_DIR" "$DIST_DIR" "$SPEC_PATH"
if [[ ! -x "$PYINSTALLER" ]]; then
    printf 'PyInstaller was not installed in the repository-local build environment.\n' >&2
    exit 1
fi
"$PYINSTALLER" "${PYINSTALLER_ARGUMENTS[@]}"
