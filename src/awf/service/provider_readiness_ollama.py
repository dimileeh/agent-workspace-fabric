"""Ollama daemon probe and model-pull helpers for provider readiness.

Extracted from ``provider_readiness_helpers`` to keep each first-party module
under the maintainability line limit. These helpers carry out the version/tags
probe and the model availability/pull flow against the Ollama daemon; they are a
downstream concern called by ``_check_opencode`` and the create/retry admission
path, and depend on the URL/model helpers in ``provider_readiness_helpers``, the
HTTP Protocols and timeout constants in ``provider_readiness``, and the redaction
helpers in ``provider_readiness_redaction`` (all imported at module end to mirror
the established late-binding import ordering).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx


def _ollama_probe_failure_debug(
    *,
    url: str,
    status: str,
    detail: str,
    secrets: frozenset[str],
    status_code: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": _redact(url, secrets),
        "status": status,
        "detail": _truncate(_redact(detail, secrets)),
    }
    if status_code is not None:
        payload["status_code"] = status_code
    return payload


def _probe_ollama(
    urls: tuple[str, ...],
    *,
    http_get: HttpGet,
    secrets: frozenset[str],
) -> dict[str, Any]:
    failures: list[str] = []
    http_failures: list[str] = []
    recovered_failures: list[dict[str, Any]] = []
    exceptions: list[Exception] = []
    for url in urls:
        try:
            response = http_get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # httpx transport failures *and* a syntactically invalid probe URL
            # (``httpx.InvalidURL`` — e.g. an unresolved ``${OLLAMA_HOST}``
            # placeholder or a bad percent escape from operator config) become
            # probe "exception" dispositions, mirroring ``_pull_ollama_model``
            # below and the ``smoke._default_console_checker`` guard. ``InvalidURL``
            # is not an ``httpx.HTTPError`` subclass, so it must be named
            # explicitly or an operator config error would escape as an unhandled
            # raise instead of a structured, redacted readiness result. Other
            # exceptions remain wrapper/programming bugs that must surface.
            exceptions.append(exc)
            detail = f"{type(exc).__name__}: {exc}"
            failures.append(f"{url}: {detail}" if len(urls) > 1 else detail)
            recovered_failures.append(
                _ollama_probe_failure_debug(
                    url=url,
                    status="exception",
                    detail=detail,
                    secrets=secrets,
                )
            )
            continue
        if 200 <= response.status_code < 300:
            payload: dict[str, Any] = {"ok": True}
            if recovered_failures:
                payload["debug"] = {"recovered_failures": recovered_failures}
            return payload
        detail = response.text or f"HTTP {response.status_code}"
        failure = f"HTTP {response.status_code}: {detail}"
        failure_detail = f"{url}: {failure}" if len(urls) > 1 else failure
        failures.append(failure_detail)
        http_failures.append(failure_detail)
        recovered_failures.append(
            _ollama_probe_failure_debug(
                url=url,
                status="http_error",
                status_code=response.status_code,
                detail=failure,
                secrets=secrets,
            )
        )
    for logged_exc in exceptions:
        _log_redacted_exception(
            "provider_readiness.ollama_probe_exception",
            logged_exc,
            secrets,
        )
    if http_failures:
        _log_redacted_terminal_failure(
            "provider_readiness.ollama_probe_exception",
            "; ".join(http_failures),
            secrets,
        )
    return {"ok": False, "detail": _redact("; ".join(failures), secrets)}


def _probe_ollama_model(
    urls: tuple[str, ...],
    *,
    model: str | None,
    http_get: HttpGet,
    secrets: frozenset[str],
    allow_cloud: bool = False,
    pull_pending_ok: bool = False,
) -> dict[str, Any]:
    candidates = _ollama_model_candidates(model)
    if not candidates:
        return {
            "status": "fail",
            "reason_code": "MODEL_NOT_SELECTED",
            "message": "No OpenCode/Ollama model was selected for launch.",
        }

    failures: list[str] = []
    exceptions: list[Exception] = []
    recovered_failures: list[dict[str, Any]] = []
    available_models: set[str] = set()
    saw_model_response = False
    for url in urls:
        try:
            response = http_get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # httpx transport failures *and* a syntactically invalid probe URL
            # (``httpx.InvalidURL`` — e.g. an unresolved ``${OLLAMA_HOST}``
            # placeholder or a bad percent escape from operator config) become
            # probe "exception" dispositions, mirroring ``_pull_ollama_model``
            # below and the ``smoke._default_console_checker`` guard. ``InvalidURL``
            # is not an ``httpx.HTTPError`` subclass, so it must be named
            # explicitly or an operator config error would escape as an unhandled
            # raise instead of a structured, redacted readiness result. Other
            # exceptions remain wrapper/programming bugs that must surface.
            exceptions.append(exc)
            detail = f"{type(exc).__name__}: {exc}"
            failures.append(f"{url}: {detail}" if len(urls) > 1 else detail)
            recovered_failures.append(
                _ollama_probe_failure_debug(
                    url=url,
                    status="exception",
                    detail=detail,
                    secrets=secrets,
                )
            )
            continue
        if not 200 <= response.status_code < 300:
            detail = response.text or f"HTTP {response.status_code}"
            failure = f"HTTP {response.status_code}: {detail}"
            failures.append(f"{url}: {failure}" if len(urls) > 1 else failure)
            recovered_failures.append(
                _ollama_probe_failure_debug(
                    url=url,
                    status="http_error",
                    status_code=response.status_code,
                    detail=failure,
                    secrets=secrets,
                )
            )
            continue
        try:
            payload = json.loads(response.text or "{}")
        except json.JSONDecodeError as exc:
            failures.append(
                f"{url}: invalid JSON from Ollama /api/tags: {exc}"
                if len(urls) > 1
                else f"invalid JSON from Ollama /api/tags: {exc}"
            )
            continue

        available = _ollama_model_names(payload)
        if candidates & available:
            result: dict[str, Any] = {
                "status": "ok",
                "reason_code": "OLLAMA_MODEL_AVAILABLE",
            }
            if recovered_failures:
                result["debug"] = {"recovered_failures": recovered_failures}
            return result
        saw_model_response = True
        available_models.update(available)

    if saw_model_response:
        _log_ollama_model_probe_exceptions(exceptions, secrets)
        detail = f"selected={model}; available_count={len(available_models)}"
        if failures:
            detail = f"{detail}; probe_failures={'; '.join(failures)}"
        redacted_detail = _truncate(_redact(detail, secrets))
        # The daemon answered but does not (yet) serve the model. A ``:cloud``
        # model is served remotely (never pulled), and an absent non-cloud model
        # is pullable — both are non-blocking launch dispositions when the caller
        # opts in. Otherwise this stays the historical hard "not available" fail.
        if allow_cloud and _is_cloud_model(model):
            return {
                "status": "ok",
                "reason_code": "OLLAMA_MODEL_CLOUD",
                "message": "Selected Ollama Cloud model is served remotely; no local pull required.",
                "detail": redacted_detail,
            }
        if pull_pending_ok:
            return {
                "status": "pending",
                "reason_code": "OLLAMA_MODEL_PULL_PENDING",
                "message": (
                    "Selected Ollama model is not present locally yet; "
                    "AWF will pull it before the agent runs."
                ),
                "detail": redacted_detail,
            }
        return {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_NOT_AVAILABLE",
            "message": "Selected OpenCode/Ollama model is not available from Ollama /api/tags.",
            "detail": redacted_detail,
        }

    _log_ollama_model_probe_exceptions(exceptions, secrets)

    return {
        "status": "fail",
        "reason_code": "OLLAMA_MODEL_PROBE_FAILED",
        "message": "Ollama model availability probe did not complete successfully.",
        "detail": _truncate(_redact("; ".join(failures), secrets)),
    }


def _log_ollama_model_probe_exceptions(
    exceptions: Sequence[Exception],
    secrets: frozenset[str],
) -> None:
    for logged_exc in exceptions:
        _log_redacted_exception(
            "provider_readiness.ollama_model_probe_exception",
            logged_exc,
            secrets,
        )


def _ollama_model_candidates(model: str | None) -> set[str]:
    if model is None:
        return set()
    raw = model.strip()
    if not raw:
        return set()
    candidates = {raw}
    model_name = raw
    if "/" in raw:
        provider, remainder = raw.split("/", 1)
        if provider == "ollama" and remainder:
            model_name = remainder
            candidates.add(model_name)
    if ":" not in model_name:
        candidates.add(f"{model_name}:latest")
    return candidates


def _ollama_model_names(payload: object) -> set[str]:
    if not isinstance(payload, Mapping):
        return set()
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return set()
    names: set[str] = set()
    for item in raw_models:
        if isinstance(item, str) and item:
            names.add(item)
            continue
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("model")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def ensure_ollama_model_available(
    *,
    model: str | None,
    tags_urls: tuple[str, ...],
    pull_urls: tuple[str, ...],
    http_get: HttpGet,
    http_post_stream: HttpPostStream,
    secrets: frozenset[str],
    timeout: float | None = None,
    on_progress: Callable[[str], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Discover, classify, and (if needed) auto-pull the requested Ollama model.

    The host Ollama daemon is the source of truth. Dispositions:

    - already in ``/api/tags`` → ``OLLAMA_MODEL_AVAILABLE`` (no pull);
    - daemon reachable + ``:cloud`` model → ``OLLAMA_MODEL_CLOUD`` (served
      remotely, no pull; the daemon still proxies cloud requests, so it must be
      up);
    - daemon unreachable → ``OLLAMA_MODEL_PROBE_FAILED`` (no pull, no hang;
      applies to cloud models too);
    - absent non-cloud → ``POST /api/pull`` with bounded ``timeout`` and streamed
      (redacted) progress, then re-check ``/api/tags``: success →
      ``OLLAMA_MODEL_PULLED``; daemon error / timeout / still-missing →
      ``OLLAMA_MODEL_PULL_FAILED`` carrying the redacted daemon message.

    Returns a structured ``{"status", "reason_code", "message"[, "detail"]}``.
    """

    pull_timeout = _OLLAMA_PULL_TIMEOUT_SECONDS if timeout is None else timeout

    # An Ollama Cloud model is served remotely and is never pulled, but OpenCode
    # still reaches it *through the local host Ollama daemon* (the adapter points
    # ``provider.ollama`` at ``host.docker.internal:11434``). So the daemon must
    # still be reachable at agent-launch time even for a cloud model. Probe
    # ``/api/tags`` with ``allow_cloud`` — a daemon that answers resolves a cloud
    # tag (absent from the local catalog) to ``OLLAMA_MODEL_CLOUD``, while a
    # daemon that has gone down between create-time readiness and execution
    # surfaces the clear ``OLLAMA_MODEL_PROBE_FAILED`` reason this pre-agent step
    # exists to provide, instead of a confusing downstream ``AGENT_CLI_FAILED``.
    probe = _probe_ollama_model(
        tags_urls, model=model, http_get=http_get, secrets=secrets, allow_cloud=True
    )
    reason = probe.get("reason_code")
    if reason == "OLLAMA_MODEL_AVAILABLE":
        return {
            "status": "ok",
            "reason_code": "OLLAMA_MODEL_AVAILABLE",
            "message": "Selected Ollama model is already available from /api/tags.",
        }
    if reason == "OLLAMA_MODEL_CLOUD":
        # Daemon reachable; the selected cloud model is served remotely (no pull).
        return dict(probe)
    if reason in {"MODEL_NOT_SELECTED", "OLLAMA_MODEL_PROBE_FAILED"}:
        # No selectable model, or the daemon never answered: do not pull.
        return dict(probe)
    if reason != "OLLAMA_MODEL_NOT_AVAILABLE":
        # Unexpected probe disposition (e.g. a future non-pull reason code): do
        # not pull; surface the raw probe result rather than fall through.
        return dict(probe)

    # reason == OLLAMA_MODEL_NOT_AVAILABLE: the daemon answered but lacks it.
    pull_name = _ollama_pull_name(model)
    pull_result = _pull_ollama_model(
        pull_urls,
        name=pull_name,
        http_post_stream=http_post_stream,
        secrets=secrets,
        timeout=pull_timeout,
        on_progress=on_progress,
        monotonic=monotonic,
    )
    if not pull_result["ok"]:
        return {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_PULL_FAILED",
            "message": f"Ollama pull of {pull_name!r} did not complete successfully.",
            "detail": pull_result["detail"],
        }

    recheck = _probe_ollama_model(tags_urls, model=model, http_get=http_get, secrets=secrets)
    if recheck.get("reason_code") == "OLLAMA_MODEL_AVAILABLE":
        return {
            "status": "ok",
            "reason_code": "OLLAMA_MODEL_PULLED",
            "message": f"Ollama model {pull_name!r} was pulled and is now available.",
        }
    return {
        "status": "fail",
        "reason_code": "OLLAMA_MODEL_PULL_FAILED",
        "message": f"Ollama model {pull_name!r} is still unavailable after the pull completed.",
        "detail": recheck.get("detail"),
    }


