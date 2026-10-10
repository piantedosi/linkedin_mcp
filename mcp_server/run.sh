#!/usr/bin/env bash
# Install this folder's Python env and Chromium when they are missing, then
# start the MCP server. Cursor runs this on connect. Nothing is written
# outside mcp_server.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export UV_CACHE_DIR="$ROOT/.uv-cache"
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.playwright"
PY="$ROOT/.venv/bin/python"
STAMP="$ROOT/.venv/.requirements-stamp"

need_py=0
if [[ ! -x "$PY" ]]; then
  need_py=1
else
  current="$(sha256sum "$ROOT/requirements.txt" | awk '{print $1}')"
  previous="$(cat "$STAMP" 2>/dev/null || true)"
  if [[ "$current" != "$previous" ]]; then
    need_py=1
  fi
fi

if [[ "$need_py" == 1 ]]; then
  if [[ ! -x "$PY" ]]; then
    uv venv "$ROOT/.venv" >&2
  fi
  uv pip install --python "$PY" -r "$ROOT/requirements.txt" >&2
  sha256sum "$ROOT/requirements.txt" | awk '{print $1}' > "$STAMP"
fi

if ! "$PY" - <<'PY' >&2
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        present = Path(playwright.chromium.executable_path).is_file()
except Exception:
    present = False
sys.exit(0 if present else 1)
PY
then
  "$PY" -m playwright install chromium >&2
fi

exec "$PY" "$ROOT/server.py"
