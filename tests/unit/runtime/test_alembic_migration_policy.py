"""Profile-gated Alembic migration-chain policy tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.validation import ValidationRunner

_COMPOSE_PROJECT = "awf_ws_alembic_policy"
_COMPOSE_FILE = Path("/fake/compose.yml")


def _write_alembic_ini(repo: Path) -> None:
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "alembic.ini").write_text(
        "[alembic]\nscript_location = migrations\nversion_path_separator = os\n",
        encoding="utf-8",
    )


def _write_revision(
    repo: Path,
    revision: str,
    down_revision: str | None,
    *,
    name: str | None = None,
    branch_labels: str | tuple[str, ...] | None = None,
) -> None:
    down_revision_literal = "None" if down_revision is None else repr(down_revision)
    branch_labels_literal = "None" if branch_labels is None else repr(branch_labels)
    (repo / "migrations" / "versions" / f"{revision}_{name or revision}.py").write_text(
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision_literal}\n"
        f"branch_labels = {branch_labels_literal}\n"
        "depends_on = None\n",
        encoding="utf-8",
    )


def _policy_profile(**alembic: object) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "python-db",
            "phases": {"validate": ["pytest -q"]},
            "validation": {"alembic": {"enabled": True, **alembic}},
        }
    )


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


@pytest.mark.unit
async def test_enabled_policy_blocks_multiple_heads_before_validation_commands(
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
) -> None:
    fake, validator = runner
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "left001", "base001")
    _write_revision(tmp_path, "right001", "base001")

    result = await validator.run_profile_phases(
        workspace_id="ws_multiple_heads",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert not result.all_passed
    assert result.first_failure is not None
    assert result.first_failure.phase == "migration_policy"
    assert result.first_failure.reason_code == "ALEMBIC_MULTIPLE_HEADS"
    assert result.first_failure.policy_failed is True
    assert result.first_failure.metadata["heads"] == ["left001", "right001"]
    assert result.first_failure.metadata["findings"][0]["reason_code"] == ("ALEMBIC_MULTIPLE_HEADS")
    assert (
        json.loads(result.first_failure.stderr_path.read_text(encoding="utf-8"))["reason_code"]
        == "ALEMBIC_MULTIPLE_HEADS"
    )
    assert fake.calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fixture_name", "expected_reason", "expected_detail"),
    [
        (
            "missing_down_revision",
            "ALEMBIC_MISSING_DOWN_REVISION",
            ("missing_down_revisions", ["missing001"]),
        ),
        (
            "duplicate_revision",
            "ALEMBIC_DUPLICATE_REVISION",
            ("duplicate_revisions", ["dup001"]),
        ),
        (
            "branch_label_anomaly",
            "ALEMBIC_BRANCH_LABEL_ANOMALY",
            ("duplicate_branch_labels", {"shared": ["base001", "other001"]}),
        ),
    ],
)
async def test_enabled_policy_returns_structured_failure_for_graph_anomalies(
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
    fixture_name: str,
    expected_reason: str,
    expected_detail: tuple[str, object],
) -> None:
    fake, validator = runner
    _write_alembic_ini(tmp_path)
    if fixture_name == "missing_down_revision":
        _write_revision(tmp_path, "orphan001", "missing001")
    elif fixture_name == "duplicate_revision":
        _write_revision(tmp_path, "dup001", None, name="left")
        _write_revision(tmp_path, "dup001", None, name="right")
    else:
        _write_revision(tmp_path, "base001", None, branch_labels="shared")
        _write_revision(tmp_path, "other001", None, branch_labels="shared")

    result = await validator.run_profile_phases(
        workspace_id=f"ws_{fixture_name}",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert not result.all_passed
    assert result.first_failure is not None
    assert result.first_failure.reason_code == expected_reason
    key, value = expected_detail
    assert result.first_failure.metadata["details"][key] == value
    assert fake.calls == []


@pytest.mark.unit
async def test_clean_single_head_policy_logs_pass_then_runs_validation_command(
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
) -> None:
    fake, validator = runner
    fake.queue_result(returncode=0, stdout="tests ok")
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "head001", "base001")

    result = await validator.run_profile_phases(
        workspace_id="ws_clean_chain",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert result.all_passed
    assert [(command.phase, command.reason_code) for command in result.commands] == [
        ("migration_policy", "ALEMBIC_GRAPH_OK"),
        ("validate", "COMMAND_FAILED"),
    ]
    assert result.commands[0].stdout_path.name == "01_migration_policy.stdout"
    assert (
        json.loads(result.commands[0].stdout_path.read_text(encoding="utf-8"))["reason_code"]
        == "ALEMBIC_GRAPH_OK"
    )
    assert len(fake.calls) == 1
    assert "pytest -q" in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_enabled_policy_runs_graph_scan_and_artifact_writes_off_loop(
    monkeypatch: pytest.MonkeyPatch,
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
) -> None:
    fake, validator = runner
    fake.queue_result(returncode=0, stdout="tests ok")
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "head001", "base001")
    to_thread_calls: list[str] = []

    async def fake_to_thread(func: Callable[..., object], /, *args: object) -> object:
        to_thread_calls.append(func.__name__)
        return func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = await validator.run_profile_phases(
        workspace_id="ws_clean_chain_threaded_io",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert result.all_passed
    assert to_thread_calls == [
        "validate_alembic_migration_chain",
        "_write_alembic_policy_artifacts",
    ]


@pytest.mark.unit
async def test_disabled_policy_does_not_inspect_alembic_graph(
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
) -> None:
    fake, validator = runner
    fake.queue_result(returncode=0, stdout="tests ok")
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "left001", "base001")
    _write_revision(tmp_path, "right001", "base001")
    profile = WorkspaceProfile.model_validate(
        {"name": "python-db", "phases": {"validate": ["pytest -q"]}}
    )

    result = await validator.run_profile_phases(
        workspace_id="ws_disabled",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=profile,
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert result.all_passed
    assert [command.phase for command in result.commands] == ["validate"]
    assert len(fake.calls) == 1


@pytest.mark.unit
async def test_enabled_policy_can_skip_unconfigured_alembic_repo(
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
) -> None:
    fake, validator = runner
    fake.queue_result(returncode=0, stdout="tests ok")

    result = await validator.run_profile_phases(
        workspace_id="ws_unconfigured_skip",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(fail_on_unconfigured=False),
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert result.all_passed
    assert [(command.phase, command.reason_code) for command in result.commands] == [
        ("migration_policy", "ALEMBIC_NOT_CONFIGURED"),
        ("validate", "COMMAND_FAILED"),
    ]
    assert result.commands[0].metadata["status"] == "unsupported"
    assert len(fake.calls) == 1


@pytest.mark.unit
async def test_enabled_policy_fails_unconfigured_alembic_repo_by_default(
    runner: tuple[FakeCommandRunner, ValidationRunner],
    tmp_path: Path,
) -> None:
    fake, validator = runner

    result = await validator.run_profile_phases(
        workspace_id="ws_unconfigured_fail",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=tmp_path,
    )

    assert not result.all_passed
    assert result.first_failure is not None
    assert result.first_failure.reason_code == "ALEMBIC_NOT_CONFIGURED"
    assert result.first_failure.metadata["status"] == "unsupported"
    assert fake.calls == []


@pytest.mark.unit
async def test_enabled_policy_requires_worktree_path(
    runner: tuple[FakeCommandRunner, ValidationRunner],
) -> None:
    fake, validator = runner

    result = await validator.run_profile_phases(
        workspace_id="ws_missing_worktree",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
    )

    assert not result.all_passed
    assert result.first_failure is not None
    assert result.first_failure.reason_code == "ALEMBIC_WORKTREE_REQUIRED"
    assert result.first_failure.metadata["policy"]["enabled"] is True
    assert fake.calls == []


@pytest.mark.unit
async def test_policy_writes_durable_stdout_and_stderr_log_streams(tmp_path: Path) -> None:
    log_store = LogStore(root=tmp_path / "logs")
    fake = FakeCommandRunner()
    validator = ValidationRunner(
        runner=fake,
        artifacts_dir=tmp_path / "artifacts",
        log_store=log_store,
    )

    clean_repo = tmp_path / "clean"
    _write_alembic_ini(clean_repo)
    _write_revision(clean_repo, "base001", None)
    _write_revision(clean_repo, "head001", "base001")
    fake.queue_result(returncode=0, stdout="tests ok")
    clean = await validator.run_profile_phases(
        workspace_id="ws_policy_stdout",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=clean_repo,
    )

    broken_repo = tmp_path / "broken"
    _write_alembic_ini(broken_repo)
    _write_revision(broken_repo, "base001", None)
    _write_revision(broken_repo, "left001", "base001")
    _write_revision(broken_repo, "right001", "base001")
    broken = await validator.run_profile_phases(
        workspace_id="ws_policy_stderr",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_policy_profile(),
        phase_names=("validate",),
        worktree_path=broken_repo,
    )

    assert clean.all_passed
    assert not broken.all_passed
    assert (
        tmp_path / "logs" / "ws_policy_stdout" / "validation.01_migration_policy.stdout.log"
    ).read_text(encoding="utf-8").find("ALEMBIC_GRAPH_OK") >= 0
    assert (
        tmp_path / "logs" / "ws_policy_stderr" / "validation.01_migration_policy.stderr.log"
    ).read_text(encoding="utf-8").find("ALEMBIC_MULTIPLE_HEADS") >= 0
