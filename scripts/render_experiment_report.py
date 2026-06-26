#!/usr/bin/env python3
"""Render a human-readable Markdown DIAGNOSTIC of the most recent AgentCache test-harness
experiment runs and (optionally) copy their raw JSON into the committed results tree.

This report is a DIAGNOSTIC tool for maintainers: it surfaces both where agentcache helps
AND where it falls short vs naive/blobless, so the maintainer can find weak spots and
improve agentcache.  It is not a marketing document.

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
# Constants
# ---------------------------------------------------------------------------

# Break-even passes above this count is considered "impractical" in realistic
# agent workflows.
_IMPRACTICAL_BREAK_EVEN = 100

# Warm saving below this fraction of blobless warm is considered "marginal".
_MARGINAL_WARM_THRESHOLD = 0.25

# Hook wall time above this threshold is flagged as expensive maintenance.
_EXPENSIVE_HOOK_WALL_S = 1.0


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


def _safe_ratio(a: float | None, b: float | None) -> float | None:
    """Return a/b or None if either is None/zero."""
    if a is None or b is None or b == 0:
        return None
    return a / b


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
    naive = summ.get("naive", {}) or {}
    blobless = summ.get("blobless", {}) or {}
    agentcache = summ.get("agentcache", {}) or {}
    win = summ.get("_win_vs_naive", {}) or {}

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


def _render_cold_start_table(campaigns: list[dict]) -> str:
    """Render the '### Cold start across the three approaches' table.

    Shows naive/blobless/agentcache cold bytes and the ratio agentcache÷blobless,
    making the bootstrap-bundle penalty visible at a glance.
    Placed BEFORE the cost/benefit table in each experiment section.
    """
    lines: list[str] = []
    lines.append("### Cold start across the three approaches\n\n")
    lines.append(
        "> **Column caveat:** agentcache cold = full-history blobless clone; "
        "blobless cold = `--depth=1` shallow clone (no history). "
        "These columns are not directly comparable on a bytes basis.\n\n"
    )
    lines.append(
        "| Repo | naive cold | blobless cold (depth-1 shallow) | agentcache cold (full history) "
        "| agentcache cold ÷ blobless |\n"
    )
    lines.append(
        "|------|----------:|-------------:|----------------:"
        "|---------------------------:|\n"
    )

    for camp in campaigns:
        repo = camp.get("repo", "?")
        err = camp.get("error")
        if err:
            lines.append(f"| {repo} | — | — | — | FAILED |\n")
            continue

        summ = camp.get("summary", {})
        naive = summ.get("naive", {}) or {}
        bl = summ.get("blobless", {}) or {}
        ac = summ.get("agentcache", {}) or {}

        naive_cold: float | None = naive.get("cold_bytes")
        bl_cold: float | None = bl.get("cold_bytes")
        ac_cold: float | None = ac.get("cold_bytes")

        ratio = _safe_ratio(ac_cold, bl_cold)
        ratio_str = f"{ratio:.0f}×" if ratio is not None else "—"

        lines.append(
            f"| {repo} | {_fmt_bytes(naive_cold)} | {_fmt_bytes(bl_cold)} "
            f"| {_fmt_bytes(ac_cold)} | {ratio_str} |\n"
        )

    lines.append("\n")
    return "".join(lines)


def _render_cost_benefit(campaigns: list[dict]) -> str:
    """Render the '### Cost / benefit vs blobless' subsection for one experiment.

    For each campaign computes:
      warm_saved_per_pass = blobless_warm_avg_bytes - agentcache_warm_avg_bytes
      cold_overhead       = agentcache_cold_bytes   - blobless_cold_bytes
      break_even_passes   = ceil(cold_overhead / warm_saved_per_pass)
    All None / division-by-zero cases are guarded and shown as '—'.

    Verdicts are neutral: they describe the break-even arithmetic without
    promotional framing.  Impractical break-evens (> _IMPRACTICAL_BREAK_EVEN) are
    explicitly called out.
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

    # Accumulate (repo_short, break_even) for the summary line.
    valid_be: list[tuple[str, int]] = []

    for camp in campaigns:
        repo = camp.get("repo", "?")
        repo_short = repo.removesuffix(".git")
        err = camp.get("error")
        if err:
            lines.append(f"| {repo} | — | — | — | — | FAILED |\n")
            continue

        summ = camp.get("summary", {})
        bl = summ.get("blobless", {}) or {}
        ac = summ.get("agentcache", {}) or {}
        win = summ.get("_win_vs_naive", {}) or {}

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
            cold_oh = None
            cold_col = "—"
        else:
            cold_oh = ac_cold - bl_cold
            cold_col = "none" if cold_oh <= 0 else _fmt_bytes(cold_oh)

        # --- break-even passes and verdict ---
        if warm_saved is None or warm_saved <= 0:
            # No warm byte win — blobless is the better choice
            break_even: int | None = None
            be_col = "N/A"
            verdict = "agentcache has no warm byte advantage over blobless — blobless preferred here"
        elif cold_oh is None:
            break_even = None
            be_col = "unknown"
            verdict = "warm saving confirmed; break-even vs blobless unknown (missing cold data)"
        elif cold_oh <= 0:
            break_even = 0
            be_col = "0"
            verdict = "break-even vs blobless: immediate (no cold overhead)"
            valid_be.append((repo_short, 0))
        else:
            break_even = math.ceil(cold_oh / warm_saved)
            be_col = str(break_even)
            if break_even > _IMPRACTICAL_BREAK_EVEN:
                verdict = (
                    f"agentcache does NOT repay its cold cost vs blobless "
                    f"within ~{break_even} passes — blobless preferred"
                )
            else:
                verdict = (
                    f"break-even vs blobless: ~{break_even} warm passes "
                    f"({_fmt_bytes(cold_oh)} ÷ {_fmt_bytes(warm_saved)}/pass)"
                )
            valid_be.append((repo_short, break_even))

        lines.append(
            f"| {repo} | {warm_col} | {win_col} | {cold_col} | {be_col} | {verdict} |\n"
        )

    lines.append("\n")

    # --- Neutral summary line ---
    if len(valid_be) >= 2:
        fastest = min(valid_be, key=lambda x: x[1])
        slowest = max(valid_be, key=lambda x: x[1])
        impractical = [r for r in valid_be if r[1] > _IMPRACTICAL_BREAK_EVEN]
        if fastest[0] != slowest[0]:
            summary = (
                f"> Break-even vs blobless ranges from ~{fastest[1]} passes ({fastest[0]}) "
                f"to ~{slowest[1]} passes ({slowest[0]})."
            )
            if impractical:
                summary += (
                    f" {len(impractical)} repo(s) exceed {_IMPRACTICAL_BREAK_EVEN} passes "
                    f"— blobless is the practical default for those."
                )
            summary += "\n\n"
        else:
            summary = (
                f"> Break-even vs blobless: ~{fastest[1]} passes "
                f"across all repos in this run.\n\n"
            )
    elif len(valid_be) == 1:
        r, be = valid_be[0]
        summary = f"> Break-even vs blobless: ~{be} passes on {r}.\n\n"
    else:
        summary = (
            "> Break-even vs blobless unavailable "
            "(missing cold/warm byte data or no warm byte win).\n\n"
        )
    lines.append(summary)

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


