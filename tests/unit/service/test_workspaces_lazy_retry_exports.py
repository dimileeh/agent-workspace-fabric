"""Coverage for lazy retry helper compatibility exports."""

from __future__ import annotations

import pytest

from awf.service import workspaces


class _RetryModuleStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> object:
        def _call(
            *args: object, **kwargs: object
        ) -> tuple[str, tuple[object, ...], dict[str, object]]:
            self.calls.append((name, args, kwargs))
            return name, args, kwargs

        return _call


@pytest.mark.unit
def test_workspaces_lazy_retry_exports_delegate_to_retry_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_module = _RetryModuleStub()
    monkeypatch.setattr(workspaces, "_workspaces_retry_module", lambda: retry_module)

    delegates = (
        workspaces.retry_workspace_row,
        workspaces._retry_task_policy,  # noqa: SLF001
        workspaces._planning_scope_recovery_payload,  # noqa: SLF001
        workspaces._conformance_salvage_recovery_payload,  # noqa: SLF001
        workspaces._agent_timeout_salvage_recovery_payload,  # noqa: SLF001
        workspaces._retry_task_for_source,  # noqa: SLF001
        workspaces._latest_failed_state_event,  # noqa: SLF001
        workspaces._compact_conformance_payload,  # noqa: SLF001
        workspaces._compact_planning_scope_payload,  # noqa: SLF001
        workspaces._compact_string_list,  # noqa: SLF001
        workspaces._compact_fallback_model,  # noqa: SLF001
        workspaces._compact_salvage_payload,  # noqa: SLF001
        workspaces._payload_str,  # noqa: SLF001
        workspaces._is_plan_conformance_unsatisfied,  # noqa: SLF001
        workspaces._agent_timeout_retry_context,  # noqa: SLF001
        workspaces._conformance_retry_context,  # noqa: SLF001
        workspaces._retry_evidence_gaps,  # noqa: SLF001
        workspaces._optional_retry_evidence_str,  # noqa: SLF001
        workspaces._planning_scope_retry_context,  # noqa: SLF001
        workspaces._approved_planning_scope_fallback_model,  # noqa: SLF001
    )

    results = [delegate("arg", flag=True) for delegate in delegates]

    assert [result[0] for result in results] == [
        "retry_workspace_row",
        "_retry_task_policy",
        "_planning_scope_recovery_payload",
        "_conformance_salvage_recovery_payload",
        "_agent_timeout_salvage_recovery_payload",
        "_retry_task_for_source",
        "_latest_failed_state_event",
        "_compact_conformance_payload",
        "_compact_planning_scope_payload",
        "_compact_string_list",
        "_compact_fallback_model",
        "_compact_salvage_payload",
        "_payload_str",
        "_is_plan_conformance_unsatisfied",
        "_agent_timeout_retry_context",
        "_conformance_retry_context",
        "_retry_evidence_gaps",
        "_optional_retry_evidence_str",
        "_planning_scope_retry_context",
        "_approved_planning_scope_fallback_model",
    ]
    assert all(result[1:] == (("arg",), {"flag": True}) for result in results)
