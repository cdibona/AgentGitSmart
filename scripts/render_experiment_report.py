#!/usr/bin/env python3
"""Render a human-readable Markdown digest of the most recent AgentCache test-harness
experiment runs and (optionally) copy their raw JSON into the committed results tree.

Usage:
    python scripts/render_experiment_report.py [--top N] [--no-copy]
                                               [--experiments-dir PATH]
                                               [--results-dir PATH]
                                               [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime, or None."""
    if not s:
        return None
    # Python < 3.11 fromisoformat does not handle trailing 'Z'
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fmt_bytes(n: int | float | None) -> str:
    """Format byte counts as a human-readable string (B / KiB / MiB)."""
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n:,} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


def _duration(created: str | None, completed: str | None) -> str:
    """Return elapsed seconds between two ISO timestamps, or '?' on failure."""
    t0 = _parse_iso(created)
    t1 = _parse_iso(completed)
    if t0 is None or t1 is None:
        return "?"
    secs = (t1 - t0).total_seconds()
    return f"{secs:.0f}"


def _fmt_ts(s: str | None) -> str:
    """Reformat an ISO timestamp as 'YYYY-MM-DD HH:MM UTC', or '?' on failure."""
    dt = _parse_iso(s)
    if dt is None:
        return s or "?"
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%d %H:%M UTC")


def _sort_key(exp: dict) -> str:
    """Sort key: completed_at, fallback to created_at (both ISO strings sort lexically)."""
    return exp.get("completed_at") or exp.get("created_at") or ""


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_campaign_row(camp: dict) -> str:
    """Render one table row for a campaign, or a FAILED line if error present."""
    repo = camp.get("repo", "?")
    files = camp.get("files", "?")
    err = camp.get("error")
    if err:
        return f"| {repo} | — | FAILED: {err} | — | — | — | — |\n"

    summ = camp.get("summary", {})
    naive = summ.get("naive", {})
    blobless = summ.get("blobless", {})
    agentcache = summ.get("agentcache", {})
    win = summ.get("_win_vs_naive", {})

    def warm(m: dict) -> str:
        return _fmt_bytes(m.get("warm_avg_bytes"))

    def cold(m: dict) -> str:
        return _fmt_bytes(m.get("cold_bytes"))

    def win_str(method: str) -> str:
        v = win.get(method)
        if v is None:
            return "—"
        return f"{v:.1f}×"

    return (
        f"| {repo} | {files} "
        f"| {warm(naive)} "
        f"| {warm(blobless)} "
        f"| {warm(agentcache)} "
        f"| {cold(agentcache)} "
        f"| {win_str('agentcache')} |\n"
    )


