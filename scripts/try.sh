#!/usr/bin/env bash
# scripts/try.sh — AgentCache "try before you adopt" one-liner
#
# Shell target: bash (requires pipefail and arrays). Use "| bash", not "| sh".
#
# SECURITY: Piping a remote script to a shell executes whatever the server
# returns at the moment of invocation — inspect before running:
#
#   curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | less
#   # or: save, inspect, then run:
#   curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh -o try.sh
#   less try.sh && bash try.sh
#
# This script is hosted in the AgentCache repository and is reviewable in
# version control — the same commit your system will run.
#
# ---------------------------------------------------------------------------
# Canonical one-liner (run from YOUR repo root — measures that repo):
#   curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | bash
#
# With an explicit target (path or GitHub URL):
#   curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | bash -s -- /path/to/repo
#   curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | bash -s -- https://github.com/user/repo.git
#
# Extra flags are passed through to try_agentcache.py:
#   curl -fsSL https://raw.githubusercontent.com/cdibona/AgentCache/main/scripts/try.sh | bash -s -- /path/to/repo --json
#
# What this script does (nothing is installed permanently):
#   1. Shallow-clones AgentCache to a temp dir  (tooling only — not measured)
#   2. Creates an isolated venv in that temp dir
#   3. Measures YOUR repo at TARGET (cwd by default)
#   4. Removes the temp dir unconditionally on exit
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# Capture TARGET *before* any cd or tempdir operations.
# $PWD here is the caller's cwd — the repo they want to measure.
#
# $1 = optional repo path or URL (default: caller's $PWD)
# Remaining positional args ($2, $3, …) are passed through to
# try_agentcache.py as extra flags (--json, --verbose, etc.).
# ---------------------------------------------------------------------------
TARGET="${1:-$PWD}"

# Shift so "$@" now holds only the extra flags (safe even when $# == 0).
if [ $# -ge 1 ]; then
    shift
fi
EXTRA_ARGS=("$@")

# ---------------------------------------------------------------------------
# Temp dir: created once, removed unconditionally on EXIT.
# The AgentCache clone lives here; the user's repo is NOT touched.
# ---------------------------------------------------------------------------
TMP_DIR="$(mktemp -d -t try_agentcache_XXXXXX)"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

AGENTCACHE_CLONE="$TMP_DIR/AgentCache"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
printf '\n'
printf '================================================================\n'
printf '  AgentCache — try before you adopt\n'
printf '================================================================\n'
printf '  Measuring:   %s\n' "$TARGET"
printf '  Tooling dir: %s  (temp, deleted on exit)\n' "$AGENTCACHE_CLONE"
printf '================================================================\n'
printf '\n'

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
if ! command -v git > /dev/null 2>&1; then
    printf 'ERROR: git is required but not found on PATH.\n' >&2
    exit 1
fi

if ! command -v python3 > /dev/null 2>&1; then
    printf 'ERROR: python3 (>=3.10) is required but not found on PATH.\n' >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1/3 — Shallow-clone AgentCache tooling into the temp dir.
#            This clone provides the harness code only; it is NOT the repo
#            being measured (TARGET is).
# ---------------------------------------------------------------------------
printf '  [1/3] Cloning AgentCache tooling (shallow) ...\n'
git clone --depth=1 --quiet \
    https://github.com/cdibona/AgentCache "$AGENTCACHE_CLONE"

# ---------------------------------------------------------------------------
# Step 2/3 — Create an isolated venv and install harness dependencies.
#            Mirrors the dep list from testharness/start.sh.
# ---------------------------------------------------------------------------
printf '  [2/3] Creating venv and installing dependencies ...\n'

python3 -m venv "$AGENTCACHE_CLONE/.venv"

"$AGENTCACHE_CLONE/.venv/bin/pip" install --quiet --upgrade pip
"$AGENTCACHE_CLONE/.venv/bin/pip" install --quiet \
    "pygit2>=1.15"            \
    "Flask>=3.0"              \
    "python-dotenv>=1.0"      \
    "fastapi>=0.111"          \
    "uvicorn[standard]>=0.29" \
    "aiofiles>=23"
# Install agentcache itself as editable so scripts/ is importable.
"$AGENTCACHE_CLONE/.venv/bin/pip" install --quiet -e "$AGENTCACHE_CLONE"

# universal-ctags is optional; without it the symbol index will be empty.
if ! command -v ctags > /dev/null 2>&1; then
    printf '  [advisory] universal-ctags not found — symbol index will be empty.\n'
    printf '             Install with: sudo apt-get install -y universal-ctags\n'
fi

# ---------------------------------------------------------------------------
# Step 3/3 — Measure TARGET (the user's repo — NOT the AgentCache clone).
#
# try_agentcache.py is invoked with $TARGET as the first argument so it
# measures the user's repo, not $AGENTCACHE_CLONE.
# Extra flags (--json, --verbose, …) follow via ${EXTRA_ARGS[@]}.
# ---------------------------------------------------------------------------
printf '  [3/3] Running measurement on: %s\n' "$TARGET"
printf '\n'
printf '  (Starts a git daemon + byte-counting proxy + agentcache service on\n'
printf '   ephemeral ports — no system ports are touched, no state persists.)\n'
printf '\n'

"$AGENTCACHE_CLONE/.venv/bin/python" \
    "$AGENTCACHE_CLONE/scripts/try_agentcache.py" \
    "$TARGET" \
    "${EXTRA_ARGS[@]}"