def _pull_ollama_model(
    urls: tuple[str, ...],
    *,
    name: str,
    http_post_stream: HttpPostStream,
    secrets: frozenset[str],
    timeout: float,
    on_progress: Callable[[str], None] | None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    # The documented /api/pull body field is ``model``; ``name`` is the
    # deprecated alias still accepted by current daemons. Send both so newer
    # daemons (which prefer ``model``) and older ones (which only know ``name``)
    # both resolve the model to pull. See https://docs.ollama.com/api/pull.
    body: dict[str, Any] = {"model": name, "name": name, "stream": True}
    failures: list[str] = []
    # ``timeout`` reaches httpx as a per-read deadline that resets on every
    # NDJSON progress line, so it is not a total bound. Hold a single wall-clock
    # deadline across all URL attempts so a daemon that streams progress forever
    # without terminating still surfaces OLLAMA_MODEL_PULL_FAILED on time.
    deadline = monotonic() + timeout
    for url in urls:
        remaining = deadline - monotonic()
        if remaining <= 0:
            # The wall-clock budget is already spent — e.g. an earlier URL
            # streamed progress until the deadline. Opening this fallback with a
            # fresh full ``timeout`` would let the intended total bound be
            # exceeded substantially, so stop here and surface the timeout
            # instead of attempting more URLs.
            failure = "pull exceeded the bounded wall-clock timeout before fallback"
            failures.append(f"{url}: {failure}" if len(urls) > 1 else failure)
            break
        # Bound this attempt by the time left in the shared deadline rather than
        # the full ``timeout``. Otherwise a first attempt that returns just
        # *before* the deadline still lets a fallback open with a fresh full
        # connect/read timeout, so the executor thread could block for roughly
        # another whole ``timeout`` past the intended total pull budget.
        attempt_timeout = min(timeout, remaining)
        try:
            with http_post_stream(url, json=body, timeout=attempt_timeout) as response:
                status_code = response.status_code
                if not 200 <= status_code < 300:
                    failure = f"HTTP {status_code}"
                    failures.append(f"{url}: {failure}" if len(urls) > 1 else failure)
                    continue
                stream_error = _consume_pull_stream(
                    response,
                    secrets=secrets,
                    on_progress=on_progress,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            if stream_error is not None:
                failures.append(f"{url}: {stream_error}" if len(urls) > 1 else stream_error)
                continue
            return {"ok": True, "detail": None}
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Transport failures *and* a syntactically invalid pull URL
            # (``httpx.InvalidURL`` — e.g. an unresolved ``${OLLAMA_HOST}``
            # placeholder or a bad percent escape from operator config) are
            # retried across URLs and ultimately become OLLAMA_MODEL_PULL_FAILED,
            # mirroring the ``_probe_ollama``/``_probe_ollama_model`` guards above.
            # ``InvalidURL`` is not an ``httpx.HTTPError`` subclass, so it must be
            # named explicitly or an operator config error would escape
            # ``ensure_ollama_model_available`` as an unhandled raise instead of a
            # structured, redacted readiness failure. Other non-transport bugs
            # (e.g. a faulty on_progress callback) must still surface, not be
            # masked as OLLAMA_MODEL_PULL_FAILED.
            _log_redacted_exception(
                "provider_readiness.ollama_pull_exception",
                exc,
                secrets,
            )
            detail = f"{type(exc).__name__}: {exc}"
            failures.append(f"{url}: {detail}" if len(urls) > 1 else detail)
            continue
    return {
        "ok": False,
        "detail": _truncate(_redact("; ".join(failures), secrets)) or None,
    }


def _consume_pull_stream(
    response: HttpStreamResponseLike,
    *,
    secrets: frozenset[str],
    on_progress: Callable[[str], None] | None,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    """Drain a ``/api/pull`` NDJSON stream; return a redacted error if any.

    ``deadline`` is an absolute ``monotonic`` wall-clock bound on the total time
    spent draining the stream. httpx's per-read timeout resets on every progress
    line, so without this check a daemon that keeps streaming progress but never
    terminates would keep this loop running indefinitely; the between-lines check
    surfaces a bounded timeout error instead.
    """
    error_detail: str | None = None
    for line in response.iter_lines():
        if monotonic() >= deadline:
            return _truncate(
                _redact("pull stream exceeded the bounded wall-clock timeout", secrets)
            )
        text = line.strip() if isinstance(line, str) else ""
        if not text:
            continue
        redacted = _truncate(_redact(text, secrets))
        if on_progress is not None:
            on_progress(redacted)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            err = payload.get("error")
            if isinstance(err, str) and err:
                error_detail = _truncate(_redact(err, secrets))
            elif payload.get("status") == "success":
                # A terminal success supersedes any earlier recoverable error
                # line: the daemon finished the pull, so a stale error from a
                # retried-then-recovered step must not be reported as a failure.
                error_detail = None
    return error_detail


# Imported at module end to mirror the established mutual-import ordering: the
# referenced names are all fully defined in their home modules before this leaf is
# pulled in, and these helpers only reference them at call time, so the late
# binding is safe.
from awf.service.provider_readiness import (  # noqa: E402
    _HTTP_TIMEOUT_SECONDS,
    _OLLAMA_PULL_TIMEOUT_SECONDS,
    HttpGet,
    HttpPostStream,
    HttpStreamResponseLike,
)
from awf.service.provider_readiness_helpers import (  # noqa: E402
    _is_cloud_model,
    _ollama_pull_name,
)
from awf.service.provider_readiness_redaction import (  # noqa: E402
    _log_redacted_exception,
    _log_redacted_terminal_failure,
    _redact,
    _truncate,
)