def _render_human_step(entry: dict) -> str:
    """Render a human-timeline entry as Markdown lines."""
    lines: list[str] = []
    hi = entry.get("human_index", "?")
    commit = (entry.get("commit") or "")[:10]
    warm_method = entry.get("warm_method")
    hook_dict = entry.get("hook") or {}
    action_dict = entry.get("action") or {}
    comparison = entry.get("comparison") or {}

    # Determine what method was used for this step
    if warm_method == "both":
        # Full comparison entry
        hw = comparison.get("hook_wall_s") or entry.get("hook_wall_s")
        aw = comparison.get("action_wall_s") or action_dict.get("wall_s")
        ratio = comparison.get("ratio_action_over_hook")
        faster = comparison.get("faster", "?")
        bb = action_dict.get("bundle_bytes")
        mode = hook_dict.get("mode", "?")
        reindexed = hook_dict.get("files_reindexed", "?")
        carried = hook_dict.get("files_carried_forward", "?")
        mat_bytes = hook_dict.get("content_bytes_materialized", "?")

        lines.append(
            f"  **Human #{hi}** ({commit}) — warm_method=**both** (hook vs action)"
        )
        lines.append(
            f"  - hook: {mode} mode, reindexed={reindexed}, carried={carried}, "
            f"{mat_bytes} B materialized, {hw}s"
        )
        lines.append(f"  - action: bundle {_fmt_bytes(bb)}, {aw}s")
        if ratio is not None:
            lines.append(
                f"  - **comparison**: hook {hw}s vs action {aw}s "
                f"→ **{faster} faster** (ratio {ratio:.2f}×)"
            )

    elif warm_method == "action":
        # Action-only entry
        aw = action_dict.get("wall_s")
        bb = action_dict.get("bundle_bytes")
        gen = action_dict.get("generation") or {}
        mode = gen.get("mode", "?")
        lines.append(f"  **Human #{hi}** ({commit}) — warm_method=action")
        lines.append(f"  - action: {mode} mode, bundle {_fmt_bytes(bb)}, {aw}s")

    elif hook_dict or entry.get("hook_wall_s") is not None:
        # hook-only entry (warm_method may be "hook" or absent but hook data present)
        hw = entry.get("hook_wall_s")
        mode = hook_dict.get("mode", "?")
        reindexed = hook_dict.get("files_reindexed", "?")
        carried = hook_dict.get("files_carried_forward", "?")
        mat_bytes = hook_dict.get("content_bytes_materialized", "?")
        label = warm_method if warm_method else "hook"
        lines.append(f"  **Human #{hi}** ({commit}) — warm_method={label}")
        if hw is not None:
            lines.append(
                f"  - hook: {mode} mode, reindexed={reindexed}, carried={carried}, "
                f"{mat_bytes} B materialized, {hw}s"
            )
        else:
            lines.append(
                f"  - hook: {mode} mode, reindexed={reindexed}, carried={carried}"
            )

    else:
        # Minimal entry — older format, no timing data
        note = entry.get("note", "")
        lines.append(
            f"  **Human #{hi}** ({commit}) — hook pre-warmed cache *(no timing recorded)*"
        )
        if note:
            lines.append(f"  - {note}")

    return "\n".join(lines) + "\n"


