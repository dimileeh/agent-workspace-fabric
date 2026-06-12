"""Provision-time, in-container toolchain version discovery.

A profile may declare ``runtime.toolchains`` (e.g. ``{"java": ["17", "21"]}``)
to assert which language toolchain versions the runtime/toolchain image must
provide. The pure lint seam :func:`awf.profiles.models.runtime_toolchain_findings`
turns a discovered ``available`` mapping into ``RUNTIME_TOOLCHAIN_UNAVAILABLE``
warnings, but it needs that mapping supplied by an image-introspecting probe.

This module is that probe's *provider-neutral core*: a small per-language
discovery registry plus the orchestration that runs each declared language's
discovery command inside the workspace container, normalizes the discovered
versions to the declared (major-version) granularity, and hands the result to
the helper. It performs no I/O itself — the caller injects an
``exec_in_container`` coroutine (the real one reuses the validation runner's
tracked compose-exec path) — so it stays unit-testable.

The probe is strictly additive and non-blocking. The ``available`` contract is
the crux of avoiding false warnings:

* probe infrastructure cannot exec into the container at all (the discovery
  command raises, or returns non-zero — it is written to always exit 0 when the
  container is reachable) -> ``available is None`` -> the helper stays globally
  silent;
* a declared language's tool is genuinely absent from a reachable image ->
  that language maps to an empty set -> the helper warns every declared version;
* a declared language with no registered discovery strategy is treated as
  satisfied (no false warning for a not-yet-supported language).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from awf.profiles.models import (
    ProfileLintFinding,
    WorkspaceProfile,
    runtime_toolchain_findings,
)


@dataclass(frozen=True)
class ProbeExecResult:
    """Result of running a discovery command inside the workspace container."""

    returncode: int
    stdout: str
    stderr: str


# A coroutine that execs an argv inside the workspace container and returns its
# result. Injected by the caller so this module performs no I/O of its own.
ExecInContainer = Callable[[list[str]], Awaitable[ProbeExecResult]]


@dataclass(frozen=True)
class ToolchainDiscoveryStrategy:
    """How to discover every installed version of one language's toolchain."""

    command: tuple[str, ...]
    parse: Callable[[str], set[str]]


# Enumerate *all* installed JDKs, not just the default ``java -version`` reports.
# Reading each ``/usr/lib/jvm/*/release`` yields ``JAVA_VERSION="17.0.9"`` lines;
# ``update-alternatives --list java`` adds the registered JDK paths as a
# fallback. The trailing ``true`` guarantees exit 0 whenever the container is
# reachable, so a non-zero return unambiguously signals probe-infra failure.
_JAVA_DISCOVERY_COMMAND = (
    "sh",
    "-c",
    "cat /usr/lib/jvm/*/release 2>/dev/null; update-alternatives --list java 2>/dev/null; true",
)

# ``JAVA_VERSION="17.0.9"`` from release files (quotes optional).
_JAVA_RELEASE_VERSION_RE = re.compile(r'JAVA_VERSION="?([0-9][0-9._]*)"?')
# ``openjdk version "17.0.9"`` style output.
_JAVA_QUOTED_VERSION_RE = re.compile(r'version "([0-9][0-9._]*)"')
# JDK major embedded in a jvm directory / alternatives path, e.g.
# ``/usr/lib/jvm/java-17-openjdk-amd64/bin/java`` or ``temurin-21-jdk``.
# Capture dotted/underscored versions too so legacy RHEL-style paths like
# ``java-1.8.0-openjdk-amd64`` yield ``1.8.0`` (normalized to ``8``) rather
# than a bare ``1`` that cannot be resolved to its real major.
_JAVA_PATH_MAJOR_RE = re.compile(
    r"(?:java|jdk|jre|openjdk|temurin|zulu|corretto|graalvm|semeru)-([0-9][0-9._]*)"
)
_LEADING_INT_RE = re.compile(r"\d+")


def _leading_int(component: str) -> str | None:
    """Return the leading integer of ``component`` (e.g. ``"0_392" -> "0"``)."""
    match = _LEADING_INT_RE.match(component.strip())
    if match is None:
        return None
    return str(int(match.group()))


def _normalize_java_version(raw: str) -> str | None:
    """Normalize a discovered Java version to its declared major granularity.

    Declared versions are coarse majors (``"17"``, ``"21"``) while installed
    versions are fine (``"17.0.9"``). Map each discovered version to the major
    so a declared ``"17"`` is satisfied by an installed ``17.0.9`` without a
    false warning, and a declared ``"23"`` against only 17/21 still warns:

    * modern ``17.0.9`` / ``21`` -> leading numeric major (``"17"`` / ``"21"``);
    * legacy ``1.8.0_392`` -> ``"8"`` (first component ``1`` -> use the second);
    * unparseable input -> ``None`` (dropped by the caller).
    """
    cleaned = raw.strip().strip('"').strip()
    if not cleaned:
        return None
    parts = cleaned.split(".")
    first = _leading_int(parts[0])
    if first is None:
        return None
    if first == "1" and len(parts) >= 2:
        return _leading_int(parts[1])
    return first


def _parse_java_versions(output: str) -> set[str]:
    """Parse discovery output into the set of installed Java major versions."""
    versions: set[str] = set()
    for pattern in (_JAVA_RELEASE_VERSION_RE, _JAVA_QUOTED_VERSION_RE, _JAVA_PATH_MAJOR_RE):
        for raw in pattern.findall(output):
            normalized = _normalize_java_version(raw)
            if normalized is not None:
                versions.add(normalized)
    return versions


# Per-language discovery registry. node/python/go/rust/cpp slot in here later by
# adding a discovery command + parser; the orchestration below is language-agnostic.
_TOOLCHAIN_DISCOVERY: dict[str, ToolchainDiscoveryStrategy] = {
    "java": ToolchainDiscoveryStrategy(
        command=_JAVA_DISCOVERY_COMMAND,
        parse=_parse_java_versions,
    ),
}


async def probe_runtime_toolchains(
    *,
    profile: WorkspaceProfile,
    exec_in_container: ExecInContainer,
) -> tuple[ProfileLintFinding, ...]:
    """Discover installed toolchain versions and return any availability findings.

    Builds the ``available`` mapping demanded by
    :func:`runtime_toolchain_findings` by running each declared language's
    discovery command in the container, then returns the helper's findings. See
    the module docstring for the ``available`` contract that keeps probe-infra
    failures globally silent while still warning on genuinely-absent tools.
    """
    if not profile.runtime.toolchains:
        return ()

    available: dict[str, set[str]] = {}
    for language in profile.runtime.toolchains:
        strategy = _TOOLCHAIN_DISCOVERY.get(language)
        if strategy is None:
            # No discovery for this language yet: treat the declared versions as
            # satisfied so an unsupported language never produces a false warning.
            available[language] = set(profile.runtime.toolchains[language])
            continue
        try:
            result = await exec_in_container(list(strategy.command))
        except Exception:
            # Cannot exec into the container at all -> probe-infra failure ->
            # stay globally silent rather than warn on missing introspection.
            return runtime_toolchain_findings(profile, None)
        if result.returncode != 0:
            # The discovery command always exits 0 when reachable, so a non-zero
            # return means the container/compose-exec is unreachable -> silent.
            return runtime_toolchain_findings(profile, None)
        # Reachable image: an empty parse means the tool is genuinely absent, so
        # the helper warns every declared version for this language.
        available[language] = strategy.parse(result.stdout)

    return runtime_toolchain_findings(profile, available)
