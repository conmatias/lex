#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS_NAME="$(uname -s)"

if [[ -n "${LEX_BUILD_ROOT:-}" ]]; then
  BUILD_ROOT="$LEX_BUILD_ROOT"
elif [[ "$OS_NAME" == "Darwin" && -d /opt/homebrew ]]; then
  BUILD_ROOT="/opt/homebrew/var/lex"
else
  BUILD_ROOT="/opt/lex"
fi

if [[ -n "${LEX_GLOBAL_BIN_DIR:-}" ]]; then
  GLOBAL_BIN_DIR="$LEX_GLOBAL_BIN_DIR"
elif [[ "$OS_NAME" == "Darwin" && -d /opt/homebrew/bin ]]; then
  GLOBAL_BIN_DIR="/opt/homebrew/bin"
else
  GLOBAL_BIN_DIR="/usr/local/bin"
fi

COMMAND_NAME="lx"

VENV_DIR="$BUILD_ROOT/venv"
MIRROR_DIR="$BUILD_ROOT/src"

find_python() {
  local candidates=()

  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("$PYTHON_BIN")
  fi

  candidates+=(python3.13 python3.12 python3.11 python3.10 python3)

  for bin in "${candidates[@]}"; do
    if ! command -v "$bin" >/dev/null 2>&1; then
      continue
    fi
    if "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
      printf '%s\n' "$bin"
      return 0
    fi
  done

  echo "error: could not find a Python 3.10+ interpreter" >&2
  exit 1
}

PYTHON_BIN="$(find_python)"

mkdir_cmd=(mkdir -p "$BUILD_ROOT")
rsync_cmd=(
  rsync
  -a
  --delete
  --exclude
  .git/
  --exclude
  .venv/
  --exclude
  __pycache__/
  --exclude
  .pytest_cache/
  --exclude
  build/
  --exclude
  dist/
  "$SCRIPT_DIR/"
  "$MIRROR_DIR/"
)

run_maybe_sudo() {
  if [[ -w "$1" || (! -e "$1" && -w "$(dirname "$1")") ]]; then
    shift
    "$@"
    return
  fi

  if command -v sudo >/dev/null 2>&1; then
    shift
    sudo "$@"
    return
  fi

  echo "error: cannot write to $1 and sudo is unavailable" >&2
  exit 1
}

venv_python_compatible() {
  [[ -x "$VENV_DIR/bin/python" ]] || return 1
  "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
}

echo "Using build root: $BUILD_ROOT"
echo "Using Python: $PYTHON_BIN"
run_maybe_sudo "$BUILD_ROOT" "${mkdir_cmd[@]}"
run_maybe_sudo "$BUILD_ROOT" mkdir -p "$MIRROR_DIR"
run_maybe_sudo "$BUILD_ROOT" "${rsync_cmd[@]}"

if [[ -d "$VENV_DIR" ]] && ! venv_python_compatible; then
  echo "Rebuilding venv because it uses Python < 3.10"
  run_maybe_sudo "$BUILD_ROOT" rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  run_maybe_sudo "$BUILD_ROOT" "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

PIP_BIN="$VENV_DIR/bin/pip"
LEX_BIN="$VENV_DIR/bin/$COMMAND_NAME"
GLOBAL_BIN="$GLOBAL_BIN_DIR/$COMMAND_NAME"

run_maybe_sudo "$BUILD_ROOT" "$PIP_BIN" install --upgrade pip setuptools
run_maybe_sudo "$BUILD_ROOT" "$PIP_BIN" install --upgrade "$MIRROR_DIR"
run_maybe_sudo "$GLOBAL_BIN_DIR" mkdir -p "$GLOBAL_BIN_DIR"
run_maybe_sudo "$GLOBAL_BIN_DIR" ln -sfn "$LEX_BIN" "$GLOBAL_BIN"

echo
echo "lex installed into:"
echo "  $VENV_DIR"
echo
echo "global command:"
echo "  $GLOBAL_BIN"
echo
echo "venv entrypoint:"
echo "  $LEX_BIN"