def _render_experiment(exp: dict) -> str:
    """Render one experiment section as Markdown."""
    eid = exp.get("experiment_id", "?")
    created = exp.get("created_at")
    completed = exp.get("completed_at")
    description = exp.get("description") or ""
    campaigns = exp.get("campaigns") or []

    # Section header
    completed_label = _fmt_ts(completed)
    dur = _duration(created, completed)
    lines: list[str] = []
    lines.append(f"## {eid} — {completed_label}\n\n")
    lines.append(f"Ran {_fmt_ts(created)} → {completed_label} ({dur}s)\n\n")

    # Description as blockquote
    if description:
        # Wrap long description — but keep it as a single blockquote paragraph
        lines.append(f"> {description}\n\n")

    # Results table per campaign
    if campaigns:
        lines.append(
            "| Repo | files | naive (warm) | blobless (warm) "
            "| agentcache (warm) | agentcache cold | win vs naive |\n"
        )
        lines.append(
            "|------|------:|-------------:|----------------:"
            "|------------------:|----------------:|-------------:|\n"
        )
        for camp in campaigns:
            lines.append(_render_campaign_row(camp))
        lines.append("\n")

    # Human steps — collect all from all campaigns
    human_entries: list[tuple[str, int, dict]] = []  # (repo, human_index, entry)
    for camp in campaigns:
        repo = camp.get("repo", "?")
        for entry in camp.get("timeline", []):
            if entry.get("kind") == "human":
                human_entries.append((repo, entry.get("human_index", 0), entry))

    if human_entries:
        lines.append("### Human steps\n\n")
        cur_repo = None
        for repo, _hi, entry in human_entries:
            if repo != cur_repo:
                lines.append(f"**{repo}:**\n\n")
                cur_repo = repo
            lines.append(_render_human_step(entry))
            lines.append("\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a Markdown digest of the most recent AgentCache experiments."
    )
    parser.add_argument(
        "--experiments-dir",
        default="testharness/data/experiments",
        help="Directory containing experiment *.json files (default: testharness/data/experiments)",
    )
    parser.add_argument(
        "--results-dir",
        default="experiments/results",
        help="Committed results directory (default: experiments/results)",
    )
    parser.add_argument(
        "--out",
        default="experiments/RECENT.md",
        help="Output Markdown path (default: experiments/RECENT.md)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="Number of most-recent complete experiments to feature (default: 3)",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Skip copying raw JSON files to results-dir/harness/",
    )
    args = parser.parse_args()

    experiments_dir = Path(args.experiments_dir)
    results_dir = Path(args.results_dir)
    out_path = Path(args.out)

    # --- Load and filter experiments ---
    all_exps: list[dict] = []
    for json_path in experiments_dir.glob("*.json"):
        try:
            with open(json_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  [warn] skipping {json_path.name}: {exc}")
            continue
        if data.get("status") == "complete":
            all_exps.append(data)

    if not all_exps:
        print(f"No complete experiments found in {experiments_dir}.")
        return

    # Sort descending by completed_at (fallback created_at)
    all_exps.sort(key=_sort_key, reverse=True)
    featured = all_exps[: args.top]

    # --- Copy raw JSON to results/harness/ ---
    copied: list[Path] = []
    if not args.no_copy:
        harness_dir = results_dir / "harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        for exp in featured:
            eid = exp.get("experiment_id", "unknown")
            src = experiments_dir / f"{eid}.json"
            dst = harness_dir / f"{eid}.json"
            if src.exists():
                shutil.copy2(src, dst)
                copied.append(dst)
            else:
                print(f"  [warn] source JSON not found: {src}")

    # --- Render Markdown ---
    out_path.parent.mkdir(parents=True, exist_ok=True)

    md_lines: list[str] = []

    # Title + intro
    md_lines.append("# Recent AgentCache experiments\n\n")
    md_lines.append(
        "These are real runs from the [test harness](../testharness/) measuring three "
        "git-fetch strategies — **naive** (full clone), **blobless** (`--filter=blob:none`), "
        "and **agentcache** (targeted blob fetch via the pre-built manifest + symbol cache). "
        "Each experiment runs multiple agent passes per repo: **pass 1 is COLD** (agentcache "
        "builds its cache from scratch on first access), and **later passes are WARM** "
        "(the cache already exists and only the requested blobs are fetched). "
        "Each interleaved human commit triggers a server-side cache update via the "
        "`post-receive` hook, keeping the next agent warm.\n\n"
    )
    md_lines.append("Regenerate with: `python scripts/render_experiment_report.py`\n\n")

    # Links to headless studies
    md_lines.append(
        "See also the headless studies: "
        "[SUMMARY.md](results/SUMMARY.md) · "
        "[exp1 (cold vs warm)](results/exp1_cold_warm.json) · "
        "[exp2 (cache taint)](results/exp2_taint.json) · "
        "[exp3 (hook update)](results/exp3_hook_update.json)\n\n"
    )
    md_lines.append("---\n\n")

    for exp in featured:
        md_lines.append(_render_experiment(exp))
        md_lines.append("---\n\n")

    # Footer: raw JSON provenance links
    if copied:
        md_lines.append("## Raw data\n\n")
        md_lines.append(
            "The raw JSON files backing this report are committed at "
            "`experiments/results/harness/`:\n\n"
        )
        for dst in sorted(copied):
            eid = dst.stem
            md_lines.append(f"- [`{dst.name}`](results/harness/{dst.name})\n")
        md_lines.append("\n")

    out_path.write_text("".join(md_lines), encoding="utf-8")

    # --- Print summary ---
    print(f"Featured experiments ({len(featured)}):")
    for exp in featured:
        eid = exp.get("experiment_id", "?")
        cat = (exp.get("completed_at") or exp.get("created_at") or "?")[:19]
        repos = [c.get("repo", "?") for c in exp.get("campaigns", [])]
        print(f"  {eid}  completed={cat}  repos={repos}")

    print(f"\nReport written to: {out_path}")
    if copied:
        print(f"JSON files copied to {results_dir / 'harness'}/:")
        for p in sorted(copied):
            print(f"  {p}")
    elif args.no_copy:
        print("(--no-copy: raw JSON not copied)")


if __name__ == "__main__":
    main()
