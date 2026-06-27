"""Tests for the cold_bundle dimension in the experiment harness.

Covers:
  - should_use_bundle() pure helper: full method × cold × cold_bundle matrix.
  - ExperimentConfig.cold_bundle default (False) and explicit (True).

These tests are Docker-free and instantiate no real repos.
"""

from __future__ import annotations

import pytest

from testharness.experiment_runner import should_use_bundle
from testharness.models import ExperimentConfig


# ---------------------------------------------------------------------------
# ExperimentConfig field tests (instantiation-only; trivially fast)
# ---------------------------------------------------------------------------


def test_cold_bundle_config_default():
    """ExperimentConfig.cold_bundle defaults to False — backward-compatible."""
    cfg = ExperimentConfig(repos=["testrepo"], methods=["agentcache"])
    assert cfg.cold_bundle is False


def test_cold_bundle_config_explicit_true():
    """ExperimentConfig(cold_bundle=True) stores the value."""
    cfg = ExperimentConfig(repos=["testrepo"], methods=["agentcache"], cold_bundle=True)
    assert cfg.cold_bundle is True


# ---------------------------------------------------------------------------
# should_use_bundle() — full matrix
# ---------------------------------------------------------------------------
# The helper is PURE (no I/O) and must cover all nine combinations of
# method × {cold, warm} × cold_bundle.  Warm passes ALWAYS get the bundle.
# Cold passes: agentcache+cold_bundle=True gets it; everything else does not.


@pytest.mark.parametrize(
    "method, is_cold, cold_bundle, expected",
    [
        # --- warm passes (is_cold=False) always use the bundle ---
        ("naive", False, False, True),
        ("naive", False, True, True),
        ("blobless", False, False, True),
        ("blobless", False, True, True),
        ("agentcache", False, False, True),
        ("agentcache", False, True, True),
        # --- cold naive / blobless: no bundle regardless of cold_bundle flag ---
        # (cold_bundle only controls agentcache cold behaviour)
        ("naive", True, False, False),
        ("naive", True, True, False),
        ("blobless", True, False, False),
        ("blobless", True, True, False),
        # --- cold agentcache: the key cases ---
        # Default (cold_bundle=False): honest first-visit, no bundle.
        ("agentcache", True, False, False),
        # Production-amortised cold (cold_bundle=True): bundle is allowed.
        ("agentcache", True, True, True),
    ],
)
def test_should_use_bundle_matrix(
    method: str, is_cold: bool, cold_bundle: bool, expected: bool
) -> None:
    """should_use_bundle returns the correct value for every matrix cell."""
    result = should_use_bundle(method, is_cold=is_cold, cold_bundle=cold_bundle)
    assert result is expected, (
        f"should_use_bundle({method!r}, is_cold={is_cold}, cold_bundle={cold_bundle}) "
        f"→ {result!r}, want {expected!r}"
    )


def test_should_use_bundle_warm_is_always_true():
    """Sanity: for any method, warm (is_cold=False) always returns True."""
    for method in ("naive", "blobless", "agentcache"):
        for cold_bundle in (False, True):
            assert should_use_bundle(method, is_cold=False, cold_bundle=cold_bundle), (
                f"Expected True for warm {method} (cold_bundle={cold_bundle})"
            )


def test_should_use_bundle_cold_non_agentcache_always_false():
    """Cold naive/blobless never use the bundle, regardless of cold_bundle flag."""
    for method in ("naive", "blobless"):
        for cold_bundle in (False, True):
            assert not should_use_bundle(
                method, is_cold=True, cold_bundle=cold_bundle
            ), f"Expected False for cold {method} (cold_bundle={cold_bundle})"
