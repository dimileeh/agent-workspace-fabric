"""No-Docker compose coverage for profile-declared workspace services (part 3).

Shared fixtures/helpers live in
``test_workspace_services_compose_part_001``; this part imports them. Split
from the original module to stay under the first-party file line limit (see
``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import filter_hosted_env_passthrough_names

FILTER = filter_hosted_env_passthrough_names


@pytest.mark.unit
def test_compose_passthrough_env_slot_list_form_not_carried(
    tmp_path: Path,
) -> None:
    """The list-form pass-through syntax ``environment: [NAME]`` is not carried.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PYnJJ: the reviewer's exact
    example uses Compose's pass-through env list syntax
    ``environment: [AWS_REGION]`` (a YAML list item with no ``=``). Docker
    Compose takes the value from the worker shell at stack launch; the hosted
    path must keep the name in ``env_passthrough_names`` (resolved out-of-band)
    and must NOT carry an empty literal into ``profile_env`` (which would
    override the real worker region in the hosted request).
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        # List form: bare name (pass-through) and NAME=value.
                        "environment": [
                            "AWS_REGION",
                            "OTHER=literal",
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {"AWS_REGION": "us-west-2"}
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)

    # Pass-through slot is NOT carried as an empty literal.
    assert "AWS_REGION" not in carried
    # Literal value is carried.
    assert carried.get("OTHER") == "literal"

    # Pass-through slot stays in env_passthrough_names; literal is excluded.
    names = ("AWS_REGION", "OTHER", "ANTHROPIC_API_KEY")
    filtered = FILTER(names, compose_file=compose_file, worker_env=worker_env)
    assert "AWS_REGION" in filtered
    assert "OTHER" not in filtered
    assert "ANTHROPIC_API_KEY" in filtered


