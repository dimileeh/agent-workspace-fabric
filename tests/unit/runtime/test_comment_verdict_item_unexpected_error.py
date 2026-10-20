"""``reraise_or_park_item`` and the item unexpected-error park (#1076)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import structlog

from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GitHubClientError
from awf.runtime.pr_monitor_runner import comments, fix_cycle, loop, operator_hints
from awf.runtime.pr_monitor_runner.comment_verdict_item_unexpected_error import (
    DEDICATED_UPSTREAM_ERRORS,
    ItemSite,
    ItemUnexpectedError,
    item_unexpected_error_result,
    reraise_or_park_item,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_UNEXPECTED_ERROR,
    AgentVerdictExecutionError,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

pytestmark = pytest.mark.unit

_FLOOR = "a" * 40
_PARKED_EVENT = "monitor.agent_verdict_item_unexpected_error_parked"

# One instance per dedicated class; the lockstep assert below keeps it complete.
_DEDICATED_INSTANCES: list[Exception] = [
    AgentVerdictProtocolError(),
    AgentVerdictExecutionError(reason_code="AGENT_CLI_FAILED"),
    ProviderRecoveryRetryError("retry"),
    ProviderRecoveryFallbackError("fallback"),
    ProviderRecoveryAuthError("auth"),
    ProtectedScopeDiffError("diff unavailable"),
    _MonitorPolicyBlockedError("blocked"),
    _MonitorAgentRuntimeOwnershipRepairFailedError("chown failed"),
    _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING_UNRECOVERABLE", "missing"),
    _MonitorMirrorHooksPathRepairFailedError(),
    _MonitorAgentServiceRecoverySupersededError("superseded"),
    _MonitorAgentServiceRecoveryFailedError("failed"),
    ComposeExecCleanupError(
        invocation_id="inv", source="recovery", label="agent", message="cleanup failed"
    ),
    ForgeClientError("forge blip"),
]

# ``except`` arms in the four upstream modules that do not wrap the item protocol.
_NON_PROTOCOL_ARMS = {
    "ValueError": "comments.py: local guard around datetime.fromisoformat",
    "BaseException": "fix_cycle.py: settle-then-re-raise around the item start-head read",
}


def test_instance_list_covers_the_dedicated_set() -> None:
    assert {type(exc) for exc in _DEDICATED_INSTANCES} == set(DEDICATED_UPSTREAM_ERRORS)


@pytest.mark.parametrize("exc", _DEDICATED_INSTANCES, ids=lambda e: type(e).__name__)
def test_dedicated_cause_is_re_raised_unchanged(exc: Exception, tmp_path: Path) -> None:
    with pytest.raises(type(exc)) as caught:
        reraise_or_park_item(exc, "commit_sink", _FLOOR, tmp_path)
    assert caught.value is exc


def test_dedicated_subclass_is_re_raised_unchanged(tmp_path: Path) -> None:
    exc = GitHubClientError(operation="gh api", returncode=1, stderr="bad credentials")
    with pytest.raises(GitHubClientError) as caught:
        reraise_or_park_item(exc, "commit_sink", _FLOOR, tmp_path)
    assert caught.value is exc


@pytest.mark.parametrize(
    "exc",
    [
        OSError("spawn failed"),
        RuntimeError("boom"),
        TimeoutError("slow"),
        ValueError("bad"),
        KeyError("k"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_untyped_cause_raises_the_park_signal_chained_to_it(exc: Exception, tmp_path: Path) -> None:
    with pytest.raises(ItemUnexpectedError) as caught:
        reraise_or_park_item(exc, "post_attempt_tip_probe", _FLOOR, tmp_path)
    assert caught.value.__cause__ is exc
    assert caught.value.site == "post_attempt_tip_probe"
    assert caught.value.rollback_floor_head == _FLOOR


def test_unknown_floor_re_raises_the_untyped_cause(tmp_path: Path) -> None:
    exc = OSError("spawn failed")
    with pytest.raises(OSError) as caught:
        reraise_or_park_item(exc, "agent_invocation", None, tmp_path)
    assert caught.value is exc


def test_missing_worktree_re_raises_the_untyped_cause(tmp_path: Path) -> None:
    exc = RuntimeError("boom")
    with pytest.raises(RuntimeError) as caught:
        reraise_or_park_item(exc, "agent_invocation", _FLOOR, tmp_path / "gone")
    assert caught.value is exc


def test_park_signal_is_not_itself_dedicated() -> None:
    assert not issubclass(ItemUnexpectedError, DEDICATED_UPSTREAM_ERRORS)
    assert not issubclass(ItemUnexpectedError, ValueError)


def _park(cause: Exception, site: ItemSite = "commit_sink") -> ItemUnexpectedError:
    try:
        raise ItemUnexpectedError(site=site, rollback_floor_head=_FLOOR) from cause
    except ItemUnexpectedError as park:
        return park


def test_result_names_site_type_message_and_floor() -> None:
    with structlog.testing.capture_logs() as captured:
        result = item_unexpected_error_result(
            _park(OSError("git spawn failed")), workspace_id="ws_x"
        )
    assert result.verdict == "needs_human"
    assert result.kept_commit is False
    assert result.reason is not None
    assert result.reason.startswith(
        f"{AGENT_VERDICT_ITEM_UNEXPECTED_ERROR}: OSError at commit_sink"
    )
    assert "git spawn failed" in result.reason
    assert _FLOOR[:12] in result.reason
    events = [e for e in captured if e["event"] == _PARKED_EVENT]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["reason_code"] == AGENT_VERDICT_ITEM_UNEXPECTED_ERROR
    assert events[0]["exc_type"] == "OSError"
    assert events[0]["site"] == "commit_sink"
    assert events[0]["rollback_floor_head"] == _FLOOR
    assert events[0]["workspace_id"] == "ws_x"


def test_result_redacts_bounds_and_collapses_the_message() -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    cause = RuntimeError(f"push to https://user:{secret}@example.invalid\nfailed " + "x" * 2000)
    with structlog.testing.capture_logs() as captured:
        result = item_unexpected_error_result(_park(cause), workspace_id="ws_x")
    assert result.reason is not None
    assert secret not in result.reason
    assert "\n" not in result.reason
    assert len(result.reason) <= 500
    event = next(e for e in captured if e["event"] == _PARKED_EVENT)
    assert secret not in str(event["error"])
    assert len(str(event["error"])) <= 240


def test_result_keeps_an_untyped_causes_own_reason_code() -> None:
    class _CodedError(RuntimeError):
        reason_code = "SOME_GIT_FAULT"

    with structlog.testing.capture_logs() as captured:
        result = item_unexpected_error_result(_park(_CodedError("x")), workspace_id="ws_x")
    assert "SOME_GIT_FAULT" in (result.reason or "")
    event = next(e for e in captured if e["event"] == _PARKED_EVENT)
    assert event["cause_reason_code"] == "SOME_GIT_FAULT"


def test_result_with_an_empty_message_says_so() -> None:
    result = item_unexpected_error_result(_park(TimeoutError()), workspace_id="ws_x")
    assert "<no message>" in (result.reason or "")


def _caught_class_names(module: object) -> set[str]:
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            elts = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            names.update(e.id for e in elts if isinstance(e, ast.Name))
    return names


def test_dedicated_set_matches_the_upstream_except_arms() -> None:
    """The set is derived from the handlers, not copied (#1076 spec).

    A new ``except`` arm in one of the four upstream modules must either join
    ``DEDICATED_UPSTREAM_ERRORS`` or be listed in ``_NON_PROTOCOL_ARMS`` with a
    reason, so an error with its own handler is never parked instead.
    """
    caught: set[type[BaseException]] = set()
    for module in (comments, operator_hints, fix_cycle, loop):
        for name in _caught_class_names(module):
            if name not in _NON_PROTOCOL_ARMS:
                caught.add(getattr(module, name))
    assert caught == set(DEDICATED_UPSTREAM_ERRORS)