def _render_holes_section(featured_exps: list[dict]) -> str:
    """Render '## Where agentcache has holes (improvement targets)'.

    Aggregates data across ALL featured experiments and presents weaknesses
    worst-first, each with concrete numbers and repo names.  Four categories:
      1. Cold-start penalty (agentcache cold ÷ blobless cold ratio)
      2. Marginal warm win (< 25% saving vs blobless on warm passes)
      3. Impractical break-even (> 100 warm passes to repay cold cost)
      4. Expensive per-commit warm overhead (hook wall > 1 s)
    """
    # --- Collect raw data across all featured experiments ---
    # De-duplicate by repo, keeping the worst value per metric so the table
    # doesn't show the same repo twice just because it appeared in two runs.
    cold_by_repo: dict[
        str, tuple[float, float, float]
    ] = {}  # repo -> (ac_cold, bl_cold, ratio)
    warm_by_repo: dict[
        str, tuple[float, float, float]
    ] = {}  # repo -> (bl_warm, ac_warm, pct)
    be_by_repo: dict[
        str, tuple[int, float, float]
    ] = {}  # repo -> (be, cold_oh, warm_saved)
    max_hook_by_repo: dict[str, float] = {}  # repo -> max hook_wall_s

    for exp in featured_exps:
        for camp in exp.get("campaigns", []):
            if camp.get("error"):
                continue
            repo = camp.get("repo", "?")
            summ = camp.get("summary", {})
            bl = summ.get("blobless", {}) or {}
            ac = summ.get("agentcache", {}) or {}

            bl_cold: float | None = bl.get("cold_bytes")
            ac_cold: float | None = ac.get("cold_bytes")
            bl_warm: float | None = bl.get("warm_avg_bytes")
            ac_warm: float | None = ac.get("warm_avg_bytes")

            # Cold penalty
            if (
                bl_cold is not None
                and ac_cold is not None
                and bl_cold > 0
                and ac_cold > bl_cold
            ):
                ratio = ac_cold / bl_cold
                prev = cold_by_repo.get(repo)
                if prev is None or ratio > prev[2]:
                    cold_by_repo[repo] = (ac_cold, bl_cold, ratio)

            # Marginal warm win
            if bl_warm is not None and ac_warm is not None and bl_warm > 0:
                pct = (bl_warm - ac_warm) / bl_warm
                if pct < _MARGINAL_WARM_THRESHOLD:
                    prev_w = warm_by_repo.get(repo)
                    if prev_w is None or pct < prev_w[2]:
                        warm_by_repo[repo] = (bl_warm, ac_warm, pct)

            # Break-even
            if None not in (bl_warm, ac_warm, bl_cold, ac_cold):
                assert bl_warm is not None
                assert ac_warm is not None
                assert bl_cold is not None
                assert ac_cold is not None
                warm_saved = bl_warm - ac_warm
                cold_oh = ac_cold - bl_cold
                if warm_saved > 0 and cold_oh > 0:
                    be = math.ceil(cold_oh / warm_saved)
                    if be > _IMPRACTICAL_BREAK_EVEN:
                        prev_be = be_by_repo.get(repo)
                        if prev_be is None or be > prev_be[0]:
                            be_by_repo[repo] = (be, cold_oh, warm_saved)

            # Hook wall
            for entry in camp.get("timeline", []):
                if entry.get("kind") != "human":
                    continue
                hw: float | None = entry.get("hook_wall_s")
                comparison = entry.get("comparison") or {}
                if comparison:
                    hw = comparison.get("hook_wall_s", hw)
                if hw is not None and hw > _EXPENSIVE_HOOK_WALL_S:
                    if hw > max_hook_by_repo.get(repo, 0.0):
                        max_hook_by_repo[repo] = hw

    # --- Render the section ---
    lines: list[str] = []
    lines.append("## Where agentcache has holes (improvement targets)\n\n")
    lines.append(
        "The entries below are engineering improvement targets, not edge-cases. "
        "Data is aggregated across all featured experiments; repos appearing in "
        "multiple runs are de-duplicated (worst value kept).\n\n"
    )

    # 1. Full-history cold cost (with measurement caveats — NOT a clean defect)
    lines.append(
        "### 1. Full-history cold cost (read the caveat — this is NOT a clean defect)\n\n"
    )
    lines.append(
        "The table shows agentcache's cold-start bytes ÷ blobless's. "
        "**Two measurement artifacts inflate this ratio — do not read it as pure overhead:**\n"
        "1. **Full-history vs shallow.** agentcache's cold pass is a *full-history* blobless clone "
        "— it delivers complete history (agentcache's core promise). The blobless column is a "
        "`--depth=1` *shallow* clone with no history. This compares two different products.\n"
        "2. **Un-amortized vs CDN-cached.** This is one cold agent paying the full first-visit cost. "
        "In production the bootstrap bundle is built once per commit and served as an immutable "
        "CDN-cached file reused by every agent on that commit — a per-commit cost, not per-agent.\n\n"
        "**The genuine, narrower signal:** on deep-history repos the full-history payload "
        "(commits+trees) is large, and the per-commit bundle *artifact* scales with history. "
        "The real improvement target is a **base bundle + thin per-commit incremental** "
        "(chained via `--bundle-uri`), which shrinks the per-commit artifact from O(history) "
        "to O(delta) *without* losing full history. Also: the harness should measure the "
        "production-realistic cold-WITH-bundle / per-commit-amortized cost (today it hardwires "
        "cold⇒no-bundle), and an apples-to-apples arm (agentcache vs *full-history* blobless, "
        "not depth-1).\n\n"
    )
    cold_rows = sorted(cold_by_repo.items(), key=lambda x: x[1][2], reverse=True)
    if cold_rows:
        lines.append(
            "| Repo | agentcache cold (full history) | blobless cold (depth-1 shallow) "
            "| ratio (agentcache ÷ blobless) |\n"
        )
        lines.append(
            "|------|-------------------------------:|--------------------------------:"
            "|------------------------------:|\n"
        )
        for repo, (ac_cold, bl_cold, ratio) in cold_rows:
            lines.append(
                f"| {repo} | {_fmt_bytes(ac_cold)} | {_fmt_bytes(bl_cold)} "
                f"| {ratio:.0f}× |\n"
            )
        lines.append("\n")
        worst_repo, (worst_ac, worst_bl, worst_ratio) = cold_rows[0]
        lines.append(
            f"> **Note (see caveats above):** Worst case is **{worst_repo}** at "
            f"{_fmt_bytes(worst_ac)} full-history cold vs {_fmt_bytes(worst_bl)} "
            f"for depth-1 blobless ({worst_ratio:.0f}× ratio). "
            f"This ratio is inflated by the full-history vs depth-1 mismatch and the "
            f"un-amortized single-agent cost. "
            f"The real improvement lever is a **base bundle + thin per-commit incremental** "
            f"(via `--bundle-uri`), reducing per-commit artifact size from O(history) to "
            f"O(delta) without losing full history.\n\n"
        )
    else:
        lines.append("No cold-start penalty cases found in featured experiments.\n\n")

    # 2. Marginal warm win
    lines.append("### 2. Marginal warm win (< 25% saving vs blobless)\n\n")
    lines.append(
        "Repos where agentcache's warm-pass byte saving over blobless is small. "
        "The vs-naive win is large, but that is the easy case; "
        "if blobless already fetches only a few blobs, agentcache adds little.\n\n"
    )
    warm_rows = sorted(
        warm_by_repo.items(), key=lambda x: x[1][2]
    )  # worst (smallest %) first
    if warm_rows:
        lines.append(
            "| Repo | blobless warm | agentcache warm | saving vs blobless |\n"
        )
        lines.append(
            "|------|-------------:|-----------------:|--------------------:|\n"
        )
        for repo, (bl_warm, ac_warm, pct) in warm_rows:
            lines.append(
                f"| {repo} | {_fmt_bytes(bl_warm)} | {_fmt_bytes(ac_warm)} "
                f"| {pct * 100:.1f}% |\n"
            )
        lines.append("\n")
        worst_warm_repo, (_, _, worst_pct) = warm_rows[0]
        lines.append(
            f"> **TODO — improve warm selectivity:** On lean repos like "
            f"**{worst_warm_repo}** the warm saving is only "
            f"{worst_pct * 100:.1f}% vs blobless. "
            f"Consider skipping or opt-in-only agentcache on repos where the "
            f"agent's file edit set is small relative to total blobs.\n\n"
        )
    else:
        lines.append(
            "No repos with < 25% warm saving found in featured experiments.\n\n"
        )

    # 3. Impractical break-even
    lines.append(
        f"### 3. Impractical break-even (> {_IMPRACTICAL_BREAK_EVEN} warm passes)\n\n"
    )
    lines.append(
        f"Repos where agentcache needs more than {_IMPRACTICAL_BREAK_EVEN} warm passes "
        f"to repay its cold-start overhead vs blobless. "
        f"In realistic agent workflows this break-even is rarely if ever reached, "
        f"making blobless the better default for these repos.\n\n"
    )
    be_rows = sorted(
        be_by_repo.items(), key=lambda x: x[1][0], reverse=True
    )  # worst first
    if be_rows:
        lines.append(
            "| Repo | break-even passes | cold overhead vs blobless | warm saved/pass |\n"
        )
        lines.append(
            "|------|------------------:|---------------------------:|----------------:|\n"
        )
        for repo, (be, cold_oh, warm_saved) in be_rows:
            lines.append(
                f"| {repo} | {be} | {_fmt_bytes(cold_oh)} | {_fmt_bytes(warm_saved)} |\n"
            )
        lines.append("\n")
        worst_be_repo, (worst_be, _, _) = be_rows[0]
        lines.append(
            f"> **TODO — gate on repo heuristics:** {len(be_rows)} repo(s) have break-even "
            f"> {_IMPRACTICAL_BREAK_EVEN} passes (worst: **{worst_be_repo}** at {worst_be} passes). "
            f"For these repos, blobless is the practical default. "
            f"Fix: gate agentcache on a repo-size or history-depth heuristic, "
            f"or reduce the bundle footprint on shallow histories.\n\n"
        )
    else:
        lines.append(
            f"No repos with break-even > {_IMPRACTICAL_BREAK_EVEN} found in featured experiments.\n\n"
        )

    # 4. Expensive per-commit warm overhead
    lines.append("### 4. Expensive per-commit warm overhead (hook wall > 1 s)\n\n")
    lines.append(
        "Every human push triggers a server-side index rebuild that naive and blobless "
        "don't pay at all. High hook wall times are a continuous maintenance tax "
        "on developer velocity.\n\n"
    )
    hook_rows = sorted(max_hook_by_repo.items(), key=lambda x: x[1], reverse=True)
    if hook_rows:
        lines.append("| Repo | max hook wall (s) |\n")
        lines.append("|------|------------------:|\n")
        for repo, max_wall in hook_rows:
            lines.append(f"| {repo} | {max_wall:.3f}s |\n")
        lines.append("\n")
        worst_hook_repo, worst_hook_wall = hook_rows[0]
        lines.append(
            f"> **TODO — profile and async-ify the hook:** {len(hook_rows)} repo(s) have "
            f"hook wall > 1 s per human push (worst: **{worst_hook_repo}** at "
            f"{worst_hook_wall:.1f}s). "
            f"Profile the hot path for large symbol-count repos; "
            f"consider async or deferred indexing so the push completes immediately.\n\n"
        )
    else:
        lines.append("No repos with hook wall > 1 s found in featured experiments.\n\n")

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

        # Cold-start table (new — before cost/benefit)
        lines.append(_render_cold_start_table(campaigns))

        # Cost / benefit analysis
        lines.append(_render_cost_benefit(campaigns))

        # Server-side warm overhead
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
        description="Render a Markdown diagnostic of the most recent AgentCache experiments."
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

    # Title + diagnostic intro
    md_lines.append("# AgentCache experiment diagnostic\n\n")
    md_lines.append(
        "This is a **diagnostic report for maintainers**, not a marketing document. "
        "Its purpose is to surface both where agentcache helps AND where it falls short "
        "vs naive and blobless, so that weak spots can be found and fixed. "
        "Data comes from real runs of the [test harness](../testharness/) measuring three "
        "git-fetch strategies — **naive** (full clone), **blobless** (`--filter=blob:none`), "
        "and **agentcache** (targeted blob fetch via the pre-built manifest + symbol cache). "
        "Each experiment runs multiple agent passes per repo: **pass 1 is COLD** "
        "(agentcache downloads its bootstrap bundle and builds its cache from scratch), "
        "and **later passes are WARM** (only the requested blobs are fetched). "
        "**Column caveat:** the agentcache cold column delivers *full history* (full-history blobless clone, no depth limit); the blobless column uses `--depth=1` (shallow, no history) — the two cold columns are not directly comparable on a bytes basis. "
        "**Framing:** naive is the easy strawman; the real test is agentcache vs blobless. "
        "agentcache carries two costs that blobless does not: "
        "(1) a large COLD bootstrap bundle whose size scales with repo history depth, "
        "and (2) a per-commit server-side warm overhead on every human push. "
        "Both costs are exposed in detail below.\n\n"
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

    # Top-level holes section (before per-experiment detail)
    md_lines.append(_render_holes_section(featured))
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
