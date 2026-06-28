"""Unit tests for provision-time Playwright browser availability probing."""

from __future__ import annotations

import pytest

from awf.profiles.models import (
    RUNTIME_BROWSER_UNAVAILABLE,
    ProfileLintSeverity,
    WorkspaceProfile,
)
from awf.runtime.browser_probe import ProbeExecResult, probe_runtime_browsers


def _profile_with_browsers(browsers: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "browser-profile", "runtime": {"browsers": browsers}}
    )


class _SpyExec:
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


@pytest.mark.unit
class TestProbeRuntimeBrowsers:
    async def test_no_browsers_declared_skips_probe(self) -> None:
        profile = _profile_with_browsers([])
        spy = _SpyExec()

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert spy.calls == []

    async def test_all_declared_browsers_present_no_findings(self) -> None:
        profile = _profile_with_browsers(["chromium", "firefox"])
        spy = _SpyExec(
            [ProbeExecResult(returncode=0, stdout="OK chromium\nOK firefox\n", stderr="")]
        )

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1
        assert spy.calls[0][-2:] == ["chromium", "firefox"]

    async def test_declared_browser_missing_warns(self) -> None:
        profile = _profile_with_browsers(["chromium", "firefox"])
        spy = _SpyExec(
            [ProbeExecResult(returncode=0, stdout="MISSING chromium\nOK firefox\n", stderr="")]
        )

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.reason_code == RUNTIME_BROWSER_UNAVAILABLE
        assert finding.severity == ProfileLintSeverity.warning
        assert finding.path == "runtime.browsers"
        assert finding.details == {
            "browser": "chromium",
            "available_browsers": ["firefox"],
        }

    async def test_probe_exec_oserror_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec()
        spy.raise_exc = OSError("cannot exec into container")

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
        assert len(spy.calls) == 1

    async def test_probe_exec_unexpected_exception_propagates(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec()
        spy.raise_exc = RuntimeError("regression in exec path")

        with pytest.raises(RuntimeError, match="regression in exec path"):
            await probe_runtime_browsers(profile=profile, exec_in_container=spy)

    async def test_probe_returncode_nonzero_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=1, stdout="", stderr="exec failed")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()

    async def test_reachable_probe_without_parseable_status_is_silent(self) -> None:
        profile = _profile_with_browsers(["chromium"])
        spy = _SpyExec([ProbeExecResult(returncode=0, stdout="", stderr="")])

        findings = await probe_runtime_browsers(profile=profile, exec_in_container=spy)

        assert findings == ()
