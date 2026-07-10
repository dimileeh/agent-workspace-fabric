"""Compose env-value interpolation / resolution machinery.

Extracted from ``awf.profiles.compose`` to keep first-party files under the
maintainability line limit (see
``tests/unit/test_core_decomposition_maintainability.py``). The names defined
here are re-imported by ``awf.profiles.compose`` so existing callers and tests
that import them from ``awf.profiles.compose`` — including module-private
names such as ``_COMPOSE_PASSTHROUGH`` and attribute access via
``compose_module.<name>`` — keep working unchanged. This is a pure relocation;
the logic is byte-for-byte identical.

This module mirrors the interpolation model in ``awf.service.environment``
(``${VAR}`` / ``${VAR:-...}`` / ``$VAR``). Kept local so ``profiles`` does not
import the ``service`` layer. An escaped ``$$`` is a literal dollar, not an
interpolation reference.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from urllib.parse import quote, unquote, urlsplit

# Compose variable-interpolation resolver, mirroring the interpolation model in
# ``awf.service.environment`` (``${VAR}`` / ``${VAR:-...}`` / ``$VAR``). Kept
# local so ``profiles`` does not import the ``service`` layer. An escaped ``$$``
# is a literal dollar, not an interpolation reference.


class _ComposeEnvResolution(StrEnum):
    """How a compose env value resolves against the worker env.

    Drives both carry-to-``profile_env`` (``literal_profile_env_from_compose``)
    and hosted passthrough filtering
    (``_filter_hosted_env_passthrough_names_from_compose_env``):
    """

    LITERAL = "literal"
    # Worker-resolved via ``${NAME:-default}`` / ``${NAME-default}`` with ``NAME``
    # set in the worker env (non-empty for ``:-``). The local Compose container
    # receives the *worker* value at stack launch, so the hosted path must leave
    # the name available for out-of-band resolution (NOT carry the worker value in
    # ``profile_env`` — that would embed a secret — and NOT exclude the name from
    # ``env_passthrough_names`` — that would drop it entirely, diverging from the
    # local run). See PR #751 thread PRRT_kwDOSJAM6s6PVH0t.
    WORKER_RESOLVED_DEFAULTED = "worker_resolved_defaulted"
    # Worker-resolved via bare ``${NAME}`` / ``$NAME`` and ``${NAME:?...}`` /
    # ``${NAME?...}`` with the variable unset (the local stack would fail to
    # launch, so this is unreachable for a running container) — profile-owned
    # secret slots the local path keeps out of exec-time passthrough; the hosted
    # path resolves them via its own adapter contract, not by re-resolving
    # ``${NAME}`` from the worker. ``${NAME:?...}`` / ``${NAME?...}`` with the
    # variable set resolve to the worker value and are classified
    # ``WORKER_RESOLVED_DEFAULTED`` (kept in passthrough). ``${NAME:+...}`` /
    # ``${NAME+...}`` with the variable set carry the profile-owned alternate word
    # as ``LITERAL`` (the local container received the alternate, not a worker
    # value).
    WORKER_RESOLVED_SLOT = "worker_resolved_slot"


# Compose braced-expression operators, ordered longest-first so ``:-`` is
# matched before ``-`` (and ``:+`` before ``+``, ``:?`` before ``?``). Mirrors
# ``awf.service.environment._compose_expand_braced_expression``'s scan order.
_COMPOSE_BRACED_OPERATORS = (":-", "-", ":+", "+", ":?", "?")

# ``:-`` / ``-`` supply a concrete default when the referenced variable is unset
# (``:-`` tests non-empty, ``-`` tests set-ness). Only these forms carry a
# profile-owned concrete value to the hosted job when the variable is absent
# (or empty, for ``:-``) from the worker env; when the variable is set the slot
# is worker-resolved-defaulted. The non-empty vs set-ness distinction is handled
# in ``_compose_resolve_braced`` to mirror ``awf.service.environment``'s expander.
_COMPOSE_DEFAULT_OPERATORS = (":-", "-")

# ``:+`` / ``+`` supply an alternate word when the referenced variable is set
# (``:+`` tests non-empty, ``+`` tests set-ness). The alternate word is
# profile-owned config (literal text in the compose file), so it is carried as a
# literal when the test passes (the local container received the alternate word);
# when the test fails Compose resolves to "" and that empty literal is carried.
# An alternate word that references a worker secret propagates the worker-resolved
# classification so the secret never reaches ``profile_env``.
_COMPOSE_ALTERNATE_OPERATORS = (":+", "+")

# Sentinel used to mask ``$$`` escapes before interpolation scanning so an
# escaped dollar is never mistaken for a reference start.
_COMPOSE_ESCAPED_DOLLAR = "\0AWF_PROFILE_ESCAPED_DOLLAR\0"

# Sentinel value used in the normalized compose-env dict to mark a genuine
# Compose *pass-through* slot — ``environment: [NAME]`` (list item with no
# ``=``), ``NAME:`` / ``NAME: null`` (mapping value that is ``None``). Docker
# Compose models these as a nil pointer resolved from the worker shell at
# stack launch (compose-go ``TestEnvironmentMap`` / ``TestEnvironmentList``:
# ``ZO:`` / ``ZO`` -> ``env["ZO"] == nil``), exactly like a bare ``${NAME}``
# reference. This is distinct from an *explicit* empty value — ``NAME: ""``
# (mapping) or ``NAME=`` (list with an ``=`` and empty value) — which Compose
# models as a non-nil pointer to ``""`` (compose-go: ``BU: ""`` / ``BU=`` ->
# ``*env["BU"] == ""``): an empty literal that OVERRIDES the worker shell
# value. ``_compose_environment_mapping`` normalizes a pass-through slot to
# this sentinel and an explicit-empty value to the plain string ``""`` so the
# carry / passthrough-filter call sites can tell them apart: a pass-through
# slot is skipped from ``profile_env`` and kept in ``env_passthrough_names``
# (worker-resolved), while an explicit empty is CARRIED in ``profile_env`` as a
# literal ``""`` and excluded from passthrough (profile-owned). A NUL byte is
# used so the sentinel can never collide with a real env value.
_COMPOSE_PASSTHROUGH = "\0AWF_PROFILE_COMPOSE_PASSTHROUGH\0"

_COMPOSE_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_POSTGRES_PASSWORD_SUBSTRING_REDACTION_MIN_LENGTH = 12


def _compose_braced_expression_end(value: str, open_brace_index: int) -> int | None:
    """Return the index of the matching ``}`` for a ``${`` at ``open_brace_index``."""
    depth = 1
    index = open_brace_index + 1
    while index < len(value):
        char = value[index]
        if char == "$" and index + 1 < len(value) and value[index + 1] == "{":
            depth += 1
            index += 2
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _compose_resolve_value(
    value: str,
    *,
    worker_env: Mapping[str, str],
) -> tuple[str, _ComposeEnvResolution]:
    """Resolve a Compose env value against the worker env.

    Returns ``(expanded, resolution)`` where ``resolution`` classifies whether the
    value carries a concrete profile-owned literal (``LITERAL``) or pulls a
    worker-resolved value (``WORKER_RESOLVED_DEFAULTED`` for defaulted / required
    forms with the variable set; ``WORKER_RESOLVED_SLOT`` for bare ``${NAME}`` /
    ``$NAME`` and unset required forms). See :class:`_ComposeEnvResolution` for the
    carry vs passthrough rules.

    Carry rule (mirrors what the local agent container receives at stack
    launch, without embedding worker secrets in ``profile_env``):

    - A pure literal (no interpolation reference) is carried verbatim.
    - An escaped ``$$`` collapses to a single literal ``$`` and is carried.
    - ``${NAME:-default}`` / ``${NAME-default}`` with ``NAME`` unset in the
      worker env resolves to the concrete ``default`` and is carried — the
      local container receives that default, so the hosted job must too
      (dropping it leaves the hosted job missing the profile-owned value).
    - ``${NAME:-default}`` with ``NAME`` present-but-empty in the worker env
      resolves to the concrete ``default`` and is carried — ``:-`` tests
      non-empty, so Compose injects the default into the local container and
      the hosted job must receive it too.
    - ``${NAME:-default}`` with ``NAME`` set to a non-empty worker value is
      ``WORKER_RESOLVED_DEFAULTED``: skipped from ``profile_env`` (carrying it
      would embed a worker secret) but kept in ``env_passthrough_names`` so the
      hosted executor resolves the same worker value the local container received.
    - ``${NAME-default}`` with ``NAME`` set in the worker env (even empty) is
      ``WORKER_RESOLVED_DEFAULTED`` — ``-`` tests set-ness, so a present value is
      worker-resolved.
    - ``${NAME:+alternate}`` / ``${NAME+alternate}`` with ``NAME`` set (non-empty
      for ``:+``) resolves to the concrete ``alternate`` and is carried — the
      local container received the alternate word (profile-owned config, not a
      worker value), so the hosted job must too. When ``NAME`` is unset (or empty
      for ``:+``) Compose resolves to ``""`` and that empty literal is carried. An
      alternate word that references a worker secret propagates the worker-resolved
      classification so the secret never reaches ``profile_env``.
    - ``${NAME:?err}`` / ``${NAME?err}`` with ``NAME`` set (non-empty for ``:?``)
      resolves to the worker value and is ``WORKER_RESOLVED_DEFAULTED`` — kept in
      ``env_passthrough_names`` for hosted out-of-band resolution (the local
      container received the worker value) and skipped from ``profile_env`` (it
      would embed a secret). An unset required form would fail Compose at stack
      launch, so that branch is ``WORKER_RESOLVED_SLOT`` (unreachable for a
      running container).
    - A bare ``${NAME}`` / ``$NAME`` (no operator) is ``WORKER_RESOLVED_SLOT``: a
      worker-resolved slot the profile owns locally; the hosted path resolves
      credentials via its own adapter contract, not by re-resolving ``${NAME}``
      from the worker.

    The default / alternate word is itself recursively expanded against the
    worker env, mirroring ``awf.service.environment``'s env-file interpolator.
    """
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    expanded: list[str] = []
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char != "$":
            expanded.append(char)
            index += 1
            continue
        if index + 1 < len(escaped) and escaped[index + 1] == "{":
            end = _compose_braced_expression_end(escaped, index + 1)
            if end is None:
                expanded.append(char)
                index += 1
                continue
            piece, piece_resolution = _compose_resolve_braced(
                escaped[index + 2 : end], worker_env=worker_env
            )
            if piece_resolution is not _ComposeEnvResolution.LITERAL:
                return "", piece_resolution
            expanded.append(piece)
            index = end + 1
            continue
        plain_match = _COMPOSE_ENV_NAME_PATTERN.match(escaped, index + 1)
        if plain_match is None:
            expanded.append(char)
            index += 1
            continue
        # Bare ``$NAME`` (no default operator) -> worker-resolved slot, skip.
        return "", _ComposeEnvResolution.WORKER_RESOLVED_SLOT
    return "".join(expanded).replace(_COMPOSE_ESCAPED_DOLLAR, "$"), _ComposeEnvResolution.LITERAL


def _compose_bare_reference_name(value: str) -> str | None:
    """Return the variable name when ``value`` is exactly a single bare reference.

    A *bare* Compose reference is exactly ``${NAME}`` or ``$NAME`` with no
    operator and no surrounding literal text — the form Core injects via
    ``agent_environment_with_legacy_host_auth`` (``NAME: ${NAME}`` for a
    worker-present ``AGENT_AUTH_ENV_VARS`` key the profile does not declare).
    Docker Compose substitutes the worker shell value at stack launch, exactly
    like a pass-through slot. Mixed forms (e.g. ``prefix-${NAME}``) and nested
    forms (e.g. ``${X:-${SECRET}}``) are NOT bare references: the local container
    receives a profile-owned literal that interpolates a worker value, and the
    hosted executor cannot reconstruct that mixed value from the name alone, so
    they are out of scope for the bare-slot passthrough fix.

    Returns the referenced variable name, or ``None`` when ``value`` is not a
    single bare reference (a literal, a defaulted/alternate/required form, a
    mixed value, an escaped ``$$``, or an unparseable expression). Used by
    ``_filter_hosted_env_passthrough_names_from_compose_env`` to decide whether
    a ``WORKER_RESOLVED_SLOT`` name stays in ``env_passthrough_names`` for hosted
    out-of-band resolution (PR #751 thread PRRT_kwDOSJAM6s6Pi7sN).
    """
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    if escaped.startswith("${"):
        end = _compose_braced_expression_end(escaped, 1)
        if end is None or end != len(escaped) - 1:
            return None
        inner = escaped[2:end]
        match = _COMPOSE_ENV_NAME_PATTERN.match(inner)
        if match is None or match.end() != len(inner):
            return None
        return match.group(0)
    if escaped.startswith("$"):
        match = _COMPOSE_ENV_NAME_PATTERN.match(escaped, 1)
        if match is None or match.end() != len(escaped):
            return None
        return match.group(0)
    return None


def _compose_defaulted_reference_name(
    value: str,
    *,
    worker_env: Mapping[str, str],
) -> str | None:
    """Return the variable name for a single defaulted/required reference.

    Matches exactly ``${NAME:-word}``, ``${NAME-word}``, ``${NAME:?err}``, or
    ``${NAME?err}``, with no surrounding literal text, and returns ``NAME`` only
    when that outer expression itself selects the worker value. The hosted
    passthrough filter uses this to keep only same-name worker-resolved
    defaulted/required slots; cross-name aliases cannot be reconstructed from a
    target-name-only passthrough entry.
    """
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    if not escaped.startswith("${"):
        return None
    end = _compose_braced_expression_end(escaped, 1)
    if end is None or end != len(escaped) - 1:
        return None
    inner = escaped[2:end]
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(inner)
    if name_match is None:
        return None
    name = name_match.group(0)
    remainder = inner[name_match.end() :]
    for operator in _COMPOSE_BRACED_OPERATORS:
        if remainder.startswith(operator):
            worker_value = worker_env.get(name)
            is_set = name in worker_env
            is_non_empty = bool(worker_value)
            if operator == ":-" and is_non_empty:
                return name
            if operator == "-" and is_set:
                return name
            if operator == ":?" and is_non_empty:
                return name
            if operator == "?" and is_set:
                return name
            return None
    return None


def _compose_selected_worker_reference_name(
    value: str,
    *,
    worker_env: Mapping[str, str],
) -> str | None:
    """Return the worker source name selected by one exact Compose expression.

    This is intentionally narrower than ``_compose_resolve_value``: it returns a
    name only when the whole value is one expression and the selected branch is
    exactly a worker reference. Mixed values such as ``prefix-${TOKEN}`` still
    cannot be reconstructed by hosted name-only passthrough.
    """
    bare_name = _compose_bare_reference_name(value)
    if bare_name is not None:
        return bare_name
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    if not escaped.startswith("${"):
        return None
    end = _compose_braced_expression_end(escaped, 1)
    if end is None or end != len(escaped) - 1:
        return None
    inner = escaped[2:end]
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(inner)
    if name_match is None:
        return None
    name = name_match.group(0)
    remainder = inner[name_match.end() :]
    operator = ""
    word = ""
    for candidate in _COMPOSE_BRACED_OPERATORS:
        if remainder.startswith(candidate):
            operator = candidate
            word = remainder[len(candidate) :]
            break
    if not operator:
        return None

    worker_value = worker_env.get(name)
    is_set = name in worker_env
    is_non_empty = bool(worker_value)
    if operator in _COMPOSE_DEFAULT_OPERATORS:
        if (operator == ":-" and is_non_empty) or (operator == "-" and is_set):
            return name
        return _compose_selected_worker_reference_name(word, worker_env=worker_env)
    if operator in _COMPOSE_ALTERNATE_OPERATORS:
        if (operator == ":+" and is_non_empty) or (operator == "+" and is_set):
            return _compose_selected_worker_reference_name(word, worker_env=worker_env)
        return None
    if (operator == ":?" and is_non_empty) or (operator == "?" and is_set):
        return name
    return None


def _compose_default_word_is_worker_resolved(
    value: str,
    *,
    worker_env: Mapping[str, str],
) -> bool:
    """Return whether a single default expression's default word is worker-resolved."""
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    if not escaped.startswith("${"):
        return False
    end = _compose_braced_expression_end(escaped, 1)
    if end is None or end != len(escaped) - 1:
        return False
    inner = escaped[2:end]
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(inner)
    if name_match is None:
        return False
    remainder = inner[name_match.end() :]
    for operator in _COMPOSE_DEFAULT_OPERATORS:
        if remainder.startswith(operator):
            _default, resolution = _compose_resolve_value(
                remainder[len(operator) :],
                worker_env=worker_env,
            )
            return resolution is not _ComposeEnvResolution.LITERAL
    return False


def _compose_resolve_braced(
    expression: str,
    *,
    worker_env: Mapping[str, str],
) -> tuple[str, _ComposeEnvResolution]:
    """Resolve a Compose ``${...}`` braced expression.

    Returns ``(expanded, resolution)``; see :class:`_ComposeEnvResolution` and
    ``_compose_resolve_value``. Operator semantics mirror
    ``awf.service.environment._compose_expand_braced_expression`` so the hosted
    job receives the same value the local agent container gets at stack launch:

    - ``:-`` / ``-`` (default): when the variable is unset (or empty for ``:-``)
      the default word is recursively expanded and carried as ``LITERAL``
      (profile-owned concrete config); when the variable is set (non-empty for
      ``:-``) the worker value is used and the slot is ``WORKER_RESOLVED_DEFAULTED``
      (kept in passthrough, dropped from ``profile_env``).
    - ``:+`` / ``+`` (alternate): when the variable is set (non-empty for ``:+``)
      the alternate word is recursively expanded; if that expansion is a literal
      it is carried as ``LITERAL`` (the local container received the alternate
      word, which is profile-owned config, not a worker value). When the variable
      is unset (or empty for ``:+``) Compose resolves to ``""`` and that empty
      literal is carried so the hosted job matches the local container. An
      alternate word that itself references a worker secret (e.g.
      ``${FLAG:+${SECRET}}``) propagates the worker-resolved classification so the
      secret is never embedded in ``profile_env``.
    - ``:?`` / ``?`` (required): when the variable is set (non-empty for ``:?``)
      Compose resolves the worker value, so the slot is ``WORKER_RESOLVED_DEFAULTED``
      (kept in passthrough for hosted out-of-band resolution, dropped from
      ``profile_env``). When the variable is unset/empty the local stack would fail
      to launch, so that branch is unreachable for a running container and stays
      ``WORKER_RESOLVED_SLOT``.
    """
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(expression)
    if name_match is None:
        # Unparseable braced text is carried through verbatim (no reference).
        return f"${{{expression}}}", _ComposeEnvResolution.LITERAL
    name = name_match.group(0)
    remainder = expression[name_match.end() :]
    if not remainder:
        # Bare ``${NAME}`` -> worker-resolved slot, skip.
        return "", _ComposeEnvResolution.WORKER_RESOLVED_SLOT
    operator = ""
    word = ""
    for candidate in _COMPOSE_BRACED_OPERATORS:
        if remainder.startswith(candidate):
            operator = candidate
            word = remainder[len(candidate) :]
            break
    if not operator:
        # Unknown operator -> carry verbatim (no reference).
        return f"${{{expression}}}", _ComposeEnvResolution.LITERAL
    worker_value = worker_env.get(name)
    is_set = name in worker_env
    is_non_empty = bool(worker_value)
    if operator in _COMPOSE_DEFAULT_OPERATORS:
        # ``:-`` tests non-empty; ``-`` tests set-ness.
        if (operator == ":-" and is_non_empty) or (operator == "-" and is_set):
            return "", _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
        # Variable unset (or empty for :-) -> expand the default word and carry.
        default, default_resolution = _compose_resolve_value(word, worker_env=worker_env)
        if default_resolution is not _ComposeEnvResolution.LITERAL:
            return "", default_resolution
        return default, _ComposeEnvResolution.LITERAL
    if operator in _COMPOSE_ALTERNATE_OPERATORS:
        # ``:+`` tests non-empty; ``+`` tests set-ness. When the test passes the
        # alternate word is what the local container receives (profile-owned
        # config, not a worker value), so it is carried as a literal — unless the
        # word itself references a worker secret, in which case the worker-resolved
        # classification propagates. When the test fails Compose resolves to "".
        if (operator == ":+" and is_non_empty) or (operator == "+" and is_set):
            alternate, alternate_resolution = _compose_resolve_value(word, worker_env=worker_env)
            if alternate_resolution is not _ComposeEnvResolution.LITERAL:
                return "", alternate_resolution
            return alternate, _ComposeEnvResolution.LITERAL
        # Variable unset (or empty for :+) -> Compose resolves to "", carried.
        return "", _ComposeEnvResolution.LITERAL
    # ``:?`` / ``?`` (required): a set variable resolves to the worker value
    # (a secret), so the slot is worker-resolved-defaulted — kept in passthrough
    # for hosted out-of-band resolution and dropped from ``profile_env``. An
    # unset required form would fail Compose at stack launch, so that branch is
    # unreachable for a running container and stays a worker-resolved slot.
    if (operator == ":?" and is_non_empty) or (operator == "?" and is_set):
        return "", _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
    return "", _ComposeEnvResolution.WORKER_RESOLVED_SLOT


def _compose_concrete_worker_password(
    value: str,
    *,
    worker_env: Mapping[str, str],
) -> str | None:
    """Resolve a service ``POSTGRES_PASSWORD`` to its concrete worker-env value.

    Unlike :func:`_compose_resolve_value`, this returns the concrete worker value
    for *worker-resolved* forms (``${NAME:-default}`` / ``${NAME-default}`` /
    ``${NAME:?err}`` / ``${NAME?err}`` with the variable set, and bare
    ``${NAME}`` / ``$NAME``) — not the ``WORKER_RESOLVED_*`` classification. The
    result is used only to build the redaction set in
    ``_try_compose_agent_env_and_postgres_passwords``: a rendered agent env DB URL
    embeds the *resolved* password the local container received at stack launch,
    so redaction must match that concrete value. The redaction set is never
    carried in ``profile_env`` (worker-resolved values are skipped from carry),
    so recovering the concrete worker secret here does not violate the
    no-secret-values contract — it only marks which agent env values to *skip*.

    Returns ``None`` when the value does not resolve to a concrete worker value
    (e.g. an unset required form, an unparseable expression, or a nested
    reference the simple expansion below does not handle). The caller still
    tracks the raw ``${...}`` placeholder string as a redaction target, so an
    agent env value carrying the unexpanded form is still redacted.

    The expansion mirrors Compose's interpolation for the forms a
    ``POSTGRES_PASSWORD`` realistically uses: a single braced expression or bare
    ``$NAME``. A value with mixed literal + reference text (e.g.
    ``prefix-${NAME}``) is handled by expanding the whole value; if any piece is
    worker-resolved the concrete worker value is substituted in place.
    """
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    expanded: list[str] = []
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char != "$":
            expanded.append(char)
            index += 1
            continue
        if index + 1 < len(escaped) and escaped[index + 1] == "{":
            end = _compose_braced_expression_end(escaped, index + 1)
            if end is None:
                # Unreachable in production: an unterminated ``${`` classifies
                # ``LITERAL`` in ``_compose_resolve_value``, so the concrete
                # recovery path is never entered for it. Kept for completeness.
                expanded.append(char)  # pragma: no cover
                index += 1  # pragma: no cover
                continue  # pragma: no cover
            piece = _compose_concrete_worker_password_braced(
                escaped[index + 2 : end], worker_env=worker_env
            )
            if piece is None:
                # Unreachable in production: a braced piece that yields ``None``
                # (unparseable name / unknown operator / unset required) would
                # also classify ``LITERAL`` in ``_compose_resolve_value``, so the
                # concrete path is never entered. Kept for completeness.
                return None  # pragma: no cover
            expanded.append(piece)
            index = end + 1
            continue
        plain_match = _COMPOSE_ENV_NAME_PATTERN.match(escaped, index + 1)
        if plain_match is None:
            # Unreachable in production: a ``$`` not followed by a valid name
            # char classifies ``LITERAL`` in ``_compose_resolve_value``, so the
            # concrete path is never entered. Kept for completeness.
            expanded.append(char)  # pragma: no cover
            index += 1  # pragma: no cover
            continue  # pragma: no cover
        name = plain_match.group(0)
        if name not in worker_env:
            return None
        expanded.append(worker_env[name])
        index = plain_match.end()
    return "".join(expanded).replace(_COMPOSE_ESCAPED_DOLLAR, "$")


def _compose_concrete_worker_password_braced(
    expression: str,
    *,
    worker_env: Mapping[str, str],
) -> str | None:
    """Resolve a braced expression to its concrete worker value for redaction.

    Mirrors the operator semantics in :func:`_compose_resolve_braced` but returns
    the concrete value (including worker secrets) rather than a classification:

    - ``:-`` / ``-`` (default): when the variable is set (non-empty for ``:-``)
      return the worker value; otherwise return the recursively-expanded default.
    - ``:+`` / ``+`` (alternate): when the variable is set (non-empty for ``:+``)
      return the recursively-expanded alternate word; otherwise return ``""``.
    - ``:?`` / ``?`` (required): when the variable is set (non-empty for ``:?``)
      return the worker value; otherwise return ``None`` (the stack would fail
      to launch).
    - Bare ``${NAME}``: return the worker value, or ``None`` when unset.
    """
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(expression)
    if name_match is None:
        # Unreachable in production: an unparseable braced name classifies
        # ``LITERAL`` in ``_compose_resolve_value``, so the concrete path is
        # never entered for it. Kept for completeness.
        return None  # pragma: no cover
    name = name_match.group(0)
    remainder = expression[name_match.end() :]
    if not remainder:
        return worker_env.get(name, None)
    # Longest-first match against the braced operators (mirrors
    # ``_compose_resolve_braced``'s scan order). ``next`` is used instead of a
    # ``for`` loop so the no-match path is a single excluded branch rather than
    # an untracked loop-exhausted arc (an unknown operator classifies ``LITERAL``
    # in ``_compose_resolve_value`` and never reaches this concrete recovery).
    match = next(
        ((c, remainder[len(c) :]) for c in _COMPOSE_BRACED_OPERATORS if remainder.startswith(c)),
        None,
    )
    if match is None:
        # Unreachable in production: an unknown operator classifies ``LITERAL``
        # in ``_compose_resolve_value``, so the concrete path is never entered
        # for it. Kept for completeness.
        return None  # pragma: no cover
    operator, word = match
    worker_value = worker_env.get(name)
    is_set = name in worker_env
    is_non_empty = bool(worker_value)
    if operator in _COMPOSE_DEFAULT_OPERATORS:
        if (operator == ":-" and is_non_empty) or (operator == "-" and is_set):
            return worker_value
        # Unreachable through the production call path: a defaulted form with the
        # variable unset classifies ``LITERAL`` in ``_compose_resolve_value``, so
        # ``_collect_postgres_password`` never calls the concrete recovery for
        # this branch (it only calls it for ``WORKER_RESOLVED_DEFAULTED`` /
        # ``WORKER_RESOLVED_SLOT``). Kept for operator-semantic completeness.
        return _compose_concrete_worker_password(word, worker_env=worker_env)  # pragma: no cover
    # The alternate and required-unset branches below are unreachable through
    # the production call path: an alternate form (``:+`` / ``+``) always
    # classifies ``LITERAL`` (the alternate word / empty literal is carried), so
    # the concrete recovery path is never entered for it; an unset required form
    # (``:?`` / ``?``) would fail Compose at stack launch and never reaches a
    # running container. They mirror the operator semantics for
    # completeness/robustness; excluding them avoids hollow tests that call a
    # private helper solely to mark lines executed.
    if operator in _COMPOSE_ALTERNATE_OPERATORS:  # pragma: no cover
        if (operator == ":+" and is_non_empty) or (operator == "+" and is_set):
            return _compose_concrete_worker_password(word, worker_env=worker_env)
        return ""
    if (operator == ":?" and is_non_empty) or (operator == "?" and is_set):
        return worker_value
    return None  # pragma: no cover


def _expanded_value_bears_postgres_password(
    expanded: str,
    postgres_passwords: frozenset[str],
) -> bool:
    """Return whether ``expanded`` embeds any tracked postgres password.

    A rendered agent env DB URL percent-encodes the userinfo password (per
    RFC 3986), so a password containing URL-reserved characters (e.g.
    ``p@ss/word``) appears in the URL as its encoded form (``p%40ss%2Fword``).
    URL userinfo is matched structurally before the fallback substring check so
    short common local passwords (e.g. ``postgres``) do not redact unrelated
    literals such as ``POSTGRES_HOST=postgres``.
    """
    try:
        url_password = urlsplit(expanded).password
    except ValueError:
        url_password = None
    if url_password:
        expanded_passwords = frozenset(
            candidate
            for password in postgres_passwords
            for candidate in (password, quote(password, safe=""))
            if candidate
        )
        if url_password in expanded_passwords or unquote(url_password) in expanded_passwords:
            return True
    for password in postgres_passwords:
        if not password:
            continue
        if (
            len(password) >= _POSTGRES_PASSWORD_SUBSTRING_REDACTION_MIN_LENGTH
            and password in expanded
        ):
            return True
        encoded = quote(password, safe="")
        if (
            encoded != password
            and len(encoded) >= _POSTGRES_PASSWORD_SUBSTRING_REDACTION_MIN_LENGTH
            and encoded in expanded
        ):
            return True
    return False


def _compose_environment_mapping(environment: object) -> dict[str, str]:
    """Normalize a compose ``environment`` scalar, list, or mapping into a string dict.

    A *pass-through* slot (``[NAME]`` / ``NAME:`` / ``NAME: null``) declares no
    value; Compose resolves it from the worker shell at stack launch, so it is
    normalized to the :data:`_COMPOSE_PASSTHROUGH` sentinel (not ``"None"``) so
    call sites skip it from ``profile_env`` and keep it in
    ``env_passthrough_names`` for hosted out-of-band resolution.

    An *explicit* empty value (``NAME: ""`` / ``NAME=``) is a non-nil pointer to
    ``""`` that OVERRIDES the worker shell value, so it is normalized to ``""``
    and CARRIED in ``profile_env`` / EXCLUDED from passthrough — see
    ``literal_profile_env_from_compose`` and
    ``_filter_hosted_env_passthrough_names_from_compose_env``.
    """
    if isinstance(environment, Mapping):
        return {
            str(key): _COMPOSE_PASSTHROUGH if value is None else str(value)
            for key, value in environment.items()
        }
    if isinstance(environment, list):
        mapping: dict[str, str] = {}
        for item in environment:
            if isinstance(item, str):
                key, sep, value = item.partition("=")
                if key:
                    # ``sep`` is ``"="`` when an ``=`` was present (even with an
                    # empty value -> explicit empty literal ``""``); absent
                    # (``""``) for a bare-name pass-through slot.
                    mapping[key] = value if sep else _COMPOSE_PASSTHROUGH
            elif isinstance(item, Mapping):
                mapping.update(
                    {
                        str(key): _COMPOSE_PASSTHROUGH if value is None else str(value)
                        for key, value in item.items()
                    }
                )
        return mapping
    return {}
