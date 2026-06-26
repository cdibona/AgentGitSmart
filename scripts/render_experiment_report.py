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
import math
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


def _fmt_pct(numerator: float | None, denominator: float | None) -> str:
    """Format a fraction as a percentage string like '12.4%', guarding None/zero-division."""
    if numerator is None or denominator is None or denominator == 0:
        return "—"
    return f"{100.0 * numerator / denominator:.1f}%"


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


def _render_cost_benefit(campaigns: list[dict]) -> str:
    """Render the '### Cost / benefit vs blobless' subsection for one experiment.

    For each campaign computes:
      warm_saved_per_pass = blobless_warm_avg_bytes - agentcache_warm_avg_bytes
      cold_overhead       = agentcache_cold_bytes   - blobless_cold_bytes
      break_even_passes   = ceil(cold_overhead / warm_saved_per_pass)
    All None / division-by-zero cases are guarded and shown as '—'.
    """
    lines: list[str] = []
    lines.append("### Cost / benefit vs blobless (the honest competitor)\n\n")
    lines.append(
        "| Repo | warm saved/pass vs blobless | warm vs naive "
        "| cold overhead vs blobless | break-even (warm passes) | verdict |\n"
    )
    lines.append(
        "|------|----------------------------:|:--------------"
        ":|---------------------------:|:------------------------:|:--------|\n"
    )

    # Accumulate (repo_short, break_even) for the takeaway line.
    valid_be: list[tuple[str, int]] = []

    for camp in campaigns:
        repo = camp.get("repo", "?")
        repo_short = repo.removesuffix(".git")
        err = camp.get("error")
        if err:
            lines.append(f"| {repo} | — | — | — | — | FAILED |\n")
            continue

        summ = camp.get("summary", {})
        bl = summ.get("blobless", {})
        ac = summ.get("agentcache", {})
        win = summ.get("_win_vs_naive", {})

        bl_warm: float | None = bl.get("warm_avg_bytes")
        ac_warm: float | None = ac.get("warm_avg_bytes")
        bl_cold: float | None = bl.get("cold_bytes")
        ac_cold: float | None = ac.get("cold_bytes")
        ac_win: float | None = win.get("agentcache")

        # --- warm saved per pass ---
        if bl_warm is None or ac_warm is None:
            warm_saved: float | None = None
        else:
            warm_saved = bl_warm - ac_warm

        if warm_saved is None:
            warm_col = "—"
        else:
            pct = _fmt_pct(warm_saved, bl_warm)
            warm_col = f"{_fmt_bytes(warm_saved)} ({pct})"

        # --- win vs naive ---
        win_col = f"{ac_win:.1f}×" if ac_win is not None else "—"

        # --- cold overhead ---
        if bl_cold is None or ac_cold is None:
            cold_oh: float | None = None
            cold_col = "—"
        else:
            cold_oh = ac_cold - bl_cold
            cold_col = "none" if cold_oh <= 0 else _fmt_bytes(cold_oh)

        # --- break-even passes ---
        if warm_saved is None or warm_saved <= 0:
            # No warm byte win — blobless is the better choice
            break_even: int | None = None
            be_col = "N/A"
            verdict = (
                "✗ no warm byte win over blobless — blobless is the better choice here"
            )
        elif cold_oh is None:
            break_even = None
            be_col = "unknown"
            verdict = "beats naive; blobless break-even unknown (missing cold data)"
        elif cold_oh <= 0:
            break_even = 0
            be_col = "0"
            verdict = "✓ beats blobless immediately (no cold penalty)"
            valid_be.append((repo_short, 0))
        else:
            break_even = math.ceil(cold_oh / warm_saved)
            be_col = str(break_even)
            verdict = f"✓ beats naive immediately; beats blobless after ~{break_even} warm passes"
            valid_be.append((repo_short, break_even))

        lines.append(
            f"| {repo} | {warm_col} | {win_col} | {cold_col} | {be_col} | {verdict} |\n"
        )

    lines.append("\n")

    # --- Takeaway line ---
    if len(valid_be) >= 2:
        fastest = min(valid_be, key=lambda x: x[1])
        slowest = max(valid_be, key=lambda x: x[1])
        if fastest[0] != slowest[0]:
            lines.append(
                f"> agentcache pays for itself fastest on blob-heavy repos "
                f"({fastest[0]} ~{fastest[1]} warm passes) "
                f"and slowest on lean code repos "
                f"({slowest[0]} ~{slowest[1]} warm passes).\n\n"
            )
        else:
            # Only one distinct repo (all tied)
            lines.append(
                f"> agentcache breaks even vs blobless after ~{fastest[1]} warm passes "
                f"across all repos in this run.\n\n"
            )
    elif len(valid_be) == 1:
        r, be = valid_be[0]
        lines.append(
            f"> agentcache breaks even vs blobless after ~{be} warm passes on {r}.\n\n"
        )
    else:
        lines.append(
            "> Break-even vs blobless unavailable "
            "(missing cold/warm byte data or no warm byte win).\n\n"
        )

    return "".join(lines)


