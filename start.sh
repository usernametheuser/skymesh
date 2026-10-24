#!/usr/bin/env sh
# SkyMesh OS - start on Linux / macOS / Raspberry Pi. First run installs dependencies (internet needed once).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  command -v python3 >/dev/null || { echo "Install Python 3.11+ (e.g. sudo apt install python3 python3-venv)"; exit 1; }
  echo "Creating the SkyMesh environment (first run only)..."
  python3 -m venv .venv
fi
if [ ! -f .venv/.installed ]; then
  echo "Installing SkyMesh components (first run only, needs internet)..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r backend/requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python backend/skymesh.py serve "$@"
