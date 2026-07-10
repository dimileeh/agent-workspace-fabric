"""No-Docker compose coverage for profile-declared workspace services (part 4).

Targets the uncovered branches of ``awf.profiles.compose_env`` and
``awf.profiles.compose`` introduced by PR #751's compose env interpolation /
redaction machinery. Behaviors are asserted through the public surface
(``literal_profile_env_from_compose`` / ``filter_hosted_env_passthrough_names``)
so the tests exercise the real carry / passthrough / redaction contract rather
than calling private helpers in isolation. Split from the earlier parts to stay
under the first-party file line limit (see
``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import (
    filter_hosted_env_passthrough_names,
    hosted_github_token_passthrough_names,
    hosted_profile_env_passthrough_aliases,
    literal_profile_env_from_compose,
)
from awf.profiles.compose_env import (
    _compose_bare_reference_name,
    _compose_default_word_is_worker_resolved,
    _compose_defaulted_reference_name,
    _compose_selected_worker_reference_name,
)


def _write(tmp_path: Path, payload: object) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return compose_file


@pytest.mark.unit
def test_compose_bare_reference_name_accepts_only_single_reference() -> None:
    """Bare reference detection accepts exact slots and rejects mixed values."""
    assert _compose_bare_reference_name("${OPENAI_API_KEY}") == "OPENAI_API_KEY"
    assert _compose_bare_reference_name("$OPENAI_API_KEY") == "OPENAI_API_KEY"
    assert _compose_bare_reference_name("${OPENAI_API_KEY}suffix") is None
    assert _compose_bare_reference_name("$OPENAI_API_KEY-suffix") is None
    assert _compose_bare_reference_name("literal") is None


@pytest.mark.unit
def test_compose_defaulted_reference_name_accepts_only_selected_outer_source() -> None:
    """Defaulted/required source extraction is intentionally exact and selected."""
    worker_env = {"AWS_REGION": "eu-central-1", "EMPTY": "", "REQUIRED": "value"}

    assert (
        _compose_defaulted_reference_name(
            "${AWS_REGION:-us-west-2}",
            worker_env=worker_env,
        )
        == "AWS_REGION"
    )
    assert (
        _compose_defaulted_reference_name(
            "${EMPTY-default}",
            worker_env=worker_env,
        )
        == "EMPTY"
    )
    assert (
        _compose_defaulted_reference_name(
            "${EMPTY?required}",
            worker_env=worker_env,
        )
        == "EMPTY"
    )
    assert (
        _compose_defaulted_reference_name(
            "${REQUIRED:?required}",
            worker_env=worker_env,
        )
        == "REQUIRED"
    )
    assert (
        _compose_defaulted_reference_name(
            "prefix-${AWS_REGION:-us-west-2}",
            worker_env=worker_env,
        )
        is None
    )
    assert (
        _compose_defaulted_reference_name(
            "${AWS_REGION:-us-west-2}suffix",
            worker_env=worker_env,
        )
        is None
    )
    assert _compose_defaulted_reference_name("${!:-fallback}", worker_env=worker_env) is None
    assert (
        _compose_defaulted_reference_name(
            "${MISSING:?required}",
            worker_env=worker_env,
        )
        is None
    )
    assert _compose_defaulted_reference_name("${AWS_REGION}", worker_env=worker_env) is None


@pytest.mark.unit
def test_compose_selected_worker_reference_name_tracks_selected_nested_source() -> None:
    """Selected worker-source extraction rejects mixed, unselected, and literal forms."""
    worker_env = {
        "FLAG": "1",
        "EMPTY": "",
        "TOKEN": "secret-token",
        "REQUIRED": "required-secret",
    }

    assert _compose_selected_worker_reference_name("${TOKEN}", worker_env=worker_env) == "TOKEN"
    assert _compose_selected_worker_reference_name("$TOKEN", worker_env=worker_env) == "TOKEN"
    assert (
        _compose_selected_worker_reference_name(
            "${MISSING:-${TOKEN}}",
            worker_env=worker_env,
        )
        == "TOKEN"
    )
    assert (
        _compose_selected_worker_reference_name(
            "${FLAG:+${TOKEN}}",
            worker_env=worker_env,
        )
        == "TOKEN"
    )
    assert (
        _compose_selected_worker_reference_name(
            "${REQUIRED:?required}",
            worker_env=worker_env,
        )
        == "REQUIRED"
    )
    assert (
        _compose_selected_worker_reference_name(
            "prefix-${TOKEN}",
            worker_env=worker_env,
        )
        is None
    )
    assert _compose_selected_worker_reference_name("${TOKEN", worker_env=worker_env) is None
    assert _compose_selected_worker_reference_name("${!:-${TOKEN}}", worker_env=worker_env) is None
    assert (
        _compose_selected_worker_reference_name("${TOKEN=literal}", worker_env=worker_env) is None
    )
    assert _compose_selected_worker_reference_name("${TOKEN}", worker_env={}) == "TOKEN"
    assert _compose_selected_worker_reference_name("${TOKEN:-literal}", worker_env={}) is None
    assert (
        _compose_selected_worker_reference_name("${EMPTY:+${TOKEN}}", worker_env=worker_env) is None
    )
    assert _compose_selected_worker_reference_name("${MISSING:?required}", worker_env={}) is None


@pytest.mark.unit
def test_compose_default_word_is_worker_resolved_only_for_worker_selected_defaults() -> None:
    """Default-word classification detects when a default embeds worker state."""
    worker_env = {"TOKEN": "secret-token"}

    assert (
        _compose_default_word_is_worker_resolved(
            "${MISSING:-${TOKEN}}",
            worker_env=worker_env,
        )
        is True
    )
    assert (
        _compose_default_word_is_worker_resolved(
            "${MISSING:-literal}",
            worker_env=worker_env,
        )
        is False
    )
    assert _compose_default_word_is_worker_resolved("literal", worker_env=worker_env) is False
    assert _compose_default_word_is_worker_resolved("${TOKEN", worker_env=worker_env) is False
    assert (
        _compose_default_word_is_worker_resolved("${!:-${TOKEN}}", worker_env=worker_env) is False
    )
    assert _compose_default_word_is_worker_resolved("${TOKEN}", worker_env=worker_env) is False


@pytest.mark.unit
def test_literal_profile_env_carries_unmatched_brace_verbatim(tmp_path: Path) -> None:
    """An unterminated ``${`` reference is carried verbatim as a literal.

    Compose (and the local expander in ``awf.service.environment``) treat an
    unterminated braced reference as literal text rather than failing; the hosted
    carry must mirror that so the hosted job receives the same bytes the local
    container got. ``_compose_braced_expression_end`` returns ``None`` for the
    unterminated form and ``_compose_resolve_value`` falls through to literal
    carry (lines 125 / 196-198 of ``compose_env.py``).
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        # Unterminated braced reference -> literal carry.
                        "BROKEN_BRACE": "${UNCLOSED",
                        # A literal dollar not followed by a valid name char is
                        # also carried verbatim (lines 209-211).
                        "LITERAL_DOLLAR": "cost is $ !",
                    },
                }
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("BROKEN_BRACE") == "${UNCLOSED"
    assert carried.get("LITERAL_DOLLAR") == "cost is $ !"


