"""Cross-client capability registry and shared normalizers.

The contract harness encodes the canonical operator surfaces that REST, CLI, and
MCP all expose. Every entry maps a single operator capability to:

- the canonical REST endpoint (``method`` + ``path``);
- the MCP tool name when one exists (``mcp_tool``);
- the CLI invocation tokens when one exists (``cli_tokens``);
- the parity matrix Status / Backlog Slice columns at the time of writing
  (``parity_status``, ``parity_backlog_slice``).
- whether ``Idempotency-Key`` is optional replay support or a required
  client-boundary contract.

The registry is cross-validated against ``docs/MCP_CLIENT_PARITY.md`` at test
collection time so the harness fails loudly if either side drifts. Tests that
exercise individual contract dimensions iterate over this registry instead of
hard-coding endpoints, so adding a new capability only needs one row here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tests.unit.mcp._parity_utils import (
    _extract_mcp_tool_tokens_from_cell,
    _extract_rest_endpoints_from_cell,
    _parity_rows,
    _strip_backticks,
)

_WORKSPACE_CONTROL_RESPONSE_FIELDS = frozenset(
    {"workspace_id", "operation_id", "operation_status", "status", "message", "warnings"}
)


@dataclass(frozen=True)
class ContractCapability:
    """One operator capability viewed across REST, CLI, and MCP."""

    name: str
    parity_capability: str
    rest_method: str
    rest_path: str
    mcp_tool: str | None
    cli_tokens: tuple[str, ...] | None
    parity_status: str
    parity_backlog_slice: str
    supports_idempotency_key: bool
    supports_if_match: bool
    requires_idempotency_key: bool = False
    error_codes: frozenset[str] = field(default_factory=frozenset)
    rest_response_model: str | None = None
    rest_path_fields: frozenset[str] = field(default_factory=frozenset)
    rest_query_fields: frozenset[str] = field(default_factory=frozenset)
    rest_header_fields: frozenset[str] = field(default_factory=frozenset)
    rest_body_fields: frozenset[str] = field(default_factory=frozenset)
    mcp_request_fields: frozenset[str] = field(default_factory=frozenset)
    mcp_required_fields: frozenset[str] = field(default_factory=frozenset)
    cli_options: frozenset[str] = field(default_factory=frozenset)
    cli_arguments: frozenset[str] = field(default_factory=frozenset)
    # Representative client-visible keys. For MCP-backed capabilities, these
    # must be present in the MCP structuredContent payload; REST models are
    # tracked separately by rest_response_model.
    response_fields: frozenset[str] = field(default_factory=frozenset)
    auth_required: bool = False

    @property
    def is_mcp_implemented(self) -> bool:
        return self.parity_status == "MCP implemented"

    @property
    def is_mcp_partial(self) -> bool:
        return self.parity_status == "MCP partial"

    @property
    def is_mcp_missing(self) -> bool:
        return self.parity_status == "MCP missing/backlog"

    @property
    def is_safe_read(self) -> bool:
        return self.rest_method == "GET"


_CAPABILITIES: tuple[ContractCapability, ...] = (
    ContractCapability(
        name="cancel_workspace",
        parity_capability="Cancel workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/cancel",
        mcp_tool="awf_cancel_workspace",
        cli_tokens=("workspace", "cancel"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset({"NOT_FOUND", "VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT"}),
        rest_response_model="WorkspaceControlResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        rest_body_fields=frozenset({"reason", "stop_stack"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "reason", "stop_stack", "idempotency_key", "expected_version"}
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset(
            {
                "--reason",
                "--stop-stack/--no-stop-stack",
                "--idempotency-key",
                "--if-match",
                "--api-token",
            }
        ),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=_WORKSPACE_CONTROL_RESPONSE_FIELDS,
        auth_required=True,
    ),
    ContractCapability(
        name="stop_workspace",
        parity_capability="Stop workspace stack",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/stop",
        mcp_tool="awf_stop_workspace",
        cli_tokens=("workspace", "stop"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset(
            {"NOT_FOUND", "VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT", "STACK_STOP_FAILED"}
        ),
        rest_response_model="WorkspaceControlResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        rest_body_fields=frozenset({"reason"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "reason", "idempotency_key", "expected_version"}
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset({"--reason", "--idempotency-key", "--if-match", "--api-token"}),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=_WORKSPACE_CONTROL_RESPONSE_FIELDS,
        auth_required=True,
    ),
    ContractCapability(
        name="destroy_workspace",
        parity_capability="Destroy workspace resources",
        rest_method="DELETE",
        rest_path="/v1/workspaces/{workspace_id}",
        mcp_tool="awf_destroy_workspace",
        cli_tokens=("workspace", "destroy"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_ACTIVE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
                "STACK_STOP_FAILED",
            }
        ),
        rest_response_model="WorkspaceControlResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"force", "remove_volumes", "remove_worktree"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        mcp_request_fields=frozenset(
            {
                "workspace_id",
                "force",
                "remove_volumes",
                "remove_worktree",
                "idempotency_key",
                "expected_version",
            }
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset(
            {
                "--force",
                "--remove-volumes/--no-remove-volumes",
                "--remove-worktree/--no-remove-worktree",
                "--idempotency-key",
                "--if-match",
                "--api-token",
            }
        ),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=_WORKSPACE_CONTROL_RESPONSE_FIELDS,
        auth_required=True,
    ),
    ContractCapability(
        name="remonitor_workspace",
        parity_capability="Remonitor workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/remonitor",
        mcp_tool="awf_remonitor_workspace",
        cli_tokens=("workspace", "remonitor"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_PR_URL_REQUIRED",
                "WORKSPACE_STATE_NOT_REMONITORABLE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
        rest_response_model="WorkspaceControlResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        rest_body_fields=frozenset({"reason"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "reason", "idempotency_key", "expected_version"}
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset({"--reason", "--idempotency-key", "--if-match", "--api-token"}),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=_WORKSPACE_CONTROL_RESPONSE_FIELDS,
        auth_required=True,
    ),
    ContractCapability(
        name="request_validation",
        parity_capability="Request validation",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/validate",
        mcp_tool="awf_request_workspace_validation",
        cli_tokens=("workspace", "validate"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_PR_URL_REQUIRED",
                "WORKSPACE_STATE_NOT_VALIDATABLE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
        rest_response_model="OperationResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        rest_body_fields=frozenset({"reason", "requested_tier"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "reason", "requested_tier", "idempotency_key", "expected_version"}
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset(
            {"--reason", "--requested-tier", "--idempotency-key", "--if-match", "--api-token"}
        ),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=frozenset({"id", "workspace_id", "type", "status"}),
        auth_required=True,
    ),
    ContractCapability(
        name="refresh_workspace",
        parity_capability="Refresh workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/refresh",
        mcp_tool="awf_refresh_workspace",
        cli_tokens=("workspace", "refresh"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_STATE_NOT_REFRESHABLE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
        rest_response_model="OperationResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        rest_body_fields=frozenset({"reason"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "reason", "idempotency_key", "expected_version"}
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset({"--reason", "--idempotency-key", "--if-match", "--api-token"}),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=frozenset({"id", "workspace_id", "type", "status"}),
        auth_required=True,
    ),
    ContractCapability(
        name="rebase_workspace",
        parity_capability="Rebase workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/rebase",
        mcp_tool="awf_rebase_workspace",
        cli_tokens=("workspace", "rebase"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        requires_idempotency_key=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_STATE_NOT_REBASEABLE",
                "MERGE_CANDIDATE_NOT_FOUND",
                "WORKSPACE_REBASE_CONFLICT",
                "WORKSPACE_OPERATION_CONFLICT",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
        rest_response_model="OperationResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_header_fields=frozenset({"Idempotency-Key", "If-Match"}),
        rest_body_fields=frozenset({"reason"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "reason", "idempotency_key", "expected_version"}
        ),
        mcp_required_fields=frozenset({"workspace_id", "idempotency_key"}),
        cli_options=frozenset({"--reason", "--idempotency-key", "--if-match", "--api-token"}),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=frozenset({"id", "workspace_id", "type", "status"}),
        auth_required=True,
    ),
    ContractCapability(
        name="retry_workspace",
        parity_capability="Retry workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/retry",
        mcp_tool="awf_retry_workspace",
        cli_tokens=("workspace", "retry"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        error_codes=frozenset(
            {
                "WORKSPACE_NOT_FOUND",
                "WORKSPACE_NOT_RETRYABLE",
                "WORKSPACE_RETRY_EXHAUSTED",
                "WORKSPACE_RETRY_SALVAGE_UNAVAILABLE",
                "PROVIDER_READINESS_PRECHECK_FAILED",
            }
        ),
        rest_response_model="WorkspaceRetryResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset(
            {"provider_readiness_override", "provider_readiness_override_reason"}
        ),
        mcp_request_fields=frozenset(
            {
                "workspace_id",
                "provider_readiness_override",
                "provider_readiness_override_reason",
            }
        ),
        mcp_required_fields=frozenset({"workspace_id"}),
        cli_options=frozenset(
            {"--provider-readiness-override", "--provider-readiness-override-reason", "--api-token"}
        ),
        cli_arguments=frozenset({"workspace_id"}),
        response_fields=frozenset(
            {"source_workspace_id", "new_workspace_id", "operation_id", "status"}
        ),
        auth_required=True,
    ),
    ContractCapability(
        name="create_workspace",
        parity_capability="Workspace create",
        rest_method="POST",
        rest_path="/v1/workspaces",
        mcp_tool="awf_create_workspace",
        cli_tokens=("workspace", "create"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=False,
        error_codes=frozenset(
            {
                "IDEMPOTENCY_CONFLICT",
                "INVALID_PROFILE",
                "TASK_EXTERNAL_ID_CONFLICT",
                "INSUFFICIENT_DISK",
            }
        ),
        rest_response_model="WorkspaceAcceptedResponse",
        rest_header_fields=frozenset({"Idempotency-Key"}),
        rest_body_fields=frozenset(
            {
                "repo",
                "task",
                "workspace",
                "validation",
                "preflight",
                "resources",
                "companions",
            }
        ),
        mcp_request_fields=frozenset(
            {
                "repo_url",
                "base_branch",
                "task_title",
                "task_prompt",
                "task_kind",
                "agent",
                "model",
                "effort",
                "task_external_id",
                "task_class",
                "priority",
                "human_boost",
                "out_of_scope_changes",
                "provider_recovery",
                "owned_paths",
                "cpu",
                "memory",
                "steady_state_cpu_cores",
                "steady_state_memory_gb",
                "peak_cpu_cores",
                "peak_memory_gb",
                "disk_mb",
                "profile_ref",
                "profile",
                "validation_commands",
                "requested_tier",
                "auto_merge",
                "initial_review_grace_period_seconds",
                "provider_readiness_override",
                "provider_readiness_override_reason",
                "companions",
                "idempotency_key",
            }
        ),
        mcp_required_fields=frozenset({"repo_url", "task_title", "task_prompt"}),
        cli_options=frozenset(
            {
                "--repo",
                "--title",
                "--prompt",
                "--base",
                "--agent",
                "--model",
                "--effort",
                "--task-class",
                "--priority",
                "--human-boost",
                "--owned-path",
                "--external-id",
                "--cpu",
                "--memory",
                "--steady-state-cpu-cores",
                "--steady-state-memory-gb",
                "--peak-cpu-cores",
                "--peak-memory-gb",
                "--disk-mb",
                "--profile",
                "--test",
                "--with-db",
                "--auto-merge/--no-auto-merge",
                "--initial-review-grace-period-seconds",
                "--out-of-scope-changes-json",
                "--provider-recovery-json",
                "--provider-readiness-override",
                "--provider-readiness-override-reason",
                "--companion-json",
                "--idempotency-key",
                "--api-token",
            }
        ),
        response_fields=frozenset({"workspace_id", "status", "version", "status_url"}),
        auth_required=True,
    ),
    ContractCapability(
        name="adopt_pr_monitor",
        parity_capability="Existing PR monitor adoption",
        rest_method="POST",
        rest_path="/v1/workspaces/adopt-pr",
        mcp_tool="awf_adopt_pull_request_monitor",
        cli_tokens=("workspace", "adopt-pr"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        error_codes=frozenset(
            {
                "PR_ADOPTION_INPUT_REQUIRED",
                "INVALID_GITHUB_REPO",
                "PR_NOT_FOUND",
                "PR_ALREADY_CLOSED",
                "PR_ALREADY_MERGED",
                "PR_METADATA_FETCH_FAILED",
                "PR_METADATA_INVALID",
                "PR_ADOPTION_POLICY_CONFLICT",
            }
        ),
        rest_response_model="PullRequestMonitorAdoptionResponse",
        rest_body_fields=frozenset(
            {
                "repo_url",
                "repo_slug",
                "pr_number",
                "pr_url",
                "agent",
                "model",
                "effort",
                "profile_ref",
                "profile",
                "owned_paths",
                "auto_merge",
                "initial_review_grace_period_seconds",
                "task_title",
                "task_prompt",
                "reason",
            }
        ),
        mcp_request_fields=frozenset(
            {
                "repo_url",
                "repo_slug",
                "pr_number",
                "pr_url",
                "agent",
                "model",
                "effort",
                "profile_ref",
                "profile",
                "owned_paths",
                "auto_merge",
                "initial_review_grace_period_seconds",
                "task_title",
                "task_prompt",
                "reason",
            }
        ),
        cli_options=frozenset(
            {
                "--repo",
                "--pr",
                "--pr-url",
                "--agent",
                "--model",
                "--effort",
                "--owned-path",
                "--profile",
                "--auto-merge/--no-auto-merge",
                "--initial-review-grace-period-seconds",
                "--title",
                "--prompt",
                "--reason",
                "--api-token",
            }
        ),
        response_fields=frozenset({"workspace_id", "status", "version", "pr_url"}),
        auth_required=True,
    ),
)

_CAPABILITIES += (
    ContractCapability(
        name="get_workspace",
        parity_capability="Workspace list and get",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}",
        mcp_tool="awf_get_workspace",
        cli_tokens=("workspace", "show"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        mcp_request_fields=frozenset({"workspace_id"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        cli_arguments=frozenset({"workspace_id"}),
        cli_options=frozenset({"--api-token"}),
        response_fields=frozenset({"id", "status", "version"}),
        auth_required=True,
    ),
    ContractCapability(
        name="list_workspaces",
        parity_capability="Workspace list and get",
        rest_method="GET",
        rest_path="/v1/workspaces",
        mcp_tool="awf_list_workspaces",
        cli_tokens=("workspace", "list"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="list[WorkspaceResponse]",
        rest_query_fields=frozenset({"status", "agent", "repo_url", "limit"}),
        mcp_request_fields=frozenset({"status", "agent", "repo_url", "limit"}),
        cli_options=frozenset({"--status", "--agent", "--repo-url", "--limit", "--api-token"}),
        auth_required=True,
    ),
    ContractCapability(
        name="wait_for_workspace",
        parity_capability="Workspace list and get",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}",
        mcp_tool="awf_wait_for_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        mcp_request_fields=frozenset(
            {"workspace_id", "terminal_statuses", "poll_interval_seconds", "timeout_seconds"}
        ),
        mcp_required_fields=frozenset({"workspace_id"}),
        response_fields=frozenset({"id", "status", "version"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_overview",
        parity_capability="Workspace overview",
        rest_method="GET",
        rest_path="/v1/workspaces/overview",
        mcp_tool="awf_list_workspace_overview",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceOverviewListResponse",
        rest_query_fields=frozenset({"status", "agent", "repo_url", "limit", "cursor"}),
        mcp_request_fields=frozenset({"status", "agent", "repo_url", "limit", "cursor"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="merge_queue",
        parity_capability="Merge queue",
        rest_method="GET",
        rest_path="/v1/merge-queue",
        mcp_tool="awf_list_merge_queue",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="MergeQueueListResponse",
        rest_query_fields=frozenset({"repo_url", "base_branch", "status", "limit", "cursor"}),
        mcp_request_fields=frozenset({"repo_url", "base_branch", "status", "limit", "cursor"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="list_tasks",
        parity_capability="Task attempts",
        rest_method="GET",
        rest_path="/v1/tasks",
        mcp_tool="awf_list_tasks",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="TaskListResponse",
        rest_query_fields=frozenset({"status", "agent", "repo_url", "limit"}),
        mcp_request_fields=frozenset({"status", "agent", "repo_url", "limit"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="list_task_attempts",
        parity_capability="Task attempts",
        rest_method="GET",
        rest_path="/v1/tasks/{task_ref}/attempts",
        mcp_tool="awf_list_task_attempts",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="TaskAttemptListResponse",
        rest_path_fields=frozenset({"task_ref"}),
        rest_query_fields=frozenset({"limit"}),
        mcp_request_fields=frozenset({"task_ref", "limit"}),
        mcp_required_fields=frozenset({"task_ref"}),
        response_fields=frozenset(
            {"task_id", "task_ref", "items", "next_cursor", "has_more", "limit", "cursor"}
        ),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_validation",
        parity_capability="Validation provenance",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/validation",
        mcp_tool="awf_list_workspace_validation",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="ValidationProvenanceListResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"limit", "cursor"}),
        mcp_request_fields=frozenset({"workspace_id", "limit", "cursor"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_stale_reasons",
        parity_capability="Stale reasons",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/stale-reasons",
        mcp_tool="awf_list_workspace_stale_reasons",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="StaleReasonListResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"include_resolved", "limit", "cursor"}),
        mcp_request_fields=frozenset({"workspace_id", "include_resolved", "limit", "cursor"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_artifacts",
        parity_capability="Artifact metadata",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/artifacts",
        mcp_tool="awf_list_workspace_artifacts",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceArtifactListResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"limit", "cursor"}),
        mcp_request_fields=frozenset({"workspace_id", "limit", "cursor"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_artifact_download",
        parity_capability="Artifact content/download",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/artifacts/download",
        mcp_tool="awf_read_workspace_artifact",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        error_codes=frozenset(
            {"INVALID_ARTIFACT_PATH", "NOT_FOUND", "ARTIFACT_OVERSIZED", "ARTIFACT_BLOCKED"}
        ),
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"path"}),
        mcp_request_fields=frozenset({"workspace_id", "relative_path", "limit_bytes"}),
        mcp_required_fields=frozenset({"workspace_id", "relative_path"}),
        response_fields=frozenset(
            {"workspace_id", "relative_path", "name", "content_type", "size_bytes", "content"}
        ),
        auth_required=True,
    ),
    ContractCapability(
        name="failure_analysis_metrics",
        parity_capability="Failure analysis metrics",
        rest_method="GET",
        rest_path="/v1/metrics/failures/summary",
        mcp_tool="awf_get_failure_analysis_summary",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="FailureAnalysisSummaryResponse",
        rest_query_fields=frozenset({"since_hours", "limit"}),
        mcp_request_fields=frozenset({"since_hours", "limit"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_reliability_metrics",
        parity_capability="Workspace reliability metrics",
        rest_method="GET",
        rest_path="/v1/metrics/workspaces/summary",
        mcp_tool="awf_get_workspace_reliability_summary",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceReliabilitySummaryResponse",
        rest_query_fields=frozenset({"since_hours"}),
        mcp_request_fields=frozenset({"since_hours"}),
        auth_required=True,
    ),
    ContractCapability(
        name="resource_saturation_metrics",
        parity_capability="Resource saturation metrics",
        rest_method="GET",
        rest_path="/v1/metrics/resources/saturation",
        mcp_tool="awf_get_resource_saturation_summary",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="ResourceSaturationSummaryResponse",
        auth_required=True,
    ),
    ContractCapability(
        name="slo_metrics",
        parity_capability="SLO metrics",
        rest_method="GET",
        rest_path="/v1/metrics/slo",
        mcp_tool="awf_get_slo_metrics_summary",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="SloMetricsSummaryResponse",
        rest_query_fields=frozenset({"since_hours"}),
        mcp_request_fields=frozenset({"since_hours"}),
        auth_required=True,
    ),
    ContractCapability(
        name="locks",
        parity_capability="Locks and owned-path reservations",
        rest_method="GET",
        rest_path="/v1/locks",
        mcp_tool="awf_list_locks",
        cli_tokens=("locks", "list"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceLockListResponse",
        rest_query_fields=frozenset({"repo_url", "task_class", "status", "limit", "cursor"}),
        mcp_request_fields=frozenset({"repo_url", "task_class", "status", "limit", "cursor"}),
        cli_options=frozenset({"--repo-url", "--task-class", "--status", "--limit", "--api-token"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="overlap_graph",
        parity_capability="Advisory overlap graph",
        rest_method="GET",
        rest_path="/v1/locks/overlap-graph",
        mcp_tool="awf_get_overlap_graph",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceOverlapGraphResponse",
        rest_query_fields=frozenset(
            {"repo_url", "base_branch", "task_class", "queue_state", "limit"}
        ),
        mcp_request_fields=frozenset(
            {"repo_url", "base_branch", "task_class", "queue_state", "limit"}
        ),
        response_fields=frozenset({"nodes", "edges", "summary"}),
        auth_required=True,
    ),
    ContractCapability(
        name="service_health",
        parity_capability="Service health and readiness",
        rest_method="GET",
        rest_path="/healthz",
        mcp_tool="awf_get_service_health",
        cli_tokens=("service", "status"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="HealthResponse",
        cli_options=frozenset({"--format", "--provider"}),
    ),
    ContractCapability(
        name="service_readiness",
        parity_capability="Service health and readiness",
        rest_method="GET",
        rest_path="/readyz",
        mcp_tool="awf_get_service_readiness",
        cli_tokens=("service", "doctor"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="ReadyResponse",
        rest_query_fields=frozenset({"provider"}),
        mcp_request_fields=frozenset({"providers"}),
        cli_options=frozenset({"--format", "--provider", "--bundle"}),
    ),
    ContractCapability(
        name="core_release_readiness",
        parity_capability="Core release readiness scorecard",
        rest_method="GET",
        rest_path="/release-readiness",
        mcp_tool="awf_get_core_release_readiness",
        cli_tokens=("service", "readiness"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="ReleaseReadinessResponse",
        rest_query_fields=frozenset(
            {
                "provider",
                "failure_window_hours",
                "slo_window_hours",
                "allow_generic_failures",
                "allow_slo_breach",
            }
        ),
        mcp_request_fields=frozenset(
            {
                "providers",
                "failure_window_hours",
                "slo_window_hours",
                "allow_generic_failures",
                "allow_slo_breach",
            }
        ),
        cli_options=frozenset(
            {
                "--format",
                "--demo-path",
                "--failure-window-hours",
                "--slo-window-hours",
                "--allow-generic-failures/--no-allow-generic-failures",
                "--allow-slo-breach/--no-allow-slo-breach",
                "--provider",
            }
        ),
        response_fields=frozenset({"status", "summary", "checks", "next_actions"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_runtime",
        parity_capability="Workspace runtime snapshot",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/runtime",
        mcp_tool="awf_get_workspace_runtime",
        cli_tokens=("workspace", "runtime"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceRuntimeResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        mcp_request_fields=frozenset({"workspace_id"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        cli_arguments=frozenset({"workspace_id"}),
        cli_options=frozenset({"--api-token"}),
        response_fields=frozenset({"workspace_id", "stack_state", "services"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_operations",
        parity_capability="Workspace operations",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/operations",
        mcp_tool="awf_list_workspace_operations",
        cli_tokens=("workspace", "operations"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="OperationListResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"status", "type", "limit", "cursor"}),
        mcp_request_fields=frozenset({"workspace_id", "status", "type", "limit", "cursor"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        cli_arguments=frozenset({"workspace_id"}),
        cli_options=frozenset({"--limit", "--status", "--type", "--cursor", "--api-token"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="global_operations",
        parity_capability="Global operations",
        rest_method="GET",
        rest_path="/v1/operations",
        mcp_tool="awf_list_operations",
        cli_tokens=("operations", "list"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="OperationListResponse",
        rest_query_fields=frozenset({"workspace_id", "status", "type", "limit", "cursor"}),
        mcp_request_fields=frozenset({"workspace_id", "status", "type", "limit", "cursor"}),
        cli_options=frozenset(
            {"--workspace-id", "--status", "--type", "--limit", "--cursor", "--api-token"}
        ),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="get_operation",
        parity_capability="Global operations",
        rest_method="GET",
        rest_path="/v1/operations/{operation_id}",
        mcp_tool="awf_get_operation",
        cli_tokens=("operations", "show"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="OperationResponse",
        rest_path_fields=frozenset({"operation_id"}),
        mcp_request_fields=frozenset({"operation_id"}),
        mcp_required_fields=frozenset({"operation_id"}),
        cli_arguments=frozenset({"operation_id"}),
        cli_options=frozenset({"--api-token"}),
        response_fields=frozenset({"id", "workspace_id", "type", "status"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_logs",
        parity_capability="Durable workspace logs",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/logs",
        mcp_tool="awf_list_workspace_logs",
        cli_tokens=("workspace", "logs"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceLogListResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        mcp_request_fields=frozenset({"workspace_id"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        cli_arguments=frozenset({"workspace_id"}),
        cli_options=frozenset({"--api-token"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="read_workspace_log",
        parity_capability="Durable workspace logs",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/logs/{stream_id}",
        mcp_tool="awf_read_workspace_log",
        cli_tokens=("workspace", "log"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceLogReadResponse",
        rest_path_fields=frozenset({"workspace_id", "stream_id"}),
        rest_query_fields=frozenset({"offset", "limit_bytes"}),
        mcp_request_fields=frozenset({"workspace_id", "stream_id", "offset", "limit_bytes"}),
        mcp_required_fields=frozenset({"workspace_id", "stream_id"}),
        cli_arguments=frozenset({"workspace_id", "stream_id"}),
        cli_options=frozenset({"--offset", "--limit-bytes", "--api-token"}),
        response_fields=frozenset({"stream_id", "offset", "next_offset", "eof", "data"}),
        auth_required=True,
    ),
    ContractCapability(
        name="global_events",
        parity_capability="Workspace events",
        rest_method="GET",
        rest_path="/v1/events",
        mcp_tool="awf_list_events",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceEventListResponse",
        rest_query_fields=frozenset({"workspace_id", "event_type", "limit"}),
        mcp_request_fields=frozenset({"workspace_id", "event_type", "limit"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
    ContractCapability(
        name="workspace_events",
        parity_capability="Workspace events",
        rest_method="GET",
        rest_path="/v1/workspaces/{workspace_id}/events",
        mcp_tool="awf_list_workspace_events",
        cli_tokens=("workspace", "events"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=False,
        supports_if_match=False,
        rest_response_model="WorkspaceEventListResponse",
        rest_path_fields=frozenset({"workspace_id"}),
        rest_query_fields=frozenset({"event_type", "limit"}),
        mcp_request_fields=frozenset({"workspace_id", "limit", "event_type"}),
        mcp_required_fields=frozenset({"workspace_id"}),
        cli_arguments=frozenset({"workspace_id"}),
        cli_options=frozenset({"--limit", "--event-type", "--api-token"}),
        response_fields=frozenset({"items", "next_cursor", "has_more", "limit", "cursor"}),
        auth_required=True,
    ),
)


CAPABILITIES_BY_NAME: dict[str, ContractCapability] = {c.name: c for c in _CAPABILITIES}


def all_capabilities() -> tuple[ContractCapability, ...]:
    """Return the registered capabilities."""
    return _CAPABILITIES


def mutating_capabilities() -> tuple[ContractCapability, ...]:
    """Return mutating REST capabilities (POST/DELETE)."""
    return tuple(c for c in _CAPABILITIES if c.rest_method in {"POST", "DELETE"})


def implemented_surface_capabilities() -> tuple[ContractCapability, ...]:
    """Return registry rows whose MCP parity status is implemented or partial."""
    return tuple(c for c in _CAPABILITIES if c.parity_status in {"MCP implemented", "MCP partial"})


def mcp_capabilities() -> tuple[ContractCapability, ...]:
    """Return registry rows with a concrete MCP tool."""
    return tuple(c for c in implemented_surface_capabilities() if c.mcp_tool is not None)


def cli_capabilities() -> tuple[ContractCapability, ...]:
    """Return registry rows with a concrete CLI command."""
    return tuple(c for c in implemented_surface_capabilities() if c.cli_tokens is not None)


def control_capabilities() -> tuple[ContractCapability, ...]:
    """Return capabilities that require an Idempotency-Key + support If-Match.

    Excludes create/adopt because those have a different shape from versioned
    control operations.
    """
    return tuple(
        c
        for c in _CAPABILITIES
        if c.requires_idempotency_key
        and c.supports_if_match
        and c.name
        in {
            "cancel_workspace",
            "stop_workspace",
            "destroy_workspace",
            "remonitor_workspace",
            "request_validation",
            "refresh_workspace",
            "rebase_workspace",
        }
    )


# ── Parity matrix cross-validation ────────────────────────────────────────


def _parity_row_for(capability_name: str) -> dict[str, str]:
    rows = _parity_rows()
    for row in rows:
        if row.get("Capability", "").strip() == capability_name:
            return row
    raise AssertionError(
        f"Parity matrix has no row for capability {capability_name!r}; "
        "harness registry would drift from docs/MCP_CLIENT_PARITY.md"
    )


def _row_endpoints(row: Mapping[str, str]) -> list[tuple[str, str]]:
    return _extract_rest_endpoints_from_cell(row.get("Canonical REST surface", ""))


def _row_mcp_tools(row: Mapping[str, str]) -> set[str]:
    return set(_extract_mcp_tool_tokens_from_cell(row.get("MCP tool name", "")))


def _row_cli_surface(row: Mapping[str, str]) -> str:
    return _strip_backticks(row.get("CLI surface", "")).strip()


def assert_capability_matches_parity_matrix(capability: ContractCapability) -> None:
    """Hard-fail when the registry has drifted from the parity matrix.

    This is the trip-wire that prevents silent divergence between this harness
    and ``docs/MCP_CLIENT_PARITY.md``.
    """
    row = _parity_row_for(capability.parity_capability)

    endpoints = _row_endpoints(row)
    expected_endpoint = (capability.rest_method, capability.rest_path)
    assert expected_endpoint in endpoints, (
        f"Capability {capability.name!r} declares "
        f"{capability.rest_method} {capability.rest_path}, but parity matrix row "
        f"{capability.parity_capability!r} lists endpoints {endpoints!r}."
    )

    if capability.mcp_tool is not None:
        tools = _row_mcp_tools(row)
        assert capability.mcp_tool in tools, (
            f"Capability {capability.name!r} declares MCP tool "
            f"{capability.mcp_tool!r}, but parity matrix row "
            f"{capability.parity_capability!r} lists tools {sorted(tools)!r}."
        )

    matrix_status = row.get("Status", "").strip()
    assert matrix_status == capability.parity_status, (
        f"Capability {capability.name!r} declares Status "
        f"{capability.parity_status!r} but parity matrix row "
        f"{capability.parity_capability!r} has Status {matrix_status!r}."
    )

    matrix_backlog = _strip_backticks(row.get("Backlog Slice", "")).strip()
    declared = capability.parity_backlog_slice.strip()
    assert matrix_backlog == declared, (
        f"Capability {capability.name!r} declares Backlog Slice "
        f"{declared!r} but parity matrix row "
        f"{capability.parity_capability!r} has Backlog Slice {matrix_backlog!r}."
    )


# ── Response / error envelope normalizers ─────────────────────────────────


def normalize_rest_error_body(body: Any) -> dict[str, Any]:
    """Bring REST error bodies to a single shape.

    REST emits two envelope shapes for errors:

    1. FastAPI ``HTTPException(detail=<dict>)`` → ``{"detail": <dict>}`` —
       used by the controls router and most route-level error mapping.
    2. ``JSONResponse(content=ErrorResponse(...).model_dump())`` → top-level
       ``{"error_code", "message", "detail"}`` — used by the workspace create
       routes and a few dedicated error paths.

    Both are valid and end-to-end-equivalent — operators read ``error_code`` and
    ``message`` from the response. The harness collapses them into a single
    ``{"error_code", "message", "detail"}`` shape so cross-client assertions can
    be written once.
    """
    if not isinstance(body, dict):
        raise AssertionError(f"REST error body is not a dict: {body!r}")
    if "error_code" in body and "message" in body:
        return _ensure_envelope(body)
    inner = body.get("detail")
    if isinstance(inner, dict) and "error_code" in inner:
        return _ensure_envelope(inner)
    raise AssertionError(f"REST error body has no recognizable error envelope: {body!r}")


def normalize_mcp_error_body(structured: Any) -> dict[str, Any]:
    """Bring MCP structured error content to the same envelope as REST."""
    if not isinstance(structured, dict):
        raise AssertionError(f"MCP structured error is not a dict: {structured!r}")
    if "error_code" not in structured or "message" not in structured:
        raise AssertionError(f"MCP structured error missing error_code/message: {structured!r}")
    return _ensure_envelope(structured)


def _ensure_envelope(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "error_code": body.get("error_code"),
        "message": body.get("message"),
        "detail": body.get("detail"),
    }


def assert_envelope_shape(envelope: Mapping[str, Any]) -> None:
    assert isinstance(envelope.get("error_code"), str), envelope
    assert isinstance(envelope.get("message"), str), envelope
    detail = envelope.get("detail")
    assert detail is None or isinstance(detail, dict), envelope


def collect_known_error_codes() -> frozenset[str]:
    """Return the union of error codes declared by registered capabilities."""
    codes: set[str] = set()
    for capability in _CAPABILITIES:
        codes |= capability.error_codes
    return frozenset(codes)


def parity_matrix_error_codes(parity_capability: str) -> frozenset[str]:
    """Return the error codes mentioned in the parity matrix's Schema column."""
    row = _parity_row_for(parity_capability)
    cleaned = _strip_backticks(row.get("Schema / Error-Code Contract", ""))
    codes: set[str] = set()
    for raw in re.split(r"[;,]\s*", cleaned):
        token = raw.strip()
        if token and re.fullmatch(r"[A-Z][A-Z0-9_]+", token) and "_" in token:
            codes.add(token)
    return frozenset(codes)


