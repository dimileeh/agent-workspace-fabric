"""Cross-client capability registry and shared normalizers.

The contract harness encodes the canonical operator surfaces that REST, CLI, and
MCP all expose. Every entry maps a single operator capability to:

- the canonical REST endpoint (``method`` + ``path``);
- the MCP tool name when one exists (``mcp_tool``);
- the CLI invocation tokens when one exists (``cli_tokens``);
- the parity matrix Status / Backlog Slice columns at the time of writing
  (``parity_status``, ``parity_backlog_slice``).

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

from tests.unit.mcp._parity_utils import _parity_rows, _strip_backticks


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
    error_codes: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_mcp_implemented(self) -> bool:
        return self.parity_status == "MCP implemented"

    @property
    def is_mcp_partial(self) -> bool:
        return self.parity_status == "MCP partial"

    @property
    def is_mcp_missing(self) -> bool:
        return self.parity_status == "MCP missing/backlog"


_CAPABILITIES: tuple[ContractCapability, ...] = (
    ContractCapability(
        name="cancel_workspace",
        parity_capability="Cancel workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/cancel",
        mcp_tool="awf_cancel_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        error_codes=frozenset({"NOT_FOUND", "VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT"}),
    ),
    ContractCapability(
        name="stop_workspace",
        parity_capability="Stop workspace stack",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/stop",
        mcp_tool="awf_stop_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        error_codes=frozenset(
            {"NOT_FOUND", "VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT", "STACK_STOP_FAILED"}
        ),
    ),
    ContractCapability(
        name="destroy_workspace",
        parity_capability="Destroy workspace resources",
        rest_method="DELETE",
        rest_path="/v1/workspaces/{workspace_id}",
        mcp_tool="awf_destroy_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_ACTIVE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
                "STACK_STOP_FAILED",
            }
        ),
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
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_PR_URL_REQUIRED",
                "WORKSPACE_STATE_NOT_REMONITORABLE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
    ),
    ContractCapability(
        name="request_validation",
        parity_capability="Request validation",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/validate",
        mcp_tool="awf_request_workspace_validation",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_PR_URL_REQUIRED",
                "WORKSPACE_STATE_NOT_VALIDATABLE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
    ),
    ContractCapability(
        name="refresh_workspace",
        parity_capability="Refresh workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/refresh",
        mcp_tool="awf_refresh_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
        error_codes=frozenset(
            {
                "NOT_FOUND",
                "WORKSPACE_STATE_NOT_REFRESHABLE",
                "VERSION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
            }
        ),
    ),
    ContractCapability(
        name="rebase_workspace",
        parity_capability="Rebase workspace",
        rest_method="POST",
        rest_path="/v1/workspaces/{workspace_id}/rebase",
        mcp_tool="awf_rebase_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=True,
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
    ),
    ContractCapability(
        name="create_workspace_v1",
        parity_capability="Workspace create, list, and get",
        rest_method="POST",
        rest_path="/v1/workspaces",
        mcp_tool="awf_create_workspace",
        cli_tokens=None,
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=False,
        error_codes=frozenset({"IDEMPOTENCY_CONFLICT"}),
    ),
    ContractCapability(
        name="create_workspace_v2",
        parity_capability="Workspace create, list, and get",
        rest_method="POST",
        rest_path="/v2/workspaces",
        mcp_tool="awf_create_workspace_v2",
        cli_tokens=("workspace", "create"),
        parity_status="MCP implemented",
        parity_backlog_slice="—",
        supports_idempotency_key=True,
        supports_if_match=False,
        error_codes=frozenset(
            {"IDEMPOTENCY_CONFLICT", "INVALID_PROFILE", "TASK_EXTERNAL_ID_CONFLICT", "INSUFFICIENT_DISK"}
        ),
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
    ),
)


CAPABILITIES_BY_NAME: dict[str, ContractCapability] = {c.name: c for c in _CAPABILITIES}


def all_capabilities() -> tuple[ContractCapability, ...]:
    """Return the registered capabilities."""
    return _CAPABILITIES


def mutating_capabilities() -> tuple[ContractCapability, ...]:
    """Return mutating REST capabilities (POST/DELETE)."""
    return tuple(c for c in _CAPABILITIES if c.rest_method in {"POST", "DELETE"})


def control_capabilities() -> tuple[ContractCapability, ...]:
    """Return capabilities that take an Idempotency-Key + If-Match (control surfaces).

    Excludes create/adopt because those have a different shape (idempotency only,
    no If-Match).
    """
    return tuple(
        c
        for c in _CAPABILITIES
        if c.supports_idempotency_key
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


_REST_ENDPOINT_RE = re.compile(
    r"(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s,]+)",
)


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
    cleaned = _strip_backticks(row.get("Canonical REST surface", ""))
    return [(m.group(1), m.group(2).rstrip(",")) for m in _REST_ENDPOINT_RE.finditer(cleaned)]


def _row_mcp_tools(row: Mapping[str, str]) -> set[str]:
    cleaned = _strip_backticks(row.get("MCP tool name", ""))
    tools: set[str] = set()
    for raw in re.split(r",\s*", cleaned):
        token = raw.strip()
        if token.startswith("awf_"):
            tools.add(token)
    return tools


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
    raise AssertionError(
        f"REST error body has no recognizable error envelope: {body!r}"
    )


def normalize_mcp_error_body(structured: Any) -> dict[str, Any]:
    """Bring MCP structured error content to the same envelope as REST."""
    if not isinstance(structured, dict):
        raise AssertionError(f"MCP structured error is not a dict: {structured!r}")
    if "error_code" not in structured or "message" not in structured:
        raise AssertionError(
            f"MCP structured error missing error_code/message: {structured!r}"
        )
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
