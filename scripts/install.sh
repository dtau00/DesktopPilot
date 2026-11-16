#!/usr/bin/env bash
# Install DesktopPilot on PATH as a uv tool: `pilot` (Linux, X11).
#
# Idempotent - safe to re-run, and --editable means a later `git pull` needs
# no reinstall. Prints where the executable landed and runs `pilot apps` as a
# smoke check. The build needs the system packages scripts/bootstrap.sh
# installs.
#
#   scripts/install.sh
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }

original_path="$PATH"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
    echo 'uv not found: run scripts/bootstrap.sh, or install uv (https://docs.astral.sh/uv/)' >&2
    exit 1
fi

step "Installing $repo as a uv tool"
uv tool install --editable "$repo" --force

bin_dir="$(uv tool dir --bin)"
if [[ ":$original_path:" != *":$bin_dir:"* ]]; then
    step 'Adding the uv tool bin directory to PATH'
    uv tool update-shell
    echo "open a new shell, or run: export PATH=\"$bin_dir:\$PATH\""
fi
export PATH="$bin_dir:$PATH"

step 'Executable'
if found="$(command -v pilot)"; then
    printf '%-10s %s\n' pilot "$found"
else
    echo "pilot is not on PATH; run 'uv tool update-shell' and open a new shell." >&2
fi

step 'Smoke check: pilot apps'
pilot apps

step 'Done'
cat <<'EOF'
pilot <app> <group> <command>   e.g. pilot code ui tree

If a NEW shell still cannot find it, run: uv tool update-shell
EOF
