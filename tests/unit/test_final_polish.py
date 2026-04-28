"""Final polish — the last handful of uncovered lines across AWF.

Each test targets one specific uncovered line or branch. Keeping them
in a single file since they're independent micro-coverage additions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError
from awf.db.base import Base
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from scripts import run_awf

# ── github_client error paths ──────────────────────────────────────────────


class TestGhJsonErrorPaths:
    @pytest.mark.unit
    async def test_gh_json_raises_on_nonzero_exit(self) -> None:
        """Lines 409-413: when gh exits non-zero, _gh_json raises
        GitHubClientError instead of returning garbage."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="rate limit exceeded")
        client = GitHubClient(runner)
        with pytest.raises(GitHubClientError) as exc:
            await client._gh_json(["gh", "whatever"], operation="op")
        assert "rate limit" in exc.value.stderr

    @pytest.mark.unit
    async def test_gh_json_returns_none_for_empty_stdout(self) -> None:
        """Line 415: an empty stdout is 'no data', not a JSON parse error.
        Happens on gh subcommands that produce nothing when the query
        matches zero records."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout="  \n")
        client = GitHubClient(runner)
        result = await client._gh_json(["gh", "whatever"], operation="op")
        assert result is None

    @pytest.mark.unit
    async def test_run_gh_strict_raises_on_failure(self) -> None:
        """Lines 420-425: ``_run_gh(..., strict=True)`` must raise on
        non-zero exit; non-strict just returns the result."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="boom")
        client = GitHubClient(runner)
        with pytest.raises(GitHubClientError):
            await client._run_gh(["gh", "x"], operation="op", strict=True)


class TestDigHelper:
    @pytest.mark.unit
    def test_dig_returns_none_on_non_dict(self) -> None:
        """Line 444: _dig encountering a non-dict where a dict is needed
        returns None instead of raising."""
        from awf.common.github_client import _dig

        assert _dig({"a": "not a dict"}, "a", "b") is None
        assert _dig(None, "a") is None
        assert _dig([1, 2, 3], 1) == 2
        assert _dig([1, 2, 3], 10) is None


# ── executor fix-pass warnings ─────────────────────────────────────────────