@pytest.mark.unit
def test_literal_profile_env_carries_unparseable_braced_verbatim(tmp_path: Path) -> None:
    """A braced reference whose name does not parse is carried verbatim.

    ``${!}`` has no leading ``[A-Za-z_]`` name char, so
    ``_compose_resolve_braced`` returns the text verbatim as a literal (line 253)
    and the whole value is carried. An operator the local expander does not
    model (``${FOO=bar}``, ``=`` is not a Compose interpolation operator) is
    likewise carried verbatim (line 268).
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "UNPARSEABLE": "${!}",
                        "UNKNOWN_OP": "${FOO=bar}",
                    },
                }
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("UNPARSEABLE") == "${!}"
    assert carried.get("UNKNOWN_OP") == "${FOO=bar}"


@pytest.mark.unit
def test_literal_profile_env_applies_compose_alternate_and_unset_required_semantics(
    tmp_path: Path,
) -> None:
    """Alternate and unset-required forms mirror Compose carry/skip semantics."""
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "ALT_LITERAL": "${FLAG:+profile-owned}",
                        "ALT_SECRET": "${FLAG:+${TOKEN}}",
                        "ALT_UNSET": "${MISSING:+profile-owned}",
                        "REQUIRED_UNSET": "${MISSING:?required}",
                    },
                }
            }
        },
    )
    carried = dict(
        literal_profile_env_from_compose(
            compose_file,
            worker_env={"FLAG": "1", "TOKEN": "worker-secret-value"},
        )
    )

    assert carried["ALT_LITERAL"] == "profile-owned"
    assert "ALT_SECRET" not in carried
    assert carried["ALT_UNSET"] == ""
    assert "REQUIRED_UNSET" not in carried
    assert "worker-secret-value" not in "\x00".join(carried.values())


@pytest.mark.unit
def test_literal_profile_env_skips_default_word_referencing_worker_secret(
    tmp_path: Path,
) -> None:
    """A default word that itself references a worker secret is skipped.

    ``${X:-${SECRET}}``: the inner ``${SECRET}`` is a worker-resolved slot, so the
    recursive default-word expansion is non-LITERAL and the whole value
    propagates the worker-resolved classification (line 279 of
    ``compose_env.py``). The secret never reaches ``profile_env`` regardless of
    whether ``X`` is set:

    - With ``X`` unset the inner ``${SECRET}`` resolves to a worker-resolved
      *slot*, so the value is classified ``WORKER_RESOLVED_SLOT`` and excluded
      from passthrough (a profile-owned secret slot the hosted path resolves via
      its adapter contract, mirroring a bare ``${SECRET}``).
    - With ``X`` set (non-empty) the outer ``:-`` resolves to the worker value of
      ``X``, so the value is classified ``WORKER_RESOLVED_DEFAULTED``. The
      target name still stays excluded because hosted passthrough resolves by
      target key and cannot recover the cross-name source value.
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "SECRET_DEFAULT": "${X:-${SECRET}}",
                    },
                }
            }
        },
    )
    worker_env = {"SECRET": "worker-secret-value"}

    # X unset -> inner secret slot propagates -> skipped from carry, excluded
    # from passthrough (worker-resolved slot).
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    assert "SECRET_DEFAULT" not in carried
    assert "worker-secret-value" not in "".join(carried.values())
    filtered = filter_hosted_env_passthrough_names(
        ("SECRET_DEFAULT",), compose_file=compose_file, worker_env=worker_env
    )
    assert "SECRET_DEFAULT" not in filtered

    # X set -> outer defaulted resolves to the worker value of X -> skipped from
    # carry (worker secret), excluded from passthrough because the source name
    # differs from the target name.
    worker_env_set = {**worker_env, "X": "x-is-set"}
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env_set))
    assert "SECRET_DEFAULT" not in carried
    filtered = filter_hosted_env_passthrough_names(
        ("SECRET_DEFAULT",), compose_file=compose_file, worker_env=worker_env_set
    )
    assert "SECRET_DEFAULT" not in filtered


