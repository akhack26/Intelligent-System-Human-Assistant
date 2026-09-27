#!/usr/bin/env bash
# ISHA Setup bootstrapper (Linux). Finds Python 3.10-3.13 with venv support, offers the exact
# distro command if it is missing, builds a small setup environment and starts the installer
# (graphical when a display exists, text mode otherwise). Never runs anything as root silently.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_ENV="${XDG_CACHE_HOME:-$HOME/.cache}/isha-setup/env"
OFFLINE=""; [ -d "$HERE/offline" ] && OFFLINE="$HERE/offline"
HEADLESS=0; for a in "$@"; do [ "$a" = "--headless" ] && HEADLESS=1; done

ok_python() {
  "$1" - <<'PY' 2>/dev/null
import sys
try:
    import venv, ensurepip
except Exception:
    sys.exit(1)
sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) and sys.maxsize > 2**32 else 1)
PY
}

PY=""
for c in python3.12 python3.11 python3.13 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && ok_python "$(command -v "$c")"; then PY="$(command -v "$c")"; break; fi
done

if [ -z "$PY" ]; then
  echo "Python 3.10-3.13 with the venv module was not found."
  if command -v apt-get >/dev/null; then CMD="sudo apt-get install -y python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null; then CMD="sudo dnf install -y python3 python3-pip"
  elif command -v pacman >/dev/null; then CMD="sudo pacman -S --needed python python-pip"
  elif command -v zypper >/dev/null; then CMD="sudo zypper install -y python3 python3-pip"
  else echo "Install Python 3.12 from your distribution, then run this script again."; exit 1; fi
  read -r -p "Run '$CMD' now? [y/N] " ans
  case "$ans" in y|Y|yes) eval "$CMD" ;; *) echo "Setup cancelled."; exit 1 ;; esac
  for c in python3.12 python3.11 python3.13 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && ok_python "$(command -v "$c")"; then PY="$(command -v "$c")"; break; fi
  done
  [ -z "$PY" ] && { echo "Still no compatible Python found."; exit 1; }
fi
echo "Python detected: $PY ($("$PY" -V))"

[ -x "$SETUP_ENV/bin/python3" ] || "$PY" -m venv "$SETUP_ENV"
PIP_ARGS=(--disable-pip-version-check -q --prefer-binary PyQt5 psutil)
[ -n "$OFFLINE" ] && [ -d "$OFFLINE/wheels" ] && PIP_ARGS+=(--find-links "$OFFLINE/wheels")
if ! "$SETUP_ENV/bin/python3" -m pip install "${PIP_ARGS[@]}"; then
  echo "Setup UI could not be installed - using text mode."; HEADLESS=1
fi
cd "$HERE"
ARGS=(-m installer); [ -n "$OFFLINE" ] && ARGS+=(--offline-dir "$OFFLINE")
if [ "$HEADLESS" = 1 ] || { [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; }; then ARGS+=(--headless); fi
exec "$SETUP_ENV/bin/python3" "${ARGS[@]}" "$@"
