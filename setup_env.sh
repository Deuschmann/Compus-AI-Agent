#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Environment ready."
echo "Activate with: source .venv/bin/activate"
if ! command -v tectonic >/dev/null 2>&1 && ! command -v xelatex >/dev/null 2>&1; then
  echo "LaTeX compiler not found. Install tectonic, BasicTeX, or MacTeX if you need PDF compilation."
fi