def _render_server_overhead(campaigns: list[dict]) -> str:
    """Render the '### Server-side warm overhead' subsection if human commits exist.

    Lists each human commit's rebuild cost: warm_method, hook wall-time,
    action wall-time, and bundle bytes. Returns an empty string when there
    are no human commits in the experiment.
    """
    all_human: list[tuple[str, dict]] = []
    for camp in campaigns:
        repo = camp.get("repo", "?")
        for entry in camp.get("timeline", []):
            if entry.get("kind") == "human":
                all_human.append((repo, entry))

    if not all_human:
        return ""

    lines: list[str] = []
    lines.append("### Server-side warm overhead (the maintenance cost)\n\n")
    lines.append(
        "Every human commit triggers a server-side cache rebuild (CPU + storage) "
        "so the next agent finds a warm cache. "
        "This is a maintenance cost that **naive** and **blobless** don't pay.\n\n"
    )
    lines.append("| Repo | warm_method | hook wall | action wall | bundle bytes |\n")
    lines.append("|------|:-----------:|----------:|------------:|-------------:|\n")

    for repo, entry in all_human:
        warm_method = entry.get("warm_method") or "hook"
        hook_wall: float | None = entry.get("hook_wall_s")
        action_dict = entry.get("action") or {}
        comparison = entry.get("comparison") or {}

        # For "both" entries, the comparison dict has the definitive per-path timings.
        if comparison:
            hook_wall = comparison.get("hook_wall_s", hook_wall)

        action_wall: float | None = action_dict.get("wall_s")
        if action_wall is None and comparison:
            action_wall = comparison.get("action_wall_s")

        bundle_bytes: int | None = action_dict.get("bundle_bytes")

        hook_str = f"{hook_wall:.3f}s" if hook_wall is not None else "—"
        action_str = f"{action_wall:.3f}s" if action_wall is not None else "—"
        bundle_str = _fmt_bytes(bundle_bytes)

        lines.append(
            f"| {repo} | {warm_method} | {hook_str} | {action_str} | {bundle_str} |\n"
        )

    lines.append("\n")
    lines.append(
        "> Server-side time and bundle bytes are CPU + storage cost paid on every "
        "human push — separate from the agent network bytes measured in the results "
        "table above. The in-process hook path is typically 2–10× faster than the "
        "GitHub Action path (subprocess + blobless bundle rebuild). Delta indexing "
        "minimises re-work by re-ctags-ing only changed files and carrying the rest "
        "forward.\n\n"
    )

    return "".join(lines)


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

        # Cost / benefit analysis (new section)
        lines.append(_render_cost_benefit(campaigns))

        # Server-side warm overhead (new section)
        lines.append(_render_server_overhead(campaigns))

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
        "`post-receive` hook, keeping the next agent warm. "
        "**Framing:** naive is the easy win; the real test is agentcache vs blobless, "
        "accounting for agentcache's cold-build cost and per-commit warm overhead — "
        "see each run's **Cost/benefit** section below.\n\n"
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
