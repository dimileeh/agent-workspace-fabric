"""Tests for the feature-PR sync core.

Mirrors the style of ``test_release_pr_sync.py``: async tests, ``FakeCommandRunner``
for subprocess mocking, ``pytest.mark.unit`` marker.

The module under test provides the PR-metadata lookup that the
``attach_feature_pr_monitor`` CLI uses to resolve which branch to
check out + whether it's safe to attach a monitor (i.e. PR is open,
not closed/merged).
"""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.runtime.feature_pr_sync import (
    FeaturePRMetadata,
    FeaturePRSyncError,
    build_sync_feature_pr_task_spec,
    fetch_pr_metadata,
    is_feature_pr_monitor_running,
    task_spec_filename,
)

_REPO = RepoRef(owner="dimileeh", name="aira-web")


def _gh_pr_view_json(
    *,
    head_ref="fix/foo",
    base_ref="development",
    state="OPEN",
    is_draft=False,
    closed=False,
    merged=False,
    author="dimileeh",
    number=277,
    url="https://github.com/dimileeh/aira-web/pull/277",
    title="fix: foo",
) -> str:
    """Canonical ``gh pr view --json ...`` payload helper."""
    return json.dumps(
        {
            "number": number,
            "headRefName": head_ref,
            "baseRefName": base_ref,
            "state": state,
            "isDraft": is_draft,
            "closed": closed,
            "merged": merged,
            "author": {"login": author} if author else None,
            "url": url,
            "title": title,
        }
    )


class TestHappyPath:
    @pytest.mark.unit
    async def test_open_pr_returns_parsed_metadata(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json())

        result = await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)

        assert isinstance(result, FeaturePRMetadata)
        assert result.number == 277
        assert result.head_branch == "fix/foo"
        assert result.base_branch == "development"
        assert result.state == "OPEN"
        assert result.is_draft is False
        assert result.closed is False
        assert result.merged is False
        assert result.author == "dimileeh"
        assert result.url == "https://github.com/dimileeh/aira-web/pull/277"
        assert result.title == "fix: foo"

    @pytest.mark.unit
    async def test_issues_one_gh_call_with_correct_args(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json())

        await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call.args[:3] == ["gh", "pr", "view"]
        assert "277" in call.args
        assert "--repo" in call.args
        assert "dimileeh/aira-web" in call.args
        assert "--json" in call.args

    @pytest.mark.unit
    async def test_draft_pr_is_allowed(self) -> None:
        """Draft PRs can still accrue reviewer comments — the monitor
        should be allowed to attach. The ``auto_merge`` gate lives in
        ``pr_monitor.decide`` and already refuses to merge drafts."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json(is_draft=True))

        result = await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)

        assert result.is_draft is True
        assert result.state == "OPEN"

    @pytest.mark.unit
    async def test_author_may_be_null(self) -> None:
        """Ghost / deleted-account PR authors come back with author=null."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json(author=None))

        result = await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)

        assert result.author is None


class TestRefusalConditions:
    """The monitor is only useful against an open, un-merged PR. Refuse
    the rest upfront so we don't waste a workspace provisioning for a
    PR that will never transition."""

    @pytest.mark.unit
    async def test_closed_pr_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json(state="CLOSED", closed=True))

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)
        assert "closed" in str(excinfo.value).lower()
        assert excinfo.value.operation == "fetch_pr_metadata"

    @pytest.mark.unit
    async def test_merged_pr_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_gh_pr_view_json(state="MERGED", closed=True, merged=True),
        )

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)
        assert "merged" in str(excinfo.value).lower()


class TestGhCliErrors:
    @pytest.mark.unit
    async def test_nonexistent_pr_raises_with_stderr(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=1,
            stdout="",
            stderr="GraphQL: Could not resolve to a PullRequest",
        )

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=99999)
        assert excinfo.value.operation == "fetch_pr_metadata"
        assert "PullRequest" in excinfo.value.stderr

    @pytest.mark.unit
    async def test_gh_not_installed_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=127, stdout="", stderr="gh: command not found")

        with pytest.raises(FeaturePRSyncError):
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)

    @pytest.mark.unit
    async def test_malformed_json_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="not json at all {")

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)
        assert "parse" in str(excinfo.value).lower() or "json" in str(excinfo.value).lower()

    @pytest.mark.unit
    async def test_missing_required_field_raises(self) -> None:
        """If GitHub ever changes the schema we want a loud failure,
        not a KeyError deep in the state machine."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=json.dumps({"number": 277}))

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)
        assert "head" in str(excinfo.value).lower() or "base" in str(excinfo.value).lower()

    @pytest.mark.unit
    async def test_empty_head_branch_raises(self) -> None:
        """An empty string is more subtle than a missing key — still reject."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json(head_ref=""))

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)
        assert "headrefname" in str(excinfo.value).lower() or "head" in str(excinfo.value).lower()

    @pytest.mark.unit
    async def test_empty_base_branch_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_gh_pr_view_json(base_ref=""))

        with pytest.raises(FeaturePRSyncError) as excinfo:
            await fetch_pr_metadata(runner=fake, repo=_REPO, pr_number=277)
        assert "baserefname" in str(excinfo.value).lower() or "base" in str(excinfo.value).lower()


