"""Tests for the verdict-gated install-offer flow, agentgitsmart.generate, and adopter workflow.

Three tiers of coverage (all pure-function / fast — no network / daemon):
  1. Pure functions in scripts/try_agentgitsmart.py:
       should_offer_install, detect_remote_kind, plan_scaffold, apply_scaffold
       run_install_offer (shell orchestrator — uses assume_yes to avoid /dev/tty)
  2. agentgitsmart.generate.main() — Part 1 packaged entrypoint
  3. docs/adopter-workflow.yml — Part 2 adopter template

TDD: tests were written before the implementation (RED → GREEN).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml

# ---------------------------------------------------------------------------
# Module-level imports — will fail (RED) until the implementation exists
# ---------------------------------------------------------------------------

from scripts.try_agentgitsmart import (  # noqa: E402
    should_offer_install,
    detect_remote_kind,
    plan_scaffold,
    apply_scaffold,
    run_install_offer,
)
from agentgitsmart.generate import main as generate_main  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def tooling_root(tmp_path: Path) -> Path:
    """Minimal fake tooling_root with the three scaffold templates."""
    root = tmp_path / "tooling_root"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "adopter-workflow.yml").write_text(
        "# adopter-workflow stub\nname: AgentGitSmart adopter\n"
    )
    (root / "docs" / "ADOPTER_AGENTS_TEMPLATE.md").write_text(
        "# Agent Instructions — `<REPO_NAME>`\n"
    )
    (root / ".agentgitsmart.example").write_text('service_url = "https://example.com"\n')
    return root


@pytest.fixture()
def target_dir(tmp_path: Path) -> Path:
    """Empty directory that represents a target repo root."""
    d = tmp_path / "target_repo"
    d.mkdir()
    return d


@pytest.fixture()
def target_git_worktree(tmp_path: Path) -> Path:
    """Real (non-bare) git worktree for apply_scaffold's no-git-mutation test."""
    repo = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return repo


# ===========================================================================
# Part 3 — should_offer_install (pure)
# ===========================================================================


class TestShouldOfferInstall:
    """Full decision matrix for should_offer_install."""

    def test_worthwhile_and_worktree_is_true(self):
        assert should_offer_install("agentgitsmart worthwhile", True) is True

    def test_worthwhile_but_not_worktree_is_false(self):
        """Do not offer when target is a URL / bare repo."""
        assert should_offer_install("agentgitsmart worthwhile", False) is False

    def test_high_reuse_and_worktree_is_false(self):
        assert should_offer_install("worth it only at high reuse", True) is False

    def test_blobless_enough_is_false(self):
        assert should_offer_install("blobless is enough", True) is False

    def test_inconclusive_is_false(self):
        assert should_offer_install("inconclusive — measure to be sure", True) is False

    def test_empty_label_is_false(self):
        assert should_offer_install("", True) is False

    def test_case_sensitive_label_mismatch_is_false(self):
        """Verdict labels are case-sensitive — partial match must not trigger."""
        assert should_offer_install("AgentGitSmart Worthwhile", True) is False


# ===========================================================================
# Part 3 — detect_remote_kind (pure)
# ===========================================================================


class TestDetectRemoteKind:
    """detect_remote_kind maps a remote URL to "github" / "other" / "none"."""

    def test_https_github_is_github(self):
        assert detect_remote_kind("https://github.com/user/repo.git") == "github"

    def test_ssh_github_is_github(self):
        assert detect_remote_kind("git@github.com:user/repo.git") == "github"

    def test_gitlab_https_is_other(self):
        assert detect_remote_kind("https://gitlab.com/user/repo.git") == "other"

    def test_bitbucket_is_other(self):
        assert detect_remote_kind("https://bitbucket.org/user/repo.git") == "other"

    def test_self_hosted_http_is_other(self):
        assert detect_remote_kind("https://git.company.internal/repo.git") == "other"

    def test_none_is_none_string(self):
        assert detect_remote_kind(None) == "none"

    def test_empty_string_is_none_string(self):
        assert detect_remote_kind("") == "none"

    def test_github_subdomain_variant(self):
        """Any URL containing github.com → "github"."""
        assert detect_remote_kind("ssh://git@github.com/user/repo.git") == "github"


# ===========================================================================
# Part 3 — plan_scaffold
# ===========================================================================


