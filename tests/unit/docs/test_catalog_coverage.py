import re
from pathlib import Path

import pytest

from awf.service.doctor.reasons import _REASON_TEXT

CATALOG_PATH = Path(__file__).parent.parent.parent.parent / "docs" / "REASON_CATALOG.md"

ALLOWLIST = {
    # Reason codes that are intentionally omitted from the public catalog.
    "DOCKER_OK": "Success states do not need failure documentation.",
    "API_OK": "Success states do not need failure documentation.",
    "WORKER_RUNNING": "Success states do not need failure documentation.",
    "GITHUB_AUTH_OK": "Success states do not need failure documentation.",
    "CODEX_AUTH_OK": "Success states do not need failure documentation.",
    "CLAUDE_CODE_AUTH_OK": "Success states do not need failure documentation.",
    "GEMINI_AUTH_OK": "Success states do not need failure documentation.",
    "OPENCODE_AUTH_OK": "Success states do not need failure documentation.",
    "PORT_OPEN": "Success states do not need failure documentation.",
    "SUFFICIENT_DISK": "Success states do not need failure documentation.",
    "NO_STRANDED_WORKSPACES": "Success states do not need failure documentation.",
    "NO_ORPHANS": "Success states do not need failure documentation.",
    "NETWORK_POSTURE_NO_ACTIVE_OPEN": "Success states do not need failure documentation.",
    "LOCAL_CONFIG_OK": "Success states do not need failure documentation.",
}

def test_catalog_coverage() -> None:
    """Ensure all public reason codes are documented in the REASON_CATALOG.md."""
    if not CATALOG_PATH.exists():
        pytest.fail(f"Catalog file {CATALOG_PATH} does not exist.")

    catalog_content = CATALOG_PATH.read_text()

    # Find all headings like `### DOCKER_CLI_NOT_FOUND`
    documented_codes: set[str] = set(re.findall(r"^###\s+([A-Z0-9_]+)", catalog_content, re.MULTILINE))

    all_reason_codes = set(_REASON_TEXT.keys())

    missing_docs = []
    for code in all_reason_codes:
        if code in ALLOWLIST:
            continue
        if code not in documented_codes:
            missing_docs.append(code)

    assert not missing_docs, f"The following reason codes are missing from {CATALOG_PATH}: {', '.join(missing_docs)}"

