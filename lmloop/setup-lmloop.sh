#!/bin/bash
# One-time setup for lmloop. Requires Python 3.10+ (Homebrew python preferred
# over Apple's 3.9). Creates lmloop/.venv, installs requirements (prompt_toolkit,
# rich, ddgs), symlinks the launcher into ~/.local/bin, and checks LM Studio.
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
VENV="$HERE/.venv"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

echo "== lmloop setup =="

py_at_least() {
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)" 2>/dev/null
}

find_python() {
  local c
  for c in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    python3.14 python3.13 python3.12 python3.11 python3.10 \
    python3
  do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
      if py_at_least "$c"; then
        command -v "$c" 2>/dev/null || echo "$c"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  echo "error: Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ is required." >&2
  echo "  Found: $(command -v python3 >/dev/null && python3 --version || echo 'no python3')" >&2
  echo "  macOS:  brew install python" >&2
  echo "  Linux:  install python3.12+ from your package manager" >&2
  exit 1
fi
echo "using $PYTHON ($("$PYTHON" --version 2>&1))"

# Recreate the venv when missing or too old (e.g. leftover Apple 3.9 tree).
if [ ! -x "$VENV/bin/python" ] || ! py_at_least "$VENV/bin/python"; then
  echo "creating $VENV and installing requirements…"
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
else
  echo "reusing $VENV ($("$VENV/bin/python" --version 2>&1))"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r "$HERE/requirements.txt"
python -c 'import sys
from importlib.metadata import version
print("python", "%d.%d.%d" % sys.version_info[:3])
for pkg in ("prompt-toolkit", "rich", "ddgs"):
    print(pkg, version(pkg))'
deactivate

# 3. Launcher symlink
mkdir -p "$HOME/.local/bin"
chmod +x "$HERE/bin/lmloop"
ln -sfn "$HERE/bin/lmloop" "$HOME/.local/bin/lmloop"
echo "linked ~/.local/bin/lmloop -> $HERE/bin/lmloop"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    echo
    echo "note: ~/.local/bin is not on your PATH. Add this to ~/.zshrc.local:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac

# 4. LM Studio toolchain (informational — lmloop also works against any
#    OpenAI-compatible server via `lmloop config set base_url <url>`)
echo
if command -v lms >/dev/null 2>&1; then
  echo "lms CLI found: $(command -v lms)"
  echo "lmloop will auto-run 'lms server start' / 'lms load' when needed."
else
  echo "lms CLI not found. Install LM Studio from https://lmstudio.ai, then run:"
  echo "  ~/.lmstudio/bin/lms bootstrap   (installs the lms CLI on PATH)"
fi

echo
echo "done. Try:  lmloop \"list the files here and summarize what this project is\""
