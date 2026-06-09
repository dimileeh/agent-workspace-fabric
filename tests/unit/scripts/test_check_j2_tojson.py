"""Tests for the structured-config Jinja2 escaping guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_j2_tojson.py"


def _run_checker(*paths: Path) -> subprocess.CompletedProcess[str]:
    """Run the Jinja2 tojson checker in a subprocess."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(path) for path in paths)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def _write_template(path: Path, content: str) -> Path:
    """Write a fixture template and return its path."""
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.unit
def test_checker_fails_on_raw_scalar_value_interpolation(tmp_path: Path) -> None:
    """A raw value interpolation in structured YAML is reported with path and line."""
    template = _write_template(
        tmp_path / "unsafe.yml.j2",
        'services:\n  app:\n    image: "{{ image }}"\n',
    )

    result = _run_checker(template)

    assert result.returncode == 1
    assert f"::error file={template},line=3,title=Jinja2 raw interpolation::" in result.stderr
    assert f"{template}:3:" in result.stderr
    assert "image" in result.stderr
    assert "tojson" in result.stderr


@pytest.mark.unit
def test_checker_passes_escaped_scalar_value_interpolation(tmp_path: Path) -> None:
    """A value interpolation ending in the approved tojson filter is accepted."""
    template = _write_template(
        tmp_path / "safe.yml.j2",
        "services:\n  app:\n    image: {{ image | tojson }}\n",
    )

    result = _run_checker(template)

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.unit
def test_checker_requires_tojson_as_the_final_filter(tmp_path: Path) -> None:
    """A nested or non-final tojson filter does not count as scalar escaping."""
    template = _write_template(
        tmp_path / "non_final.yml.j2",
        "services:\n  app:\n    image: {{ image | tojson | trim }}\n",
    )

    result = _run_checker(template)

    assert result.returncode == 1
    assert f"{template}:3:" in result.stderr
    assert "image | tojson | trim" in result.stderr


@pytest.mark.unit
def test_checker_ignores_control_blocks_loop_targets_and_comments(tmp_path: Path) -> None:
    """Only output expressions are checked; Jinja blocks and comments are ignored."""
    template = _write_template(
        tmp_path / "blocks.yml.j2",
        (
            "{% for image in images %}\n"
            "{# image: {{ raw_comment_value }} #}\n"
            "services:\n"
            "  app:\n"
            "    image: {{ image | tojson }}\n"
            "{% endfor %}\n"
        ),
    )

    result = _run_checker(template)

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.unit
def test_checker_allows_documented_inline_exceptions(tmp_path: Path) -> None:
    """A raw interpolation can be allowed only by expression plus rationale."""
    template = _write_template(
        tmp_path / "allowed.yml.j2",
        (
            "{# awf-j2-tojson-allow: workspace_id -- AWF-generated identifier. #}\n"
            'name: "awf-{{ workspace_id }}-agent"\n'
        ),
    )

    result = _run_checker(template)

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.unit
def test_checker_flags_duplicate_allowlist_entries(tmp_path: Path) -> None:
    """Duplicate allowlist entries are reported even when the expression is used."""
    template = _write_template(
        tmp_path / "duplicate_allow.yml.j2",
        (
            "{# awf-j2-tojson-allow: workspace_id -- AWF-generated identifier. #}\n"
            "{# awf-j2-tojson-allow: workspace_id -- Duplicate copy. #}\n"
            'name: "awf-{{ workspace_id }}-agent"\n'
        ),
    )

    result = _run_checker(template)

    assert result.returncode == 1
    assert f"{template}:2:" in result.stderr
    assert "duplicate allowlist entry" in result.stderr
    assert "workspace_id" in result.stderr


@pytest.mark.unit
def test_checker_flags_allowlist_entries_without_rationale(tmp_path: Path) -> None:
    """Allowlist directives must include a non-empty human rationale."""
    template = _write_template(
        tmp_path / "missing_reason.yml.j2",
        ('{# awf-j2-tojson-allow: workspace_id #}\nname: "awf-{{ workspace_id }}-agent"\n'),
    )

    result = _run_checker(template)

    assert result.returncode == 1
    assert f"{template}:1:" in result.stderr
    assert "missing a rationale" in result.stderr


@pytest.mark.unit
def test_checker_flags_stale_allowlist_entries(tmp_path: Path) -> None:
    """Allowlist entries that no longer match a raw interpolation are stale."""
    template = _write_template(
        tmp_path / "stale.yml.j2",
        (
            "{# awf-j2-tojson-allow: workspace_id -- Former raw identifier. #}\n"
            "name: {{ workspace_id | tojson }}\n"
        ),
    )

    result = _run_checker(template)

    assert result.returncode == 1
    assert f"{template}:1:" in result.stderr
    assert "stale allowlist entry" in result.stderr


@pytest.mark.unit
def test_checker_passes_repository_compose_templates_by_default() -> None:
    """Running with no paths scans the tracked docker/compose Jinja2 templates."""
    result = _run_checker()

    assert result.returncode == 0
    assert result.stderr == ""
