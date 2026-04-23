"""Tests for the ``attach_feature_pr_monitor`` CLI's orchestration layer.

The CLI script itself (``scripts/attach_feature_pr_monitor.py``) is a
thin argparse shell; all of its moving parts are covered here via a
pair of extracted helpers:

  - ``orchestrate_attach`` — the async driver. Takes injectable seams
    for the runner, spec-file writer, and subprocess spawner so the
    tests can assert exactly what gets written and how ``run_awf.py``
    is invoked without ever touching the real FS or spawning anything.
  - ``parse_args`` — argparse factory. Exposed so a test can verify
    flag parsing without calling into ``sys.exit``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.runtime.feature_pr_sync import FeaturePRMetadata
from scripts import attach_feature_pr_monitor as cli

_REPO_URL = "git@github.com:dimileeh/aira-web.git"
_REPO = RepoRef(owner="dimileeh", name="aira-web")


def _gh_pr_view_ok(pr_number: int = 277) -> str:
    return json.dumps(
        {
            "number": pr_number,
            "headRefName": "fix/sprints-ai-plan-button-guard",
            "baseRefName": "development",
            "state": "OPEN",
            "isDraft": False,
            "closed": False,
            "merged": False,
            "author": {"login": "dimileeh"},
            "url": f"https://github.com/dimileeh/aira-web/pull/{pr_number}",
            "title": "fix(sprints): guard AI Plan Sprint button (AIRA-T37 FE)",
        }
    )


class _SpawnCapture:
    """Drop-in replacement for ``subprocess.Popen`` in tests. Records
    every invocation + returns a dummy handle."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(args))

        class _Dummy:
            pid = 12345

        return _Dummy()


class TestParseArgs:
    @pytest.mark.unit
    def test_required_args_are_enforced(self) -> None:
        with pytest.raises(SystemExit):
            cli.parse_args([])

    @pytest.mark.unit
    def test_minimal_invocation(self) -> None:
        ns = cli.parse_args(
            [
                "--repo",
                _REPO_URL,
                "--pr",
                "277",
            ]
        )
        assert ns.repo == _REPO_URL
        assert ns.pr == 277
        assert ns.agent == "codex"  # default matches the other scripts
        # Default is auto-merge ON for feature→dev PRs (AWF's contract is
        # "deliver the feature, land it when green"). Callers pass
        # ``--no-auto-merge`` for one-off recovery runs.
        assert ns.auto_merge is True
        assert ns.companions is None

    @pytest.mark.unit
    def test_agent_override(self) -> None:
        ns = cli.parse_args(["--repo", _REPO_URL, "--pr", "277", "--agent", "claude_code"])
        assert ns.agent == "claude_code"

    @pytest.mark.unit
    def test_no_auto_merge_flag_disables_it(self) -> None:
        ns = cli.parse_args(["--repo", _REPO_URL, "--pr", "277", "--no-auto-merge"])
        assert ns.auto_merge is False

    @pytest.mark.unit
    def test_companions_path(self, tmp_path: Path) -> None:
        companions_file = tmp_path / "companions.json"
        companions_file.write_text("[]")
        ns = cli.parse_args(
            [
                "--repo",
                _REPO_URL,
                "--pr",
                "277",
                "--companions",
                str(companions_file),
            ]
        )
        assert ns.companions == companions_file