@pytest.mark.unit
def test_literal_profile_env_redacts_required_form_postgres_password(
    tmp_path: Path,
) -> None:
    """``${POSTGRES_PASSWORD:?err}`` with the variable set redacts the DB URL.

    A required form with the variable set (non-empty for ``:?``) resolves to the
    worker value at stack launch, so a rendered agent env DB URL embedding that
    resolved password carries the workspace credential and must be redacted.
    Exercises the ``:?`` set branch of ``_compose_concrete_worker_password_braced``
    (lines 417-418) via ``_collect_postgres_password``'s
    ``WORKER_RESOLVED_DEFAULTED`` path.
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {
                        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?required}",
                    },
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": ("postgresql://awf:resolved-pw@postgres:5432/awf"),
                    },
                },
            }
        },
    )
    carried = dict(
        literal_profile_env_from_compose(
            compose_file, worker_env={"POSTGRES_PASSWORD": "resolved-pw"}
        )
    )
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "resolved-pw" not in "".join(carried.values())


@pytest.mark.unit
def test_literal_profile_env_redacts_defaulted_postgres_password_from_worker(
    tmp_path: Path,
) -> None:
    """A set worker password selected by ``:-`` redacts rendered DB URLs."""
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {
                        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:-fallback-pw}",
                    },
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": "postgresql://awf:worker-pw@postgres:5432/awf",
                    },
                },
            }
        },
    )

    carried = dict(
        literal_profile_env_from_compose(
            compose_file,
            worker_env={"POSTGRES_PASSWORD": "worker-pw"},
        )
    )

    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "worker-pw" not in "".join(carried.values())


@pytest.mark.unit
def test_literal_profile_env_redacts_bare_dollar_name_postgres_password(
    tmp_path: Path,
) -> None:
    """A bare ``$POSTGRES_PASSWORD`` (no braces) slot redacts the resolved URL.

    The bare-``$NAME`` form (no braces) is a worker-resolved slot; the rendered
    agent env DB URL embedding the resolved worker password carries the
    workspace credential and must be redacted. Exercises the plain-match branch
    of ``_compose_concrete_worker_password`` (lines 358-367) — both the var-set
    path (the resolved password is recovered and the URL redacted) and the
    var-unset path (the slot recovers ``None`` and is tolerated, line 365).
    A mixed ``prefix-${NAME}`` braced password exercises the literal-char prefix
    of the concrete recovery (lines 341-343 / 350-352).
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {"POSTGRES_PASSWORD": "$POSTGRES_PASSWORD"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": ("postgresql://awf:resolved-pw@postgres:5432/awf"),
                    },
                },
            }
        },
    )
    carried = dict(
        literal_profile_env_from_compose(
            compose_file, worker_env={"POSTGRES_PASSWORD": "resolved-pw"}
        )
    )
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "resolved-pw" not in "".join(carried.values())

    # var unset -> the bare slot recovers None and is tolerated (line 365); the
    # raw ``$POSTGRES_PASSWORD`` placeholder is still a redaction target, but a
    # URL embedding a *different* literal password is NOT redacted by it.
    compose_file.unlink()
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {"POSTGRES_PASSWORD": "$POSTGRES_PASSWORD"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                    },
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"

    # A mixed ``prefix-${NAME}`` braced password (var set) recovers the concrete
    # worker value via the braced path, redacting a URL that embeds it.
    compose_file.unlink()
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {
                        "POSTGRES_PASSWORD": "pw-${POSTGRES_PASSWORD}",
                    },
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": ("postgresql://awf:pw-resolved@postgres:5432/awf"),
                    },
                },
            }
        },
    )
    carried = dict(
        literal_profile_env_from_compose(compose_file, worker_env={"POSTGRES_PASSWORD": "resolved"})
    )
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "pw-resolved" not in "".join(carried.values())


