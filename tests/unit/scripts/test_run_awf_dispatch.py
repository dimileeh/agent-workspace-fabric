"""Tests for ``run_awf._run_task`` dispatch by ``task_kind``.

The full handlers (``_run_sync_release_pr``, ``_run_sync_feature_pr``)
do heavy compose + DB work that's exercised only by real workspace
runs — see the other integration tests. What this file pins is the
purely-logical *dispatch* layer: given a TaskConfig with
``task_kind=X``, the right handler runs.

The cheap way to verify this without mocking every compose/DB seam:
monkeypatch each handler to a simple async callable that records its
arguments, then assert exactly one of them fired."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from scripts import run_awf


def _cfg(task_kind: str = "feature_branch_pr", **overrides: Any) -> run_awf.TaskConfig:
    base = run_awf.TaskConfig(
        repo_url="git@github.com:dimileeh/aira-web.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent="codex",
        test_commands=[],
        task_kind=task_kind,
    )
    return replace(base, **overrides) if overrides else base


class _RecordingHandler:
    """Async callable that captures its ``cfg`` argument so tests can
    assert which dispatch arm ran without touching the real handler."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[run_awf.TaskConfig] = []

    async def __call__(self, cfg, *, work_dir, session_factory, auth_mounts, git_name, git_email):  # type: ignore[no-untyped-def]
        self.calls.append(cfg)
        return {"handler": self.label, "task_kind": cfg.task_kind}


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch):
    """Swap both sync handlers with recorders AND short-circuit the
    feature_branch_pr branch so it doesn't try to open a compose stack.

    The feature_branch_pr branch has no single function seam to swap —
    it's inlined in ``_run_task`` — so we detect which branch ran by
    raising a sentinel exception when neither sync handler fired."""
    release = _RecordingHandler("sync_release_pr")
    feature = _RecordingHandler("sync_feature_pr")
    monkeypatch.setattr(run_awf, "_run_sync_release_pr", release)
    monkeypatch.setattr(run_awf, "_run_sync_feature_pr", feature)

    class _DidNotDispatchToSyncError(Exception):
        """Raised to mark "the inline feature_branch_pr branch was taken"."""

    # Poison the first object the feature_branch_pr branch touches so
    # the test fails deterministically if the default path runs.
    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _DidNotDispatchToSyncError("fell through to feature_branch_pr branch")

    monkeypatch.setattr(run_awf, "AsyncioSubprocessRunner", _boom)
    return release, feature, _DidNotDispatchToSyncError


class TestDispatch:
    @pytest.mark.unit
    async def test_sync_release_pr_routes_to_release_handler(self, handlers) -> None:
        release, feature, _ = handlers
        cfg = _cfg(task_kind="sync_release_pr", pr_number=272, source_branch="development")

        result = await run_awf._run_task(
            cfg,
            work_dir=None,  # type: ignore[arg-type]
            session_factory=None,  # type: ignore[arg-type]
            auth_mounts=[],
            git_name="x",
            git_email="x@x",
        )

        assert release.calls == [cfg]
        assert feature.calls == []
        assert result["handler"] == "sync_release_pr"

    @pytest.mark.unit
    async def test_sync_feature_pr_routes_to_feature_handler(self, handlers) -> None:
        release, feature, _ = handlers
        cfg = _cfg(
            task_kind="sync_feature_pr",
            pr_number=277,
            source_branch="fix/sprints-ai-plan-button-guard",
            auto_merge=False,
        )

        result = await run_awf._run_task(
            cfg,
            work_dir=None,  # type: ignore[arg-type]
            session_factory=None,  # type: ignore[arg-type]
            auth_mounts=[],
            git_name="x",
            git_email="x@x",
        )

        assert feature.calls == [cfg]
        assert release.calls == []
        assert result["handler"] == "sync_feature_pr"

    @pytest.mark.unit
    async def test_feature_branch_pr_does_not_hit_sync_handlers(self, handlers) -> None:
        """The default ``feature_branch_pr`` kind must NOT be picked up
        by either sync dispatch arm."""
        release, feature, sentinel = handlers
        cfg = _cfg(task_kind="feature_branch_pr")

        with pytest.raises(sentinel):
            await run_awf._run_task(
                cfg,
                work_dir=None,  # type: ignore[arg-type]
                session_factory=None,  # type: ignore[arg-type]
                auth_mounts=[],
                git_name="x",
                git_email="x@x",
            )
        # Neither sync handler was invoked before the default-branch poison fired.
        assert release.calls == []
        assert feature.calls == []


class TestTaskConfigDefaults:
    """Guardrails on the ``auto_merge`` field default — the CLI expects
    ``False`` unless overridden."""

    @pytest.mark.unit
    def test_auto_merge_defaults_to_false(self) -> None:
        cfg = run_awf.TaskConfig(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        assert cfg.auto_merge is False

    @pytest.mark.unit
    def test_auto_merge_can_be_set_true(self) -> None:
        cfg = run_awf.TaskConfig(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="t",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            auto_merge=True,
        )
        assert cfg.auto_merge is True
