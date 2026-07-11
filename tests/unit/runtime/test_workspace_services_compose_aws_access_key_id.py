"""Focused hosted env regressions for AWS access key identifiers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import (
    filter_hosted_env_passthrough_names,
    literal_profile_env_from_compose,
)


def _compose_with_agent_env(tmp_path: Path, environment: dict[str, str]) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {"services": {"agent": {"image": "agent:latest", "environment": environment}}}
        ),
        encoding="utf-8",
    )
    return compose_file


@pytest.mark.unit
def test_literal_aws_access_key_id_without_matching_worker_value_is_not_carried(
    tmp_path: Path,
) -> None:
    compose_file = _compose_with_agent_env(
        tmp_path,
        {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "secret-profile-value",
        },
    )
    worker_env = {"AWS_ACCESS_KEY_ID": "AKIADIFFERENT"}

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    filtered = filter_hosted_env_passthrough_names(
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        compose_file=compose_file,
        worker_env=worker_env,
    )

    assert "AWS_ACCESS_KEY_ID" not in profile_env
    assert "AWS_SECRET_ACCESS_KEY" not in profile_env
    assert "AWS_ACCESS_KEY_ID" not in filtered


@pytest.mark.unit
def test_literal_aws_access_key_id_with_same_worker_value_stays_name_only(
    tmp_path: Path,
) -> None:
    access_key_id = "AKIAIOSFODNN7EXAMPLE"
    compose_file = _compose_with_agent_env(tmp_path, {"AWS_ACCESS_KEY_ID": access_key_id})

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            worker_env={"AWS_ACCESS_KEY_ID": access_key_id},
        )
    )
    filtered = filter_hosted_env_passthrough_names(
        ("AWS_ACCESS_KEY_ID",),
        compose_file=compose_file,
        worker_env={"AWS_ACCESS_KEY_ID": access_key_id},
    )

    assert "AWS_ACCESS_KEY_ID" not in profile_env
    assert "AWS_ACCESS_KEY_ID" in filtered


@pytest.mark.unit
def test_empty_aws_access_key_id_does_not_reenable_worker_identifier(
    tmp_path: Path,
) -> None:
    compose_file = _compose_with_agent_env(
        tmp_path,
        {
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
        },
    )
    worker_env = {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "secret-worker-value",
    }

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    filtered = filter_hosted_env_passthrough_names(
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        compose_file=compose_file,
        worker_env=worker_env,
    )

    assert "AWS_ACCESS_KEY_ID" not in profile_env
    assert "AWS_ACCESS_KEY_ID" not in filtered
    assert "AWS_SECRET_ACCESS_KEY" in filtered


@pytest.mark.unit
def test_unset_aws_access_key_id_slot_does_not_become_name_only(
    tmp_path: Path,
) -> None:
    compose_file = _compose_with_agent_env(
        tmp_path,
        {
            "AWS_ACCESS_KEY_ID": "${AWS_ACCESS_KEY_ID}",
            "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
        },
    )
    worker_env = {
        "AWS_SECRET_ACCESS_KEY": "secret-worker-value",
    }

    profile_env = dict(
        literal_profile_env_from_compose(
            compose_file,
            worker_env=worker_env,
        )
    )
    filtered = filter_hosted_env_passthrough_names(
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        compose_file=compose_file,
        worker_env=worker_env,
    )

    assert "AWS_ACCESS_KEY_ID" not in profile_env
    assert "AWS_ACCESS_KEY_ID" not in filtered
    assert "AWS_SECRET_ACCESS_KEY" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_suppresses_profile_owned_backend_supplement(
    tmp_path: Path,
) -> None:
    compose_file = _compose_with_agent_env(
        tmp_path,
        {
            "AWS_REGION": "us-west-2",
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
            "CLAUDE_CODE_USE_VERTEX": "1",
        },
    )

    names = (
        "CLAUDE_CODE_USE_VERTEX",
        "AWS_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "AWS_ACCESS_KEY_ID",
        "ANTHROPIC_API_KEY",
    )
    filtered = filter_hosted_env_passthrough_names(names, compose_file=compose_file, worker_env={})

    assert "CLAUDE_CODE_USE_VERTEX" not in filtered
    assert "AWS_REGION" not in filtered
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in filtered
    assert "AWS_ACCESS_KEY_ID" not in filtered
    assert "ANTHROPIC_API_KEY" in filtered
