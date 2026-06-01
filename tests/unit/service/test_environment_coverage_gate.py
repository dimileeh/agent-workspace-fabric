"""Coverage-gate unit tests for the Compose interpolation-keys cache.

These tests exercise previously-uncovered *partial* branches in
:mod:`awf.service.environment`, all inside ``_cached_compose_interpolation_keys``.

Each of the three finalization paths (clean success, ``except Exception``, and
``except BaseException``) guards its in-flight-marker cleanup with::

    if _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key) is inflight:
        del _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key]

The *False* arc of that guard -- where another worker has already replaced the
in-flight marker, so this call must NOT delete the newer event -- was uncovered
(coverage reports the partial arcs ``222->224``, ``228->230``, ``237->239``).
We reach it deterministically and single-threaded by monkeypatching the parser
so that, while it runs, it swaps ``_COMPOSE_INTERPOLATION_KEYS_INFLIGHT[key]``
to a *different* ``threading.Event`` (exactly what a racing worker would do).
After the call we assert the newer event survived (was not deleted), proving the
False arc executed and the function correctly avoided clobbering another worker.

Everything is pure: no real Docker, DB, network, or sleeping. Each test uses a
unique cache key and the module-level caches are reset around each test for
isolation.
"""

from __future__ import annotations

import threading

import pytest

from awf.service import environment


@pytest.fixture(autouse=True)
def _reset_interpolation_cache():
    """Clear the module-level interpolation caches before and after each test."""

    environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()
    environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()
    yield
    environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()
    environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()


def _install_racing_parser(
    monkeypatch: pytest.MonkeyPatch,
    cache_key: tuple[str, str, int],
    replacement: threading.Event,
    action,
):
    """Patch the parser so it replaces the in-flight marker mid-parse.

    This emulates another worker taking over ``cache_key`` while this call's
    parse runs, then performs ``action`` (return keys or raise).
    """

    def _parser(_contents: str):
        environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key] = replacement
        return action()

    monkeypatch.setattr(environment, "_parse_compose_interpolation_keys", _parser)


@pytest.mark.unit
def test_success_path_skips_deleting_newer_inflight_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean-parse finalization must not delete a newer in-flight marker.

    Covers the False arc of the success-path guard (coverage arc ``237->239``):
    when another worker has replaced the in-flight event, this call still caches
    and returns its parsed keys but leaves the newer marker in place.
    """

    cache_key = ("/tmp/race-success.yml", "digest-success", 11)
    replacement = threading.Event()
    _install_racing_parser(monkeypatch, cache_key, replacement, lambda: ("RACE_KEY",))

    result = environment._cached_compose_interpolation_keys(*cache_key, "RACE_KEY")

    assert result == ("RACE_KEY",)
    # Our parse still cached its result for this key.
    assert environment._COMPOSE_INTERPOLATION_KEYS_CACHE[cache_key] == ("RACE_KEY",)
    # The newer worker's marker survived: the guard's False arc skipped the del.
    assert environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key] is replacement


@pytest.mark.unit
def test_exception_path_skips_deleting_newer_inflight_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``except Exception`` finalization must not delete a newer in-flight marker.

    Covers the False arc of the exception-path guard (coverage arc ``222->224``):
    the failure is cached and the original error propagates, but the newer
    worker's in-flight marker is left intact.
    """

    cache_key = ("/tmp/race-exception.yml", "digest-exception", 22)
    replacement = threading.Event()

    def _raise() -> tuple[str, ...]:
        raise ValueError("boom during race")

    _install_racing_parser(monkeypatch, cache_key, replacement, _raise)

    with pytest.raises(ValueError, match="boom during race"):
        environment._cached_compose_interpolation_keys(*cache_key, "RACE_KEY")

    # The failure sentinel was cached for this key.
    cached = environment._COMPOSE_INTERPOLATION_KEYS_CACHE[cache_key]
    assert isinstance(cached, environment._ComposeInterpolationKeysCacheFailure)
    assert cached.exception_type is ValueError
    # The newer worker's marker survived: the guard's False arc skipped the del.
    assert environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key] is replacement


@pytest.mark.unit
def test_base_exception_path_skips_deleting_newer_inflight_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``except BaseException`` finalization must not delete a newer marker.

    Covers the False arc of the base-exception guard (coverage arc ``228->230``):
    a ``KeyboardInterrupt`` propagates, nothing is cached, and the newer worker's
    in-flight marker is left intact.
    """

    cache_key = ("/tmp/race-baseexc.yml", "digest-baseexc", 33)
    replacement = threading.Event()

    def _interrupt() -> tuple[str, ...]:
        raise KeyboardInterrupt

    _install_racing_parser(monkeypatch, cache_key, replacement, _interrupt)

    with pytest.raises(KeyboardInterrupt):
        environment._cached_compose_interpolation_keys(*cache_key, "RACE_KEY")

    # BaseException path caches nothing for this key.
    assert cache_key not in environment._COMPOSE_INTERPOLATION_KEYS_CACHE
    # The newer worker's marker survived: the guard's False arc skipped the del.
    assert environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key] is replacement


@pytest.mark.unit
def test_collect_compose_interpolation_keys_records_captured_name() -> None:
    """``_collect_compose_interpolation_keys`` records non-empty variable names.

    Exercises the recording branch of the ``if key:`` guard by walking a nested
    structure containing both braced and plain interpolations, confirming the
    names are collected and de-duplicated.
    """

    collected: set[str] = set()
    payload = {
        "services": {
            "web": {
                "image": "${IMAGE}",
                "command": "run --token $PLAIN_TOKEN",
                "ports": ["${HOST_PORT}:8080"],
                "labels": ["repeat=${IMAGE}"],
            }
        }
    }

    environment._collect_compose_interpolation_keys(payload, collected)

    assert collected == {"IMAGE", "PLAIN_TOKEN", "HOST_PORT"}
