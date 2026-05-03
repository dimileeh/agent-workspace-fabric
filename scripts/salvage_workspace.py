"""Salvage uncommitted work left behind by an agent that was killed.

Use when an agent CLI (typically ``claude_code`` on a long session) exited
non-zero but the worktree contains completed work that AWF's old executor
code threw away (commit #29e19fc fixes the executor going forward; this
script rescues the single pre-fix casualty).

Pipeline: swap the live adapter for a no-op so the executor skips the
(already-happened) agent run, then run the normal post-agent path —
auto-commit → validate → push → open PR. Every gate (validation, merge
ancestry check, orphan-recovery) still fires. The PR is only opened if
validation genuinely passes.

Usage:
    ./scripts/salvage_workspace.py --work-dir /tmp/awf-realrun --workspace-id ws_<id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make ``src/`` importable.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

# Import the concrete adapters so the registry is populated even though we
# then override entries for no-op salvage.
import awf.adapters.claude_code  # noqa: E402, F401
import awf.adapters.codex  # noqa: E402, F401
import awf.adapters.gemini  # noqa: E402, F401
import awf.adapters.opencode  # noqa: E402, F401
from awf.adapters import base as _adapter_base  # noqa: E402
from awf.adapters.base import AgentAdapter, AgentRunResult  # noqa: E402
from awf.common.commands import AsyncioSubprocessRunner  # noqa: E402
from awf.control.executor import ExecutorConfig, WorkspaceExecutor  # noqa: E402
from awf.db.enums import AgentRuntime, WorkspaceStatus  # noqa: E402
from awf.db.repositories import WorkspaceRepository  # noqa: E402
from awf.db.session import make_session_factory  # noqa: E402
from awf.node.compose_manager import ComposeManager  # noqa: E402
from awf.runtime.pr_creator import PullRequestCreator  # noqa: E402
from awf.runtime.validation import ValidationRunner  # noqa: E402

_TEMPLATE = _ROOT / "docker" / "compose" / "workspace.base.yml.j2"


class _NoOpAdapter(AgentAdapter):
    """Pretends the agent ran and exited cleanly; does nothing."""

    def __init__(self, *, runtime: AgentRuntime, **_: object) -> None:
        self._runtime = runtime

    def get_provider(self, model: str | None) -> str:
        return "fake"

    @property
    def name(self) -> AgentRuntime:  # type: ignore[override]
        return self._runtime

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:  # type: ignore[override]
        return []

    async def run(  # type: ignore[override]
        self, *, compose_project: str, compose_file: Path, prompt: str, model: str | None = None
    ) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="[salvage] skipping agent run — worktree already holds completed work",
            stderr="",
        )


def _make_noop_factory(runtime_value: AgentRuntime) -> type[AgentAdapter]:
    """Build a no-op adapter class bound to *this* runtime value.

    Extracted from the registry-install loop specifically to sidestep
    the classic Python closure-capture pitfall: classes defined inside
    a ``for runtime in ...`` loop share the enclosing ``runtime``
    variable by REFERENCE, so every class ends up pointing at whatever
    runtime the loop variable last held — all three registered classes
    would report the same ``name``. Review feedback on PR #2
    (CodeRabbit): "closure capture in for-loop".

    Returning a class from a function closes ``runtime_value`` by
    value because it's a fresh local in this frame, one per call.
    """

    class _Factory(AgentAdapter):  # type: ignore[misc]
        runtime = runtime_value  # type: ignore[assignment]

        def __init__(
            self,
            *,
            runner: object = None,
            default_model: str | None = None,
            default_effort: str | None = None,
        ) -> None:
            # runner / defaults accepted for signature compat, unused.
            self._runtime = runtime_value

        def get_provider(self, model: str | None) -> str:
            return "fake"

        @property
        def name(self) -> AgentRuntime:
            return self._runtime

        def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
            return []

        async def run(
            self,
            *,
            compose_project: str,
            compose_file: Path,
            prompt: str,
            model: str | None = None,
        ) -> AgentRunResult:
            return AgentRunResult(
                returncode=0,
                stdout="[salvage] skipping agent run — worktree already holds completed work",
                stderr="",
            )

    return _Factory


def _install_noop_adapter_factory() -> None:
    """Replace every entry in the adapter registry with a no-op factory.

    The executor calls ``get_adapter(runtime, runner, default_model)`` once
    per run; we replace the registered classes so it returns our no-op
    regardless of which runtime was configured for the workspace.
    """
    for runtime in list(_adapter_base._REGISTRY.keys()):
        _adapter_base._REGISTRY[runtime] = _make_noop_factory(runtime)


async def _main(work_dir: Path, workspace_id: str) -> int:
    db_path = work_dir / "awf.db"
    if not db_path.exists():
        print(f"No AWF DB at {db_path}", file=sys.stderr)
        return 2

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = make_session_factory(engine)

    try:
        # Reset workspace back to ``ready`` so the executor will accept it.
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                print(f"No workspace {workspace_id} in DB", file=sys.stderr)
                return 2
            print(
                f"Salvage target: {workspace_id}\n"
                f"  status now: {ws.status}\n"
                f"  agent:      {ws.agent}\n"
                f"  branch:     {ws.branch_name}\n"
                f"  base:       {ws.base_commit[:10] if ws.base_commit else '?'}\n"
                f"  repo:       {ws.repo_url}",
                flush=True,
            )
            if ws.status != WorkspaceStatus.ready.value:
                # The state machine only permits ``failed -> destroying``, so a
                # normal ``transition()`` call would refuse. For salvage we
                # deliberately bypass the invariant: the containers + worktree
                # the ``failed`` state was guarding are EXACTLY what we want to
                # re-enter with. Set the field directly and record the reason
                # in the transition log would require a new state; for the
                # one-off script we just rewrite the column + clear failure
                # info so the executor sees a clean ``ready`` workspace.
                ws.status = WorkspaceStatus.ready.value
                ws.failure_reason = None
                ws.failure_message = None
                await s.commit()

        _install_noop_adapter_factory()

        runner = AsyncioSubprocessRunner()
        compose = ComposeManager(work_dir=work_dir / "compose", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=runner, artifacts_dir=work_dir / "artifacts")
        pr_creator = PullRequestCreator(runner)

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=runner,
            compose=compose,
            validation=validation,
            pr_creator=pr_creator,
            config=ExecutorConfig(
                worktrees_root=work_dir / "git" / "worktrees",
                compose_projects_root=work_dir / "compose" / "compose",
                default_models={},
            ),
        )

        print("[salvage] running executor (agent step is a no-op) ...", flush=True)
        await executor.execute(workspace_id)

        async with factory() as s:
            final = await WorkspaceRepository(s).get(workspace_id)
            assert final is not None
            print(
                f"[salvage] done.\n"
                f"  status:  {final.status}\n"
                f"  pr_url:  {final.pr_url}\n"
                f"  reason:  {final.failure_reason}\n"
                f"  message: {final.failure_message}",
                flush=True,
            )
        return 0 if final and final.status == WorkspaceStatus.completed.value else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--workspace-id", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.work_dir, args.workspace_id)))
