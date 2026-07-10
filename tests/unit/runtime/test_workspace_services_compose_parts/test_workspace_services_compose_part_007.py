"""No-Docker compose coverage for profile-declared workspace services (part 7)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import (
    filter_hosted_env_passthrough_names,
    literal_profile_env_from_compose,
)


@pytest.mark.unit
def test_compose_passthrough_env_slot_not_carried_kept_in_passthrough(
    tmp_path: Path,
) -> None:
    """A Compose pass-through env slot is resolved from the worker, not carried.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PYnJJ: a Compose pass-through
    slot — ``environment: [NAME]`` (list item with no ``=``) or ``NAME:`` /
    ``NAME: null`` (mapping value that is ``None``) — declares no value; Docker
    Compose takes the value from the worker shell at stack launch, exactly like
    a bare ``${NAME}`` reference. The local agent container receives the
    worker's ``AWS_REGION`` when it is set. (An *explicit* empty value —
    mapping ``NAME: ""`` or list ``NAME=`` — is a separate case covered by
    ``test_compose_explicit_empty_value_carried_not_passthrough``: Compose sets
    an empty literal that overrides the worker value, so it is carried in
    ``profile_env`` and excluded from passthrough.)

    Previously ``_compose_environment_mapping`` normalized a pass-through slot
    to ``""`` (or ``"None"`` for a YAML null value) and
    ``literal_profile_env_from_compose`` carried it as a literal empty/
    ``"None"`` value into ``profile_env``, which overrode the real worker region
    in the hosted request, while ``filter_hosted_env_passthrough_names`` excluded
    the name from passthrough (it resolved to ``LITERAL``), so the hosted job
    received neither the worker value nor the passthrough slot.

    Now a pass-through slot is skipped from ``profile_env`` (an empty literal
    would clobber the real worker value) and kept in ``env_passthrough_names``
    for hosted out-of-band resolution, mirroring the local Compose container. A
    literal empty resolved from an interpolation default (e.g. ``${MISSING:-}``
    with ``MISSING`` unset) is still carried as ``LITERAL`` (the local container
    received that empty default, not a worker shell value).
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "AWS_REGION": None,
                            "AWS_NULL_REGION": None,
                            "AWS_EMPTY_REGION": "",
                            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            "EMPTY_DEFAULT": "${MISSING:-}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {"AWS_REGION": "us-west-2", "OPENAI_API_KEY": "sk-secret"}
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)

    assert "AWS_REGION" not in carried
    assert "AWS_NULL_REGION" not in carried
    assert carried.get("AWS_EMPTY_REGION") == ""
    assert "None" not in carried.values()
    assert carried.get("ANTHROPIC_VERTEX_PROJECT_ID") == "proj-123"
    assert "OPENAI_API_KEY" not in carried
    assert carried.get("EMPTY_DEFAULT") == ""

    names = (
        "AWS_REGION",
        "AWS_NULL_REGION",
        "AWS_EMPTY_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "OPENAI_API_KEY",
        "EMPTY_DEFAULT",
        "ANTHROPIC_API_KEY",
    )
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )
    assert "AWS_REGION" in filtered
    assert "AWS_NULL_REGION" in filtered
    assert "AWS_EMPTY_REGION" not in filtered
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in filtered
    assert "OPENAI_API_KEY" in filtered
    assert "EMPTY_DEFAULT" not in filtered
    assert "ANTHROPIC_API_KEY" in filtered