def parity_capabilities_with_status(statuses: Iterable[str]) -> list[str]:
    """Return parity-matrix capability names whose Status is in ``statuses``."""
    rows = _parity_rows()
    target = set(statuses)
    return [row["Capability"].strip() for row in rows if row.get("Status", "").strip() in target]


def parity_mutating_capabilities_with_status(statuses: Iterable[str]) -> list[str]:
    """Return parity-matrix capabilities with a POST/DELETE endpoint and matching status."""
    rows = _parity_rows()
    target = set(statuses)
    out: list[str] = []
    for row in rows:
        if row.get("Status", "").strip() not in target:
            continue
        endpoints = _row_endpoints(row)
        if any(method in {"POST", "DELETE"} for method, _ in endpoints):
            out.append(row["Capability"].strip())
    return out


def parity_endpoint_capabilities_with_status(statuses: Iterable[str]) -> list[str]:
    """Return parity-matrix capability names with REST endpoints and matching status."""
    rows = _parity_rows()
    target = set(statuses)
    out: list[str] = []
    for row in rows:
        if row.get("Status", "").strip() not in target:
            continue
        if _row_endpoints(row):
            out.append(row["Capability"].strip())
    return out


def parity_rows_by_capability() -> dict[str, dict[str, str]]:
    """Return parity-matrix rows keyed by capability name."""
    return {row["Capability"].strip(): row for row in _parity_rows()}


def parity_capability_cli_surface(parity_capability: str) -> str:
    """Return the parity-matrix CLI column for a capability."""
    return _row_cli_surface(_parity_row_for(parity_capability))
