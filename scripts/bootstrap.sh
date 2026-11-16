#!/usr/bin/env bash
# Prepare this Linux (X11) machine for working ON DesktopPilot: the system
# packages, uv, the dev virtualenv, AT-SPI on, and a check of the session.
#
# Idempotent: a package already installed is not installed again. To just
# USE the tool, run scripts/install.sh instead.
#
#   scripts/bootstrap.sh
#
# Exit 0 when the machine is ready, 1 when a check failed (the report says
# which and what to do).
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# What `uv sync` needs to build PyGObject and pycairo, and what the tool
# calls at run time (AT-SPI, xclip, ffmpeg). The last three are diagnostics
# for an agent: the tool does not call them.
PACKAGES=(
    build-essential pkg-config python3-dev
    libgirepository-2.0-dev libcairo2-dev
    gir1.2-atspi-2.0 at-spi2-core xclip ffmpeg
    xdotool wmctrl x11-utils
)

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
failed=()
ok() { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$1"; failed+=("$1: $2"); }

step 'Checking system packages'
if command -v dpkg-query >/dev/null && command -v apt-get >/dev/null; then
    missing=()
    for pkg in "${PACKAGES[@]}"; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
            missing+=("$pkg")
        fi
    done
    if ((${#missing[@]})); then
        echo "installing: ${missing[*]}"
        sudo apt-get update
        sudo apt-get install -y "${missing[@]}"
    else
        echo 'all installed'
    fi
else
    echo "not a Debian/Ubuntu system: install the equivalents of ${PACKAGES[*]} by hand"
fi

step 'Checking uv'
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
    echo 'uv not found; installing it from astral.sh'
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv --version

step 'Syncing the dev virtualenv'
(cd "$repo" && uv sync --extra dev)

step 'Turning AT-SPI on'
if command -v gsettings >/dev/null; then
    before="$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null || echo unknown)"
    gsettings set org.gnome.desktop.interface toolkit-accessibility true
    echo "toolkit-accessibility: $before -> $(gsettings get org.gnome.desktop.interface toolkit-accessibility)"
    if [[ "$before" != true ]]; then
        echo 'restart the apps that were already open: an app reads it at start'
    fi
else
    echo 'gsettings not found: set org.gnome.desktop.interface toolkit-accessibility to true by hand'
fi

step 'Checking the session'
if [[ -n "${DISPLAY:-}" ]]; then
    ok "DISPLAY=$DISPLAY"
else
    bad 'DISPLAY' 'not set: run this from the X session (or export DISPLAY=:N)'
fi

if [[ "${XDG_SESSION_TYPE:-}" == x11 ]]; then
    ok 'XDG_SESSION_TYPE=x11'
elif [[ -z "${XDG_SESSION_TYPE:-}" && -n "${DISPLAY:-}" ]]; then
    ok 'XDG_SESSION_TYPE unset, DISPLAY set: taken as X11'
else
    bad 'XDG_SESSION_TYPE' "is '${XDG_SESSION_TYPE:-}': pilot needs an X11 session (no Wayland)"
fi

if [[ "$(gsettings get org.gnome.desktop.interface toolkit-accessibility 2>/dev/null)" == true ]]; then
    ok 'toolkit-accessibility true'
else
    bad 'toolkit-accessibility' 'is not true: gsettings set org.gnome.desktop.interface toolkit-accessibility true'
fi

if [[ -n "${NO_AT_BRIDGE:-}" ]]; then
    echo "  note  NO_AT_BRIDGE=$NO_AT_BRIDGE here (the VS Code snap's terminal): pilot strips it for the apps it starts;"
    echo '        an app you start by hand from this shell needs: env -u NO_AT_BRIDGE APP'
fi

# Read-only: ask the AT-SPI registry for the desktop, as the tool does.
atspi_check='
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.set_timeout(5000, 5000)
desktop = Atspi.get_desktop(0)
print(desktop.get_child_count())
'
if [[ -n "${DISPLAY:-}" ]] && apps="$(cd "$repo" && timeout 20 uv run --no-sync python -c "$atspi_check" 2>&1)"; then
    apps="${apps##*$'\n'}"
    if [[ "$apps" =~ ^[0-9]+$ ]] && ((apps > 0)); then
        ok "AT-SPI bus reachable: $apps applications on the desktop"
    else
        bad 'AT-SPI' "the registry answered '$apps' applications: is toolkit-accessibility on, and were the apps started after it?"
    fi
else
    bad 'AT-SPI' "the bus is not reachable (at-spi2-core running? a D-Bus session?): ${apps:-no X display}"
fi

for tool in xclip ffmpeg; do
    if command -v "$tool" >/dev/null; then ok "$tool"; else bad "$tool" 'not on PATH'; fi
done

step 'Done'
cat <<'EOF'
Install on PATH:  scripts/install.sh                          -> pilot
Unit gate:        uv run pytest -m "not word and not ui" -q
Lint:             uvx ruff check src tests && uvx ruff format .
Live gate:        uv run pytest tests/ui/linux -m ui -q      (an X session nobody else drives)
EOF

if ((${#failed[@]})); then
    printf '\nNOT READY:\n'
    printf '  - %s\n' "${failed[@]}"
    exit 1
fi
printf '\nReady.\n'