@pytest.mark.unit
def test_collect_postgres_password_tolerates_empty_concrete_value(
    tmp_path: Path,
) -> None:
    """Worker-resolved / empty-default password forms are tolerated for redaction.

    Three redaction-set edges:

    - ``${PW?need}`` with ``PW`` set to ``""`` is ``WORKER_RESOLVED_DEFAULTED``
      (``?`` tests set-ness); ``_compose_concrete_worker_password`` recovers the
      concrete worker value ``""``. The ``if concrete:`` guard is falsy for an
      empty string, so the empty value is not added (the raw ``${...}``
      placeholder already tracked above is enough). Exercises the
      ``WORKER_RESOLVED_DEFAULTED`` falsy-exit partial branch (line 790->792).
    - A bare ``${MISSING_PW}`` slot whose variable is unset recovers ``None`` and
      skips the add (the ``WORKER_RESOLVED_SLOT`` falsy-exit branch, line
      806->exit); the raw placeholder is still a redaction target.
    - A defaulted form resolving to an empty literal (``${MISSING:-}`` with
      ``MISSING`` unset) classifies ``LITERAL`` with an empty resolved value, so
      the ``if resolution is LITERAL and resolved:`` guard is falsy and neither
      the ``DEFAULTED`` nor ``SLOT`` elif applies — the function falls through
      (branch 792->exit) having already tracked the raw placeholder.
    """
    # ? with var set to empty -> WORKER_RESOLVED_DEFAULTED, concrete "" -> falsy.
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {"POSTGRES_PASSWORD": "${PW?need}"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": "postgresql://awf@postgres:5432/awf",
                    },
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={"PW": ""}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert carried.get("DATABASE_URL") == "postgresql://awf@postgres:5432/awf"

    # Bare slot with var unset -> WORKER_RESOLVED_SLOT, concrete None -> falsy.
    compose_file.unlink()
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {"POSTGRES_PASSWORD": "${MISSING_PW}"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {"OLLAMA_HOST": "http://ollama.profile:11434"},
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"

    # Empty-default literal -> LITERAL with resolved "" -> falls through (the
    # raw placeholder is tracked, no concrete value added).
    compose_file.unlink()
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {"POSTGRES_PASSWORD": "${MISSING:-}"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {"OLLAMA_HOST": "http://ollama.profile:11434"},
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"


@pytest.mark.unit
def test_literal_profile_env_preserves_env_file_only_custom_image_postgres_url(
    tmp_path: Path,
) -> None:
    """A custom-image Postgres sidecar may declare its Postgres env only in env_file."""
    env_file = tmp_path / "db.env"
    env_file.write_text("POSTGRES_PASSWORD=env-file-secret\n", encoding="utf-8")
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "db": {
                    "image": "registry.example.com/postgres-compatible:latest",
                    "env_file": str(env_file),
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "DATABASE_URL": "postgresql://awf@db:5432/app",
                    },
                },
            }
        },
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert carried["DATABASE_URL"] == "postgresql://awf@db:5432/app"


