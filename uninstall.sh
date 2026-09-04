#!/bin/sh
# Remove the hooks, stop the daemon and unlink the entry points.
# Leaves your config file alone -- delete it yourself if you want to.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}
BUZ="$ROOT/bin/buzerkostka"

"$BUZ" uninstall || true
"$BUZ" off       || true
"$BUZ" stop      || true

for name in buzerkostka buzerkostka-event; do
  target="$BIN_DIR/$name"
  if [ -L "$target" ]; then rm -f "$target"; echo "removed $target"; fi
done

printf '\033[32m%s\033[0m\n' "ok   uninstalled"
echo "Your configuration is still at $("$BUZ" config --path)"
