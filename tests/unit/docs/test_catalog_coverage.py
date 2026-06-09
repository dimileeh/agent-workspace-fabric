import ast
import re
from pathlib import Path

import pytest

from awf.service.doctor.reasons import _REASON_TEXT

CATALOG_PATH = Path(__file__).parent.parent.parent.parent / "docs" / "REASON_CATALOG.md"
SRC_AWF_PATH = Path(__file__).parent.parent.parent.parent / "src" / "awf"

ALLOWLIST = {
    # Reason codes that are intentionally omitted from the public catalog.
    "DOCKER_OK": "Success states do not need failure documentation.",
    "API_OK": "Success states do not need failure documentation.",
    "WORKER_RUNNING": "Success states do not need failure documentation.",
    "GITHUB_AUTH_OK": "Success states do not need failure documentation.",
    "CODEX_AUTH_OK": "Success states do not need failure documentation.",
    "CLAUDE_CODE_AUTH_OK": "Success states do not need failure documentation.",
    "CURSOR_AUTH_OK": "Success states do not need failure documentation.",
    "GEMINI_AUTH_OK": "Success states do not need failure documentation.",
    "OPENCODE_AUTH_OK": "Success states do not need failure documentation.",
    "GROK_AUTH_OK": "Success states do not need failure documentation.",
    "PORT_OPEN": "Success states do not need failure documentation.",
    "SUFFICIENT_DISK": "Success states do not need failure documentation.",
    "NO_STRANDED_WORKSPACES": "Success states do not need failure documentation.",
    "NO_ORPHANS": "Success states do not need failure documentation.",
    "NETWORK_POSTURE_NO_ACTIVE_OPEN": "Success states do not need failure documentation.",
    "LOCAL_CONFIG_OK": "Success states do not need failure documentation.",
    # Grandfathered missing error codes to allow the expanded coverage gate to pass.
    # These should ideally be documented in the future.
    "API_TOKEN_NOT_CONFIGURED": "Grandfathered",
    "CALLBACKS_DISABLED": "Grandfathered",
    "CALLBACK_DISABLED": "Grandfathered",
    "CALLBACK_REQUEST_FAILED": "Grandfathered",
    "f'CALLBACK_HTTP_{result.status_code}'": "Grandfathered",
    "CLEANUP_FAILED": "Grandfathered",
    "GITHUB_ERROR": "Grandfathered",
    "GITHUB_MERGE_FAILED": "Grandfathered",
    "GITHUB_TRANSIENT_ERROR": "Grandfathered",
    "GIT_PUSH_FAILED": "Grandfathered",
    "IDEMPOTENCY_CONFLICT": "Grandfathered",
    "INVALID_ARTIFACT_PATH": "Grandfathered",
    "INVALID_CURSOR": "Grandfathered",
    "INVALID_PROFILE": "Grandfathered",
    "INVALID_REQUEST": "Grandfathered",
    "LOG_FILE_MISSING": "Grandfathered",
    "MERGE_CANDIDATE_NOT_FOUND": "Grandfathered",
    "MONITOR_RECOVERY_FAILED": "Grandfathered",
    "MONITOR_RECOVERY_NO_EXECUTOR": "Grandfathered",
    "MONITOR_RECOVERY_REBASE_FAILED": "Grandfathered",
    "NOT_FOUND": "Grandfathered",
    "OPERATOR_REMONITOR": "Grandfathered",
    "PROVIDER_FALLBACK": "Grandfathered",
    "PROVIDER_OUTAGE": "Grandfathered",
    "PROVIDER_READINESS_PRECHECK_FAILED": "Grandfathered",
    "STACK_STOP_FAILED": "Grandfathered",
    "TASK_EXTERNAL_ID_CONFLICT": "Grandfathered",
    "UNAUTHORIZED": "Grandfathered",
    "VERSION_CONFLICT": "Grandfathered",
    "WORKSPACE_ACTIVE": "Grandfathered",
    "WORKSPACE_NOT_FOUND": "Grandfathered",
    "WORKSPACE_NOT_RETRYABLE": "Grandfathered",
    "WORKSPACE_OPERATION_CONFLICT": "Grandfathered",
    "WORKSPACE_PR_URL_REQUIRED": "Grandfathered",
    "WORKSPACE_RETRY_ERROR": "Grandfathered",
    "WORKSPACE_RETRY_EXHAUSTED": "Grandfathered",
    "WORKSPACE_RETRY_SALVAGE_UNAVAILABLE": "Grandfathered",
    "WORKSPACE_STATE_NOT_REBASEABLE": "Grandfathered",
    "WORKSPACE_STATE_NOT_REFRESHABLE": "Grandfathered",
    "WORKSPACE_STATE_NOT_REMONITORABLE": "Grandfathered",
    "WORKSPACE_STATE_NOT_VALIDATABLE": "Grandfathered",
}


