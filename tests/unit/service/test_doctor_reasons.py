"""Tests for AWF Reason Catalog synchronization."""

from pathlib import Path

import pytest

from awf.host_setup.rendering import FIRST_RUN_FAILURE_REASON_CODES
from awf.service.doctor.reasons import _REASON_TEXT


@pytest.mark.unit
def test_protected_scope_terminal_reasons_have_doctor_guidance() -> None:
    expected_codes = {
        "PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        "PROTECTED_SCOPE_PUSH_BLOCKED",
    }

    missing_codes = expected_codes - set(_REASON_TEXT)

    assert not missing_codes
    for code in expected_codes:
        reason = _REASON_TEXT[code]
        assert reason.message
        assert reason.likely_cause
        assert reason.action
        assert reason.docs_link == f"docs/REASON_CATALOG.md#{code.lower()}"


@pytest.mark.unit
def test_first_run_failure_reasons_have_operator_guidance() -> None:
    missing_codes = set(FIRST_RUN_FAILURE_REASON_CODES) - set(_REASON_TEXT)

    assert not missing_codes
    for code in FIRST_RUN_FAILURE_REASON_CODES:
        reason = _REASON_TEXT[code]
        assert reason.message
        assert reason.likely_cause
        assert reason.action
        assert reason.docs_link


@pytest.mark.unit
def test_first_run_failure_reasons_are_documented() -> None:
    repo_root = Path(__file__).parent.parent.parent.parent
    catalog_content = (repo_root / "docs" / "REASON_CATALOG.md").read_text()

    for code in FIRST_RUN_FAILURE_REASON_CODES:
        assert f"### {code}" in catalog_content


def generate_expected_catalog(existing_content: str = "") -> str:
    lines = [
        "# AWF Reason and Error Code Catalog\n",
        "This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.\n",
    ]

    sections = {}
    current_section = None
    current_lines = []
    for line in existing_content.splitlines():
        if line.startswith("### "):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = line[4:].strip()
            current_lines = []
        elif current_section:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    reasons = {k: v for k, v in _REASON_TEXT.items() if v.likely_cause and v.action and v.message}
    for key, val in reasons.items():
        block_lines = []
        block_lines.append(f"**Problem:** {val.message}")
        block_lines.append(f"**Likely Cause:** {val.likely_cause}")
        block_lines.append(f"**Operator Fix:** {val.action}")
        if val.related_command:
            block_lines.append(f"**Related Command:** `{val.related_command}`")
        if val.docs_link:
            if val.docs_link.startswith("http"):
                block_lines.append(f"**Docs Link:** [{val.docs_link}]({val.docs_link})")
            elif val.docs_link.startswith("docs/REASON_CATALOG.md#"):
                anchor = val.docs_link.split("#", 1)[1]
                block_lines.append(f"**Docs Link:** [{val.docs_link}](#{anchor})")
            else:
                block_lines.append(f"**Docs Link:** [{val.docs_link}]({val.docs_link})")
        sections[key] = "\n".join(block_lines)

    for key in sorted(sections.keys()):
        lines.append(f"### {key}")
        lines.append(sections[key])
        lines.append("")

    return "\n".join(lines)


@pytest.mark.unit
def test_reason_catalog_is_synchronized_with_python_source() -> None:
    repo_root = Path(__file__).parent.parent.parent.parent
    catalog_path = repo_root / "docs" / "REASON_CATALOG.md"

    actual_content = catalog_path.read_text()
    expected_content = generate_expected_catalog(actual_content)

    assert actual_content.strip() == expected_content.strip(), (
        "docs/REASON_CATALOG.md is out of sync with src/awf/service/doctor/reasons.py. "
        "Please run `uv run python scripts/generate_reason_catalog.py` to update it."
    )
