#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv sync --extra desktop --group build
uv run --extra desktop --group build python scripts/build_desktop.py
