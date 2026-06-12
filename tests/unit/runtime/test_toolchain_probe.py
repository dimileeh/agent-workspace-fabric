"""Unit tests for the provision-time toolchain version-discovery probe.

These cover the pure orchestration core in
``awf.runtime.toolchain_probe.probe_runtime_toolchains`` and its java
normalization/parse helpers. The ``available`` contract is the crux: probe-infra
failure (exception or non-zero return) stays globally silent, while a reachable
but tool-less image warns. The probe never runs an exec when no toolchains are
declared or for a language with no registered strategy.
"""

from __future__ import annotations

import pytest

from awf.profiles.models import (
    RUNTIME_TOOLCHAIN_UNAVAILABLE,
    ProfileLintSeverity,
    WorkspaceProfile,
)
from awf.runtime.toolchain_probe import (
    ProbeExecResult,
    _normalize_java_version,
    _parse_java_versions,
    probe_runtime_toolchains,
)


def _profile_with_toolchains(toolchains: dict[str, list[str]]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "toolchain-profile", "runtime": {"toolchains": toolchains}}
    )


class _SpyExec:
    """Records the argvs it was asked to run and returns queued results."""

    def __init__(self, results: list[ProbeExecResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results or [])
        self.raise_exc: BaseException | None = None

    async def __call__(self, cli_args: list[str]) -> ProbeExecResult:
        self.calls.append(cli_args)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self._results:
            return self._results.pop(0)
        return ProbeExecResult(returncode=0, stdout="", stderr="")


_RELEASE_17 = 'JAVA_VERSION="17.0.9"\n'
_RELEASE_21 = 'JAVA_VERSION="21.0.1"\n'


@pytest.mark.unit
class TestProbeRuntimeToolchains:
    async def test_no_toolchains_declared_skips_probe(self) -> None:
        profile = _profile_with_toolchains({})
        spy = _SpyExec()

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert spy.calls == []

    async def test_all_declared_versions_present_no_findings(self) -> None:
        profile = _profile_with_toolchains({"java": ["17", "21"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout=_RELEASE_17 + _RELEASE_21, stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1

    async def test_only_one_version_present_warns_missing(self) -> None:
        profile = _profile_with_toolchains({"java": ["17", "21"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout=_RELEASE_17, stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.reason_code == RUNTIME_TOOLCHAIN_UNAVAILABLE
        assert finding.severity == ProfileLintSeverity.warning
        assert finding.path == "runtime.toolchains.java"
        assert finding.details["language"] == "java"
        assert finding.details["version"] == "21"

    async def test_probe_exec_exception_is_silent(self) -> None:
        profile = _profile_with_toolchains({"java": ["17", "21"]})
        spy = _SpyExec()
        spy.raise_exc = RuntimeError("cannot exec into container")

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        # Probe-infra failure -> available=None -> globally silent.
        assert findings == ()
        assert len(spy.calls) == 1

    async def test_probe_returncode_nonzero_is_silent(self) -> None:
        profile = _profile_with_toolchains({"java": ["17", "21"]})
        spy = _SpyExec([ProbeExecResult(returncode=1, stdout="", stderr="boom")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_tool_absent_empty_set_warns_all(self) -> None:
        profile = _profile_with_toolchains({"java": ["17", "21"]})
        # Reachable image (rc=0) but no JDK installed -> empty parse -> warns both.
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="", stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        warned = sorted(f.details["version"] for f in findings)
        assert warned == ["17", "21"]

    async def test_installed_17_0_9_satisfies_declared_17(self) -> None:
        profile = _profile_with_toolchains({"java": ["17"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout=_RELEASE_17, stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_declared_23_with_only_17_21_installed_warns(self) -> None:
        profile = _profile_with_toolchains({"java": ["17", "21", "23"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout=_RELEASE_17 + _RELEASE_21, stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert [f.details["version"] for f in findings] == ["23"]

    async def test_unprobed_language_does_not_warn(self) -> None:
        # A language with no registered discovery strategy is treated as
        # satisfied so it never produces a false warning — and no exec runs.
        profile = _profile_with_toolchains({"python": ["3.12"]})
        spy = _SpyExec()

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert spy.calls == []

    async def test_unprobed_language_alongside_probed_java(self) -> None:
        # Java is probed (and satisfied); the unsupported language is treated as
        # satisfied without an exec of its own.
        profile = _profile_with_toolchains({"java": ["17"], "python": ["3.12"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout=_RELEASE_17, stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()
        # Only java triggers an exec.
        assert len(spy.calls) == 1


@pytest.mark.unit
class TestNormalizeJavaVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("17.0.9", "17"),
            ("21", "21"),
            ("21.0.1", "21"),
            ("1.8.0_392", "8"),
            ("1.8", "8"),
            ('"17.0.9"', "17"),
            ("08", "8"),
            ("", None),
            ("   ", None),
            ("not-a-version", None),
        ],
    )
    def test_normalize(self, raw: str, expected: str | None) -> None:
        assert _normalize_java_version(raw) == expected


@pytest.mark.unit
class TestParseJavaVersions:
    def test_parses_release_and_alternatives_output(self) -> None:
        output = (
            'JAVA_VERSION="17.0.9"\n'
            "IMPLEMENTOR=Eclipse Adoptium\n"
            'JAVA_VERSION="21.0.1"\n'
            "/usr/lib/jvm/java-17-openjdk-amd64/bin/java\n"
            "/usr/lib/jvm/temurin-21-jdk-amd64/bin/java\n"
        )

        assert _parse_java_versions(output) == {"17", "21"}

    def test_parses_quoted_java_version_line(self) -> None:
        output = 'openjdk version "17.0.9" 2023-10-17\n'

        assert _parse_java_versions(output) == {"17"}

    def test_empty_output_is_empty_set(self) -> None:
        assert _parse_java_versions("") == set()
