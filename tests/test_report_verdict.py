"""Tests for suitability_verdict() in scripts/render_experiment_report.py.

Covers the four verdict labels across the threshold matrix:
  - "blobless is enough"         (no warm win, tiny repo, marginal saving,
                                   or impractical break-even)
  - "worth it only at high reuse" (mid saving, high break-even)
  - "agentcache worthwhile"       (good saving, quick break-even)

All inputs use the exact test cases from the spec so threshold regressions
are immediately visible.
"""

from __future__ import annotations


from scripts.render_experiment_report import suitability_verdict


# ---------------------------------------------------------------------------
# Label constants (match the function's documented return values exactly)
# ---------------------------------------------------------------------------

_BLOBLESS = "blobless is enough"
_HIGH_REUSE = "worth it only at high reuse"
_WORTHWHILE = "agentcache worthwhile"


# ---------------------------------------------------------------------------
# Spec-mandated test cases
# ---------------------------------------------------------------------------


def test_tiny_repo_blobless_enough():
    """files < 150 → blobless is enough regardless of ratio and break-even."""
    label, reason = suitability_verdict(
        files=57, warm_saving_ratio=0.12, break_even_passes=715
    )
    assert label == _BLOBLESS
    assert reason  # non-empty reason string


def test_blob_heavy_repo_agentcache_worthwhile():
    """Large repo, strong warm saving, fast break-even → agentcache worthwhile."""
    label, reason = suitability_verdict(
        files=574, warm_saving_ratio=0.55, break_even_passes=19
    )
    assert label == _WORTHWHILE
    assert reason


def test_zero_ratio_blobless_enough():
    """ratio == 0 → no warm byte win → blobless is enough."""
    label, reason = suitability_verdict(
        files=None, warm_saving_ratio=0.0, break_even_passes=None
    )
    assert label == _BLOBLESS


def test_negative_ratio_blobless_enough():
    """ratio < 0 → agentcache is WORSE than blobless → blobless is enough."""
    label, reason = suitability_verdict(
        files=200, warm_saving_ratio=-0.1, break_even_passes=None
    )
    assert label == _BLOBLESS


def test_none_ratio_blobless_enough():
    """warm_saving_ratio=None (missing data) → blobless is enough."""
    label, reason = suitability_verdict(
        files=500, warm_saving_ratio=None, break_even_passes=None
    )
    assert label == _BLOBLESS


def test_mid_case_high_reuse_only():
    """ratio=0.30 (< 0.40), break_even=60 (> 50) → worth it only at high reuse."""
    label, reason = suitability_verdict(
        files=None, warm_saving_ratio=0.30, break_even_passes=60
    )
    assert label == _HIGH_REUSE


# ---------------------------------------------------------------------------
# Additional edge / boundary cases
# ---------------------------------------------------------------------------


def test_impractical_break_even_blobless_enough():
    """break_even > 200 → blobless is enough even with decent saving."""
    label, reason = suitability_verdict(
        files=300, warm_saving_ratio=0.20, break_even_passes=201
    )
    assert label == _BLOBLESS


def test_missing_break_even_blobless_enough():
    """break_even_passes=None with saving in the marginal band → blobless is enough."""
    label, reason = suitability_verdict(
        files=300, warm_saving_ratio=0.20, break_even_passes=None
    )
    assert label == _BLOBLESS


def test_high_reuse_due_to_ratio_only():
    """ratio in (0.15, 0.40) with reasonable break-even → high-reuse only."""
    label, reason = suitability_verdict(
        files=None, warm_saving_ratio=0.25, break_even_passes=30
    )
    assert label == _HIGH_REUSE


def test_worthwhile_boundary():
    """ratio >= 0.40 and break_even <= 50 → agentcache worthwhile."""
    label, reason = suitability_verdict(
        files=200, warm_saving_ratio=0.40, break_even_passes=50
    )
    assert label == _WORTHWHILE


def test_all_verdicts_return_nonempty_reason():
    """Every verdict must include a non-empty human-readable reason string."""
    cases = [
        dict(files=57, warm_saving_ratio=0.12, break_even_passes=715),
        dict(files=None, warm_saving_ratio=0.0, break_even_passes=None),
        dict(files=None, warm_saving_ratio=0.30, break_even_passes=60),
        dict(files=574, warm_saving_ratio=0.55, break_even_passes=19),
    ]
    for kwargs in cases:
        label, reason = suitability_verdict(**kwargs)
        assert isinstance(label, str) and label, f"Empty label for {kwargs}"
        assert isinstance(reason, str) and reason, f"Empty reason for {kwargs}"
