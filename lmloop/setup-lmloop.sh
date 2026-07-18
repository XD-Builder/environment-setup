#!/bin/bash
# One-time setup for lmloop. Zero pip dependencies (stdlib only, Python 3.9+).
# Symlinks the launcher into ~/.local/bin and checks the LM Studio toolchain.
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

echo "== lmloop setup =="

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found. Install it first (brew install python3)." >&2
  exit 1
fi
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "python3 $PYVER found"

# 2. Launcher symlink
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

# 3. LM Studio toolchain (informational — lmloop also works against any
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