class TestExecutorFixPassWarnings:
    """Lines 425 + 452: the fix-cycle logs a warning when ``git add -A``
    or ``git commit`` in a fix pass fails. Both are non-fatal — they
    let the next retry's validation confirm or refute that the CLI did
    useful work — but we still want the operator to see the warning."""

    @pytest.mark.unit
    async def test_fix_pass_add_and_commit_failures_log_and_continue(self, tmp_path: Path) -> None:
        from awf.adapters import base as _adapter_base
        from awf.adapters import registry as _registry  # noqa: F401
        from awf.adapters.codex import CodexAdapter
        from awf.common.commands import FakeCommandRunner
        from awf.control.executor import ExecutorConfig, WorkspaceExecutor
        from awf.db.enums import AgentRuntime
        from awf.node.compose_manager import ComposeManager
        from awf.runtime.pr_creator import PullRequestResult
        from awf.runtime.validation import ValidationCommandResult, ValidationResult

        template = (
            Path(__file__).resolve().parents[2] / "docker" / "compose" / "workspace.base.yml.j2"
        )
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'fp.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = make_session_factory(engine)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url="r",
                branch_base="development",
                task_title="fp",
                task_prompt="p",
                agent="codex",
                test_commands=["pytest -q"],
                requires_database=False,
            )
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="T")
            ws.branch_name = "awf/x"
            ws.remote_push_branch = "awf/x"
            ws.base_commit = "a" * 40
            ws.compose_project_name = "awf_x"
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="T")
            await s.commit()
            ws_id = ws.id
        (tmp_path / "w" / "wt" / ws_id).mkdir(parents=True, exist_ok=True)

        fake = FakeCommandRunner()
        # adapter.run is via subprocess.
        fake.queue_result(returncode=0)  # adapter
        # Initial commit block: branch check, add, cached diff (non-empty), commit, rev-list.
        fake.queue_result(returncode=0, stdout="awf/x\n")  # rev-parse --abbrev-ref HEAD
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a\n")  # diff --cached
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD
        # Fix pass 1: adapter → fix_add FAILS (warning) → cached diff →
        # commit FAILS (warning) → validation GREEN.
        fake.queue_result(returncode=0)  # adapter (fix pass)
        fake.queue_result(returncode=1, stderr="index.lock held")  # fix_add FAILS
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff (hack: still has change)
        fake.queue_result(returncode=1, stderr="commit would be empty")  # fix_commit FAILS
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # pre-validation rev-parse HEAD
        class _FixPassValidation:
            def __init__(self, artifacts_dir: Path) -> None:
                self.artifacts_dir = artifacts_dir
                self.validation_calls = 0

            async def run_profile_coverage(self, **_kwargs: Any) -> None:
                return None

            async def run_profile_phases(
                self,
                *,
                workspace_id: str,
                phase_names: tuple[str, ...],
                **_kwargs: Any,
            ) -> ValidationResult:
                if phase_names == ("setup", "pre_agent"):
                    return ValidationResult()
                assert phase_names == ("post_agent", "validate")
                self.validation_calls += 1
                artifacts = self.artifacts_dir / workspace_id
                artifacts.mkdir(parents=True, exist_ok=True)
                stdout = artifacts / f"validate_{self.validation_calls}.stdout"
                stderr = artifacts / f"validate_{self.validation_calls}.stderr"
                if self.validation_calls == 1:
                    stdout.write_text("", encoding="utf-8")
                    stderr.write_text("pytest: 1 failed", encoding="utf-8")
                    return ValidationResult(
                        commands=[
                            ValidationCommandResult(
                                command="pytest -q",
                                returncode=1,
                                duration_seconds=0.1,
                                stdout_path=stdout,
                                stderr_path=stderr,
                                phase="validate",
                                reason_code="COMMAND_FAILED",
                            )
                        ]
                    )
                stdout.write_text("passed", encoding="utf-8")
                stderr.write_text("", encoding="utf-8")
                return ValidationResult(
                    commands=[
                        ValidationCommandResult(
                            command="pytest -q",
                            returncode=0,
                            duration_seconds=0.1,
                            stdout_path=stdout,
                            stderr_path=stderr,
                            phase="validate",
                        )
                    ]
                )

        class _SuccessfulPrCreator:
            async def push_and_open(
                self,
                *,
                branch_name: str,
                **_kwargs: Any,
            ) -> PullRequestResult:
                return PullRequestResult(
                    url="https://github.com/x/y/pull/1",
                    branch=branch_name,
                    head_sha="c" * 40,
                )

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "w", template_path=template),
            validation=_FixPassValidation(tmp_path / "a"),  # type: ignore[arg-type]
            pr_creator=_SuccessfulPrCreator(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "w" / "wt",
                compose_projects_root=tmp_path / "w" / "c",
                default_models={
                    AgentRuntime.codex: "gpt-5",
                },
                max_validation_fix_passes=3,
            ),
        )
        original_adapter_registry = dict(_adapter_base._REGISTRY)
        _adapter_base._REGISTRY[AgentRuntime.codex] = CodexAdapter
        try:
            with structlog.testing.capture_logs() as captured:
                await executor.execute(ws_id)
        finally:
            _adapter_base._REGISTRY.clear()
            _adapter_base._REGISTRY.update(original_adapter_registry)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # Landed at completed despite fix-pass sub-failures.
            assert ws.status == WorkspaceStatus.completed.value, {
                "failure_reason": ws.failure_reason,
                "failure_message": ws.failure_message,
                "events": [(event.event_type, event.reason_code) for event in ws.events],
                "calls": [call.args for call in fake.calls],
            }
        assert any(
            event.get("event") == "executor.fix_pass_add_failed"
            and event.get("stderr") == "index.lock held"
            for event in captured
        )
        assert any(
            event.get("event") == "executor.fix_pass_commit_failed"
            and event.get("stderr") == "commit would be empty"
            for event in captured
        )
        await engine.dispose()


# ── executor mark_failed: status already diverged ──────────────────────────


