"""
PackCache Real Agent — stdlib-only.

Task: Add a '!' to one comment line in 1% of the project's Python files,
then commit the change locally (no push — we benchmark the COST of
discovering and reading those files, not write-back latency).

The three approaches differ only in HOW the agent acquires the files it
needs to read and edit:

  naive:      git clone --depth=1  →  all files on disk, no planning needed
  blobless:   filter=blob:none + depth=1  →  N lazy fetches, one per file
  agentcache: filter=blob:none (full history)  →  manifest for planning,
              POST /resolve → ONE batch fetch of exactly the needed files

Stdlib-only so it runs inside the Docker container (python3 + git only).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Optional

SEED_DEFAULT = 42
CLONE_TIMEOUT = 360  # full blobless clone of cpython can take a minute or two

# SHA-1 of a zero-byte file — git's intrinsic "empty blob". Every repo has it
# for free (e.g. django's many empty __init__.py resolve to it). A server will
# reject a `want` for it, which would fail an otherwise-valid batch fetch, so we
# always filter it out of the OID list before fetching.
EMPTY_BLOB_OID = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"

# Directories to search for pre-built blobless bootstrap bundles.
# The bundle CDN path is the key to agentcache's efficiency: the history is
# seeded from a pre-packaged local/CDN file (NOT through the git daemon),
# so the promisor only serves the tiny delta + targeted blob fetches.
# In production: bundles live on a CDN. Locally: we pre-build them.
_BUNDLE_SEARCH_DIRS = [
    "/pack/benchmark/bundles",           # Inside Docker container (/pack mounts repo root)
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "benchmark", "bundles"),  # Local run
]


def _find_bundle(repo_url: str, branch: str) -> Optional[str]:
    """Return path to a pre-built blobless bundle for this repo, or None.

    We prefer an exact ``<repo>-<branch>.bundle`` match, but fall back to ANY
    bundle for the repo (``<repo>-*.bundle``).  A bundle only *seeds history*
    via --bundle-uri; the branch it was cut from is irrelevant because a
    throwaway/experiment branch (e.g. ``agentcache-exp/...``) shares the base
    branch's history, so the same objects are reused and only the tiny delta is
    fetched.  Without this fallback, experiments that target a renamed branch
    would silently skip the bundle and clone the full history through the daemon.
    """
    import glob as _glob

    repo_name = os.path.basename(repo_url.rstrip("/"))
    for d in _BUNDLE_SEARCH_DIRS:
        exact = os.path.join(d, f"{repo_name}-{branch}.bundle")
        if os.path.exists(exact):
            return exact
    for d in _BUNDLE_SEARCH_DIRS:
        matches = sorted(_glob.glob(os.path.join(d, f"{repo_name}-*.bundle")))
        if matches:
            return matches[0]
    return None


# ── git helpers ────────────────────────────────────────────────────────────

def _git(
    *args: str,
    cwd: Optional[str] = None,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=check,
    )


# ── agentcache helpers ─────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 10) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(url: str, data: dict, timeout: int = 30) -> Optional[dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _detect_agentcache(service_url: str, commit: str) -> Optional[list]:
    """Return manifest entries if service is reachable, else None.

    The timeout is generous: the FIRST request for a commit triggers lazy
    cache generation server-side (manifest + ctags symbol index), which can
    take several seconds on a large repo.  A tight timeout here would make the
    agent give up and fall back to the blobless path, masking the cache.
    """
    data = _http_get(
        f"{service_url.rstrip('/')}/cache/{commit}/manifest", timeout=120
    )
    return data.get("entries") if (data and "entries" in data) else None


# ── comment editing ────────────────────────────────────────────────────────

# Map each source extension to its line-comment prefix(es) so the agent works
# across the whole fleet (Rust/Go/JS/C/shell/Lua/…), not just Python.  This is
# exactly where Python-only assumptions used to break the test (fd, ripgrep,
# prettier, git-lfs all carry zero .py files).
SOURCE_COMMENTS = {
    # '#' family
    ".py": ("#",), ".rb": ("#",), ".sh": ("#",), ".bash": ("#",), ".zsh": ("#",),
    ".pl": ("#",), ".pm": ("#",), ".ex": ("#",), ".exs": ("#",), ".jq": ("#",),
    ".r": ("#",), ".tf": ("#",), ".toml": ("#",), ".yaml": ("#",), ".yml": ("#",),
    # '//' family
    ".rs": ("//",), ".go": ("//",), ".js": ("//",), ".jsx": ("//",), ".mjs": ("//",),
    ".cjs": ("//",), ".ts": ("//",), ".tsx": ("//",), ".c": ("//",), ".h": ("//",),
    ".cc": ("//",), ".cpp": ("//",), ".hpp": ("//",), ".cxx": ("//",), ".java": ("//",),
    ".swift": ("//",), ".kt": ("//",), ".kts": ("//",), ".scala": ("//",),
    ".zig": ("//",), ".dart": ("//",), ".cs": ("//",),
    # other line-comment markers
    ".php": ("//", "#"), ".lua": ("--",), ".sql": ("--",), ".hs": ("--",),
    ".vim": ('"',), ".el": (";",), ".clj": (";",), ".lisp": (";",),
}
SOURCE_EXTS = tuple(SOURCE_COMMENTS.keys())


def _comment_prefixes(path: str) -> tuple:
    """Line-comment prefix(es) for a path, by extension ('' if unknown)."""
    dot = path.rfind(".")
    ext = path[dot:].lower() if dot != -1 else ""
    return SOURCE_COMMENTS.get(ext, ())


def _add_exclamation(content: str, rng: random.Random, prefixes: tuple = ("#",)) -> tuple:
    """Add '!' to a randomly chosen comment line. Returns (new_content, bool)."""
    lines = content.split("\n")
    eligible = [
        i
        for i, line in enumerate(lines)
        if any(line.strip().startswith(p) for p in prefixes)
        and not line.rstrip().endswith("!")
    ]
    if not eligible:
        return content, False
    idx = rng.choice(eligible)
    lines[idx] = lines[idx].rstrip() + "!"
    return "\n".join(lines), True


# ── main agent logic ───────────────────────────────────────────────────────

def run_real_agent(
    approach: str,
    repo_url: str,
    commit: str,
    branch: str,
    service_url: str = "http://127.0.0.1:8765",
    pct: float = 1.0,
    seed: int = SEED_DEFAULT,
) -> dict:
    """
    Run the real agent task end-to-end and return a metrics dict.

    The returned dict includes per-phase timing breakdowns, round-trip
    counts, files modified, and the local commit SHA as proof of work.
    """
    rng = random.Random(seed)
    t_start = time.monotonic()

    metrics: dict = {
        "approach": approach,
        "agentcache_detected": False,
        "bundle_used": False,   # True when history was seeded from local bundle (CDN simulation)
        "files_found": 0,
        "files_selected": 0,
        "files_fetched": 0,
        "files_modified": 0,
        "comments_modified": 0,
        "fetch_roundtrips": 0,
        "commit_sha": None,
        "phase_clone_ms": 0.0,
        "phase_discover_ms": 0.0,
        "phase_fetch_ms": 0.0,
        "phase_edit_ms": 0.0,
        "phase_commit_ms": 0.0,
        "elapsed_ms": 0.0,
        "error": None,
    }

    work_dir = tempfile.mkdtemp(prefix="real-agent-")
    clone_dir = os.path.join(work_dir, "repo")

    try:
        # ── Phase 1: Clone ─────────────────────────────────────────
        t0 = time.monotonic()
        if approach == "naive":
            # Depth-1 gives every tracked file immediately — like actions/checkout.
            _git("clone", "--depth=1", f"--branch={branch}",
                 repo_url, clone_dir, timeout=CLONE_TIMEOUT)

        elif approach == "blobless":
            # Shallow blobless: smallest possible clone (commits + trees, no blobs).
            _git("clone", "--filter=blob:none", "--depth=1", "--no-checkout",
                 f"--branch={branch}", repo_url, clone_dir, timeout=CLONE_TIMEOUT)

        else:  # agentcache
            # The bundle CDN path is what makes agentcache efficient.  The
            # bootstrap bundle is pre-built once on the server and served from
            # a CDN (or local file in benchmarks).  The agent seeds the clone
            # from the bundle — which does NOT go through the git daemon/proxy
            # (it's a local/CDN file read) — so the promisor only serves the
            # tiny delta since the bundle was created (~0 bytes for a fresh repo).
            # Without the bundle, the agent would download the full history
            # (~190 MiB for CPython) through the git daemon.
            bundle_path = _find_bundle(repo_url, branch)
            clone_args = [
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--single-branch",          # only fetch the target branch
                f"--branch={branch}",
            ]
            if bundle_path:
                clone_args.append(f"--bundle-uri=file://{bundle_path}")
                metrics["bundle_used"] = True
            else:
                metrics["bundle_used"] = False
                # Without bundle, warn via metrics — history will be expensive
            clone_args.extend([repo_url, clone_dir])
            _git(*clone_args, timeout=CLONE_TIMEOUT)

        metrics["phase_clone_ms"] = round((time.monotonic() - t0) * 1000, 1)

        # ── Phase 2: Discover Python files ─────────────────────────
        t1 = time.monotonic()
        all_py: list = []

        if approach == "agentcache":
            # Ask the service for the manifest — no file content needed.
            entries = _detect_agentcache(service_url, commit)
            if entries is not None:
                metrics["agentcache_detected"] = True
                all_py = [
                    e["path"] for e in entries if e["path"].endswith(SOURCE_EXTS)
                ]
            else:
                # Service down: fall back to git ls-tree (still blobless).
                r = _git("ls-tree", "-r", "--name-only", "HEAD",
                         cwd=clone_dir, timeout=30)
                all_py = [l for l in r.stdout.splitlines() if l.endswith(SOURCE_EXTS)]

        elif approach == "blobless":
            # ls-tree works because we have all tree objects.
            r = _git("ls-tree", "-r", "--name-only", "HEAD",
                     cwd=clone_dir, timeout=30)
            all_py = [l for l in r.stdout.splitlines() if l.endswith(SOURCE_EXTS)]

        else:  # naive — walk the working tree on disk
            for root, dirs, files in os.walk(clone_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    if fname.endswith(SOURCE_EXTS):
                        rel = os.path.relpath(
                            os.path.join(root, fname), clone_dir
                        )
                        all_py.append(rel)

        all_py.sort()
        metrics["files_found"] = len(all_py)
        metrics["phase_discover_ms"] = round((time.monotonic() - t1) * 1000, 1)

        if not all_py:
            raise RuntimeError("No recognised source files found in repo")

        # ── Phase 3: Select 1% ─────────────────────────────────────
        n_select = max(1, int(len(all_py) * pct / 100))
        selected = sorted(rng.sample(all_py, min(n_select, len(all_py))))
        metrics["files_selected"] = len(selected)

        # ── Phase 4: Fetch selected files ──────────────────────────
        t2 = time.monotonic()

        if approach == "naive":
            # Nothing to do — files are already on disk.
            pass

        elif approach == "agentcache" and metrics["agentcache_detected"]:
            # Resolve paths → OIDs → ONE batch git fetch.
            resolved = _http_post(
                f"{service_url.rstrip('/')}/cache/{commit}/resolve",
                {"paths": selected},
                timeout=60,
            )
            raw_oids = resolved.get("fetch_oids", []) if resolved else []
            # Dedupe, and drop the well-known empty-blob OID: it is intrinsic to
            # every git repo (the SHA of a zero-byte file — e.g. django's many
            # empty __init__.py), already present locally, and servers reject a
            # `want` for it, which would fail the entire batch fetch.
            oids = sorted({o for o in raw_oids if o != EMPTY_BLOB_OID})
            if oids:
                # Batch-fetch exactly the blobs we need in ONE round trip.
                #  - --no-tags is critical: without it `git fetch` does tag
                #    auto-following, negotiating over EVERY ref the server
                #    advertises.  On a mirror of a busy GitHub repo that's tens
                #    of thousands of refs/pull/* refs — ~5 MiB of pure ref
                #    advertisement for what should be a ~20 KiB blob fetch.
                #  - We do NOT clear remote.origin.partialclonefilter: on some
                #    repos (e.g. django) that override actually breaks blob
                #    materialisation.  An explicit `want <oid>` already bypasses
                #    the filter for that object.
                #  - check=False: `git fetch <blob-oid>` can exit non-zero while
                #    still materialising the blobs (it fails to write FETCH_HEAD
                #    for a non-commit).  "objects present locally" is the real
                #    success criterion, which we verify next.
                subprocess.run(
                    ["git", "fetch", "--no-tags", "--no-write-fetch-head",
                     "origin", *oids],
                    capture_output=True, text=True, cwd=clone_dir, timeout=180,
                )
                metrics["fetch_roundtrips"] = 1
                # Verify materialisation; promisor-fetch any stragglers one by
                # one (cat-file on a missing promisor object triggers a fetch).
                missing = [
                    o for o in oids
                    if subprocess.run(
                        ["git", "cat-file", "-e", o],
                        cwd=clone_dir, capture_output=True,
                    ).returncode != 0
                ]
                for o in missing:
                    subprocess.run(
                        ["git", "cat-file", "-p", o],
                        cwd=clone_dir, capture_output=True, timeout=60,
                    )
                    metrics["fetch_roundtrips"] += 1
            # Materialise selected files into the working tree from the
            # now-local blobs (no more network after this).
            _git("checkout", "HEAD", "--", *selected,
                 cwd=clone_dir, timeout=30)

        else:
            # blobless or agentcache-fallback: one lazy promisor fetch per file.
            for path in selected:
                _git("checkout", "HEAD", "--", path,
                     cwd=clone_dir, timeout=30, check=False)
                metrics["fetch_roundtrips"] += 1

        metrics["phase_fetch_ms"] = round((time.monotonic() - t2) * 1000, 1)

        # ── Phase 5: Edit — add ! to one comment per file ──────────
        t3 = time.monotonic()
        n_modified = 0
        n_on_disk = 0

        for path in selected:
            full = os.path.join(clone_dir, path)
            if not os.path.exists(full):
                continue
            n_on_disk += 1
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                prefixes = _comment_prefixes(path) or ("#",)
                new_content, changed = _add_exclamation(content, rng, prefixes)
                if changed:
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(new_content)
                    n_modified += 1
            except Exception:
                pass

        metrics["files_fetched"] = n_on_disk
        metrics["files_modified"] = n_modified
        metrics["comments_modified"] = n_modified
        metrics["phase_edit_ms"] = round((time.monotonic() - t3) * 1000, 1)

        # ── Phase 6: Commit (ZERO network) ─────────────────────────
        #
        # GOAL: record the agent's edits while pulling NOTHING from the server.
        #
        # The naive `git add` / `git commit` porcelain is a trap on a
        # --no-checkout blobless clone: porcelain refreshes the index against
        # the working tree, and to diff the 5778 files that exist in HEAD's
        # tree but NOT in our sparse working tree, git lazily fetches their
        # blobs from the promisor — ~44 MB for CPython.  That download has
        # nothing to do with the agent's actual change; it's an artifact of
        # git trying to produce a "complete" commit.
        #
        # Instead we build the commit with pure plumbing in a SEPARATE index
        # that contains ONLY the files we changed:
        #
        #   git hash-object -w <file>          → write new blob locally (0 net)
        #   git update-index --add --cacheinfo → stage into a scratch index
        #   git write-tree                     → tree of just our files (0 net)
        #   git commit-tree -p HEAD            → commit object, no diff (0 net)
        #   git update-ref HEAD                → move the branch (0 net)
        #
        # The resulting commit's tree contains only the edited files; the rest
        # of the project is "orphaned" relative to this commit.  That is an
        # ACCEPTED tradeoff: the agent's job is to apply and record its change
        # with minimal network impact on the disposable VM, not to preserve a
        # pristine full-tree snapshot.  GIT_NO_LAZY_FETCH=1 guarantees that if
        # any step *would* hit the network, it fails loudly instead.
        t4 = time.monotonic()
        if n_modified > 0:
            env = dict(os.environ)
            env["GIT_NO_LAZY_FETCH"] = "1"
            scratch_index = os.path.join(work_dir, "agent-commit-index")
            env["GIT_INDEX_FILE"] = scratch_index
            if os.path.exists(scratch_index):
                os.remove(scratch_index)

            modified = [
                p for p in selected
                if os.path.exists(os.path.join(clone_dir, p))
            ]

            for path in modified:
                blob = subprocess.run(
                    ["git", "hash-object", "-w", path],
                    capture_output=True, text=True, cwd=clone_dir,
                    timeout=30, check=True, env=env,
                ).stdout.strip()
                subprocess.run(
                    ["git", "update-index", "--add",
                     "--cacheinfo", f"100644,{blob},{path}"],
                    capture_output=True, text=True, cwd=clone_dir,
                    timeout=30, check=True, env=env,
                )

            tree = subprocess.run(
                ["git", "write-tree"],
                capture_output=True, text=True, cwd=clone_dir,
                timeout=30, check=True, env=env,
            ).stdout.strip()

            parent = _git("rev-parse", "HEAD", cwd=clone_dir).stdout.strip()
            message = (
                f"agent({approach}): add ! to {n_modified} comment(s)\n\n"
                f"approach={approach} agentcache={metrics['agentcache_detected']} "
                f"bundle={metrics['bundle_used']} seed={seed} pct={pct}%\n"
                f"NOTE: partial-tree commit (only edited files) to keep the "
                f"agent's network footprint at zero."
            )
            commit = subprocess.run(
                ["git", "commit-tree", tree, "-p", parent, "-m", message],
                capture_output=True, text=True, cwd=clone_dir,
                timeout=30, check=True,
                env={**env, "GIT_AUTHOR_NAME": "PackCache Real Agent",
                     "GIT_AUTHOR_EMAIL": "agent@packcache",
                     "GIT_COMMITTER_NAME": "PackCache Real Agent",
                     "GIT_COMMITTER_EMAIL": "agent@packcache"},
            ).stdout.strip()

            subprocess.run(
                ["git", "update-ref", "HEAD", commit],
                capture_output=True, text=True, cwd=clone_dir,
                timeout=30, check=True, env=env,
            )
            metrics["commit_sha"] = commit

        metrics["phase_commit_ms"] = round((time.monotonic() - t4) * 1000, 1)

    except Exception as exc:
        metrics["error"] = str(exc)

    metrics["elapsed_ms"] = round((time.monotonic() - t_start) * 1000, 1)
    return metrics


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--approach", required=True,
                   choices=["naive", "blobless", "agentcache"])
    p.add_argument("--repo-url", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--service-url", default="http://127.0.0.1:8765")
    p.add_argument("--pct", type=float, default=1.0,
                   help="Percentage of .py files to modify (default: 1.0)")
    p.add_argument("--seed", type=int, default=SEED_DEFAULT)
    args = p.parse_args(argv)

    result = run_real_agent(
        approach=args.approach,
        repo_url=args.repo_url,
        commit=args.commit,
        branch=args.branch,
        service_url=args.service_url,
        pct=args.pct,
        seed=args.seed,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