@pytest.mark.unit
def test_compose_explicit_empty_value_carried_not_passthrough(
    tmp_path: Path,
) -> None:
    """An explicit empty compose env value is carried, not a pass-through slot.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PY8zB: Docker Compose treats an
    *explicit* empty value — mapping ``NAME: ""`` or list ``NAME=`` — as a
    non-nil pointer to ``""`` (an empty literal that OVERRIDES the worker shell
    value), while only a *pass-through* slot — mapping ``NAME:`` /
    ``NAME: null`` or list ``NAME`` (no ``=``) — is a nil pointer resolved from
    the worker shell at stack launch (see compose-go
    ``loader/tests/environment_test.go`` ``TestEnvironmentMap`` /
    ``TestEnvironmentList``: ``BU: ""`` -> ``*env["BU"] == ""``, ``ZO:`` ->
    ``env["ZO"] == nil``).

    So the local agent container receives an explicit ``""`` for
    ``AWS_REGION: ""`` / ``AWS_EMPTY_REGION=`` EVEN when the worker shell has a
    non-empty ``AWS_REGION``. The hosted path must mirror that:
    - ``profile_env`` CARRIES ``""`` (the local container had the var blank, not
      the worker value); an empty literal would otherwise be skipped by the old
      ``raw == ""`` sentinel, dropping the explicit blank and letting the hosted
      job inherit a worker value the local container never had.
    - ``env_passthrough_names`` EXCLUDES the name (it is a profile-owned literal,
      not a worker-resolved slot); keeping it would make the hosted executor
      re-resolve a worker value the local container never received.

    Genuine pass-through slots (``NAME:``, list ``NAME``) keep the old behavior:
    skipped from ``profile_env`` and kept in ``env_passthrough_names``.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Explicit empty (mapping) -> carried literal "".
                            "AWS_REGION": "",
                            # Pass-through (mapping null) -> worker-resolved.
                            "AWS_NULL_REGION": None,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    worker_env = {"AWS_REGION": "us-west-2", "AWS_NULL_REGION": "us-west-2"}
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))

    # Explicit empty is CARRIED as a literal "" (mirrors the local container,
    # which had the var explicitly blank, not the worker "us-west-2").
    assert carried.get("AWS_REGION") == ""
    # Pass-through slot is NOT carried (worker-resolved; carrying would embed the
    # worker value / override the real worker resolution).
    assert "AWS_NULL_REGION" not in carried

    names = ("AWS_REGION", "AWS_NULL_REGION", "ANTHROPIC_API_KEY")
    filtered = FILTER(names, compose_file=compose_file, worker_env=worker_env)
    # Explicit empty -> EXCLUDED (profile-owned literal, not worker-resolved).
    assert "AWS_REGION" not in filtered
    # Pass-through slot -> kept for hosted out-of-band resolution.
    assert "AWS_NULL_REGION" in filtered
    # Name absent from the compose env block still passes through.
    assert "ANTHROPIC_API_KEY" in filtered


@pytest.mark.unit
def test_compose_explicit_empty_list_form_carried_not_passthrough(
    tmp_path: Path,
) -> None:
    """The list-form explicit empty ``NAME=`` is carried, not a pass-through slot.

    Companion to ``test_compose_explicit_empty_value_carried_not_passthrough``:
    ``environment: [NAME=]`` (a list item WITH an ``=`` and an empty value) is an
    explicit empty literal in Docker Compose (compose-go ``TestEnvironmentList``:
    ``BU=`` -> ``*env["BU"] == ""``), distinct from ``environment: [NAME]`` (no
    ``=``), which is a worker-resolved pass-through slot. The explicit-empty form
    is CARRIED in ``profile_env`` and EXCLUDED from ``env_passthrough_names``;
    the no-``=`` form is skipped from ``profile_env`` and kept in passthrough.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": [
                            "AWS_REGION=",  # explicit empty (has '=')
                            "AWS_NULL_REGION",  # pass-through (no '=')
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    worker_env = {"AWS_REGION": "us-west-2", "AWS_NULL_REGION": "us-west-2"}
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))

    # Explicit empty (list form) -> CARRIED as literal "".
    assert carried.get("AWS_REGION") == ""
    # Pass-through (list no '=') -> NOT carried.
    assert "AWS_NULL_REGION" not in carried

    names = ("AWS_REGION", "AWS_NULL_REGION")
    filtered = FILTER(names, compose_file=compose_file, worker_env=worker_env)
    assert "AWS_REGION" not in filtered
    assert "AWS_NULL_REGION" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_defaulted_unset_excluded_by_default_env(
    tmp_path: Path,
) -> None:
    """With the variable unset in the worker env, a defaulted form is excluded.

    Mirrors the unset case from the regression above against the default worker
    env (``os.environ``): when no worker override is present the concrete default
    reaches the local container and is carried via ``profile_env``, so the name
    is excluded from passthrough. Uses an exotic name unlikely to be set in the
    worker env so the default-env path exercises the unset branch deterministically.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "AWF_TEST_DEFAULTED_REGION_UNSET": (
                                "${AWF_TEST_DEFAULTED_REGION_UNSET:-us-west-2}"
                            ),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = ("AWF_TEST_DEFAULTED_REGION_UNSET",)
    filtered = FILTER(names, compose_file=compose_file)

    # Unset defaulted form -> concrete default carried via profile_env, excluded
    # from passthrough.
    assert "AWF_TEST_DEFAULTED_REGION_UNSET" not in filtered


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_placeholders(
    tmp_path: Path,
) -> None:
    """Literal profile-owned env values are carried to the hosted executor.

    The local ``docker compose exec`` path does not forward profile-owned env
    because the running container already has it (substituted from the compose
    env block at stack launch). The hosted (non-compose) path has no compose env
    block, so the hosted executor must inject the same values the local
    container received. Compose interpolation is rendered against the worker env
    so the hosted job gets the concrete value:

    - Pure literals -> carried verbatim.
    - ``${NAME:-default}`` / ``${NAME-default}`` with ``NAME`` unset -> the
      concrete default is carried (the local container receives it; dropping
      it leaves the hosted job missing the profile-owned value).
    - ``$$`` escapes -> collapsed to a single ``$`` and carried (Compose models
      ``$$`` as a literal dollar, not a reference).
    - Bare ``${NAME}`` / ``$NAME`` / ``${NAME:?...}`` / ``${NAME:+...}``
      references -> skipped (worker-resolved secrets the profile owns locally;
      the hosted path resolves credentials via its own adapter contract, not by
      re-resolving ``${NAME}`` from the worker, and carrying the worker value
      would embed a secret in ``profile_env``).
    - ``${NAME:-default}`` with ``NAME`` set in the worker env -> skipped
      (worker-resolved value; carrying it would embed a worker secret).
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Literal profile value -> carried.
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # Worker-resolved secret placeholder -> skipped.
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            # Required placeholder -> skipped.
                            "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY:?set}",
                            # Defaulted expression with the variable unset -> the
                            # concrete default reaches the local container, so the
                            # hosted job must receive it too (regression for the
                            # drop-defaulted-expression defect).
                            "AWS_REGION": "${AWS_REGION:-us-west-2}",
                            # Escaped dollar literal -> collapsed to a single "$"
                            # and carried (regression for the verbatim-$$ defect).
                            "LITERAL_DOLLAR": "$$NOT_A_VAR",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # Explicit empty worker env so AWS_REGION is unset -> default carried.
    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # $$ collapses to a single $, matching the local container's stack-launch env.
    assert ("LITERAL_DOLLAR", "$NOT_A_VAR") in profile_env
    # Defaulted expression with the variable unset -> concrete default carried.
    assert ("AWS_REGION", "us-west-2") in profile_env
    # Worker-resolved placeholders are not carried as values.
    assert "OPENAI_API_KEY" not in dict(profile_env)
    assert "ANTHROPIC_API_KEY" not in dict(profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_non_auth_literal_secret_names(
    tmp_path: Path,
) -> None:
    """Non-auth secret-looking literals never enter hosted profile_env."""
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "NPM_TOKEN": "npm_profile_token",
                            "CUSTOM_API_TOKEN": "custom_profile_token",
                            "STRIPE_KEY": "sk_live_profile",
                            "SENDGRID_KEY": "sendgrid_profile_key",
                            "PAYMENTS_CLIENT_SECRET": "client_secret",
                            "PGPASSWORD": "postgres_profile_password",
                            "MYSQL_PWD": "mysql_profile_password",
                            "AUTHORIZATION": "Bearer profile-token",
                            "HTTP_AUTHORIZATION": "Basic profile-token",
                            "UPSTREAM_AUTH": "Bearer upstream-token",
                            "AUTH_MODE": "disabled",
                            "AUTH_PROVIDER": "local",
                            "POSTGRES_HOST_AUTH_METHOD": "trust",
                            "GEMINI_API_KEY_AUTH_MECHANISM": "api-key",
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            "AWS_REGION": "us-west-2",
                            "APP_MODE": "ci",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert "NPM_TOKEN" not in carried
    assert "CUSTOM_API_TOKEN" not in carried
    assert "STRIPE_KEY" not in carried
    assert "SENDGRID_KEY" not in carried
    assert "PAYMENTS_CLIENT_SECRET" not in carried
    assert "PGPASSWORD" not in carried
    assert "MYSQL_PWD" not in carried
    assert "AUTHORIZATION" not in carried
    assert "HTTP_AUTHORIZATION" not in carried
    assert "UPSTREAM_AUTH" not in carried
    assert carried["AUTH_MODE"] == "disabled"
    assert carried["AUTH_PROVIDER"] == "local"
    assert carried["POSTGRES_HOST_AUTH_METHOD"] == "trust"
    assert carried["GEMINI_API_KEY_AUTH_MECHANISM"] == "api-key"
    assert carried["OLLAMA_HOST"] == "http://ollama.profile:11434"
    assert carried["AWS_REGION"] == "us-west-2"
    assert carried["APP_MODE"] == "ci"


@pytest.mark.unit
def test_literal_profile_env_from_compose_resolves_defaults_and_escapes(
    tmp_path: Path,
) -> None:
    """Compose interpolation is rendered against the worker env before carry.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PT7KL: a defaulted
    ``${NAME:-default}`` whose variable is set in the worker env resolves to the
    worker value and is skipped (worker-resolved; carrying it would embed a
    secret in ``profile_env``); one whose variable is unset resolves to the
    concrete default and is carried. Mixed literal+default values and multiple
    ``$$`` escapes are carried as the expanded form.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Variable set in worker env -> worker-resolved, skipped.
                            "AWS_REGION_SET": "${AWS_REGION_SET:-us-west-2}",
                            # Variable unset -> concrete default carried.
                            "AWS_REGION_UNSET": "${AWS_REGION_UNSET:-us-west-2}",
                            # Mixed literal + unset-var default -> carried expanded.
                            "ENDPOINT": "https://${API_HOST:-api.example.com}/v1",
                            # Multiple $$ escapes collapse to single $ each
                            # for non-secret profile config.
                            "ESCAPED_LITERAL": "pa$$word",
                            # Secret-looking literals are redacted even when
                            # they contain Compose escapes.
                            "PASSWORD": "pa$$word",
                            # :- with empty default and unset var -> carried as "".
                            "EMPTY_DEFAULT": "${MISSING:-}",
                            # Bare $NAME (no braces) worker-resolved -> skipped.
                            "BARE_PLAIN": "$BARE_PLAIN_VAR",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {"AWS_REGION_SET": "eu-central-1"}
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)

    # Set-var default -> worker-resolved, skipped (no secret value carried).
    assert "AWS_REGION_SET" not in carried
    # Unset-var default -> concrete default carried.
    assert carried.get("AWS_REGION_UNSET") == "us-west-2"
    # Mixed literal + unset default -> expanded form carried.
    assert carried.get("ENDPOINT") == "https://api.example.com/v1"
    # $$ collapses to a single $ for non-secret config.
    assert carried.get("ESCAPED_LITERAL") == "pa$word"
    # Secret-looking literals are still redacted from hosted profile_env.
    assert "PASSWORD" not in carried
    # Empty default with unset var -> carried as empty string (matches local).
    assert carried.get("EMPTY_DEFAULT") == ""
    # Bare $NAME worker-resolved -> skipped.
    assert "BARE_PLAIN" not in carried


@pytest.mark.unit
def test_literal_profile_env_from_compose_dash_dash_tests_non_empty(
    tmp_path: Path,
) -> None:
    """``:-`` tests non-empty, ``-`` tests set-ness, matching Compose.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PUZLe: when the worker env
    contains an empty string (``AWS_REGION=""``), ``${AWS_REGION:-us-west-2}``
    injects the default because ``:-`` tests non-empty (mirroring
    ``awf.service.environment._compose_expand_braced_expression``). The local
    Compose container receives ``us-west-2``, so the hosted job must too. The
    previous code used a presence check for both ``:-`` and ``-``, so an empty
    worker value was treated as worker-resolved and the default was dropped,
    leaving hosted runs without ``AWS_REGION``. ``-`` tests set-ness, so an
    empty-but-present value resolves to the empty value (worker-resolved, skip).
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # :- with empty worker value -> default carried.
                            "AWS_REGION": "${AWS_REGION:-us-west-2}",
                            # - with empty worker value -> worker-resolved, skip.
                            "AWS_REGION_DASH": "${AWS_REGION-us-west-2}",
                            # :- with non-empty worker value -> worker-resolved, skip.
                            "AWS_REGION_SET": "${AWS_REGION_SET:-us-west-2}",
                            # - with non-empty worker value -> worker-resolved, skip.
                            "AWS_REGION_SET_DASH": "${AWS_REGION_SET-us-west-2}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {"AWS_REGION": "", "AWS_REGION_DASH": "", "AWS_REGION_SET": "eu-central-1"}
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)

    # :-) empty worker value -> default carried (local container gets us-west-2).
    assert carried.get("AWS_REGION") == "us-west-2"
    # - : empty-but-present -> worker-resolved empty value, skip (no carry).
    assert "AWS_REGION_DASH" not in carried
    # :-) non-empty worker value -> worker-resolved, skip.
    assert "AWS_REGION_SET" not in carried
    # - : non-empty worker value -> worker-resolved, skip.
    assert "AWS_REGION_SET_DASH" not in carried


