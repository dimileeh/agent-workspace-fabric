"""Shared local service environment helpers."""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import NoReturn, cast

import yaml

_COMPOSE_CLI_ENV_KEYS = ("COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME")
_DOCKER_CLI_CLIENT_ENV_KEYS = (
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
)
_COMPOSE_INTERPOLATION_PATTERN = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?=[}:?+\-])[^}]*\}|"
    r"\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
)
_COMPOSE_ENV_LINE_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(?P<value>.*))?$"
)
_COMPOSE_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMPOSE_ESCAPED_DOLLAR = "\0AWF_ESCAPED_DOLLAR\0"
_COMPOSE_INTERPOLATION_CACHE_MAX_SIZE = 32
_COMPOSE_INTERPOLATION_KEYS_CACHE_MISSING = object()


class _ComposeInterpolationKeysCacheFailure:
    """Cached unexpected parser failure without retained traceback frames."""

    def __init__(self, exception_type: type[Exception], message: str) -> None:
        self.exception_type = exception_type
        self.message = message


class ComposeEnvInterpolationError(ValueError):
    """Raised when Compose env-file interpolation requires a missing value."""

    def __init__(self, variable: str, message: str) -> None:
        self.variable = variable
        super().__init__(message or f"{variable} is required")


_COMPOSE_INTERPOLATION_KEYS_CACHE: OrderedDict[
    tuple[str, str, int],
    tuple[str, ...] | _ComposeInterpolationKeysCacheFailure,
] = OrderedDict()
_COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK = threading.Lock()
_COMPOSE_INTERPOLATION_KEYS_INFLIGHT: dict[tuple[str, str, int], threading.Event] = {}


def env_lookup(environ: Mapping[str, str], key: str) -> tuple[bool, str]:
    """Return whether an environment key is present using case-insensitive matching."""

    value = environ.get(key)
    if value is not None:
        return True, value
    wanted = key.upper()
    best_priority: tuple[bool, bool, str] | None = None
    best_value = ""
    for existing, value in environ.items():
        if existing.upper() != wanted:
            continue
        priority = (existing != wanted, existing.upper() != existing, existing)
        if best_priority is None or priority < best_priority:
            best_priority = priority
            best_value = value
    if best_priority is not None:
        return True, best_value
    return False, ""


def non_empty_env_value(environ: Mapping[str, str], key: str) -> str | None:
    """Look up a case-insensitive environment value and ignore empty strings."""

    found, value = env_lookup(environ, key)
    if found and value:
        return value
    return None


def docker_cli_client_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Docker CLI controls needed to reach the selected daemon."""

    resolved: dict[str, str] = {}
    for key in _DOCKER_CLI_CLIENT_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if found and value:
            resolved[key] = value
    return resolved


def cleared_docker_cli_client_keys(environ: Mapping[str, str]) -> frozenset[str]:
    """Return Docker CLI keys explicitly cleared by the service environment."""

    cleared_keys: set[str] = set()
    for key in _DOCKER_CLI_CLIENT_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if not found or value:
            continue
        caller_found, caller_value = env_lookup(os.environ, key)
        if caller_found and caller_value:
            cleared_keys.add(key)
    return frozenset(cleared_keys)


def compose_cli_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Compose CLI controls that affect stack selection."""

    resolved: dict[str, str] = {}
    for key in _COMPOSE_CLI_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if found and value:
            resolved[key] = value
            continue
        if found:
            caller_found, caller_value = env_lookup(os.environ, key)
            if caller_found and caller_value:
                resolved[key] = ""
            continue
        # Compose CLI selectors are intentionally inherited from the caller
        # when the service env is silent; explicit service values override them,
        # and explicit blank service values above clear stale caller settings.
        # Leaving silent keys out keeps subprocess env inheritance implicit and
        # avoids forcing `env=` for logs calls that need no real override.
    return resolved


def compose_interpolation_environ(
    environ: Mapping[str, str],
    *,
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, str]:
    """Return resolved service values Docker Compose still interpolates."""

    resolved: dict[str, str] = {}
    env_file_values = compose_env_file_values(compose_env_file)
    for key in compose_interpolation_keys(compose_file):
        found, value = env_lookup(environ, key)
        if not found:
            continue
        caller_found, caller_value = env_lookup(os.environ, key)
        env_file_found, env_file_value = env_lookup(env_file_values, key)
        # Caller env wins over --env-file for interpolation. If it already
        # matches the resolved service value, a stale env-file value is harmless.
        if caller_found:
            if caller_value != value:
                resolved[key] = value
            continue
        # Without a caller value, Compose can read matching values from
        # --env-file; stale or missing env-file values need an explicit override.
        if not env_file_found or env_file_value != value:
            resolved[key] = value
    return resolved