def _metadata(**overrides) -> FeaturePRMetadata:
    defaults = {
        "number": 277,
        "head_branch": "fix/sprints-ai-plan-button-guard",
        "base_branch": "development",
        "state": "OPEN",
        "is_draft": False,
        "closed": False,
        "merged": False,
        "author": "dimileeh",
        "url": "https://github.com/dimileeh/aira-web/pull/277",
        "title": "fix(sprints): guard AI Plan Sprint button (AIRA-T37 FE)",
    }
    defaults.update(overrides)
    return FeaturePRMetadata(**defaults)


class TestBuildSyncFeaturePrTaskSpec:
    """``build_sync_feature_pr_task_spec`` is the pure function that
    produces the task spec ``run_awf.py`` consumes. Must be deterministic
    (same input → same spec) so the CLI can safely write it to a
    deterministic path for idempotency."""

    @pytest.mark.unit
    def test_minimal_spec_has_required_fields(self) -> None:
        spec = build_sync_feature_pr_task_spec(
            repo_url="git@github.com:dimileeh/aira-web.git",
            metadata=_metadata(),
            agent="claude_code",
            auto_merge=False,
        )
        assert spec["repo_url"] == "git@github.com:dimileeh/aira-web.git"
        assert spec["task_kind"] == "sync_feature_pr"
        assert spec["branch_base"] == "development"
        assert spec["source_branch"] == "fix/sprints-ai-plan-button-guard"
        assert spec["pr_number"] == 277
        assert spec["agent"] == "claude_code"
        assert spec["auto_merge"] is False
        assert spec["requires_database"] is False
        assert spec["test_commands"] == []
        assert spec["companions"] == []

    @pytest.mark.unit
    def test_task_title_identifies_repo_and_pr(self) -> None:
        """The title appears in logs + workspace row. It MUST include
        repo slug + PR number so a human scanning ``awf workspace list``
        can tell which PR each monitor is watching."""
        spec = build_sync_feature_pr_task_spec(
            repo_url="git@github.com:dimileeh/aira-web.git",
            metadata=_metadata(),
            agent="claude_code",
            auto_merge=False,
        )
        assert "dimileeh/aira-web" in spec["task_title"]
        assert "277" in spec["task_title"]

    @pytest.mark.unit
    def test_auto_merge_flag_propagates(self) -> None:
        enabled = build_sync_feature_pr_task_spec(
            repo_url="git@github.com:dimileeh/aira-web.git",
            metadata=_metadata(),
            agent="claude_code",
            auto_merge=True,
        )
        assert enabled["auto_merge"] is True

    @pytest.mark.unit
    def test_companions_passed_through(self) -> None:
        companions = [{"name": "backend", "build_context": "/path"}]
        spec = build_sync_feature_pr_task_spec(
            repo_url="git@github.com:dimileeh/aira-web.git",
            metadata=_metadata(),
            agent="claude_code",
            auto_merge=False,
            companions=companions,
        )
        assert spec["companions"] == companions
        # Defensive copy — mutating the argument list must not leak into
        # the returned spec (callers may reuse the list).
        companions.clear()
        assert len(spec["companions"]) == 1

    @pytest.mark.unit
    def test_different_https_url_parses(self) -> None:
        spec = build_sync_feature_pr_task_spec(
            repo_url="https://github.com/dimileeh/aira-web",
            metadata=_metadata(),
            agent="codex",
            auto_merge=False,
        )
        assert "dimileeh/aira-web" in spec["task_title"]

    @pytest.mark.unit
    def test_task_prompt_explains_the_role(self) -> None:
        """Inside the agent container the coding CLI sees task_prompt as
        context. For a sync_feature_pr the prompt shouldn't say
        "implement the feature" (code's already there); it should frame
        the job as addressing reviewer comments."""
        spec = build_sync_feature_pr_task_spec(
            repo_url="git@github.com:dimileeh/aira-web.git",
            metadata=_metadata(),
            agent="claude_code",
            auto_merge=False,
        )
        prompt_lower = spec["task_prompt"].lower()
        assert "review" in prompt_lower or "comment" in prompt_lower
        # Must NOT instruct re-implementation of the feature. The prompt
        # uses "implementation" only in negating phrases like "no fresh
        # implementation runs on entry"; the surrounding context must
        # mention addressing comments, which is the actual job.
        assert "no fresh implementation" in prompt_lower
        assert "address" in prompt_lower