class TestPlanScaffold:
    """plan_scaffold returns an ordered list of file-plan dicts."""

    def test_github_plan_includes_workflow(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        dests = [item["dest"] for item in plan]
        assert ".github/workflows/agentgitsmart.yml" in dests

    def test_other_remote_plan_excludes_workflow(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "other")
        dests = [item["dest"] for item in plan]
        assert ".github/workflows/agentgitsmart.yml" not in dests

    def test_none_remote_plan_excludes_workflow(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "none")
        dests = [item["dest"] for item in plan]
        assert ".github/workflows/agentgitsmart.yml" not in dests

    def test_plan_always_includes_agents_md(self, tooling_root, target_dir):
        for kind in ("github", "other", "none"):
            plan = plan_scaffold(str(tooling_root), str(target_dir), kind)
            dests = [item["dest"] for item in plan]
            assert "AGENTS.md" in dests, f"AGENTS.md missing for remote_kind={kind!r}"

    def test_plan_always_includes_agentgitsmart_dot(self, tooling_root, target_dir):
        for kind in ("github", "other", "none"):
            plan = plan_scaffold(str(tooling_root), str(target_dir), kind)
            dests = [item["dest"] for item in plan]
            assert ".agentgitsmart" in dests, f".agentgitsmart missing for remote_kind={kind!r}"

    def test_sources_point_into_tooling_root(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        for item in plan:
            assert item["source"].startswith(str(tooling_root)), (
                f"source {item['source']!r} is not under tooling_root"
            )

    def test_preexisting_agents_md_marked_skip_exists(self, tooling_root, target_dir):
        """Pre-create AGENTS.md → action must be 'skip-exists'."""
        (target_dir / "AGENTS.md").write_text("# existing\n")
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        agents_items = [i for i in plan if i["dest"] == "AGENTS.md"]
        assert agents_items, "AGENTS.md not in plan"
        item = agents_items[0]
        assert item["exists"] is True
        assert item["action"] == "skip-exists"

    def test_missing_file_marked_create(self, tooling_root, target_dir):
        """A file absent from target → action must be 'create'."""
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        for item in plan:
            assert item["action"] == "create", (
                f"{item['dest']!r} already exists unexpectedly"
            )

    def test_plan_items_have_required_keys(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        for item in plan:
            assert "dest" in item
            assert "source" in item
            assert "exists" in item
            assert "action" in item

    def test_github_plan_order(self, tooling_root, target_dir):
        """workflow comes first, then AGENTS.md, then .agentgitsmart."""
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        dests = [item["dest"] for item in plan]
        assert dests.index(".github/workflows/agentgitsmart.yml") < dests.index("AGENTS.md")
        assert dests.index("AGENTS.md") < dests.index(".agentgitsmart")

    def test_workflow_source_is_adopter_workflow_yml(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        wf = next(
            i for i in plan if i["dest"] == ".github/workflows/agentgitsmart.yml"
        )
        assert wf["source"].endswith("docs/adopter-workflow.yml")

    def test_agents_source_is_adopter_template(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        ag = next(i for i in plan if i["dest"] == "AGENTS.md")
        assert ag["source"].endswith("docs/ADOPTER_AGENTS_TEMPLATE.md")

    def test_agentgitsmart_source_is_example_file(self, tooling_root, target_dir):
        plan = plan_scaffold(str(tooling_root), str(target_dir), "github")
        ac = next(i for i in plan if i["dest"] == ".agentgitsmart")
        assert ac["source"].endswith(".agentgitsmart.example")


# ===========================================================================
# Part 3 — apply_scaffold
# ===========================================================================


class TestApplyScaffold:
    """apply_scaffold writes only 'create' items and returns their relpaths."""

    def _make_plan(
        self,
        tooling_root: Path,
        target_dir: Path,
        *,
        include_wf: bool = True,
        skip_agents: bool = False,
    ) -> list[dict]:
        """Build a synthetic plan from real tooling_root files."""
        plan: list[dict] = []
        if include_wf:
            wf_src = str(tooling_root / "docs" / "adopter-workflow.yml")
            wf_dest_full = target_dir / ".github" / "workflows" / "agentgitsmart.yml"
            plan.append(
                {
                    "dest": ".github/workflows/agentgitsmart.yml",
                    "source": wf_src,
                    "exists": wf_dest_full.exists(),
                    "action": "skip-exists" if wf_dest_full.exists() else "create",
                }
            )
        ag_src = str(tooling_root / "docs" / "ADOPTER_AGENTS_TEMPLATE.md")
        ag_dest_full = target_dir / "AGENTS.md"
        plan.append(
            {
                "dest": "AGENTS.md",
                "source": ag_src,
                "exists": ag_dest_full.exists(),
                "action": "skip-exists" if skip_agents else "create",
            }
        )
        ac_src = str(tooling_root / ".agentgitsmart.example")
        ac_dest_full = target_dir / ".agentgitsmart"
        plan.append(
            {
                "dest": ".agentgitsmart",
                "source": ac_src,
                "exists": ac_dest_full.exists(),
                "action": "skip-exists" if ac_dest_full.exists() else "create",
            }
        )
        return plan

    def test_creates_github_workflows_parents(self, tooling_root, target_dir):
        """apply_scaffold must mkdir -p .github/workflows/."""
        plan = self._make_plan(tooling_root, target_dir)
        apply_scaffold(plan, str(target_dir))
        assert (target_dir / ".github" / "workflows" / "agentgitsmart.yml").exists()

    def test_creates_agents_md(self, tooling_root, target_dir):
        plan = self._make_plan(tooling_root, target_dir)
        apply_scaffold(plan, str(target_dir))
        assert (target_dir / "AGENTS.md").exists()

    def test_creates_agentgitsmart_dot(self, tooling_root, target_dir):
        plan = self._make_plan(tooling_root, target_dir)
        apply_scaffold(plan, str(target_dir))
        assert (target_dir / ".agentgitsmart").exists()

    def test_returns_created_relpaths(self, tooling_root, target_dir):
        plan = self._make_plan(tooling_root, target_dir)
        created = apply_scaffold(plan, str(target_dir))
        assert ".github/workflows/agentgitsmart.yml" in created
        assert "AGENTS.md" in created
        assert ".agentgitsmart" in created

    def test_does_not_overwrite_existing_file(self, tooling_root, target_dir):
        """skip-exists items must not be overwritten."""
        existing = target_dir / "AGENTS.md"
        existing.write_text("# KEEP THIS\n")
        plan = self._make_plan(tooling_root, target_dir, skip_agents=True)
        apply_scaffold(plan, str(target_dir))
        assert existing.read_text() == "# KEEP THIS\n"

    def test_skipped_item_not_in_returned_list(self, tooling_root, target_dir):
        (target_dir / "AGENTS.md").write_text("# existing\n")
        plan = self._make_plan(tooling_root, target_dir, skip_agents=True)
        created = apply_scaffold(plan, str(target_dir))
        assert "AGENTS.md" not in created

    def test_no_git_mutation(self, tooling_root, target_git_worktree):
        """apply_scaffold must not change the repo's HEAD or refs."""
        repo = target_git_worktree
        head_before = (repo / ".git" / "HEAD").read_text()
        plan = self._make_plan(tooling_root, repo)
        apply_scaffold(plan, str(repo))
        head_after = (repo / ".git" / "HEAD").read_text()
        assert head_before == head_after, "apply_scaffold mutated .git/HEAD"

    def test_empty_plan_returns_empty_list(self, target_dir):
        created = apply_scaffold([], str(target_dir))
        assert created == []


# ===========================================================================
# Part 3 — run_install_offer (shell orchestrator)
# ===========================================================================


class TestRunInstallOffer:
    """run_install_offer orchestrates the full consent → scaffold flow."""

    @pytest.fixture(autouse=True)
    def _require_git(self):
        if not __import__("shutil").which("git"):
            pytest.skip("git not available")

    def _make_tooling(self, tmp_path: Path) -> Path:
        root = tmp_path / "tooling"
        root.mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "adopter-workflow.yml").write_text(
            "name: AgentGitSmart adopter\n"
        )
        (root / "docs" / "ADOPTER_AGENTS_TEMPLATE.md").write_text(
            "# Agent Instructions\n"
        )
        (root / ".agentgitsmart.example").write_text('service_url = "https://e.g."\n')
        return root

    def test_worthwhile_assume_yes_returns_installed_true(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        result = run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=True,
            no_install=False,
            interactive=False,
        )
        assert result["offered"] is True
        assert result["installed"] is True

    def test_worthwhile_assume_yes_creates_files(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=True,
            no_install=False,
            interactive=False,
        )
        assert (target / ".github" / "workflows" / "agentgitsmart.yml").exists()
        assert (target / "AGENTS.md").exists()
        assert (target / ".agentgitsmart").exists()

    def test_no_install_flag_returns_installed_false(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        result = run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=False,
            no_install=True,
            interactive=False,
        )
        assert result["offered"] is True
        assert result["installed"] is False
        assert result.get("reason") == "no_install"

    def test_no_install_flag_writes_nothing(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=False,
            no_install=True,
            interactive=False,
        )
        assert not (target / "AGENTS.md").exists()
        assert not (target / ".agentgitsmart").exists()

    def test_non_worthwhile_label_not_offered(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        result = run_install_offer(
            verdict_label="blobless is enough",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=True,
            no_install=False,
            interactive=False,
        )
        assert result["offered"] is False

    def test_non_worthwhile_label_writes_nothing(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        run_install_offer(
            verdict_label="worth it only at high reuse",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=True,
            no_install=False,
            interactive=False,
        )
        assert not (target / "AGENTS.md").exists()

    def test_not_a_worktree_not_offered(self, tmp_path):
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        result = run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=False,  # bare repo / URL target
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=True,
            no_install=False,
            interactive=False,
        )
        assert result["offered"] is False

    def test_non_github_remote_skips_workflow_file(self, tmp_path):
        """For 'other' remotes, workflow is excluded; AGENTS.md + .agentgitsmart still created."""
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://gitlab.com/user/repo.git",
            assume_yes=True,
            no_install=False,
            interactive=False,
        )
        assert not (target / ".github" / "workflows" / "agentgitsmart.yml").exists()
        assert (target / "AGENTS.md").exists()
        assert (target / ".agentgitsmart").exists()

    def test_no_tty_non_interactive_not_assume_yes_returns_installed_false(
        self, tmp_path
    ):
        """Without /dev/tty or assume_yes and non-interactive → installed False."""
        root = self._make_tooling(tmp_path)
        target = tmp_path / "repo"
        target.mkdir()
        result = run_install_offer(
            verdict_label="agentgitsmart worthwhile",
            target_is_local_worktree=True,
            target_repo=str(target),
            tooling_root=str(root),
            remote_url="https://github.com/user/repo.git",
            assume_yes=False,
            no_install=False,
            interactive=False,  # no tty; /dev/tty open may or may not fail
        )
        # Either not offered or installed=False (if /dev/tty opened unexpectedly, skip)
        # The key contract: we never hang.
        assert not result.get("installed", False) or result.get("offered") is False


# ===========================================================================
# Part 1 — agentgitsmart.generate.main()
# ===========================================================================


class TestGenerateMain:
    """Packaged agentgitsmart-generate entrypoint."""

    def test_main_returns_zero(self, repo, capsys):
        r, _commit_hex = repo
        rc = generate_main(["--repo", r.path, "--commit", "HEAD"])
        assert rc == 0

    def test_main_emits_agentgitsmart_ref_marker(self, repo, capsys):
        r, _commit_hex = repo
        generate_main(["--repo", r.path, "--commit", "HEAD"])
        out = capsys.readouterr().out
        assert "::AGENTGITSMART_REF::" in out

    def test_main_marker_contains_refs_agent_git_smart(self, repo, capsys):
        r, _commit_hex = repo
        generate_main(["--repo", r.path, "--commit", "HEAD"])
        out = capsys.readouterr().out
        marker_lines = [ln for ln in out.splitlines() if "::AGENTGITSMART_REF::" in ln]
        assert marker_lines, "No ::AGENTGITSMART_REF:: line in output"
        assert "refs/agent-git-smart/" in marker_lines[0]

    def test_main_builds_agent_git_smart_ref(self, repo, capsys):
        """The side ref refs/agent-git-smart/<sha> must exist after generate_main."""
        r, commit_hex = repo
        generate_main(["--repo", r.path, "--commit", "HEAD"])
        expected_ref = f"refs/agent-git-smart/{commit_hex}"
        # pygit2 lookup
        ref = r.references.get(expected_ref)
        assert ref is not None, f"{expected_ref} was not created"

    def test_main_prints_source_commit(self, repo, capsys):
        r, _commit_hex = repo
        generate_main(["--repo", r.path, "--commit", "HEAD"])
        out = capsys.readouterr().out
        assert "source_commit" in out

    def test_main_prints_cache_ref(self, repo, capsys):
        r, _commit_hex = repo
        generate_main(["--repo", r.path, "--commit", "HEAD"])
        out = capsys.readouterr().out
        assert "cache_ref" in out

    def test_shim_script_calls_packaged_main(self):
        """scripts/generate_agentgitsmart.py must import agentgitsmart.generate and delegate."""
        shim_src = (_ROOT / "scripts" / "generate_agentgitsmart.py").read_text()
        assert "agentgitsmart.generate" in shim_src, (
            "Shim does not reference agentgitsmart.generate"
        )

    def test_console_script_registered(self):
        """pyproject.toml must register agentgitsmart-generate = 'agentgitsmart.generate:main'."""
        pyproject = (_ROOT / "pyproject.toml").read_text()
        assert "agentgitsmart-generate" in pyproject
        assert "agentgitsmart.generate:main" in pyproject


# ===========================================================================
# Part 2 — docs/adopter-workflow.yml
# ===========================================================================


_ADOPTER_WF = _ROOT / "docs" / "adopter-workflow.yml"


class TestAdopterWorkflow:
    """docs/adopter-workflow.yml must exist and satisfy loop-safety + install contract."""

    def test_file_exists(self):
        assert _ADOPTER_WF.exists(), "docs/adopter-workflow.yml is missing"

    def test_is_valid_yaml(self):
        with open(_ADOPTER_WF, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        assert isinstance(data, dict), "adopter-workflow.yml did not parse as a YAML mapping"

    def test_triggers_on_push_main(self):
        with open(_ADOPTER_WF, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        on = data.get("on") or data.get(True)  # YAML 'on' may parse as True
        push = on.get("push") if isinstance(on, dict) else None
        assert push is not None, "'on.push' trigger missing"
        branches = push.get("branches", [])
        assert "main" in branches, f"'main' not in branches: {branches}"

    def test_triggers_on_workflow_dispatch(self):
        with open(_ADOPTER_WF, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        on = data.get("on") or data.get(True)
        assert "workflow_dispatch" in (on or {}), "'workflow_dispatch' trigger missing"

    def test_loop_safe_push_targets_refs_agent_git_smart(self):
        """Artifact push must target refs/agent-git-smart/* (not a branch → no re-trigger)."""
        text = _ADOPTER_WF.read_text()
        assert "refs/agent-git-smart" in text, (
            "Push step does not reference refs/agent-git-smart — workflow may be loop-unsafe"
        )

    def test_installs_via_pip_not_pip_install_e(self):
        """Adopter workflow must pip-install the published package, NOT pip install -e ."""
        text = _ADOPTER_WF.read_text()
        assert "pip install -e" not in text, (
            "Adopter workflow uses 'pip install -e .' — must use published package instead"
        )

    def test_installs_agentgitsmart_package(self):
        text = _ADOPTER_WF.read_text()
        assert "agentgitsmart" in text.lower()
        # Must reference the pip-installable package (not repo-internal scripts)
        assert "agentgitsmart @" in text or "agentgitsmart==" in text or "AgentGitSmart@" in text

    def test_uses_agentgitsmart_generate_cli(self):
        """Must call agentgitsmart-generate (the console script), not scripts/generate_agentgitsmart.py."""
        text = _ADOPTER_WF.read_text()
        assert "agentgitsmart-generate" in text, (
            "Adopter workflow does not use the agentgitsmart-generate console script"
        )

    def test_has_contents_write_permission(self):
        with open(_ADOPTER_WF, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        perms = data.get("permissions", {})
        assert perms.get("contents") == "write", (
            f"'permissions.contents' is not 'write': {perms}"
        )

    def test_has_concurrency_with_cancel(self):
        with open(_ADOPTER_WF, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        conc = data.get("concurrency", {})
        assert conc, "No concurrency block found"
        assert conc.get("cancel-in-progress") is True, (
            "concurrency.cancel-in-progress is not true"
        )

    def test_has_top_comment_mentioning_adopter(self):
        text = _ADOPTER_WF.read_text()
        # The top comment should mention it's an adopter template
        assert "adopter" in text.lower() or "Adopter" in text, (
            "Top comment does not mention adopter"
        )

    def test_fetches_full_history(self):
        text = _ADOPTER_WF.read_text()
        assert "fetch-depth: 0" in text, (
            "Adopter workflow missing 'fetch-depth: 0' (needed for the blobless bundle)"
        )

    def test_uploads_bundle_artifact(self):
        text = _ADOPTER_WF.read_text()
        assert "upload-artifact" in text, "No upload-artifact step found"
