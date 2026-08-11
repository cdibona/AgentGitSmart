#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# benchmark/setup_repo.sh
#
# Initialise a LOCAL bare git repo for benchmarking agentgitsmart.
# Everything stays on this machine — no public repositories are touched.
#
# TYPICAL WORKFLOW
#
#   # 1. You have cpython on disk already (local clone, backup, etc.).
#   #    Point --source at it and we mirror into a bare repo here.
#   bash benchmark/setup_repo.sh \
#       --source /path/to/your/local/cpython-clone \
#       benchmark/repos/cpython.git
#
#   # 2. Start the agentgitsmart service in one terminal:
#   AGENTGITSMART_REPO_DIR=benchmark/repos/cpython.git \
#       .venv/bin/python -m agentgitsmart.service &
#
#   # 3. Run the benchmark in another:
#   .venv/bin/python -m benchmark.run \
#       --repo benchmark/repos/cpython.git \
#       --branch main \
#       --paths Lib/asyncio/tasks.py Lib/ast.py \
#       --runs 3
#
# USING git daemon (optional, more realistic network simulation)
#
#   After setup you can serve the repo over git:// instead of file://:
#
#   git daemon --reuseaddr --base-path=benchmark/repos \
#       --export-all benchmark/repos/ &
#
#   Then pass --repo git://localhost/cpython.git to run.py.
#
# SMOKE TEST (no setup required)
#
#   .venv/bin/python -m benchmark.run --smoke
# ---------------------------------------------------------------------------
set -euo pipefail

usage() {
    echo "Usage: $0 [--source <local-clone-path>] <dest-bare-repo-path>" >&2
    echo "" >&2
    echo "  --source  Path to an existing local git repo to mirror." >&2
    echo "            If omitted, a tiny synthetic fixture repo is created instead." >&2
    echo "" >&2
    echo "  dest      Path for the new bare repo (e.g. benchmark/repos/cpython.git)." >&2
    exit 1
}

SOURCE=""
DEST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE="$2"; shift 2 ;;
        --source=*)
            SOURCE="${1#--source=}"; shift ;;
        -h|--help)
            usage ;;
        *)
            if [[ -z "$DEST" ]]; then
                DEST="$1"; shift
            else
                echo "Unexpected argument: $1" >&2; usage
            fi ;;
    esac
done

if [[ -z "$DEST" ]]; then
    usage
fi

# ---------------------------------------------------------------------------
# Step 1: Create / populate the bare repo.
# ---------------------------------------------------------------------------
DEST_ABS="$(realpath -m "$DEST")"
mkdir -p "$(dirname "$DEST_ABS")"

if [[ -n "$SOURCE" ]]; then
    echo "==> Mirroring $SOURCE -> $DEST_ABS"
    if [[ -d "$DEST_ABS" ]]; then
        echo "    Updating existing mirror..."
        git --git-dir="$DEST_ABS" remote update --prune
    else
        git clone --mirror "$SOURCE" "$DEST_ABS"
    fi
else
    echo "==> No --source given; creating a tiny synthetic fixture repo."
    if [[ -d "$DEST_ABS" ]]; then
        echo "    $DEST_ABS already exists, skipping init."
    else
        git init --bare "$DEST_ABS"
        # Populate via Python so we use the same fixture as the test suite.
        python3 - "$DEST_ABS" <<'PYEOF'
import sys, pygit2
from collections import defaultdict

FILES = {
    "src/app.py": (
        '"""Token refresh helpers."""\n'
        "from __future__ import annotations\n\n\n"
        "class TokenRefresher:\n"
        "    def __init__(self, client_id: str, client_secret: str) -> None:\n"
        "        self.client_id = client_id\n"
        "        self.client_secret = client_secret\n"
        '        self._token: str = ""\n\n'
        "    def refresh(self) -> str:\n"
        '        self._token = "refreshed"\n'
        "        return self._token\n\n\n"
        "def make_refresher(client_id: str, client_secret: str):\n"
        "    return TokenRefresher(client_id, client_secret)\n"
    ),
    "src/util.c": "#include <string.h>\nsize_t str_len(const char *s){return strlen(s);}\n",
    "README.md": "# benchmark fixture repo\n",
}

