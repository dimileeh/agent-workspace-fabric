"""Unit tests for the dynamic Ollama discovery + auto-pull helper.

Covers ``ensure_ollama_model_available`` (issue #552): the host Ollama daemon is
the source of truth, an absent non-cloud model is auto-pulled, ``:cloud`` models
are served remotely without a pull, and every failure resolves to a clear
``OLLAMA_MODEL_PULL_FAILED`` / ``OLLAMA_MODEL_PROBE_FAILED`` reason code instead
of a confusing agent-CLI failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from awf.service.provider_readiness_helpers import (
    _is_cloud_model,
    _ollama_pull_name,
    _ollama_pull_urls,
    ensure_ollama_model_available,
)

_TAGS_URLS = ("http://ollama.local:11434/api/tags",)
_PULL_URLS = ("http://ollama.local:11434/api/pull",)


class _FakeStreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        lines: tuple[str, ...] = (),
        iter_exc: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._lines = list(lines)
        self._iter_exc = iter_exc

    def iter_lines(self) -> Iterator[str]:
        if self._iter_exc is not None:
            raise self._iter_exc
        yield from self._lines


class _Ctx:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStreamResponse:
        return self._response

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakePostStream:
    def __init__(
        self,
        response: _FakeStreamResponse | None = None,
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[SimpleNamespace] = []
        self._response = response
        self._raise_exc = raise_exc

    def __call__(self, url: str, *, json: Mapping[str, Any], timeout: float) -> _Ctx:
        self.calls.append(SimpleNamespace(url=url, json=dict(json), timeout=timeout))
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._response is not None
        return _Ctx(self._response)


def _unexpected_post_stream(url: str, *, json: Mapping[str, Any], timeout: float) -> _Ctx:
    raise AssertionError(f"unexpected pull POST to {url}")


def _tags_response(models: tuple[str, ...]) -> Any:
    payload = {"models": [{"name": name} for name in models]}
    return SimpleNamespace(status_code=200, text=json.dumps(payload))


def _http_get_returning(models: tuple[str, ...]) -> Any:
    def _get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return _tags_response(models)

    return _get


@pytest.mark.unit
def test_model_already_in_tags_does_not_pull() -> None:
    post = _FakePostStream()
    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("llama4:70b",)),
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_AVAILABLE"
    assert result["status"] == "ok"
    assert post.calls == []


@pytest.mark.unit
def test_absent_non_cloud_model_is_pulled_then_used() -> None:
    # /api/tags misses the model first, then reports it after the pull.
    responses = iter(
        [
            _tags_response(("other:latest",)),
            _tags_response(("llama4:70b",)),
        ]
    )

    def _get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return next(responses)

    post = _FakePostStream(
        _FakeStreamResponse(
            lines=(
                json.dumps({"status": "pulling manifest"}),
                json.dumps({"status": "downloading", "completed": 10, "total": 100}),
                json.dumps({"status": "success"}),
            )
        )
    )
    progress: list[str] = []

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=post,
        secrets=frozenset(),
        on_progress=progress.append,
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PULLED"
    assert result["status"] == "ok"
    assert len(post.calls) == 1
    assert post.calls[0].json == {
        "model": "llama4:70b",
        "name": "llama4:70b",
        "stream": True,
    }
    assert post.calls[0].url == "http://ollama.local:11434/api/pull"
    # Streamed progress lines reach the callback.
    assert any("pulling manifest" in line for line in progress)


@pytest.mark.unit
def test_cloud_model_absent_from_tags_is_used_without_pull() -> None:
    post = _FakePostStream()
    result = ensure_ollama_model_available(
        model="ollama/kimi-k2.7:cloud",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_CLOUD"
    assert result["status"] == "ok"
    assert post.calls == []


@pytest.mark.unit
def test_cloud_model_with_unreachable_daemon_probe_fails_without_pull() -> None:
    # A cloud model is still reached through the local host Ollama daemon (the
    # adapter points provider.ollama at host.docker.internal:11434). If the daemon
    # is down at agent-launch time the pre-agent step must surface the clear
    # OLLAMA_MODEL_PROBE_FAILED reason rather than short-circuit to a success that
    # later collapses into a confusing AGENT_CLI_FAILED.
    def _get(url: str, *, timeout: float) -> Any:
        raise httpx.ConnectError("connection refused")

    result = ensure_ollama_model_available(
        model="ollama/kimi-k2.7:cloud",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=_unexpected_post_stream,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert result["reason_code"] != "AGENT_CLI_FAILED"


@pytest.mark.unit
def test_pull_daemon_error_maps_to_pull_failed() -> None:
    post = _FakePostStream(
        _FakeStreamResponse(
            lines=(
                json.dumps({"status": "pulling manifest"}),
                json.dumps({"error": "pull model manifest: file does not exist"}),
            )
        )
    )

    result = ensure_ollama_model_available(
        model="ollama/does-not-exist:latest",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    assert result["reason_code"] != "AGENT_CLI_FAILED"
    assert "file does not exist" in result["detail"]


@pytest.mark.unit
def test_pull_recoverable_error_before_success_is_not_failure() -> None:
    # A recoverable error line followed by a terminal success must not be
    # treated as a failed pull: the daemon finished successfully.
    responses = iter(
        [
            _tags_response(("other:latest",)),
            _tags_response(("llama4:70b",)),
        ]
    )

    def _get(url: str, *, timeout: float) -> Any:
        return next(responses)

    post = _FakePostStream(
        _FakeStreamResponse(
            lines=(
                json.dumps({"status": "pulling manifest"}),
                json.dumps({"error": "max retries exceeded, retrying"}),
                json.dumps({"status": "success"}),
            )
        )
    )

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["status"] == "ok"
    assert result["reason_code"] == "OLLAMA_MODEL_PULLED"


@pytest.mark.unit
def test_pull_timeout_maps_to_pull_failed() -> None:
    post = _FakePostStream(raise_exc=httpx.TimeoutException("pull read timed out"))

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=post,
        secrets=frozenset(),
        timeout=1800.0,
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    assert "TimeoutException" in result["detail"]
    assert post.calls[0].timeout == 1800.0


@pytest.mark.unit
def test_pull_stream_never_terminating_is_bounded_by_wall_clock() -> None:
    # A daemon whose /api/pull keeps streaming progress lines but never emits a
    # terminal status must not hang the executor thread forever: httpx's timeout
    # only bounds each read (it resets on every NDJSON line), so a total
    # wall-clock deadline has to surface OLLAMA_MODEL_PULL_FAILED after the bound.
    def _endless_lines() -> Iterator[str]:
        while True:
            yield json.dumps({"status": "downloading", "completed": 1, "total": 100})

    class _EndlessResponse:
        status_code = 200

        def iter_lines(self) -> Iterator[str]:
            return _endless_lines()

    class _EndlessPost:
        def __call__(self, url: str, *, json: Mapping[str, Any], timeout: float) -> _Ctx:
            return _Ctx(_EndlessResponse())  # type: ignore[arg-type]

    # A monotonic clock that advances one second per call: the deadline of
    # ``timeout`` seconds is crossed after a bounded number of progress lines.
    ticks = iter(range(10_000))

    def _monotonic() -> float:
        return float(next(ticks))

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=_EndlessPost(),
        secrets=frozenset(),
        timeout=3.0,
        monotonic=_monotonic,
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    assert "timeout" in result["detail"].lower()


@pytest.mark.unit
def test_expired_deadline_skips_fallback_url() -> None:
    # Once the shared wall-clock deadline is reached on the first URL, the
    # localhost fallback must not be opened with a fresh full timeout — that
    # would let the intended total bound be exceeded substantially. The loop
    # stops and surfaces OLLAMA_MODEL_PULL_FAILED instead of attempting more URLs.
    pull_urls = (
        "http://host.docker.internal:11434/api/pull",
        "http://localhost:11434/api/pull",
    )

    def _endless_lines() -> Iterator[str]:
        while True:
            yield json.dumps({"status": "downloading", "completed": 1, "total": 100})

    class _EndlessResponse:
        status_code = 200

        def iter_lines(self) -> Iterator[str]:
            return _endless_lines()

    class _FirstUrlEndlessPost:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, url: str, *, json: Mapping[str, Any], timeout: float) -> _Ctx:
            self.calls.append(url)
            if url.startswith("http://host.docker.internal"):
                return _Ctx(_EndlessResponse())  # type: ignore[arg-type]
            raise AssertionError("fallback URL must not be opened after deadline")

    ticks = iter(range(10_000))

    def _monotonic() -> float:
        return float(next(ticks))

    post = _FirstUrlEndlessPost()
    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=pull_urls,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=post,
        secrets=frozenset(),
        timeout=3.0,
        monotonic=_monotonic,
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    assert "timeout" in result["detail"].lower()
    # Only the first URL was opened; the fallback was skipped once the deadline
    # had already elapsed.
    assert post.calls == ["http://host.docker.internal:11434/api/pull"]


@pytest.mark.unit
def test_pull_non_transport_exception_propagates() -> None:
    # A non-transport error (e.g. a programming bug, not an httpx transport
    # failure) must surface rather than being masked as OLLAMA_MODEL_PULL_FAILED
    # during URL retries.
    post = _FakePostStream(raise_exc=RuntimeError("boom: not a transport error"))

    with pytest.raises(RuntimeError, match="boom"):
        ensure_ollama_model_available(
            model="ollama/llama4:70b",
            tags_urls=_TAGS_URLS,
            pull_urls=_PULL_URLS,
            http_get=_http_get_returning(("other:latest",)),
            http_post_stream=post,
            secrets=frozenset(),
        )


@pytest.mark.unit
def test_daemon_unreachable_for_tags_does_not_pull() -> None:
    def _get(url: str, *, timeout: float) -> Any:
        raise httpx.ConnectError("connection refused")

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=_unexpected_post_stream,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"


@pytest.mark.unit
def test_concurrent_pull_dedup_recheck_finds_model() -> None:
    # First check misses the model; the stream emits only progress (no explicit
    # success), but a concurrent pull made it appear on the re-check.
    responses = iter(
        [
            _tags_response(("other:latest",)),
            _tags_response(("llama4:70b",)),
        ]
    )

    def _get(url: str, *, timeout: float) -> Any:
        return next(responses)

    post = _FakePostStream(_FakeStreamResponse(lines=(json.dumps({"status": "downloading"}),)))

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PULLED"


@pytest.mark.unit
def test_pull_completes_but_model_still_absent_is_pull_failed() -> None:
    # The pull stream reports success, yet the re-check still misses the model.
    def _get(url: str, *, timeout: float) -> Any:
        return _tags_response(("other:latest",))

    post = _FakePostStream(_FakeStreamResponse(lines=(json.dumps({"status": "success"}),)))

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    assert "still unavailable" in result["message"]


@pytest.mark.unit
def test_pull_non_2xx_status_maps_to_pull_failed() -> None:
    post = _FakePostStream(_FakeStreamResponse(status_code=500, lines=()))

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    assert "HTTP 500" in result["detail"]


@pytest.mark.unit
def test_no_model_selected_is_not_pulled() -> None:
    result = ensure_ollama_model_available(
        model=None,
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=_unexpected_post_stream,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "MODEL_NOT_SELECTED"


@pytest.mark.unit
def test_unexpected_probe_disposition_is_not_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A probe reason code that is neither available/not-selected/probe-failed nor
    # the pullable NOT_AVAILABLE (e.g. a future non-pull disposition) must surface
    # the raw probe result instead of implicitly falling through to a pull.
    probe = {
        "status": "fail",
        "reason_code": "OLLAMA_MODEL_FUTURE_DISPOSITION",
        "message": "hypothetical future non-pull disposition",
    }
    monkeypatch.setattr(
        "awf.service.provider_readiness_helpers._probe_ollama_model",
        lambda *_args, **_kwargs: dict(probe),
    )

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=_unexpected_post_stream,
        secrets=frozenset(),
    )

    assert result == probe


@pytest.mark.unit
def test_pull_output_and_progress_are_redacted() -> None:
    secret = "ollama-secret-token-value"
    post = _FakePostStream(
        _FakeStreamResponse(
            lines=(
                json.dumps({"status": f"connecting with {secret}"}),
                json.dumps({"error": f"unauthorized for {secret}"}),
            )
        )
    )
    progress: list[str] = []

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=post,
        secrets=frozenset({secret}),
        on_progress=progress.append,
    )

    blob = json.dumps(result, sort_keys=True) + "".join(progress)
    assert secret not in blob
    assert "<redacted>" in blob
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"


@pytest.mark.unit
def test_pull_stream_tolerates_blank_and_non_json_and_non_mapping_lines() -> None:
    responses = iter(
        [
            _tags_response(("other:latest",)),
            _tags_response(("llama4:70b",)),
        ]
    )

    def _get(url: str, *, timeout: float) -> Any:
        return next(responses)

    post = _FakePostStream(
        _FakeStreamResponse(
            lines=(
                "",  # blank line is skipped
                "not-json",  # invalid JSON is tolerated
                json.dumps([1, 2, 3]),  # non-mapping JSON is tolerated
                json.dumps({"status": "success"}),
            )
        )
    )
    progress: list[str] = []

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=_PULL_URLS,
        http_get=_get,
        http_post_stream=post,
        secrets=frozenset(),
        on_progress=progress.append,
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PULLED"
    # The blank line is dropped; the other lines still stream as progress.
    assert "" not in progress
    assert "not-json" in progress


@pytest.mark.unit
def test_pull_tries_multiple_urls_until_one_succeeds() -> None:
    pull_urls = (
        "http://host.docker.internal:11434/api/pull",
        "http://localhost:11434/api/pull",
    )
    responses = iter(
        [
            _tags_response(("other:latest",)),
            _tags_response(("llama4:70b",)),
        ]
    )

    def _get(url: str, *, timeout: float) -> Any:
        return next(responses)

    class _MultiUrlPost:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, url: str, *, json: Mapping[str, Any], timeout: float) -> _Ctx:
            self.calls.append(url)
            if url.startswith("http://host.docker.internal"):
                raise httpx.ConnectError("host gateway refused")
            return _Ctx(_FakeStreamResponse(lines=(json_dumps_success(),)))

    def json_dumps_success() -> str:
        return json.dumps({"status": "success"})

    post = _MultiUrlPost()
    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=pull_urls,
        http_get=_get,
        http_post_stream=post,
        secrets=frozenset(),
    )

    assert result["reason_code"] == "OLLAMA_MODEL_PULLED"
    assert post.calls == list(pull_urls)


@pytest.mark.unit
def test_pull_all_urls_fail_reports_each_failure() -> None:
    pull_urls = (
        "http://host.docker.internal:11434/api/pull",
        "http://localhost:11434/api/pull",
    )

    error_line = json.dumps({"error": "manifest missing"})

    class _AllFailPost:
        def __call__(self, url: str, *, json: Mapping[str, Any], timeout: float) -> _Ctx:
            if url.startswith("http://host.docker.internal"):
                return _Ctx(_FakeStreamResponse(status_code=503, lines=()))
            return _Ctx(_FakeStreamResponse(lines=(error_line,)))

    result = ensure_ollama_model_available(
        model="ollama/llama4:70b",
        tags_urls=_TAGS_URLS,
        pull_urls=pull_urls,
        http_get=_http_get_returning(("other:latest",)),
        http_post_stream=_AllFailPost(),
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_FAILED"
    # Both per-URL failures are surfaced (HTTP error + daemon error).
    assert "HTTP 503" in result["detail"]
    assert "manifest missing" in result["detail"]


@pytest.mark.unit
def test_helpers_classify_cloud_and_pull_name_and_urls() -> None:
    assert _is_cloud_model("ollama/kimi-k2.7:cloud") is True
    # Size-qualified ``-cloud`` tags are also Ollama Cloud models served
    # remotely (e.g. the ``gemma4:31b-cloud`` fallback entry).
    assert _is_cloud_model("ollama/gemma4:31b-cloud") is True
    assert _is_cloud_model("gpt-oss:120b-cloud") is True
    assert _is_cloud_model("llama4:70b") is False
    # A model name ending in ``-cloud`` without a tag is not a cloud model.
    assert _is_cloud_model("my-cloud") is False
    assert _is_cloud_model(None) is False
    assert _ollama_pull_name("ollama/llama4:70b") == "llama4:70b"
    assert _ollama_pull_name("llama4:70b") == "llama4:70b"
    # A non-ollama provider prefix is preserved (only ``ollama/`` is stripped).
    assert _ollama_pull_name("openai/gpt-x") == "openai/gpt-x"
    assert _ollama_pull_name(None) == ""
    urls = _ollama_pull_urls({"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"})
    assert urls == ("http://ollama.local:11434/api/pull",)