class TestTaskSpecFilename:
    """The filename lives in ``<work_dir>/feature-pr-specs/`` and is
    grep'd for by ``is_feature_pr_monitor_running`` — it MUST be:

      - Deterministic for the same (repo, PR) pair (idempotency).
      - Distinguishable from release-PR spec filenames (to avoid a
        false-positive "monitor already running" when a release-PR
        monitor is running for a different repo).
      - Filesystem-safe (no ``/`` in filename).
    """

    @pytest.mark.unit
    def test_deterministic_for_same_inputs(self) -> None:
        a = task_spec_filename(repo_slug="dimileeh/aira-web", pr_number=277)
        b = task_spec_filename(repo_slug="dimileeh/aira-web", pr_number=277)
        assert a == b

    @pytest.mark.unit
    def test_includes_repo_and_pr_number(self) -> None:
        name = task_spec_filename(repo_slug="dimileeh/aira-web", pr_number=277)
        assert "dimileeh" in name
        assert "aira-web" in name
        assert "277" in name
        assert name.endswith(".json")

    @pytest.mark.unit
    def test_slashes_are_escaped(self) -> None:
        """Repo slugs contain ``/``. The filename must not."""
        name = task_spec_filename(repo_slug="dimileeh/aira-web", pr_number=277)
        assert "/" not in name

    @pytest.mark.unit
    def test_differs_from_release_spec_filename(self) -> None:
        """Release-PR specs use ``-pr<n>`` suffix. Feature-PR specs
        must be distinguishable so the pgrep idempotency check doesn't
        see cross-kind false positives on the same repo+PR number."""
        feature_name = task_spec_filename(repo_slug="dimileeh/aira-web", pr_number=277)
        # The release convention (from schedule_release_pr.py):
        #   f"{slug.replace('/', '__')}-pr{pr_number}.json"
        release_name = "dimileeh__aira-web-pr277.json"
        assert feature_name != release_name


class TestIsFeaturePrMonitorRunning:
    """Idempotency guard: pgrep ``run_awf.py`` + check whether one of
    the processes is already driving our spec file. The ``process_lister``
    parameter is a seam for tests (production passes ``None`` and the
    module calls pgrep itself)."""

    @pytest.mark.unit
    def test_empty_process_list_returns_false(self) -> None:
        assert (
            is_feature_pr_monitor_running(
                spec_filename="dimileeh__aira-web-feature-pr277.json",
                process_lister=lambda: "",
            )
            is False
        )

    @pytest.mark.unit
    def test_matching_spec_filename_returns_true(self) -> None:
        pgrep_out = (
            "12345 python /path/run_awf.py --config "
            "/tmp/awf/feature-pr-specs/dimileeh__aira-web-feature-pr277.json --work-dir /tmp/awf\n"
            "12346 bash /some/other/thing\n"
        )
        assert (
            is_feature_pr_monitor_running(
                spec_filename="dimileeh__aira-web-feature-pr277.json",
                process_lister=lambda: pgrep_out,
            )
            is True
        )

    @pytest.mark.unit
    def test_different_filename_returns_false(self) -> None:
        pgrep_out = (
            "12345 python /path/run_awf.py --config "
            "/tmp/awf/release-pr-specs/dimileeh__aira-web-pr272.json\n"
        )
        assert (
            is_feature_pr_monitor_running(
                spec_filename="dimileeh__aira-web-feature-pr277.json",
                process_lister=lambda: pgrep_out,
            )
            is False
        )

    @pytest.mark.unit
    def test_process_lister_failure_returns_false(self) -> None:
        """If pgrep isn't installed / fails, assume no monitor running
        — never block a launch on an introspection failure."""

        def boom() -> str:
            raise FileNotFoundError("pgrep not installed")

        assert (
            is_feature_pr_monitor_running(
                spec_filename="dimileeh__aira-web-feature-pr277.json",
                process_lister=boom,
            )
            is False
        )