@pytest.mark.unit
def test_literal_profile_env_from_compose_alternate_carries_word_when_set(
    tmp_path: Path,
) -> None:
    """``:+`` / ``+`` carry the alternate word when the variable is worker-set.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PVhhm: for an agent env value
    such as ``ENDPOINT: ${FLAG:+https://eu.example.com}``, Docker Compose resolves
    the alternate word (``https://eu.example.com``) into the local agent container
    at stack launch when ``FLAG`` is set (non-empty for ``:+``, set for ``+``) and
    resolves to ``""`` when ``FLAG`` is unset/empty. The previous code classified
    every ``:+`` / ``+`` form as ``WORKER_RESOLVED_SLOT``, dropping the key from
    ``profile_env`` and excluding it from ``env_passthrough_names`` — so the
    hosted job received neither the alternate word nor the worker value, while the
    local container received the alternate word. The alternate word is profile-owned
    config (literal text in the compose file), so it is carried as ``LITERAL`` so
    the hosted job receives the same concrete value the local container got; when
    the variable is unset/empty the local container receives ``""``, which is also
    carried so the hosted job matches. The alternate word is recursively expanded
    against the worker env (mirroring ``awf.service.environment``'s expander) so a
    word that itself references a worker secret (e.g. ``${FLAG:+${SECRET}}``) does
    not embed that secret in ``profile_env``.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # :+ with FLAG set & non-empty -> alternate word carried.
                            "ENDPOINT_SET": "${FLAG_SET:+https://eu.example.com}",
                            # :+ with FLAG unset -> local container gets "", carried.
                            "ENDPOINT_UNSET": "${FLAG_UNSET:+https://eu.example.com}",
                            # :+ with FLAG present-but-empty -> local gets "", carried.
                            "ENDPOINT_EMPTY": "${FLAG_EMPTY:+https://eu.example.com}",
                            # + with FLAG set (even empty) -> alternate word carried.
                            "ENDPOINT_PLUS_SET": "${FLAG_PLUS_SET+https://eu.example.com}",
                            # + with FLAG unset -> local container gets "", carried.
                            "ENDPOINT_PLUS_UNSET": "${FLAG_PLUS_UNSET+https://eu.example.com}",
                            # :+ whose alternate word references a worker secret:
                            # when FLAG is set the word ${SECRET} expands to the
                            # worker value (a secret), so the whole value must NOT
                            # be carried as a literal — it stays worker-resolved.
                            "SECRET_BEARING": "${FLAG_SET:+${SECRET}}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {
        "FLAG_SET": "true",
        "FLAG_EMPTY": "",
        "FLAG_PLUS_SET": "",
    }
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)

    # :+ set & non-empty -> alternate word carried (local container got the word).
    assert carried.get("ENDPOINT_SET") == "https://eu.example.com"
    # :+ unset -> local container got "", carried as empty.
    assert carried.get("ENDPOINT_UNSET") == ""
    # :+ present-but-empty -> local container got "", carried as empty.
    assert carried.get("ENDPOINT_EMPTY") == ""
    # + set (even empty) -> alternate word carried.
    assert carried.get("ENDPOINT_PLUS_SET") == "https://eu.example.com"
    # + unset -> local container got "", carried as empty.
    assert carried.get("ENDPOINT_PLUS_UNSET") == ""
    # :+ whose word references a worker secret -> not carried (worker-resolved).
    assert "SECRET_BEARING" not in carried
