#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# testharness/start.sh
#
# One-command startup for the PackCache Test Harness.
#
# Starts:
#   - git daemon          port 9418  (serves benchmark/repos/)
#   - counting proxy      port 9419  (forwards to 9418, counts bytes)
#   - agentcache service  port 8765  (started per-repo on demand)
#   - FastAPI web UI      port 8080  (http://localhost:8080)
#
# All traffic flows: test client → proxy:9419 → git daemon:9418
# Bytes are attributed per test run via snapshot/delta on the proxy.
#
# Usage:
#   bash testharness/start.sh
#   bash testharness/start.sh --port 9090     # custom web port
#   bash testharness/start.sh --open          # open browser automatically
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."   # always run from repo root

WEB_PORT=8080
OPEN_BROWSER=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)    WEB_PORT="$2"; shift 2 ;;
        --port=*)  WEB_PORT="${1#--port=}"; shift ;;
        --open)    OPEN_BROWSER=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Ensure venv exists and has the required packages.
# ---------------------------------------------------------------------------
if [[ ! -f .venv/bin/python ]]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Checking / installing dependencies..."
.venv/bin/pip install --quiet \
    "fastapi>=0.111" "uvicorn[standard]>=0.29" "aiofiles>=23" \
    "pygit2>=1.15" "Flask>=3.0" "python-dotenv>=1.0" \
    -e ".[dev]" 2>&1 | tail -3 || true

# ---------------------------------------------------------------------------
# Check for repos (warn, don't block).
# ---------------------------------------------------------------------------
REPOS_DIR="benchmark/repos"
if [[ ! -d "$REPOS_DIR" ]] || [[ -z "$(ls "$REPOS_DIR" 2>/dev/null)" ]]; then
    echo ""
    echo "WARNING: No repos found in $REPOS_DIR/"
    echo "         The UI will show a setup prompt. Add a repo with:"
    echo "         bash benchmark/setup_repo.sh --source /your/clone $REPOS_DIR/myrepo.git"
    echo ""
fi

# ---------------------------------------------------------------------------
# Create data directory for SQLite.
# ---------------------------------------------------------------------------
mkdir -p testharness/data

# ---------------------------------------------------------------------------
# Start the web server.
# ---------------------------------------------------------------------------
export AGENTCACHE_WEB_PORT="$WEB_PORT"

echo ""
echo "════════════════════════════════════════════════════"
echo "  PackCache Test Harness"
echo "  http://127.0.0.1:${WEB_PORT}"
echo ""
echo "  git daemon  → port 9418"
echo "  proxy       → port 9419 (byte counting)"
echo "  agentcache  → port 8765 (per-repo, on demand)"
echo "════════════════════════════════════════════════════"
echo ""
echo "Press Ctrl-C to stop."
echo ""

if [[ $OPEN_BROWSER -eq 1 ]]; then
    # Give uvicorn a moment to bind before opening the browser.
    (sleep 2 && xdg-open "http://127.0.0.1:${WEB_PORT}" 2>/dev/null \
        || open "http://127.0.0.1:${WEB_PORT}" 2>/dev/null || true) &
fi

exec .venv/bin/uvicorn testharness.app:app \
    --host 127.0.0.1 \
    --port "$WEB_PORT" \
    --reload \
    --reload-dir testharness \
    --reload-dir agentcache \
    --log-level info