class TestExecutorMarkFailedStatusDiverged:
    @pytest.mark.unit
    async def test_mark_failed_respects_diverged_status(self, tmp_path: Path) -> None:
        """Line 582: ``_mark_failed`` is called with a ``from_status``
        that no longer matches the workspace's actual state. The
        function must silently return rather than forcing the flag
        (the workspace may have been cancelled etc. in the meantime)."""
        from awf.common.commands import FakeCommandRunner
        from awf.control.executor import ExecutorConfig, WorkspaceExecutor
        from awf.db.enums import AgentRuntime
        from awf.node.compose_manager import ComposeManager
        from awf.runtime.pr_creator import PullRequestCreator
        from awf.runtime.validation import ValidationRunner

        template = (
            Path(__file__).resolve().parents[2] / "docker" / "compose" / "workspace.base.yml.j2"
        )
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'mf.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = make_session_factory(engine)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url="r",
                branch_base="b",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                requires_database=False,
            )
            await s.commit()
            ws_id = ws.id
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=FakeCommandRunner(),
            compose=ComposeManager(work_dir=tmp_path / "w", template_path=template),
            validation=ValidationRunner(runner=FakeCommandRunner(), artifacts_dir=tmp_path / "a"),
            pr_creator=PullRequestCreator(FakeCommandRunner()),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "w",
                compose_projects_root=tmp_path / "c",
                default_models={AgentRuntime.codex: "gpt-5"},
            ),
        )
        # ws is still 'requested'. Ask _mark_failed to act if from_status
        # was 'running' → should no-op + keep status 'requested'.
        await executor._mark_failed(
            workspace_id=ws_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message="would-be fail",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == "requested"
            assert ws.failure_reason is None
        await engine.dispose()


# ── git_manager: empty ref skip ────────────────────────────────────────────


class TestGitManagerEmptyRefSkip:
    @pytest.mark.unit
    async def test_empty_ref_lines_skipped_in_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 178: ``for-each-ref`` output can have blank lines from
        split; GitManager must ``continue`` past them rather than
        passing empty strings to ``update-ref -d`` (which would crash).

        Drives the REAL ``GitManager.ensure_mirror`` through its first-
        clone path by patching ``_run`` with a recorder that returns
        canned stdout. The assertion is "no ``update-ref -d`` call
        received an empty-string ref" — which proves the ``continue``
        branch executed against mixed output."""
        from awf.node.git_manager import GitManager, GitResult

        recorded: list[tuple[list[str], str]] = []

        async def _fake_run(self, args: list[str], *, operation: str) -> GitResult:
            recorded.append((list(args), operation))
            stdout = ""
            if operation == "mirror.list_local_heads":
                # Mixed output: two real refs with a blank line between them.
                stdout = "refs/heads/main\n\nrefs/heads/awf/ws_old\n"
            return GitResult(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(GitManager, "_run", _fake_run)
        gm = GitManager(tmp_path / "git")
        await gm.ensure_mirror("git@github.com:dimileeh/aira-web.git")

        # Exactly two ``update-ref -d`` calls — one per non-blank ref.
        # The blank line between them did NOT trigger a third call with
        # an empty arg (which would have crashed the real git).
        update_calls = [
            (args, op) for args, op in recorded if op == "mirror.delete_stale_local_head"
        ]
        assert len(update_calls) == 2, (
            f"expected 2 update-ref calls, got {len(update_calls)} — did the blank-line skip break?"
        )
        for args, _ in update_calls:
            assert "" not in args, (
                f"update-ref received an empty-string ref — the blank-line"
                f" skip at git_manager.py:178 regressed. Args: {args}"
            )


# ── run_awf _main's "status != completed without failure_reason" ──────────


class TestRunAwfIncompleteNoFailureReason:
    @pytest.mark.unit
    async def test_non_completed_status_without_reason_flips_exit_to_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Line 1072: a task result whose status isn't 'completed' but
        also has no failure_reason is odd (state machine never reached
        completed nor was it explicitly marked failed). Still exits 1
        so the operator notices."""
        config = tmp_path / "tasks.json"
        config.write_text(
            '[{"repo_url": "git@github.com:x/y.git", "branch_base": "development", '
            '"task_title": "weird", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}]'
        )

        async def _fake_run_task_with_guard(cfg, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "workspace_id": "ws_weird",
                "title": "weird",
                # Odd state: not completed, but no failure_reason either.
                "status": "cancelled",
                "pr_url": None,
                "failure_reason": None,
                "failure_message": None,
                "branch": "awf/weird",
                "base_commit": "a" * 40,
            }

        monkeypatch.setattr(run_awf, "_run_task_with_failure_guard", _fake_run_task_with_guard)
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        rc = await run_awf._main(
            config_path=config,
            work_dir=tmp_path / "work",
            keep_state=True,
        )
        assert rc == 1


# validation display tests live in tests/unit/test_polish_small_gaps.py,
# where they drive ``ValidationRunner._exec`` directly with a fake
# runner instead of reimplementing the formatting logic.


# ── attach_feature_pr_monitor._main ────────────────────────────────────────


class TestAttachFeaturePrMain:
    @pytest.mark.unit
    async def test_main_invokes_orchestrate_attach(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lines 218-219: ``_main`` is the argparse-bound entry. It
        constructs a runner and delegates to orchestrate_attach. We
        monkeypatch the delegatee and verify the delegation happens."""
        import argparse

        from scripts import attach_feature_pr_monitor as mod

        captured: dict[str, Any] = {}

        async def _fake_orchestrate(**kwargs: Any) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(mod, "orchestrate_attach", _fake_orchestrate)

        ns = argparse.Namespace(
            repo="git@github.com:dimileeh/aira-web.git",
            pr=277,
            agent="codex",
            auto_merge=False,
            companions=None,
            work_dir=tmp_path,
        )
        rc = await mod._main(ns)
        assert rc == 0
        assert captured["repo_url"] == "git@github.com:dimileeh/aira-web.git"
        assert captured["pr_number"] == 277
        assert captured["agent"] == "codex"