@pytest.mark.unit
def test_literal_profile_env_redacts_postgres_password_from_string_env_file(
    tmp_path: Path,
) -> None:
    """A service may declare ``env_file`` as a single path string.

    Compose accepts ``env_file: ./db.env`` (a scalar string) in addition to the
    list form. The redaction source must read a string-form ``env_file`` so a
    ``POSTGRES_PASSWORD`` declared there redacts the rendered agent env DB URL.
    Exercises the string branch of ``_compose_service_env_file_paths`` (line 735).
    """
    env_file = tmp_path / "db.env"
    env_file.write_text("POSTGRES_PASSWORD=string-env-secret\n", encoding="utf-8")
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "env_file": str(env_file),
                    "environment": {"POSTGRES_USER": "awf"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": ("postgresql://awf:string-env-secret@postgres:5432/awf"),
                    },
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "string-env-secret" not in "".join(carried.values())


@pytest.mark.unit
def test_literal_profile_env_redacts_postgres_password_from_mapping_env_file(
    tmp_path: Path,
) -> None:
    """A service may declare ``env_file`` as a list of ``{path: ...}`` mappings.

    Compose's long-form ``env_file`` list items are mappings with a ``path``
    key. The redaction source must read the ``path`` key so a password declared
    in such an env file redacts the rendered DB URL. Exercises the Mapping-item
    branch of ``_compose_service_env_file_paths`` (lines 740-743), including a
    non-string item and a mapping whose ``path`` is not a string (both skipped
    rather than raising — branches 740->737 and 742->737).
    """
    env_file = tmp_path / "db.env"
    env_file.write_text("POSTGRES_PASSWORD=mapping-env-secret\n", encoding="utf-8")
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "env_file": [
                        # A non-string list item (int) is skipped, not raised.
                        42,
                        # A mapping whose ``path`` is not a string is skipped.
                        {"path": None},
                        # The valid mapping form whose ``path`` is read.
                        {"path": str(env_file)},
                    ],
                    "environment": {"POSTGRES_USER": "awf"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "OLLAMA_HOST": "http://ollama.profile:11434",
                        "DATABASE_URL": ("postgresql://awf:mapping-env-secret@postgres:5432/awf"),
                    },
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried
    assert "mapping-env-secret" not in "".join(carried.values())


@pytest.mark.unit
def test_literal_profile_env_tolerates_unreadable_env_file(tmp_path: Path) -> None:
    """A missing/unreadable ``env_file`` is tolerated, not fatal.

    ``_compose_service_env_file_paths`` may yield a path whose contents cannot
    be decoded (a binary/non-UTF-8 env file). ``compose_env_file_values`` raises
    ``UnicodeDecodeError`` for such a file, and the redaction source must skip it
    without raising so a profile without a usable DB env file still carries its
    non-secret literals. Exercises the ``OSError`` / ``UnicodeDecodeError``
    continue branch (lines 704-705).
    """
    env_file = tmp_path / "bad.env"
    env_file.write_bytes(b"\xff\xfePOSTGRES_PASSWORD=\xffbinary-secret\n")
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "env_file": [str(env_file)],
                    "environment": {"POSTGRES_USER": "awf"},
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {"OLLAMA_HOST": "http://ollama.profile:11434"},
                },
            }
        },
    )
    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"