def _extract_reason_code_value(
    val_node: ast.expr,
    file_constants: dict[str, str],
) -> str | None:
    if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
        return val_node.value
    if isinstance(val_node, ast.Name) and val_node.id in file_constants:
        return file_constants[val_node.id]
    if isinstance(val_node, ast.JoinedStr):
        return ast.unparse(val_node)
    return None


def test_catalog_coverage() -> None:
    """Ensure all public reason codes are documented in the REASON_CATALOG.md."""
    if not CATALOG_PATH.exists():
        pytest.fail(f"Catalog file {CATALOG_PATH} does not exist.")

    catalog_content = CATALOG_PATH.read_text()

    # Find all headings like `### DOCKER_CLI_NOT_FOUND`
    documented_codes: set[str] = set(
        re.findall(r"^###\s+([A-Z0-9_]+)", catalog_content, re.MULTILINE)
    )

    all_reason_codes = set(_REASON_TEXT.keys())

    # Dynamically scan source for other literal error_code assignments
    for py_file in SRC_AWF_PATH.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())

            # Map file-level constants to their string values
            file_constants: dict[str, str] = {}
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            file_constants[target.id] = node.value.value

            for node in ast.walk(tree):
                # keyword arg: error_code="XYZ" or error_code=CONSTANT
                if isinstance(node, ast.keyword) and node.arg == "error_code":
                    extracted = _extract_reason_code_value(node.value, file_constants)
                    if extracted:
                        all_reason_codes.add(extracted)

                # assignment: error_code = "XYZ" or self.error_code = "XYZ"
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (isinstance(target, ast.Name) and target.id == "error_code") or (
                            isinstance(target, ast.Attribute) and target.attr == "error_code"
                        ):
                            extracted = _extract_reason_code_value(
                                node.value,
                                file_constants,
                            )
                            if extracted:
                                all_reason_codes.add(extracted)

                # dict key: {"error_code": "XYZ"} or {"error_code": CONSTANT}
                elif isinstance(node, ast.Dict):
                    for key, val in zip(node.keys, node.values, strict=False):
                        if isinstance(key, ast.Constant) and key.value == "error_code":
                            extracted = _extract_reason_code_value(val, file_constants)
                            if extracted:
                                all_reason_codes.add(extracted)
        except SyntaxError:
            pass

    missing_docs = []
    for code in sorted(all_reason_codes):
        if code in ALLOWLIST:
            continue
        if code not in documented_codes:
            missing_docs.append(code)

    assert not missing_docs, (
        f"The following reason codes are missing from {CATALOG_PATH}: {', '.join(missing_docs)}"
    )

    documented_but_allowlisted = [
        code
        for code in ALLOWLIST
        if code in documented_codes and ALLOWLIST[code] == "Grandfathered"
    ]
    assert not documented_but_allowlisted, (
        f"The following reason codes are fully documented but still in ALLOWLIST (Grandfathered): {', '.join(documented_but_allowlisted)}"
    )