def build(repo, files):
    root, subs = {}, defaultdict(dict)
    for p, v in files.items():
        h, _, t = p.partition("/")
        (subs[h] if t else root)[t or h] = v
    b = repo.TreeBuilder()
    for n in sorted(root):
        b.insert(n, repo.create_blob(root[n].encode()), 0o100644)
    for d in sorted(subs):
        b.insert(d, build(repo, subs[d]), 0o040000)
    return b.write()

r = pygit2.Repository(sys.argv[1])
oid = r.create_commit(
    "refs/heads/master",
    pygit2.Signature("Setup", "setup@local"),
    pygit2.Signature("Setup", "setup@local"),
    "Initial commit\n",
    build(r, FILES), []
)
print(f"Created commit {oid}")
PYEOF
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: Configure the repo for agentgitsmart and filtered fetches.
# ---------------------------------------------------------------------------
echo "==> Configuring uploadpack settings..."
git --git-dir="$DEST_ABS" config uploadpack.allowFilter       true
git --git-dir="$DEST_ABS" config uploadpack.allowanysha1inwant true

# ---------------------------------------------------------------------------
# Step 3: Generate the agentgitsmart artifacts for HEAD on every branch.
# ---------------------------------------------------------------------------
echo "==> Generating agentgitsmart cache artifacts..."
AGENTGITSMART_REPO_DIR="$DEST_ABS" \
    python3 - "$DEST_ABS" <<'PYEOF'
import sys, json, os, pygit2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentgitsmart.config import AgentGitSmartConfig
from agentgitsmart.hook import generate_for_commit

repo_dir = sys.argv[1]
r = pygit2.Repository(repo_dir)
cfg = AgentGitSmartConfig(repo_dir=repo_dir)

generated = 0
for ref_name in r.references:
    if not ref_name.startswith("refs/heads/"):
        continue
    ref = r.references[ref_name]
    commit = ref.peel(pygit2.Commit)
    commit_hex = str(commit.id)
    print(f"  {ref_name}: {commit_hex[:12]}...", end=" ", flush=True)
    result = generate_for_commit(r, commit_hex, cfg)
    n = result["meta"]["manifest_entries"]
    s = result["meta"]["symbol_count"]
    print(f"({n} files, {s} symbols)")
    generated += 1

if generated == 0:
    print("  WARNING: no refs/heads/* found — is the repo empty?")
else:
    print(f"  Done. {generated} branch(es) cached.")
PYEOF

# ---------------------------------------------------------------------------
# Done.
# ---------------------------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""
echo "Repo:  $DEST_ABS"
echo ""
echo "Next steps:"
echo ""
echo "  1. Start the agentgitsmart service:"
echo "     AGENTGITSMART_REPO_DIR=$DEST_ABS \\"
echo "         .venv/bin/python -m agentgitsmart.service &"
echo ""
echo "  2. Run the benchmark (examples):"
echo ""
echo "     # Small task on the default branch:"
echo "     .venv/bin/python -m benchmark.run \\"
echo "         --repo $DEST_ABS \\"
echo "         --paths src/app.py \\"
echo "         --runs 3"
echo ""
if [[ -n "$SOURCE" ]]; then
echo "     # Realistic cpython agent task:"
echo "     .venv/bin/python -m benchmark.run \\"
echo "         --repo $DEST_ABS \\"
echo "         --branch main \\"
echo "         --paths Lib/asyncio/tasks.py Lib/ast.py \\"
echo "         --runs 3"
echo ""
fi
echo "  3. (Optional) Serve via git daemon for network realism:"
echo "     git daemon --reuseaddr \\"
echo "         --base-path=$(dirname "$DEST_ABS") \\"
echo "         --export-all $(dirname "$DEST_ABS")/ &"
echo "     # Then use --repo git://localhost/$(basename "$DEST_ABS")"
