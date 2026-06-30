#!/usr/bin/env bash
# scripts/try.sh — "Try before you adopt" entry point for AgentCache.
#
# Measures whether AgentCache would benefit YOUR repo and by how much,
# by running a real two-pass experiment (COLD → WARM) through the testharness
# byte-counting proxy.
#
# Usage (from within an AgentCache checkout):
#   bash scripts/try.sh <path-or-URL>
#   bash scripts/try.sh https://github.com/your/repo.git
#   bash scripts/try.sh /path/to/local/repo
#
# For the one-liner install pattern:
#   curl -fsSL https://raw.githubusercontent.com/…/main/scripts/try.sh | bash -s -- <TARGET>
#
# ⚠ SECURITY NOTE: piping a remote script into bash grants it unrestricted
# execution on your machine.  Inspect the script at the URL before running.
# All code here is in this checkout — nothing is downloaded at runtime.
# The dependency installation (pip install) is the only network operation.
#
# Requirements:
#   - python3 (>=3.10) and git must be on $PATH
#   - Must run from within (or set AGENTCACHE_DIR to) an AgentCache checkout
#   - Internet access for pip install (or use a local wheel cache)
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve the AgentCache checkout root
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/try.sh lives at <root>/scripts/try.sh
AGENTCACHE_DIR="${AGENTCACHE_DIR:-$(dirname "$SCRIPT_DIR")}"

if [[ ! -f "$AGENTCACHE_DIR/pyproject.toml" ]]; then
    echo "ERROR: Could not find AgentCache checkout." >&2
    echo "  Run this script from within an AgentCache clone, or set AGENTCACHE_DIR." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Argument
# ---------------------------------------------------------------------------
if [[ $# -lt 1 ]]; then
    echo "Usage: bash scripts/try.sh <repo-path-or-URL>" >&2
    exit 1
fi
TARGET="$1"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          AgentCache — try before you adopt               ║"
echo "║  Measuring whether AgentCache would help: ${TARGET:0:30}"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------------------
# Create a throwaway venv in a temp dir so we leave the user's env clean
# ---------------------------------------------------------------------------
WORK_DIR="$(mktemp -d -t try_agentcache_XXXXXX)"
VENV_DIR="$WORK_DIR/venv"

cleanup() {
    echo ""
    echo "Cleaning up temporary environment…"
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "Creating isolated venv in $VENV_DIR …"
python3 -m venv "$VENV_DIR"

echo "Installing dependencies …"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet \
    "pygit2>=1.15" \
    "Flask>=3.0" \
    "python-dotenv>=1.0" \
    "fastapi>=0.111" \
    "uvicorn[standard]>=0.29" \
    "aiofiles>=23"

# Install the agentcache package itself (editable so scripts/ is importable)
"$VENV_DIR/bin/pip" install --quiet -e "$AGENTCACHE_DIR"

echo ""
echo "Running measurement …"
echo "(This starts a git daemon, byte-counting proxy, and agentcache service"
echo " on ephemeral ports — no system ports touched, no persistent state.)"
echo ""

# ---------------------------------------------------------------------------
# Run the measurement
# ---------------------------------------------------------------------------
"$VENV_DIR/bin/python" "$AGENTCACHE_DIR/scripts/try_agentcache.py" "$TARGET"
