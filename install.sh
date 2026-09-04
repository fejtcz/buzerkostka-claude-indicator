#!/bin/sh
# One-shot installer for buzerkostka-claude-indicator.
#
#   git clone https://github.com/fejtcz/buzerkostka-claude-indicator
#   cd buzerkostka-claude-indicator && ./install.sh
#
# There is nothing to compile and nothing to pip install -- the project is
# stdlib-only Python. This script just makes the entry points executable,
# puts `buzerkostka` on your PATH, and hands over to the setup wizard.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
green(){ printf '\033[32m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# --- 1. interpreter ---------------------------------------------------
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON=$candidate; break; fi
  done
fi
if [ -z "$PYTHON" ]; then
  red "No python3 found. Install Python 3.8 or newer and re-run."
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  red "$PYTHON is too old; Python 3.8 or newer is required."
  exit 1
fi
green "ok   using $($PYTHON -V 2>&1) at $(command -v "$PYTHON")"

# --- 2. entry points --------------------------------------------------
chmod +x "$ROOT/bin/buzerkostka" "$ROOT/bin/buzerkostka-event"
green "ok   entry points are executable"

# --- 3. PATH ----------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$ROOT/bin/buzerkostka" "$BIN_DIR/buzerkostka"
ln -sf "$ROOT/bin/buzerkostka-event" "$BIN_DIR/buzerkostka-event"
green "ok   linked into $BIN_DIR"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf '\033[33m%s\033[0m\n' \
       "warn $BIN_DIR is not on your PATH -- add it to your shell profile:" ;
     echo "       export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac

# --- 4. configure -----------------------------------------------------
CONFIG_PATH=$("$ROOT/bin/buzerkostka" config --path)
echo
if [ -f "$CONFIG_PATH" ]; then
  bold "Existing configuration found at $CONFIG_PATH"
  echo "Run 'buzerkostka setup' if you want to change it."
  "$ROOT/bin/buzerkostka" doctor || true
else
  bold "Let's configure your cube."
  "$ROOT/bin/buzerkostka" setup
fi

echo
green "Done. Useful next steps:"
echo "  buzerkostka doctor     # check everything end to end"
echo "  buzerkostka demo       # watch every state on the cube"
echo "  buzerkostka status     # what is the light showing, and why"
