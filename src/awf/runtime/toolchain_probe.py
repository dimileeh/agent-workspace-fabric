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
    # Parse discovery output into the *exact* installed version strings (e.g.
    # ``17.0.9`` / ``1.8.0``), not coarsened to majors. Used to honour the
    # patch-level intent of a dotted declaration (``11.0.2``) so a sibling patch on
    # the same major (``11.0.1``) does not silence it.
    parse_exact: Callable[[str], set[str]]
    # Map a *declared* version string to the same granularity ``parse`` emits, so a
    # bare-major declaration (``17``) matches a discovered major regardless of patch.
    # Returns ``None`` if unparseable.
    normalize: Callable[[str], str | None]


# Enumerate *all* installed JDKs, not just the default ``java -version`` reports.
# Reading each ``/usr/lib/jvm/*/release`` yields ``JAVA_VERSION="17.0.9"`` lines;
# ``update-alternatives --list java`` adds the registered JDK paths as a
# fallback. Common official images (eclipse-temurin, amazoncorretto) instead
# install the JDK under ``$JAVA_HOME`` (e.g. ``/opt/java/openjdk``) outside
# ``/usr/lib/jvm`` and register no alternatives, so also read ``$JAVA_HOME/release``
# and fall back to ``java -version`` (which prints to stderr, hence ``2>&1``) —
# otherwise such an image parses empty and is wrongly reported as missing java.
# The trailing ``true`` guarantees exit 0 whenever the container is reachable, so
# a non-zero return unambiguously signals probe-infra failure.
_JAVA_DISCOVERY_COMMAND = (
    "sh",
    "-c",
    'cat /usr/lib/jvm/*/release "$JAVA_HOME/release" 2>/dev/null; '
    "update-alternatives --list java 2>/dev/null; java -version 2>&1; true",
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


def _parse_java_exact_versions(output: str) -> set[str]:
    """Parse discovery output into the set of *exact* installed Java versions.

    Unlike :func:`_parse_java_versions` (which coarsens to majors), this keeps the
    full discovered version string — ``17.0.9`` from ``JAVA_VERSION="17.0.9"``,
    ``1.8.0`` from a legacy ``java-1.8.0-openjdk`` path — so a patch-precise
    declaration (``11.0.2``) can be matched against the installed patch rather than
    merely its major. Each raw capture is stripped of surrounding quotes/whitespace.
    """
    exact: set[str] = set()
    for pattern in (_JAVA_RELEASE_VERSION_RE, _JAVA_QUOTED_VERSION_RE, _JAVA_PATH_MAJOR_RE):
        # Each pattern's capture group is digit-led and quote-free, so the raw match
        # is already a clean version string (just trim any incidental whitespace).
        exact.update(raw.strip() for raw in pattern.findall(output))
    return exact


def _declared_version_satisfied(
    version: str,
    discovered_majors: set[str],
    discovered_exact: set[str],
    normalize: Callable[[str], str | None],
) -> bool:
    """Decide whether one declared toolchain version is satisfied by discovery.

    A *bare-major* declaration (no dot, e.g. ``"17"`` / ``"21"``) matches by major,
    so an installed ``17.0.9`` satisfies ``"17"`` regardless of patch level. A
    *dotted* declaration carries patch-level intent the operator must be warned
    about when unmet: it is satisfied only by an exact discovered version that
    equals it or refines it at a component boundary — ``"1.8"`` by an installed
    ``1.8.0``, ``"11.0.2"`` by an installed ``11.0.2`` — never merely by a sibling
    patch on the same major, so a declared ``"11.0.2"`` is *not* satisfied by an
    installed ``11.0.1``. Legacy JDK release strings join the update level with an
    underscore (``1.8.0_392``), so that too counts as a refining boundary —
    ``"1.8.0"`` is satisfied by an installed ``1.8.0_392``.
    """
    if "." not in version:
        major = normalize(version)
        return major is not None and major in discovered_majors
    return any(
        exact == version or exact.startswith(f"{version}.") or exact.startswith(f"{version}_")
        for exact in discovered_exact
    )


# Per-language discovery registry. node/python/go/rust/cpp slot in here later by
# adding a discovery command + parser; the orchestration below is language-agnostic.
_TOOLCHAIN_DISCOVERY: dict[str, ToolchainDiscoveryStrategy] = {
    "java": ToolchainDiscoveryStrategy(
        command=_JAVA_DISCOVERY_COMMAND,
        parse=_parse_java_versions,
        parse_exact=_parse_java_exact_versions,
        normalize=_normalize_java_version,
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
            result = None
        # The discovery command always exits 0 when the container is reachable, so
        # an exception or a non-zero return both signal a probe-infra failure for
        # this language. If nothing has been probed yet the container is wholly
        # unreachable -> stay globally silent (available is None). But once an
        # earlier language has probed cleanly the container *is* reachable, so
        # preserve those accurate findings and treat only this language as
        # satisfied (silent for it) instead of discarding the partial results.
        if result is None or result.returncode != 0:
            if not available:
                return runtime_toolchain_findings(profile, None)
            available[language] = set(profile.runtime.toolchains[language])
            continue
        # Reachable image: an empty parse means the tool is genuinely absent, so
        # the helper warns every declared version for this language.
        discovered = strategy.parse(result.stdout)
        discovered_exact = strategy.parse_exact(result.stdout)
        # ``discovered`` holds majors (``17``); the helper compares declared strings
        # exactly and the schema accepts both bare majors (``17``) and finer dotted
        # declarations (``1.8``, ``11.0.2``). A bare-major declaration is satisfied by
        # any installed patch on that major, but a dotted declaration carries
        # patch-level intent: match it only against an exact discovered version that
        # equals or refines it, so an installed ``11.0.1`` does *not* silence a
        # declared ``11.0.2``. Keep the discovered majors in ``available`` so
        # bare-major declarations still match (and surface as ``available_versions``).
        satisfied = {
            version
            for version in profile.runtime.toolchains[language]
            if _declared_version_satisfied(
                version, discovered, discovered_exact, strategy.normalize
            )
        }
        available[language] = discovered | satisfied

    return runtime_toolchain_findings(profile, available)
