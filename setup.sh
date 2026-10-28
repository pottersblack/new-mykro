#!/usr/bin/env bash
# One command after clone. Presents A / B / C / D, then starts MYKRO.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Installing python3, venv, pip, and tk..."
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip python3-tk
fi

exec python3 setup.py "$@"