class TestOrchestrateAttachHappyPath:
    @pytest.mark.unit
    async def test_writes_spec_and_spawns_run_awf(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn = _SpawnCapture()

        exit_code = await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=277,
            agent="claude_code",
            auto_merge=False,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        assert exit_code == 0

        # Spec file written to the deterministic path.
        spec_dir = tmp_path / "feature-pr-specs"
        spec_path = spec_dir / "dimileeh__aira-web-feature-pr277.json"
        assert spec_path.exists()
        spec_contents = json.loads(spec_path.read_text())
        # run_awf.py consumes a LIST of task specs.
        assert isinstance(spec_contents, list)
        assert len(spec_contents) == 1
        task = spec_contents[0]
        assert task["task_kind"] == "sync_feature_pr"
        assert task["pr_number"] == 277
        assert task["source_branch"] == "fix/sprints-ai-plan-button-guard"
        assert task["branch_base"] == "development"
        assert task["auto_merge"] is False
        assert task["agent"] == "claude_code"

        # run_awf.py was spawned with --config pointing at our spec and
        # --work-dir matching what we asked for, plus --keep-state (the
        # AWF DB must persist across the script invocation).
        assert len(spawn.calls) == 1
        call = spawn.calls[0]
        assert any("run_awf.py" in a for a in call)
        assert "--config" in call
        assert str(spec_path) in call
        assert "--work-dir" in call
        assert str(tmp_path) in call
        assert "--keep-state" in call

    @pytest.mark.unit
    async def test_auto_merge_propagates_to_spec(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn = _SpawnCapture()

        await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=277,
            agent="codex",
            auto_merge=True,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        spec_path = tmp_path / "feature-pr-specs" / "dimileeh__aira-web-feature-pr277.json"
        task = json.loads(spec_path.read_text())[0]
        assert task["auto_merge"] is True

    @pytest.mark.unit
    async def test_companions_file_loaded_into_spec(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn = _SpawnCapture()

        companions_path = tmp_path / "companions.json"
        companions_path.write_text(
            json.dumps(
                [
                    {
                        "name": "backend",
                        "build_context": "/host/path",
                        "dockerfile": "Dockerfile",
                    }
                ]
            )
        )

        await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=277,
            agent="codex",
            auto_merge=False,
            companions_path=companions_path,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        spec_path = tmp_path / "feature-pr-specs" / "dimileeh__aira-web-feature-pr277.json"
        task = json.loads(spec_path.read_text())[0]
        assert len(task["companions"]) == 1
        assert task["companions"][0]["name"] == "backend"


class TestIdempotency:
    @pytest.mark.unit
    async def test_existing_monitor_skips_spawn(self, tmp_path: Path) -> None:
        """If a monitor is already running for this repo+PR, don't spawn
        a second one. This is the core idempotency property that lets a
        cron / watcher invoke the CLI repeatedly without duplicating
        work."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn = _SpawnCapture()

        existing_proc = (
            "12345 python /path/run_awf.py --config "
            f"{tmp_path}/feature-pr-specs/dimileeh__aira-web-feature-pr277.json "
            "--work-dir /tmp\n"
        )

        exit_code = await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=277,
            agent="claude_code",
            auto_merge=False,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: existing_proc,
        )

        # Exit 0 (no-op success — not a failure).
        assert exit_code == 0
        # No fresh spawn — idempotency held.
        assert spawn.calls == []


class TestErrorPaths:
    @pytest.mark.unit
    async def test_closed_pr_exits_nonzero_without_spawn(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 277,
                    "headRefName": "fix/foo",
                    "baseRefName": "development",
                    "state": "CLOSED",
                    "isDraft": False,
                    "closed": True,
                    "merged": False,
                    "author": {"login": "dimileeh"},
                    "url": "https://github.com/dimileeh/aira-web/pull/277",
                    "title": "fix: foo",
                }
            ),
        )
        spawn = _SpawnCapture()

        exit_code = await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=277,
            agent="claude_code",
            auto_merge=False,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        assert exit_code == 1
        assert spawn.calls == []
        # No spec file should have been left on disk — failure is clean.
        assert not (tmp_path / "feature-pr-specs").exists()

    @pytest.mark.unit
    async def test_nonexistent_pr_exits_nonzero(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=1,
            stdout="",
            stderr="GraphQL: Could not resolve to a PullRequest",
        )
        spawn = _SpawnCapture()

        exit_code = await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=99999,
            agent="claude_code",
            auto_merge=False,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        assert exit_code == 1
        assert spawn.calls == []

    @pytest.mark.unit
    async def test_invalid_repo_url_exits_nonzero(self, tmp_path: Path) -> None:
        runner = FakeCommandRunner()
        spawn = _SpawnCapture()

        exit_code = await cli.orchestrate_attach(
            repo_url="https://not-github.example.com/foo/bar",
            pr_number=277,
            agent="claude_code",
            auto_merge=False,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        assert exit_code == 1
        # No ``gh`` call — we failed at URL parsing before hitting the network.
        assert len(runner.calls) == 0
        assert spawn.calls == []


class TestMetadataIntoSpec:
    """Verifies FeaturePRMetadata flows through the CLI into the spec
    unchanged — covers the metadata-to-spec glue at the CLI seam
    without re-testing the pure builder (which has its own tests)."""

    @pytest.mark.unit
    async def test_custom_base_branch(self, tmp_path: Path) -> None:
        """If a PR targets ``main`` (hotfix) instead of ``development``,
        the spec's ``branch_base`` follows the PR, not a default."""
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 500,
                    "headRefName": "hotfix/prod-crash",
                    "baseRefName": "main",
                    "state": "OPEN",
                    "isDraft": False,
                    "closed": False,
                    "merged": False,
                    "author": {"login": "dimileeh"},
                    "url": "https://github.com/dimileeh/aira-web/pull/500",
                    "title": "hotfix: prod crash",
                }
            ),
        )
        spawn = _SpawnCapture()

        await cli.orchestrate_attach(
            repo_url=_REPO_URL,
            pr_number=500,
            agent="claude_code",
            auto_merge=False,
            companions_path=None,
            work_dir=tmp_path,
            runner=runner,
            spawn=spawn,
            process_lister=lambda: "",
        )

        spec_path = tmp_path / "feature-pr-specs" / "dimileeh__aira-web-feature-pr500.json"
        task = json.loads(spec_path.read_text())[0]
        assert task["branch_base"] == "main"
        assert task["source_branch"] == "hotfix/prod-crash"

    @pytest.mark.unit
    def test_metadata_record_as_fixture(self) -> None:
        """Sanity check that _metadata helper constructs the dataclass
        correctly — a regression guard for tests downstream that
        assemble metadata inline."""
        m = FeaturePRMetadata(
            number=1,
            head_branch="h",
            base_branch="b",
            state="OPEN",
            is_draft=False,
            closed=False,
            merged=False,
            author="a",
            url="u",
            title="t",
        )
        assert m.number == 1
