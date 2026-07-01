#!/usr/bin/env bash
# install.sh — one-time setup for the `neoxider` command (bash / git-bash / macOS / Linux).
# Run this yourself (it does not run itself): appends a PATH export to your shell rc file.
#
#   bash bin/install.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rc="$HOME/.bashrc"
[ -n "${ZSH_VERSION:-}" ] && rc="$HOME/.zshrc"
line="export PATH=\"$HERE:\$PATH\""

if [ -f "$rc" ] && grep -qF "$HERE" "$rc" 2>/dev/null; then
    echo "Already set up in $rc"
else
    { echo ""; echo "# neoxider-agents: added by bin/install.sh"; echo "$line"; } >> "$rc"
    echo "Added to $rc"
    echo "Open a NEW terminal (or: source $rc), then run: neoxider doctor"
fi
