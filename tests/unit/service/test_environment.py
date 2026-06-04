"""Unit tests for shared local service environment helpers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest


class _ExactKeyOnlyMapping(Mapping[str, str]):
    """Mapping that exposes exact-key lookup but rejects linear scans."""

    def __getitem__(self, key: str) -> str:
        if key == "AWF_TOKEN":
            return "direct-token"
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("env_lookup should use the exact-key fast path")

    def __len__(self) -> int:
        return 1


@pytest.mark.unit
def test_env_lookup_uses_exact_key_fast_path() -> None:
    from awf.service.environment import env_lookup

    assert env_lookup(_ExactKeyOnlyMapping(), "AWF_TOKEN") == (True, "direct-token")


@pytest.mark.unit
def test_env_lookup_fallback_uses_stable_case_variant_priority() -> None:
    from awf.service.environment import env_lookup

    assert env_lookup(
        {"docker_host": "lowercase", "Docker_Host": "mixedcase"},
        "DOCKER_HOST",
    ) == (True, "mixedcase")
    assert env_lookup(
        {"Docker_Host": "mixedcase", "docker_host": "lowercase"},
        "DOCKER_HOST",
    ) == (True, "mixedcase")


@pytest.mark.unit
def test_local_service_environ_applies_root_compose_local_defaults(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing/empty local auth and Postgres values mirror root Compose defaults."""
    from awf.service.config import local_service_environ

    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=\nAWF_POSTGRES_PASSWORD=\n", encoding="utf-8")
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("AWF_POSTGRES_PASSWORD", raising=False)

    environ = local_service_environ({}, env_file=env_file)

    assert environ["AWF_API_TOKEN"] == "local-dev-token"
    assert environ["AWF_POSTGRES_PASSWORD"] == "awf_dev"


