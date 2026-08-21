"""Remonitor Compose runtime availability helper tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.service.controls_helpers import (
    _remonitor_missing_metadata,
    remonitor_compose_runtime_available,
)


def _workspace(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "pr_number": 1,
        "task_kind": "feature_branch_pr",
        "branch_name": "awf/ws_1",
        "remote_push_branch": "awf/ws_1",
        "compose_project_name": "awf_ws_1",
        "compose_file_path": None,
        "task_policy": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_remonitor_compose_runtime_available_requires_existing_compose_file(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "gone" / "compose.yml"
    assert not remonitor_compose_runtime_available(
        _workspace(compose_file_path=str(missing_path))  # type: ignore[arg-type]
    )

    present = tmp_path / "compose.yml"
    present.write_text("services: {}\n", encoding="utf-8")
    assert remonitor_compose_runtime_available(
        _workspace(compose_file_path=str(present))  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_remonitor_compose_runtime_available_hosted_skips_compose_checks() -> None:
    assert remonitor_compose_runtime_available(
        _workspace(  # type: ignore[arg-type]
            compose_project_name=None,
            compose_file_path=None,
            task_policy={"pr_adoption": {"execution": {"mode": "hosted"}}},
        )
    )


@pytest.mark.unit
def test_remonitor_missing_metadata_marks_nonexistent_compose_file(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "removed" / "compose.yml"
    assert _remonitor_missing_metadata(
        _workspace(compose_file_path=str(missing_path))  # type: ignore[arg-type]
    ) == ["compose_file_path"]
