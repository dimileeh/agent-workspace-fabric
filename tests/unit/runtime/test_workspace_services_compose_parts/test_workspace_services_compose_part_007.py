"""No-Docker compose coverage for profile-declared workspace services (part 7)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import (
    filter_hosted_env_passthrough_names,
    hosted_profile_env_passthrough_aliases,
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
    for hosted out-of-band resolution only when the worker supplied a value,
    mirroring the local Compose container. A literal empty resolved from an
    interpolation default (e.g. ``${MISSING:-}`` with ``MISSING`` unset) is still
    carried as ``LITERAL`` (the local container received that empty default, not
    a worker shell value).
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
    assert "AWS_NULL_REGION" not in filtered
    assert "AWS_EMPTY_REGION" not in filtered
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in filtered
    assert "OPENAI_API_KEY" in filtered
    assert "EMPTY_DEFAULT" not in filtered
    assert "ANTHROPIC_API_KEY" in filtered


@pytest.mark.unit
def test_empty_defaulted_or_required_setness_reference_carried_not_passthrough(
    tmp_path: Path,
) -> None:
    """Set-but-empty ``-`` / ``?`` references preserve Compose's empty override."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OPENAI_API_KEY": "${OPENAI_API_KEY-default}",
                            "CODEX_API_KEY": "${CODEX_API_KEY?required}",
                            "NON_EMPTY_DEFAULT": "${NON_EMPTY_DEFAULT-default}",
                            "NON_EMPTY_REQUIRED": "${NON_EMPTY_REQUIRED?required}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    worker_env = {
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
        "NON_EMPTY_DEFAULT": "worker-default",
        "NON_EMPTY_REQUIRED": "worker-required",
    }

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    filtered = filter_hosted_env_passthrough_names(
        ("OPENAI_API_KEY", "CODEX_API_KEY", "NON_EMPTY_DEFAULT", "NON_EMPTY_REQUIRED"),
        compose_file=compose_file,
        worker_env=worker_env,
    )

    assert profile_env["OPENAI_API_KEY"] == ""
    assert profile_env["CODEX_API_KEY"] == ""
    assert "NON_EMPTY_DEFAULT" not in profile_env
    assert "NON_EMPTY_REQUIRED" not in profile_env
    assert "OPENAI_API_KEY" not in filtered
    assert "CODEX_API_KEY" not in filtered
    assert "NON_EMPTY_DEFAULT" in filtered
    assert "NON_EMPTY_REQUIRED" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_excludes_cross_name_bare_reference(
    tmp_path: Path,
) -> None:
    """A bare ``${SOURCE}`` slot whose target name differs from SOURCE stays excluded.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PjYmf: a bare reference to a
    *different* worker variable (e.g. a declared env secret lease rendering
    ``ANTHROPIC_API_KEY: ${MY_ANTHROPIC_TOKEN}`` or
    ``AWS_REGION: ${AWS_DEFAULT_REGION}``) classifies ``WORKER_RESOLVED_SLOT``
    and the source name exists in ``worker_env``, so the previous
    ``worker_resolved_slots`` comprehension kept the *target* key in
    ``env_passthrough_names``. ``literal_profile_env_from_compose`` skips the
    slot (worker-resolved), so the hosted request carried neither the
    source-to-target mapping nor the resolved value; the hosted executor then
    resolved the target by name and found nothing (the worker env has the
    *source* name, not the target). Only a same-name bare slot (the exact form
    Core injects via ``agent_environment_with_legacy_host_auth``) takes the
    passthrough path; a cross-name bare slot stays excluded from target-name
    passthrough and is carried as explicit source-to-target alias metadata
    instead.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Cross-name bare reference: target != source. The
                            # source is worker-set, but the hosted executor
                            # resolves by TARGET name (absent from worker_env),
                            # so keeping it in passthrough resolves nothing.
                            "ANTHROPIC_API_KEY": "${MY_ANTHROPIC_TOKEN}",
                            "AWS_REGION": "${AWS_DEFAULT_REGION}",
                            # Same-name bare reference (Core-injected form) ->
                            # stays in passthrough (worker value resolves by name).
                            "OLLAMA_HOST": "${OLLAMA_HOST}",
                            # Non-alias forms must not appear in the alias list.
                            "UNSET_ALIAS": "${UNSET_SOURCE}",
                            "MIXED_ALIAS": "prefix-${MY_ANTHROPIC_TOKEN}",
                            "LITERAL_VALUE": "literal",
                            # Pass-through slot with no worker value: exclude it
                            # from hosted passthrough and do not carry a value.
                            "PASSTHROUGH_SLOT": None,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = (
        "ANTHROPIC_API_KEY",
        "AWS_REGION",
        "OLLAMA_HOST",
        "MY_ANTHROPIC_TOKEN",
        "AWS_DEFAULT_REGION",
        "UNSET_ALIAS",
        "MIXED_ALIAS",
        "LITERAL_VALUE",
        "PASSTHROUGH_SLOT",
    )
    worker_env = {
        "MY_ANTHROPIC_TOKEN": "sk-ant-secret",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "OLLAMA_HOST": "http://ollama:11434",
    }
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )

    # Cross-name bare references are EXCLUDED — the hosted executor resolves
    # by the target name, which is not in the worker env, so the passthrough
    # slot would resolve to nothing and silently drop the credential.
    assert "ANTHROPIC_API_KEY" not in filtered
    assert "AWS_REGION" not in filtered
    # Same-name bare reference stays in passthrough (the Core-injected form;
    # the hosted executor resolves the worker value by name).
    assert "OLLAMA_HOST" in filtered
    assert "UNSET_ALIAS" not in filtered
    assert "MIXED_ALIAS" not in filtered
    assert "LITERAL_VALUE" not in filtered
    assert "PASSTHROUGH_SLOT" not in filtered

    # The cross-name bare slots are NOT carried in profile_env (worker-resolved;
    # carrying the worker value would embed a secret), and the same-name slot is
    # skipped too. The unset cross-name bare reference resolves to Compose's
    # empty literal, while the unset pass-through slot is not carried. None of
    # the alias values reach profile_env.
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)
    assert "ANTHROPIC_API_KEY" not in carried
    assert "AWS_REGION" not in carried
    assert "OLLAMA_HOST" not in carried
    assert carried["UNSET_ALIAS"] == ""
    assert "MIXED_ALIAS" not in carried
    assert carried["LITERAL_VALUE"] == "literal"
    assert "PASSTHROUGH_SLOT" not in carried

    aliases = hosted_profile_env_passthrough_aliases(
        compose_file,
        worker_env=worker_env,
    )
    assert aliases == (
        ("ANTHROPIC_API_KEY", "MY_ANTHROPIC_TOKEN"),
        ("AWS_REGION", "AWS_DEFAULT_REGION"),
    )


@pytest.mark.unit
def test_unset_bare_compose_placeholder_carried_as_empty_literal(
    tmp_path: Path,
) -> None:
    """Unset bare Compose placeholders mirror Compose's empty expansion."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "FEATURE_FLAG": "${FEATURE_FLAG}",
                            "PLAIN_FEATURE_FLAG": "$PLAIN_FEATURE_FLAG",
                            "MIXED_FEATURE_FLAG": "prefix-${MISSING_FEATURE_FLAG}",
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {"OPENAI_API_KEY": "sk-secret"}
    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))

    assert profile_env["FEATURE_FLAG"] == ""
    assert profile_env["PLAIN_FEATURE_FLAG"] == ""
    assert profile_env["MIXED_FEATURE_FLAG"] == "prefix-"
    assert "OPENAI_API_KEY" not in profile_env

    filtered = filter_hosted_env_passthrough_names(
        (
            "FEATURE_FLAG",
            "PLAIN_FEATURE_FLAG",
            "MIXED_FEATURE_FLAG",
            "OPENAI_API_KEY",
        ),
        compose_file=compose_file,
        worker_env=worker_env,
    )

    assert "FEATURE_FLAG" not in filtered
    assert "PLAIN_FEATURE_FLAG" not in filtered
    assert "MIXED_FEATURE_FLAG" not in filtered
    assert "OPENAI_API_KEY" in filtered


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_set_cookie_header_blobs(
    tmp_path: Path,
) -> None:
    """Neutral env names carrying Set-Cookie headers must not reach profile_env."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "RESPONSE_HEADERS": "Set-Cookie: sid=session-secret; HttpOnly",
                            "JSON_RESPONSE_HEADERS": '{"Set-Cookie":"sid=json-secret"}',
                            "SAFE_RESPONSE_HEADERS": '{"Cache-Control":"no-store"}',
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert "RESPONSE_HEADERS" not in profile_env
    assert "JSON_RESPONSE_HEADERS" not in profile_env
    assert profile_env["SAFE_RESPONSE_HEADERS"] == '{"Cache-Control":"no-store"}'


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_camelcase_prefixed_secret_literals(
    tmp_path: Path,
) -> None:
    """Neutral config env values carrying camelCase secret fields are NOT carried."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "APP_CONFIG": '{"stripeClientSecret":"stripe-profile-secret"}',
                            "WEBHOOK_CONFIG": "webhookSecret=webhook-profile-secret",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert "APP_CONFIG" not in carried
    assert "WEBHOOK_CONFIG" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "stripe-profile-secret" not in blob
    assert "webhook-profile-secret" not in blob