@pytest.mark.unit
def test_local_service_environ_preserves_explicit_local_auth_and_password(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit operator values still win over the root Compose local defaults."""
    from awf.service.config import local_service_environ

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_API_TOKEN=operator-token\nAWF_POSTGRES_PASSWORD=operator-password\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("AWF_POSTGRES_PASSWORD", raising=False)

    environ = local_service_environ({}, env_file=env_file)

    assert environ["AWF_API_TOKEN"] == "operator-token"
    assert environ["AWF_POSTGRES_PASSWORD"] == "operator-password"


@pytest.mark.unit
def test_local_service_environ_preserves_raw_dollar_values_from_env_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider tokens containing `${...}` are credentials, not substitutions."""
    from awf.service.config import local_service_environ

    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN='secret-${TOKEN_SUFFIX}'\n", encoding="utf-8")
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("TOKEN_SUFFIX", raising=False)

    environ = local_service_environ({}, env_file=env_file)

    assert environ["AWF_API_TOKEN"] == "secret-${TOKEN_SUFFIX}"


@pytest.mark.unit
def test_compose_env_file_values_preserves_raw_dollar_values(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service.environment import compose_env_file_values

    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN='secret-${TOKEN_SUFFIX}'\n", encoding="utf-8")
    monkeypatch.delenv("TOKEN_SUFFIX", raising=False)

    assert compose_env_file_values(env_file)["AWF_API_TOKEN"] == "secret-${TOKEN_SUFFIX}"


@pytest.mark.unit
def test_compose_interpolation_keys_ignores_unreadable_and_non_utf8_files(
    tmp_path,
) -> None:
    from awf.service.environment import compose_interpolation_keys

    missing_file = tmp_path / "missing-compose.yml"
    invalid_file = tmp_path / "invalid-compose.yml"
    invalid_file.write_bytes(b"\xff\xfe\x00")

    assert compose_interpolation_keys(missing_file) == ()
    assert compose_interpolation_keys(invalid_file) == ()


@pytest.mark.unit
def test_compose_interpolation_environ_skips_unset_and_collects_sequence_values(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service.environment import (
        cleared_docker_cli_client_keys,
        compose_interpolation_environ,
        compose_interpolation_keys,
    )

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  api:
    environment:
      - TOKEN=${AWF_SEQUENCE_TOKEN}
      - ESCAPED=$$AWF_ESCAPED_TOKEN
      - EMPTY=${}
      - PLAIN=$AWF_PLAIN_TOKEN
""",
        encoding="utf-8",
    )

    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    assert cleared_docker_cli_client_keys({"DOCKER_CONTEXT": ""}) == frozenset()
    assert compose_interpolation_keys(compose_file) == (
        "AWF_PLAIN_TOKEN",
        "AWF_SEQUENCE_TOKEN",
    )
    assert compose_interpolation_environ(
        {"AWF_SEQUENCE_TOKEN": "service-value"},
        compose_file=compose_file,
        compose_env_file=None,
    ) == {"AWF_SEQUENCE_TOKEN": "service-value"}


@pytest.mark.unit
def test_compose_interpolation_keys_follow_included_compose_files(tmp_path) -> None:
    """The root public compose entrypoint must expose variables in included assets."""
    from awf.service.environment import compose_interpolation_keys

    root_compose = tmp_path / "compose.yaml"
    included_compose = tmp_path / "docker" / "compose" / "local-service.yml"
    included_compose.parent.mkdir(parents=True)
    root_compose.write_text(
        """
include:
  - ./docker/compose/local-service.yml
services:
  api:
    environment:
      ROOT_ONLY: ${AWF_ROOT_ONLY:-}
""",
        encoding="utf-8",
    )
    included_compose.write_text(
        """
services:
  worker:
    environment:
      CURSOR_API_KEY: ${CURSOR_API_KEY:-}
      AWF_API_TOKEN: ${AWF_API_TOKEN:?set AWF_API_TOKEN}
""",
        encoding="utf-8",
    )

    assert compose_interpolation_keys(root_compose) == (
        "AWF_API_TOKEN",
        "AWF_ROOT_ONLY",
        "CURSOR_API_KEY",
    )


@pytest.mark.unit
def test_compose_interpolation_cache_evicts_old_successes(monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.service import environment as service_environment

    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
    service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001
    monkeypatch.setattr(
        service_environment,
        "_parse_compose_interpolation_keys",
        lambda contents: (contents,),
    )

    try:
        for index in range(service_environment._COMPOSE_INTERPOLATION_CACHE_MAX_SIZE + 1):
            assert service_environment._cached_compose_interpolation_keys(  # noqa: SLF001
                f"/tmp/compose-{index}.yml",
                f"digest-{index}",
                index,
                f"KEY_{index}",
            ) == (f"KEY_{index}",)

        cache_keys = service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE  # noqa: SLF001
        assert len(cache_keys) == service_environment._COMPOSE_INTERPOLATION_CACHE_MAX_SIZE
        assert ("/tmp/compose-0.yml", "digest-0", 0) not in cache_keys
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
        service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001


@pytest.mark.unit
def test_compose_interpolation_cache_evicts_old_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.service import environment as service_environment

    def _raise_runtime(_contents: str) -> tuple[str, ...]:
        raise RuntimeError("parse failed")

    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
    service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001
    monkeypatch.setattr(service_environment, "_parse_compose_interpolation_keys", _raise_runtime)

    try:
        for index in range(service_environment._COMPOSE_INTERPOLATION_CACHE_MAX_SIZE + 1):
            with pytest.raises(RuntimeError, match="parse failed"):
                service_environment._cached_compose_interpolation_keys(  # noqa: SLF001
                    f"/tmp/failing-compose-{index}.yml",
                    f"failing-digest-{index}",
                    index,
                    f"KEY_{index}",
                )

        cache_keys = service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE  # noqa: SLF001
        assert len(cache_keys) == service_environment._COMPOSE_INTERPOLATION_CACHE_MAX_SIZE
        assert ("/tmp/failing-compose-0.yml", "failing-digest-0", 0) not in cache_keys
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
        service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001


@pytest.mark.unit
def test_compose_interpolation_cache_clears_inflight_after_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import environment as service_environment

    def _raise_keyboard_interrupt(_contents: str) -> tuple[str, ...]:
        raise KeyboardInterrupt

    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
    service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001
    monkeypatch.setattr(
        service_environment,
        "_parse_compose_interpolation_keys",
        _raise_keyboard_interrupt,
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            service_environment._cached_compose_interpolation_keys(  # noqa: SLF001
                "/tmp/interrupt-compose.yml",
                "interrupt-digest",
                1,
                "KEY",
            )
        assert service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT == {}  # noqa: SLF001
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
        service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001


@pytest.mark.unit
def test_raise_compose_interpolation_cache_failure_wraps_unconstructable_exception() -> None:
    from awf.service import environment as service_environment

    class _NeedsTwoArgsError(Exception):
        def __init__(self, first: str, second: str) -> None:
            super().__init__(first, second)

    with pytest.raises(RuntimeError, match="cached parser failure"):
        service_environment._raise_compose_interpolation_cache_failure(  # noqa: SLF001
            service_environment._ComposeInterpolationKeysCacheFailure(  # noqa: SLF001
                _NeedsTwoArgsError,
                "cached parser failure",
            )
        )