def compose_env_file_values(
    compose_env_file: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return parsed Compose env-file values, omitting unset entries.

    Docker Compose interpolates unquoted and double-quoted env-file values, but
    leaves single-quoted values literal. ``python-dotenv`` cannot represent that
    distinction after parsing, so this tiny parser preserves the Compose
    behavior host-side while still allowing single-quoted secrets such as
    ``'secret-${suffix}'`` to remain untouched.
    """

    if compose_env_file is None or not compose_env_file.exists():
        return {}
    caller_environ = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for line in compose_env_file.read_text(encoding="utf-8").splitlines():
        parsed = _parse_compose_env_line(line)
        if parsed is None:
            continue
        key, value, quote = parsed
        if quote != "single":
            value = _compose_expand_env_value(value, caller_environ=caller_environ, values=values)
        values[key] = value
    return values


def _parse_compose_env_line(line: str) -> tuple[str, str, str | None] | None:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _COMPOSE_ENV_LINE_PATTERN.match(line)
    if match is None or match.group("value") is None:
        return None
    key = match.group("key")
    raw_value = match.group("value").rstrip()
    if not raw_value:
        return key, "", None

    leading = raw_value.lstrip()
    if leading.startswith("'"):
        return key, _consume_compose_single_quoted_value(leading), "single"
    if leading.startswith('"'):
        return (
            key,
            _decode_compose_double_quoted_value(
                _consume_compose_quoted_value(leading, '"', preserve_escapes=True)
            ),
            "double",
        )
    return key, _strip_compose_inline_comment(leading).strip(), None


def _consume_compose_quoted_value(
    raw_value: str,
    quote: str,
    *,
    preserve_escapes: bool = False,
) -> str:
    chars: list[str] = []
    escaped = False
    for char in raw_value[1:]:
        if escaped:
            if preserve_escapes:
                chars.append("\\")
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return "".join(chars)
        chars.append(char)
    if escaped and preserve_escapes:
        chars.append("\\")
    return "".join(chars)


def _consume_compose_single_quoted_value(raw_value: str) -> str:
    chars: list[str] = []
    escaped = False
    for char in raw_value[1:]:
        if escaped:
            if char != "'":
                chars.append("\\")
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'":
            return "".join(chars)
        chars.append(char)
    if escaped:
        chars.append("\\")
    return "".join(chars)


def _decode_compose_double_quoted_value(value: str) -> str:
    replacements = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "\\": "\\",
        '"': '"',
        "$": _COMPOSE_ESCAPED_DOLLAR,
    }
    decoded: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            decoded.append(replacements.get(char, char))
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        decoded.append(char)
    if escaped:
        decoded.append("\\")
    return "".join(decoded)


def _strip_compose_inline_comment(raw_value: str) -> str:
    for index, char in enumerate(raw_value):
        if char == "#" and (index == 0 or raw_value[index - 1].isspace()):
            return raw_value[:index].rstrip()
    return raw_value


def _compose_expand_env_value(
    value: str,
    *,
    caller_environ: Mapping[str, str],
    values: Mapping[str, str],
) -> str:
    interpolation_context = {**values, **caller_environ}
    escaped_value = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    expanded: list[str] = []
    index = 0
    while index < len(escaped_value):
        char = escaped_value[index]
        if char != "$":
            expanded.append(char)
            index += 1
            continue
        if index + 1 < len(escaped_value) and escaped_value[index + 1] == "{":
            end_index = _find_compose_braced_expression_end(escaped_value, index + 1)
            if end_index is None:
                expanded.append(char)
                index += 1
                continue
            expanded.append(
                _compose_expand_braced_expression(
                    escaped_value[index + 2 : end_index],
                    caller_environ=caller_environ,
                    values=values,
                    interpolation_context=interpolation_context,
                )
            )
            index = end_index + 1
            continue
        plain_match = _COMPOSE_ENV_NAME_PATTERN.match(escaped_value, index + 1)
        if plain_match is None:
            expanded.append(char)
            index += 1
            continue
        expanded.append(_compose_env_lookup(interpolation_context, plain_match.group(0)) or "")
        index = plain_match.end()

    return "".join(expanded).replace(_COMPOSE_ESCAPED_DOLLAR, "$")


def _find_compose_braced_expression_end(value: str, open_brace_index: int) -> int | None:
    depth = 1
    index = open_brace_index + 1
    while index < len(value):
        if value[index] == "$" and index + 1 < len(value) and value[index + 1] == "{":
            depth += 1
            index += 2
            continue
        if value[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _compose_expand_braced_expression(
    expression: str,
    *,
    caller_environ: Mapping[str, str],
    values: Mapping[str, str],
    interpolation_context: Mapping[str, str],
) -> str:
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(expression)
    if name_match is None:
        return f"${{{expression}}}"
    name = name_match.group(0)
    remainder = expression[name_match.end() :]
    if not remainder:
        return _compose_env_lookup(interpolation_context, name) or ""
    operator = ""
    word = ""
    for candidate in (":-", "-", ":+", "+", ":?", "?"):
        if remainder.startswith(candidate):
            operator = candidate
            word = remainder[len(candidate) :]
            break
    if not operator:
        return f"${{{expression}}}"
    resolved = _compose_env_lookup(interpolation_context, name)
    is_set = resolved is not None
    is_non_empty = bool(resolved)
    if operator == ":-":
        return (
            resolved or ""
            if is_non_empty
            else _compose_expand_env_value(word, caller_environ=caller_environ, values=values)
        )
    if operator == "-":
        return (
            resolved or ""
            if is_set
            else _compose_expand_env_value(word, caller_environ=caller_environ, values=values)
        )
    if operator == ":+":
        return (
            _compose_expand_env_value(word, caller_environ=caller_environ, values=values)
            if is_non_empty
            else ""
        )
    if operator == "+":
        return (
            _compose_expand_env_value(word, caller_environ=caller_environ, values=values)
            if is_set
            else ""
        )
    if operator == ":?":
        if is_non_empty:
            return resolved or ""
        raise ComposeEnvInterpolationError(name, word)
    if operator == "?":
        if is_set:
            return resolved or ""
        raise ComposeEnvInterpolationError(name, word)
    return resolved or ""


def _compose_env_lookup(environ: Mapping[str, str], key: str) -> str | None:
    found, value = env_lookup(environ, key)
    return value if found else None


def compose_interpolation_keys(compose_file: Path) -> tuple[str, ...]:
    """Return Compose interpolation variable names referenced by a Compose stack."""

    keys = _compose_interpolation_keys_recursive(compose_file, seen=frozenset())
    return tuple(sorted(keys))


def _compose_interpolation_keys_recursive(
    compose_file: Path,
    *,
    seen: frozenset[Path],
) -> set[str]:
    """Return interpolation keys from one Compose file and literal includes."""

    compose_file = compose_file.expanduser().resolve()
    if compose_file in seen:
        return set()
    seen = frozenset((*seen, compose_file))
    try:
        contents_bytes = compose_file.read_bytes()
    except OSError:
        return set()
    try:
        contents = contents_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return set()
    contents_digest = sha256(contents_bytes).hexdigest()
    keys = set(
        _cached_compose_interpolation_keys(
            str(compose_file),
            contents_digest,
            len(contents_bytes),
            contents,
        )
    )
    if "include" not in contents:
        return keys
    for include_path in _parse_compose_include_paths(contents, base_dir=compose_file.parent):
        keys.update(_compose_interpolation_keys_recursive(include_path, seen=seen))
    return keys


def _parse_compose_include_paths(contents: str, *, base_dir: Path) -> tuple[Path, ...]:
    """Return literal Compose include paths relative to ``base_dir``."""

    try:
        payload: object = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise yaml.YAMLError(f"could not parse Compose YAML: {exc}") from exc
    if not isinstance(payload, Mapping):
        return ()
    includes = payload.get("include")
    return tuple(_compose_include_paths(includes, base_dir=base_dir))


def _compose_include_paths(value: object, *, base_dir: Path) -> list[Path]:
    """Extract literal include paths from supported Compose include shapes."""

    paths: list[Path] = []
    if isinstance(value, str):
        resolved = _literal_compose_include_path(value, base_dir=base_dir)
        if resolved is not None:
            paths.append(resolved)
        return paths
    if isinstance(value, Mapping):
        nested = value.get("path")
        if isinstance(nested, Sequence) and not isinstance(nested, str | bytes | bytearray):
            for item in nested:
                paths.extend(_compose_include_paths(item, base_dir=base_dir))
            return paths
        paths.extend(_compose_include_paths(nested, base_dir=base_dir))
        return paths
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for item in value:
            paths.extend(_compose_include_paths(item, base_dir=base_dir))
    return paths


def _literal_compose_include_path(value: str, *, base_dir: Path) -> Path | None:
    """Return a literal include path, skipping interpolated include targets."""

    if "$" in value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if path.is_dir():
        path = path / "compose.yaml"
    return path


def _cached_compose_interpolation_keys(
    _compose_file: str,
    contents_digest: str,
    contents_size: int,
    contents: str,
) -> tuple[str, ...]:
    """Return cached Compose interpolation keys for one file version."""

    cache_key = (_compose_file, contents_digest, contents_size)
    while True:
        with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
            cached = _COMPOSE_INTERPOLATION_KEYS_CACHE.get(
                cache_key, _COMPOSE_INTERPOLATION_KEYS_CACHE_MISSING
            )
            if cached is not _COMPOSE_INTERPOLATION_KEYS_CACHE_MISSING:
                _COMPOSE_INTERPOLATION_KEYS_CACHE.move_to_end(cache_key)
                if isinstance(cached, _ComposeInterpolationKeysCacheFailure):
                    _raise_compose_interpolation_cache_failure(cached)
                return cast(tuple[str, ...], cached)
            inflight = _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key] = inflight
                break
        inflight.wait()

    try:
        keys = _parse_compose_interpolation_keys(contents)
    except Exception as exc:
        with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
            _COMPOSE_INTERPOLATION_KEYS_CACHE[cache_key] = _ComposeInterpolationKeysCacheFailure(
                type(exc), str(exc)
            )
            if len(_COMPOSE_INTERPOLATION_KEYS_CACHE) > _COMPOSE_INTERPOLATION_CACHE_MAX_SIZE:
                _COMPOSE_INTERPOLATION_KEYS_CACHE.popitem(last=False)
            if _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key) is inflight:
                del _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key]
            inflight.set()
        raise
    except BaseException:
        with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
            if _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key) is inflight:
                del _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key]
            inflight.set()
        raise

    with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
        _COMPOSE_INTERPOLATION_KEYS_CACHE[cache_key] = keys
        if len(_COMPOSE_INTERPOLATION_KEYS_CACHE) > _COMPOSE_INTERPOLATION_CACHE_MAX_SIZE:
            _COMPOSE_INTERPOLATION_KEYS_CACHE.popitem(last=False)
        if _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key) is inflight:
            del _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key]
        inflight.set()
    return keys


def _raise_compose_interpolation_cache_failure(
    failure: _ComposeInterpolationKeysCacheFailure,
) -> NoReturn:
    try:
        exception = failure.exception_type(failure.message)
    except TypeError as exc:
        raise RuntimeError(failure.message) from exc
    raise exception


def _parse_compose_interpolation_keys(contents: str) -> tuple[str, ...]:
    """Parse Compose YAML contents and collect interpolation variable names."""

    try:
        payload: object = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise yaml.YAMLError(f"could not parse Compose YAML: {exc}") from exc
    collected_keys: set[str] = set()
    _collect_compose_interpolation_keys(payload, collected_keys)
    return tuple(sorted(collected_keys))


def _collect_compose_interpolation_keys(value: object, keys: set[str]) -> None:
    """Collect Compose interpolation variable names from nested YAML values."""

    if isinstance(value, str):
        for match in _COMPOSE_INTERPOLATION_PATTERN.finditer(value):
            if _is_escaped_compose_interpolation(value, match.start()):
                continue
            key = match.group("braced") or match.group("plain")
            if key:
                keys.add(key)
        return
    if isinstance(value, Mapping):
        # Compose 2.x interpolates YAML scalar values, not mapping keys such as
        # service, network, or volume names, so this intentionally walks values only.
        for nested_value in value.values():
            _collect_compose_interpolation_keys(nested_value, keys)
        return
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for nested in value:
            _collect_compose_interpolation_keys(nested, keys)


def _is_escaped_compose_interpolation(value: str, dollar_index: int) -> bool:
    """Return whether a Compose ``$`` interpolation start is escaped by ``$$``."""

    previous_dollars = 0
    index = dollar_index - 1
    while index >= 0 and value[index] == "$":
        previous_dollars += 1
        index -= 1
    return previous_dollars % 2 == 1
