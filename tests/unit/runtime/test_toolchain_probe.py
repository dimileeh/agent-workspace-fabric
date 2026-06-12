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
    _JAVA_DISCOVERY_COMMAND,
    _TOOLCHAIN_DISCOVERY,
    ProbeExecResult,
    ToolchainDiscoveryStrategy,
    _normalize_java_version,
    _parse_java_exact_versions,
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

    async def test_dotted_declared_11_0_2_satisfied_by_installed_11_0_2(self) -> None:
        # The schema accepts finer dotted declarations; an installed 11.0.2 must
        # satisfy a declared "11.0.2" rather than warning against discovered "11".
        profile = _profile_with_toolchains({"java": ["11.0.2"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout='JAVA_VERSION="11.0.2"\n', stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_dotted_declared_11_0_2_warns_when_only_sibling_patch_installed(self) -> None:
        # Regression for PRRT_kwDOSJAM6s6JD_5P: a dotted declaration carries
        # patch-level intent, so a *different* patch on the same major (11.0.1) must
        # NOT silence a declared 11.0.2 — the missing exact patch still warns, with
        # the discovered major surfaced in available_versions.
        profile = _profile_with_toolchains({"java": ["11.0.2"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout='JAVA_VERSION="11.0.1"\n', stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert [f.details["version"] for f in findings] == ["11.0.2"]
        assert findings[0].details["available_versions"] == ["11"]

    async def test_bare_major_11_satisfied_by_any_installed_patch(self) -> None:
        # A bare-major declaration stays coarse: an installed 11.0.1 satisfies a
        # declared "11" regardless of patch (contrast the dotted case above).
        profile = _profile_with_toolchains({"java": ["11"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout='JAVA_VERSION="11.0.1"\n', stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_legacy_dotted_declared_1_8_satisfied_by_installed_jdk8(self) -> None:
        profile = _profile_with_toolchains({"java": ["1.8"]})
        spy = _SpyExec(
            [
                ProbeExecResult(
                    returncode=0,
                    stdout="/usr/lib/jvm/java-1.8.0-openjdk-amd64/bin/java\n",
                    stderr="",
                )
            ]
        )

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_dotted_declared_1_8_0_satisfied_by_legacy_underscore_patch(self) -> None:
        # Regression for PRRT_kwDOSJAM6s6JENkD: legacy JDK release strings join the
        # update level with an underscore (``1.8.0_392``), which is a refining
        # boundary just like a dot — a declared ``1.8.0`` must be satisfied by an
        # installed ``1.8.0_392`` rather than warning RUNTIME_TOOLCHAIN_UNAVAILABLE.
        profile = _profile_with_toolchains({"java": ["1.8.0"]})
        spy = _SpyExec(
            [ProbeExecResult(returncode=0, stdout='openjdk version "1.8.0_392"\n', stderr="")]
        )

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_dotted_declared_absent_still_warns(self) -> None:
        # A finer dotted declaration whose major is missing still warns, and the
        # warning surfaces the discovered majors in available_versions.
        profile = _profile_with_toolchains({"java": ["23.0.1"]})
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout=_RELEASE_17, stderr="")])

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert [f.details["version"] for f in findings] == ["23.0.1"]
        assert findings[0].details["available_versions"] == ["17"]

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

    async def test_later_language_probe_failure_keeps_earlier_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Register a second discovery strategy so the multi-language orchestration
        # path is exercised: java probes cleanly (warning that 21 is missing), then
        # the second language's probe returns non-zero. A per-language infra
        # failure must not discard java's accurate finding, nor warn for the failed
        # language (whose installed versions are unknown).
        monkeypatch.setitem(
            _TOOLCHAIN_DISCOVERY,
            "node",
            ToolchainDiscoveryStrategy(
                command=("sh", "-c", "true"),
                parse=lambda _output: set(),
                parse_exact=lambda _output: set(),
                normalize=lambda version: version,
            ),
        )
        profile = _profile_with_toolchains({"java": ["17", "21"], "node": ["20"]})
        spy = _SpyExec(
            [
                ProbeExecResult(returncode=0, stdout=_RELEASE_17, stderr=""),
                ProbeExecResult(returncode=1, stdout="", stderr="boom"),
            ]
        )

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        # Only java's genuine "21 missing" warning survives; node stays silent.
        assert [(f.details["language"], f.details["version"]) for f in findings] == [("java", "21")]
        assert len(spy.calls) == 2

    async def test_later_language_probe_exception_keeps_earlier_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same as above but the second language's probe *raises* mid-loop: java's
        # finding is still preserved and the failed language stays silent.
        monkeypatch.setitem(
            _TOOLCHAIN_DISCOVERY,
            "node",
            ToolchainDiscoveryStrategy(
                command=("sh", "-c", "boom"),
                parse=lambda _output: set(),
                parse_exact=lambda _output: set(),
                normalize=lambda version: version,
            ),
        )
        profile = _profile_with_toolchains({"java": ["17", "21"], "node": ["20"]})

        class _OneGoodThenRaise:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            async def __call__(self, cli_args: list[str]) -> ProbeExecResult:
                self.calls.append(cli_args)
                if len(self.calls) == 1:
                    return ProbeExecResult(returncode=0, stdout=_RELEASE_17, stderr="")
                raise RuntimeError("cannot exec into container")

        spy = _OneGoodThenRaise()

        findings = await probe_runtime_toolchains(profile=profile, exec_in_container=spy)

        assert [(f.details["language"], f.details["version"]) for f in findings] == [("java", "21")]
        assert len(spy.calls) == 2

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

    def test_parses_legacy_dotted_jvm_path(self) -> None:
        # RHEL-style legacy JDK 8 directories embed ``1.8.0`` in the path; the
        # path regex must capture the dotted version so it normalizes to ``8``.
        output = "/usr/lib/jvm/java-1.8.0-openjdk-amd64/bin/java\n"

        assert _parse_java_versions(output) == {"8"}

    def test_parses_quoted_java_version_line(self) -> None:
        output = 'openjdk version "17.0.9" 2023-10-17\n'

        assert _parse_java_versions(output) == {"17"}

    def test_empty_output_is_empty_set(self) -> None:
        assert _parse_java_versions("") == set()

    def test_parses_java_version_stderr_style_output(self) -> None:
        # ``java -version`` fallback (merged via ``2>&1``) for images that install
        # the JDK under ``$JAVA_HOME`` outside ``/usr/lib/jvm`` with no alternatives.
        output = 'openjdk version "21.0.1" 2023-10-17\nOpenJDK Runtime Environment\n'

        assert _parse_java_versions(output) == {"21"}


@pytest.mark.unit
class TestParseJavaExactVersions:
    def test_keeps_full_patch_versions_not_just_majors(self) -> None:
        # Unlike _parse_java_versions, the exact parser preserves patch granularity
        # (and the legacy dotted path) so patch-precise declarations can be matched.
        output = (
            'JAVA_VERSION="17.0.9"\n'
            'JAVA_VERSION="11.0.2"\n'
            "/usr/lib/jvm/java-1.8.0-openjdk-amd64/bin/java\n"
        )

        assert _parse_java_exact_versions(output) == {"17.0.9", "11.0.2", "1.8.0"}

    def test_empty_output_is_empty_set(self) -> None:
        assert _parse_java_exact_versions("") == set()


@pytest.mark.unit
class TestJavaDiscoveryCommand:
    def test_command_reads_java_home_and_falls_back_to_java_version(self) -> None:
        # Regression: images that install the JDK under ``$JAVA_HOME`` (e.g.
        # eclipse-temurin's ``/opt/java/openjdk``) outside ``/usr/lib/jvm`` and
        # register no update-alternatives must still be discovered, not reported
        # as missing java.
        script = _JAVA_DISCOVERY_COMMAND[-1]
        assert '"$JAVA_HOME/release"' in script
        assert "java -version 2>&1" in script
        # The trailing ``true`` still guarantees exit 0 when the container is
        # reachable, preserving the probe-infra-failure contract.
        assert script.rstrip().endswith("true")
