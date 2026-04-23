"""Tests for ``scripts.run_awf._expand_host_path`` — the loader-side
helper that turns portable companion-spec paths (``~/...``,
``${VAR}/...``) into concrete absolute paths at runtime.

Review feedback on PR #2 (CodeRabbit Major): companion specs checked
into the repo used hardcoded absolute paths like
``/home/dimileeh/Projects/aira/aira-agent/.env``. Those break on any
other developer's machine. Spec authors can now use ``~`` /
``$HOME`` / ``${AWF_AIRA_CHECKOUT_ROOT}`` variants; this test pins the
expansion behaviour so future refactors don't silently disable it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_awf import _expand_host_path


class TestExpandHostPath:
    @pytest.mark.unit
    def test_tilde_expands_to_home(self) -> None:
        expanded = _expand_host_path("~/Projects/aira/aira-agent/.env")
        # ``~`` should become the actual HOME — not the literal string.
        assert not expanded.startswith("~")
        assert expanded.endswith("/Projects/aira/aira-agent/.env")

    @pytest.mark.unit
    def test_env_var_expands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_AIRA_CHECKOUT_ROOT", "/opt/aira")
        expanded = _expand_host_path("${AWF_AIRA_CHECKOUT_ROOT}/aira-agent/.env")
        assert expanded == "/opt/aira/aira-agent/.env"

    @pytest.mark.unit
    def test_dollar_var_without_braces_expands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_AIRA_CHECKOUT_ROOT", "/opt/aira")
        expanded = _expand_host_path("$AWF_AIRA_CHECKOUT_ROOT/x.env")
        assert expanded == "/opt/aira/x.env"

    @pytest.mark.unit
    def test_missing_env_var_leaves_literal_unchanged(self) -> None:
        """``os.path.expandvars`` leaves ``$NOT_SET`` as-is when the var
        isn't set. Confirm the loader doesn't silently swallow that —
        the subsequent file-read will fail loudly, which is what we
        want (don't mount a bogus ``/...$NOT_SET...`` path)."""
        expanded = _expand_host_path("$AWF_NOT_SET_ENV_VAR_PLS/x.env")
        assert "$AWF_NOT_SET_ENV_VAR_PLS" in expanded

    @pytest.mark.unit
    def test_absolute_path_passes_through(self) -> None:
        # An already-concrete absolute path is a no-op.
        path = "/opt/aira/aira-agent/.env"
        assert _expand_host_path(path) == path

    @pytest.mark.unit
    def test_tilde_and_env_compose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both expansions applied: ``$CHECKOUT`` then ``~``. ``~``
        only expands at the START of a path, so this checks the common
        case where the caller wrote ``~/x`` and we still handle it."""
        monkeypatch.setenv("AIRA_SUBPATH", "Projects/aira")
        assert _expand_host_path("~/${AIRA_SUBPATH}/x.env") == str(
            Path("~/Projects/aira/x.env").expanduser()
        )


class TestShellFallbackSyntax:
    """``${VAR:-default}`` lets specs self-describe their default so
    an operator without the env var set still gets a sensible path.
    Review feedback on PR #4 (CodeRabbit + gemini): ``~/Projects/aira``
    isn't fully portable; an operator whose monorepo lives elsewhere
    needs to override cleanly without hacking the spec."""

    @pytest.mark.unit
    def test_fallback_used_when_env_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWF_AIRA_CHECKOUT_ROOT", raising=False)
        expanded = _expand_host_path("${AWF_AIRA_CHECKOUT_ROOT:-/opt/aira}/aira-agent/.env")
        assert expanded == "/opt/aira/aira-agent/.env"

    @pytest.mark.unit
    def test_env_var_wins_over_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_AIRA_CHECKOUT_ROOT", "/custom/aira")
        expanded = _expand_host_path("${AWF_AIRA_CHECKOUT_ROOT:-/opt/aira}/aira-agent/.env")
        assert expanded == "/custom/aira/aira-agent/.env"

    @pytest.mark.unit
    def test_empty_env_var_falls_back_bash_semantics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shell ``${VAR:-default}`` uses the default when VAR is set
        but EMPTY, not just when unset. Without this contract a docker
        envfile that exported ``AWF_AIRA_CHECKOUT_ROOT=`` (for any
        reason — pipeline bug, human typo) would make
        ``_expand_host_path`` produce ``/aira-agent/.env`` and we'd
        bind-mount filesystem root into the container. Regression
        guard for PR #4 review feedback (CodeRabbit Major):
        ``os.environ.get(var, default)`` was handling only the unset
        case; replaced with ``or default`` to cover both."""
        monkeypatch.setenv("AWF_AIRA_CHECKOUT_ROOT", "")
        expanded = _expand_host_path("${AWF_AIRA_CHECKOUT_ROOT:-/opt/aira}/aira-agent/.env")
        assert expanded == "/opt/aira/aira-agent/.env"

    @pytest.mark.unit
    def test_fallback_itself_gets_tilde_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default value inside ``${VAR:-default}`` should also
        go through ``~`` expansion — that's the whole point of
        providing ``~/Projects/aira`` as the default: operators with
        the standard layout get a valid path, no env var required."""
        monkeypatch.delenv("AWF_AIRA_CHECKOUT_ROOT", raising=False)
        expanded = _expand_host_path("${AWF_AIRA_CHECKOUT_ROOT:-~/Projects/aira}/aira-agent/.env")
        assert expanded == str(Path("~/Projects/aira/aira-agent/.env").expanduser())
        assert not expanded.startswith("~")

    @pytest.mark.unit
    def test_empty_fallback_stays_empty_when_var_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Edge case: ``${VAR:-}`` means "empty string if unset". The
        resulting path would be bogus but we still parse cleanly
        (shell semantics, not ours to second-guess)."""
        monkeypatch.delenv("AWF_MISSING_ENV_VAR", raising=False)
        assert _expand_host_path("${AWF_MISSING_ENV_VAR:-}/x") == "/x"

    @pytest.mark.unit
    def test_no_fallback_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pre-existing ``${VAR}/path`` without ``:-`` still expands
        exactly as before — the fallback is optional."""
        monkeypatch.setenv("AWF_PATH_ONLY", "/just/path")
        assert _expand_host_path("${AWF_PATH_ONLY}/x.env") == "/just/path/x.env"