@pytest.mark.unit
def test_literal_profile_env_handles_non_mapping_services(tmp_path: Path) -> None:
    """A compose file whose top-level ``services`` is not a mapping yields no carry.

    ``_try_compose_agent_env_and_postgres_passwords`` returns
    ``(None, frozenset())`` when ``services`` is absent or not a mapping (lines
    672, 675), and ``literal_profile_env_from_compose`` then returns ``()``. A
    service whose value is not a mapping is likewise skipped (line 685).
    """
    # services as a list (non-mapping) -> line 675.
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: [a, b]\n", encoding="utf-8")
    assert literal_profile_env_from_compose(compose_file, worker_env={}) == ()

    # A top-level YAML list (payload itself not a Mapping) -> line 672.
    compose_file.write_text("- a\n- b\n", encoding="utf-8")
    assert literal_profile_env_from_compose(compose_file, worker_env={}) == ()

    # A service value that is a bare string (non-mapping) is skipped -> line 685.
    compose_file.write_text(
        yaml.safe_dump({"services": {"postgres": "just-a-string"}}),
        encoding="utf-8",
    )
    assert literal_profile_env_from_compose(compose_file, worker_env={}) == ()


@pytest.mark.unit
def test_literal_profile_env_preparsed_compose_env_skips_file_passwords(
    tmp_path: Path,
) -> None:
    """A caller that pre-parses ``compose_env`` owns its own redaction context.

    Supplying ``compose_env`` skips a second read/parse of the compose file
    (line 1177) and the file-level ``POSTGRES_PASSWORD`` collection, so the
    caller must pass ``postgres_passwords`` explicitly for the same redaction.
    Without it a secret-bearing value supplied via ``compose_env`` is NOT
    redacted (the caller opted out of file-level redaction); an empty string in
    a caller-supplied ``postgres_passwords`` set is tolerated (the
    ``if not password: continue`` guard in
    ``_expanded_value_bears_postgres_password``, line 439).
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {"OLLAMA_HOST": "http://ollama.profile:11434"},
                }
            }
        },
    )
    # Pre-parsed path: an empty password in the caller-supplied set is skipped
    # (line 439), so the non-secret literal is still carried.
    carried = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={"OLLAMA_HOST": "http://ollama.profile:11434"},
            postgres_passwords=frozenset({""}),
        )
    )
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"

    # The caller can still redact by supplying the concrete password.
    carried = dict(
        literal_profile_env_from_compose(
            compose_file,
            compose_env={
                "OLLAMA_HOST": "http://ollama.profile:11434",
                "DATABASE_URL": "postgresql://awf:caller-secret@postgres:5432/awf",
            },
            postgres_passwords=frozenset({"caller-secret"}),
        )
    )
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert "DATABASE_URL" not in carried


@pytest.mark.unit
def test_literal_profile_env_redacts_db_url_password_when_postgres_passthrough_unresolved(
    tmp_path: Path,
) -> None:
    """URL userinfo still redacts DB passwords without tracked Postgres context.

    Docker Compose would resolve ``POSTGRES_PASSWORD: null`` from the worker shell.
    If the worker has no such value, there is no concrete service password for
    AWF to match, but hosted ``profile_env`` must still not carry a literal DB
    URL with an embedded password.
    """
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "postgres": {
                    "image": "postgres:16-alpine",
                    "environment": {
                        "POSTGRES_USER": "awf",
                        "POSTGRES_PASSWORD": None,
                        "POSTGRES_DB": "awf",
                    },
                },
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "DATABASE_URL": "postgresql://awf:profile-pw@postgres:5432/awf"
                    },
                },
            }
        },
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))
    assert "DATABASE_URL" not in carried


@pytest.mark.unit
def test_literal_profile_env_preserves_passwordless_git_plus_ssh_url(
    tmp_path: Path,
) -> None:
    """Passwordless ``git+ssh://git@...`` URLs are profile config, not secrets."""
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "PRIVATE_PACKAGE_URL": "git+ssh://git@github.com/org/pkg.git",
                        "TOKENIZED_PACKAGE_URL": ("git+ssh://token@github.com/org/private.git"),
                    },
                },
            }
        },
    )

    carried = dict(literal_profile_env_from_compose(compose_file, worker_env={}))

    assert carried.get("PRIVATE_PACKAGE_URL") == "git+ssh://git@github.com/org/pkg.git"
    assert "TOKENIZED_PACKAGE_URL" not in carried


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_empty_without_worker_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No worker GitHub token source yields no hosted passthrough names.

    When the worker env carries none of ``AWF_GITHUB_TOKEN`` / ``GH_TOKEN`` /
    ``GITHUB_TOKEN``, ``_github_token_placeholder`` returns ``None`` and
    ``hosted_github_token_passthrough_names`` returns ``()`` early, mirroring the
    local path (which injects nothing). The ``_github_token_source_name``
    ``return None`` fallback (line 407) is unreachable in production — a present
    placeholder always yields a present source name — and is guarded by
    ``# pragma: no cover`` rather than a hollow test.
    """
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {"GH_TOKEN": "${GH_TOKEN}"},
                }
            }
        },
    )
    assert hosted_github_token_passthrough_names(compose_file, worker_env={}) == ()


@pytest.mark.unit
def test_hosted_git_config_preserves_worker_resolved_value_block(
    tmp_path: Path,
) -> None:
    """A worker-resolved git-config value keeps its count/key block."""
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "credential.helper",
                        "GIT_CONFIG_VALUE_0": "${GIT_HELPER}",
                    },
                }
            }
        },
    )
    worker_env = {"GIT_HELPER": "!/workspace/bin/git-helper"}

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    aliases = hosted_profile_env_passthrough_aliases(compose_file, worker_env=worker_env)

    assert profile_env == {
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_COUNT": "1",
    }
    assert aliases == (("GIT_CONFIG_VALUE_0", "GIT_HELPER"),)
    assert "!/workspace/bin/git-helper" not in "".join(profile_env.values())


@pytest.mark.unit
def test_hosted_git_config_preserves_empty_worker_resolved_value_alias(
    tmp_path: Path,
) -> None:
    """A worker-resolved empty git-config value keeps its alias."""
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "credential.helper",
                        "GIT_CONFIG_VALUE_0": "${EMPTY_GIT_HELPER-default}",
                    },
                }
            }
        },
    )
    worker_env = {"EMPTY_GIT_HELPER": ""}

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    aliases = hosted_profile_env_passthrough_aliases(compose_file, worker_env=worker_env)

    assert profile_env == {
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_COUNT": "1",
    }
    assert aliases == (("GIT_CONFIG_VALUE_0", "EMPTY_GIT_HELPER"),)


@pytest.mark.unit
def test_hosted_git_config_reindexes_worker_resolved_value_aliases(
    tmp_path: Path,
) -> None:
    """Skipped git-config entries do not leave original-index aliases behind."""
    compose_file = _write(
        tmp_path,
        {
            "services": {
                "agent": {
                    "image": "agent:latest",
                    "environment": {
                        "GIT_CONFIG_COUNT": "2",
                        "GIT_CONFIG_KEY_0": "credential.helper",
                        "GIT_CONFIG_VALUE_0": "${MISSING_HELPER}",
                        "GIT_CONFIG_KEY_1": "user.name",
                        "GIT_CONFIG_VALUE_1": "${GIT_AUTHOR_NAME}",
                    },
                }
            }
        },
    )
    worker_env = {"GIT_AUTHOR_NAME": "Profile Bot"}

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    aliases = hosted_profile_env_passthrough_aliases(compose_file, worker_env=worker_env)

    assert profile_env == {
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_COUNT": "1",
    }
    assert aliases == (("GIT_CONFIG_VALUE_0", "GIT_AUTHOR_NAME"),)
    assert "GIT_CONFIG_KEY_1" not in profile_env
