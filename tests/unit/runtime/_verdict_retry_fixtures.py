"""Shared fixtures for bounded verdict retry regression tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_residue
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command


class _VerdictRunner(SimpleNamespace):
    def __init__(
        self,
        *,
        worktrees_root: Path,
        outputs: list[str | AgentRunError],
        heads_after_attempt: list[str],
        dirty_after_attempt: list[bool] | None = None,
        stranded_dirty_after_attempt: list[bool] | None = None,
        stranded_status_raises: bool = False,
        path_touched: bool = True,
        line_touched: bool = True,
        in_item_scope: bool = True,
        provider_error_action: BaseException | None = None,
        provider_recovery_suppress_attempts: frozenset[int] | None = None,
        reset_fails: bool = False,
        rev_parse_sequence: list[str | None] | None = None,
    ) -> None:
        super().__init__()
        self._worktrees_root = worktrees_root
        self.outputs = outputs
        self.heads_after_attempt = heads_after_attempt
        self.dirty_after_attempt = dirty_after_attempt or [False] * len(outputs)
        self.stranded_dirty_after_attempt = stranded_dirty_after_attempt or [False] * len(outputs)
        self.stranded_status_raises = stranded_status_raises
        self.path_touched = path_touched
        self.line_touched = line_touched
        self.in_item_scope = in_item_scope
        self.provider_error_action = provider_error_action
        self.provider_recovery_suppress_attempts = provider_recovery_suppress_attempts
        self.reset_fails = reset_fails
        self.rev_parse_sequence = rev_parse_sequence
        self.rev_parse_index = 0
        self._workspace_runtime_context = ""
        self.prompts: list[str] = []
        self.attempt = 0
        self.current_head = heads_after_attempt[0]
        self.reset_targets: list[str] = []
        self.provider_recovery_check_count = 0
        # Persistent porcelain residue after a False commit sink so correction-
        # start attribution and post-attempt mutation probes see the same dirt
        # until a successful sink or hard reset clears it.
        self._persistent_stranded_status_stdout: str = ""
        self._pending_stranded_status_raise = False
        self._deps = SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=self._run_git),
        )

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        del kwargs
        if "reset" in cmd and "--hard" in cmd:
            self.reset_targets.append(cmd[-1])
            if self.reset_fails:
                return CommandResult(returncode=1, stdout="", stderr="reset failed")
            self.current_head = cmd[-1]
            self._persistent_stranded_status_stdout = ""
            return CommandResult(returncode=0, stdout="", stderr="")
        if "rev-parse" in cmd:
            ref = cmd[-1]
            if ref.upper() == "HEAD":
                return CommandResult(returncode=0, stdout=f"{self.current_head}\n", stderr="")
            return CommandResult(returncode=0, stdout=f"{ref}\n", stderr="")
        if "status" in cmd and "--porcelain" in cmd:
            if self._pending_stranded_status_raise:
                self._pending_stranded_status_raise = False
                raise OSError("git status spawn failed")
            return CommandResult(
                returncode=0,
                stdout=self._persistent_stranded_status_stdout,
                stderr="",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
        attempt = self.provider_recovery_check_count
        self.provider_recovery_check_count += 1
        return (
            self.provider_recovery_suppress_attempts is not None
            and attempt in self.provider_recovery_suppress_attempts
        )

    async def _run_monitor_agent_with_service_recovery(self, **kwargs: object) -> AgentRunResult:
        self.prompts.append(str(kwargs["prompt"]))
        attempt_index = self.attempt
        output = self.outputs[attempt_index]
        self.attempt += 1
        if isinstance(output, AgentRunError):
            state = kwargs.get("state")
            synced_head = self.heads_after_attempt[attempt_index]
            if (
                isinstance(state, MonitorState)
                and synced_head.lower() != str(kwargs.get("operation_start_head", "")).lower()
            ):
                state.last_push_sha = synced_head
                state.hosted_terminal_head_advanced = True
                self.current_head = synced_head
            raise output
        state = kwargs.get("state")
        synced_head = self.heads_after_attempt[attempt_index]
        operation_start_head = str(kwargs.get("operation_start_head", ""))
        if (
            self._deps.adapter.is_hosted
            and isinstance(state, MonitorState)
            and synced_head.lower() != operation_start_head.lower()
        ):
            state.last_push_sha = synced_head
            state.hosted_terminal_head_advanced = True
            self.current_head = synced_head
        # Model agent-authored dirt the subsequent commit sink will consume so
        # pre-sink residue attribution matches production (PRRT_kwDOSJAM6s6eKNQT).
        if self.dirty_after_attempt[attempt_index] and not self._persistent_stranded_status_stdout:
            self._persistent_stranded_status_stdout = " M agent_edit.py\n"
        elif (
            not self.dirty_after_attempt[attempt_index]
            and synced_head.lower() != self.current_head.lower()
        ):
            # Model an in-agent self-commit (HEAD advances before the dirty sink).
            self.current_head = synced_head
        return AgentRunResult(returncode=0, stdout=output, stderr="")

    async def _commit_dirty_worktree(self, **_kwargs: object) -> bool:
        index = self.attempt - 1
        committed = self.dirty_after_attempt[index]
        if committed:
            # Successful sink clears stranded attempt-0 residue and may advance HEAD.
            self._persistent_stranded_status_stdout = ""
            self.current_head = self.heads_after_attempt[index]
            return True
        if self.stranded_dirty_after_attempt[index]:
            # Model status/add/commit sink failure that leaves PR-worthy dirt.
            if self.stranded_status_raises:
                self._pending_stranded_status_raise = True
            else:
                self._persistent_stranded_status_stdout = " M stranded_fix.py\n"
        return False

    async def _rev_parse_head(self, _worktree_path: Path) -> str | None:
        if self.rev_parse_sequence is not None:
            if self.rev_parse_index >= len(self.rev_parse_sequence):
                return self.current_head
            value = self.rev_parse_sequence[self.rev_parse_index]
            self.rev_parse_index += 1
            if value is not None:
                self.current_head = value
            return value
        return self.current_head

    async def _head_descends_from(
        self,
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        return ancestor != descendant

    async def _commit_trees_differ(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
    ) -> bool:
        del worktree_path
        return left != right

    async def _commit_range_touches_path(self, **kwargs: object) -> bool:
        if not self.path_touched:
            return False
        line = kwargs.get("line")
        if line is not None:
            return self.line_touched
        return True

    async def _commit_range_in_item_scope(self, **_kwargs: object) -> bool:
        return self.in_item_scope

    async def _resolve_task_tag(self, _workspace_id: str) -> str | None:
        return None

    async def _hosted_pr_identity_for_workspace(
        self,
        _workspace_id: str,
        *,
        state: MonitorState | None = None,
    ) -> dict[str, object]:
        del state
        return {
            "head_repo_url": "https://example.invalid/awf.git",
            "head_ref": "awf/ws_protocol",
            "repo_url": "https://example.invalid/awf.git",
        }

    async def _invoke_cli_for_verdict_result(
        self, **kwargs: object
    ) -> comment_verdict.VerdictResult:
        return await comment_verdict._invoke_cli_for_verdict_result(self, **kwargs)  # type: ignore[arg-type]

    async def _handle_provider_agent_run_error(
        self,
        _workspace_id: str,
        _exc: AgentRunError,
        *,
        state: object | None = None,
    ) -> None:
        del state
        if self.provider_error_action is not None:
            raise self.provider_error_action


async def _mock_read_correction_residue_fingerprint(
    runner: object,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    """Map mock porcelain state to stable fingerprints for verdict retry tests.

    Production residue probes hash tracked diffs via real git subprocesses.
    ``_VerdictRunner`` worktrees are empty directories, so delegate fingerprint
    reads to the same mocked ``git status`` porcelain the commit sink uses.
    """
    if not isinstance(runner, _VerdictRunner):
        return await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )

    if not worktree_path.exists():
        return ""

    try:
        status = await runner._run_git(
            list(
                git_worktree_command(
                    worktree_path,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                )
            ),
        )
    except OSError:
        return None

    if status.returncode != 0:
        return None
    stdout = status.stdout or ""
    if not stdout.strip():
        return ""
    return f"mock-fp:{stdout.strip()}"


@pytest.fixture(autouse=True)
def _safe_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        comment_verdict,
        "_read_correction_pr_worthy_residue_fingerprint",
        _mock_read_correction_residue_fingerprint,
    )


def _agent_error(stdout: str = "") -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=1, stdout=stdout, stderr="provider failed"),
        reason_code="AGENT_CLI_FAILED",
    )


async def _invoke(
    runner: _VerdictRunner,
    *,
    require_fix_evidence: bool = True,
):
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head="a" * 40,
        require_fix_evidence=require_fix_evidence,
    )
