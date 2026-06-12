"""Focused coverage for init helper edge branches."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

from awf.cli import init_ops
from awf.cli.common import OutputFormat
from awf.profiles.models import EgressMode


def _preview(*, template: str = "generic", validate_commands: list[str] | None = None) -> Any:
    return SimpleNamespace(
        draft=SimpleNamespace(
            template=template,
            profile=SimpleNamespace(
                phases=SimpleNamespace(validate_commands=validate_commands or [])
            ),
        )
    )


@pytest.mark.unit
def test_project_onboarding_rejects_file_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_file = tmp_path / "not-a-directory"
    project_file.write_text("content\n", encoding="utf-8")

    with pytest.raises(typer.Exit) as excinfo:
        init_ops._run_init_project_onboarding(  # noqa: SLF001
            project_file,
            include_smoke_request=False,
            guided=None,
            write_profile=False,
            yes=False,
            template="generic",
            force=False,
            fmt=OutputFormat.pretty,
        )

    assert excinfo.value.exit_code == 2
    assert "not a directory" in capsys.readouterr().err


@pytest.mark.unit
def test_project_onboarding_rejects_guided_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as excinfo:
        init_ops._run_init_project_onboarding(  # noqa: SLF001
            tmp_path,
            include_smoke_request=False,
            guided=True,
            write_profile=False,
            yes=False,
            template="generic",
            force=False,
            fmt=OutputFormat.json,
        )

    assert excinfo.value.exit_code == 2
    assert "--guided cannot be used with --format json" in capsys.readouterr().err


# --- Forge detection / auth helpers (issue #539) --------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("repo_url", "expected"),
    [
        ("git@github.com:acme/widgets.git", "github.com"),
        ("https://user:token@bitbucket.org/acme/widgets.git", "bitbucket.org"),
        ("ssh://git@gitlab.example.com:22/acme/widgets.git", "gitlab.example.com"),
        # Bare ``owner/repo`` slug has no host.
        ("acme/widgets", None),
        # Malformed URL makes ``urlsplit`` raise ``ValueError`` -> ``None``.
        ("https://[", None),
    ],
)
def test_redacted_remote_host_strips_credentials_and_handles_unparseable(
    repo_url: str, expected: str | None
) -> None:
    # An ``https://user:token@host/...`` origin must never surface its embedded
    # credential; only the credential-free host is returned.
    assert init_ops._redacted_remote_host(repo_url) == expected  # noqa: SLF001


@pytest.mark.unit
def test_detect_github_forge_auth_degrades_to_unknown_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness is ``unknown`` (never blocking) when settings are unavailable."""
    from awf.host_setup.system_checks import SetupCheckLevel, SetupCheckResult

    monkeypatch.setattr(
        "awf.host_setup.system_checks.check_gh",
        lambda **_kwargs: SetupCheckResult(
            name="gh", level=SetupCheckLevel.OK, summary="ok", detail="ok"
        ),
    )

    def _readiness_must_not_run(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("readiness must not run without settings")

    monkeypatch.setattr(
        "awf.service.provider_readiness.check_single_provider_readiness",
        _readiness_must_not_run,
    )

    auth, auth_ok = init_ops._detect_github_forge_auth(  # noqa: SLF001
        settings=None, service_env=None
    )

    assert auth == {"gh_present": True, "github_readiness": "unknown"}
    assert auth_ok is False


@pytest.mark.unit
def test_detect_github_forge_auth_ok_when_token_present_without_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured token authenticates even with the ``gh`` CLI uninstalled.

    Regression for the misleading ``auth_ok: false`` (PR #541 review): AWF's
    GitHub API operations rely on ``AWF_GITHUB_TOKEN`` (readiness), not the ``gh``
    binary, so a present+valid token with ``gh`` absent is genuinely authenticated.
    ``gh`` presence stays reported as a separate advisory signal but must not drag
    ``auth_ok`` to ``False``.
    """
    from awf.host_setup.system_checks import SetupCheckLevel, SetupCheckResult

    monkeypatch.setattr(
        "awf.host_setup.system_checks.check_gh",
        lambda **_kwargs: SetupCheckResult(
            name="gh", level=SetupCheckLevel.WARNING, summary="missing", detail="missing"
        ),
    )
    monkeypatch.setattr(
        "awf.service.provider_readiness.check_single_provider_readiness",
        lambda *_a, **_k: {"ok": True},
    )

    auth, auth_ok = init_ops._detect_github_forge_auth(  # noqa: SLF001
        settings=object(), service_env=None
    )

    assert auth == {"gh_present": False, "github_readiness": "ok"}
    assert auth_ok is True


@pytest.mark.unit
def test_detect_bitbucket_forge_auth_falls_back_to_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the resolved service env is unavailable, read ``os.environ``.

    Token + email with no ``BITBUCKET_AUTH_MODE`` is the valid default-basic
    config, so the optional mode selector must not be reported as missing.
    """
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("BITBUCKET_EMAIL", "dev@example.com")
    monkeypatch.delenv("BITBUCKET_AUTH_MODE", raising=False)

    auth, auth_ok = init_ops._detect_bitbucket_forge_auth(None)  # noqa: SLF001

    assert auth["missing"] == []
    assert auth_ok is True


@pytest.mark.unit
def test_detect_bitbucket_forge_auth_bearer_mode_needs_token_only() -> None:
    """``BITBUCKET_AUTH_MODE=bearer`` authenticates with the token alone."""
    auth, auth_ok = init_ops._detect_bitbucket_forge_auth(  # noqa: SLF001
        {"BITBUCKET_API_TOKEN": "tok", "BITBUCKET_AUTH_MODE": "bearer"}
    )

    assert auth["missing"] == []
    assert auth_ok is True


@pytest.mark.unit
def test_detect_bitbucket_forge_auth_unknown_mode_requires_email() -> None:
    """An unrecognized auth mode is treated conservatively as ``basic``."""
    auth, auth_ok = init_ops._detect_bitbucket_forge_auth(  # noqa: SLF001
        {"BITBUCKET_API_TOKEN": "tok", "BITBUCKET_AUTH_MODE": "weird"}
    )

    assert auth["missing"] == ["BITBUCKET_EMAIL"]
    assert auth_ok is False


@pytest.mark.unit
def test_detect_bitbucket_forge_auth_invalid_mode_with_full_creds_is_not_ok() -> None:
    """A misspelled auth mode is a warning even when token + email are both present.

    ``BitbucketAuth.from_env`` rejects any mode outside ``basic``/``bearer`` with
    ``BITBUCKET_AUTH_NOT_CONFIGURED``, so init must not report ``auth_ok`` for a
    config the runtime client will refuse (PR #541 review).
    """
    auth, auth_ok = init_ops._detect_bitbucket_forge_auth(  # noqa: SLF001
        {
            "BITBUCKET_API_TOKEN": "tok",
            "BITBUCKET_EMAIL": "e@x.com",
            "BITBUCKET_AUTH_MODE": "bearrer",
        }
    )

    assert auth["missing"] == []
    assert auth["invalid_mode"] is True
    assert auth_ok is False


@pytest.mark.unit
def test_forge_guidance_lines_bitbucket_invalid_mode_warns() -> None:
    """Invalid Bitbucket auth mode surfaces a fix hint, not an auth-present line."""
    lines = init_ops._forge_guidance_lines(  # noqa: SLF001
        {
            "forge": "bitbucket",
            "auth": {
                "bitbucket_keys": {
                    "BITBUCKET_API_TOKEN": True,
                    "BITBUCKET_EMAIL": True,
                    "BITBUCKET_AUTH_MODE": True,
                },
                "missing": [],
                "invalid_mode": True,
            },
        }
    )

    assert any("BITBUCKET_AUTH_MODE" in line and "basic" in line for line in lines)
    assert not any("Bitbucket auth env present" in line for line in lines)


@pytest.mark.unit
def test_detect_bitbucket_forge_auth_whitespace_only_values_are_missing() -> None:
    """Whitespace-only ``BITBUCKET_*`` values are treated as absent.

    Mirrors ``bitbucket_credentials_present``, which strips token and email before
    the presence check; otherwise a whitespace-only value would falsely report
    ``auth_ok`` while runtime auth still fails.
    """
    auth, auth_ok = init_ops._detect_bitbucket_forge_auth(  # noqa: SLF001
        {"BITBUCKET_API_TOKEN": "  ", "BITBUCKET_EMAIL": "\t"}
    )

    assert auth["bitbucket_keys"] == {
        "BITBUCKET_API_TOKEN": False,
        "BITBUCKET_EMAIL": False,
        "BITBUCKET_AUTH_MODE": False,
    }
    assert auth["missing"] == ["BITBUCKET_API_TOKEN", "BITBUCKET_EMAIL"]
    assert auth_ok is False


@pytest.mark.unit
def test_detect_project_forge_auth_recomputes_service_env_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``None`` service env is recomputed from the Compose ``.env``-merged view.

    When onboarding collection leaves ``service_env`` unset (e.g.
    ``local_service_environ`` raised after settings resolved), forge auth must still
    verify the same merged view doctor/status use — not bare ``os.environ`` — so a
    Bitbucket token present only in root ``.env`` is not wrongly reported missing.
    """
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _repo: "https://bitbucket.org/acme/widgets.git",
    )
    # ``os.environ`` lacks the credentials; only the merged ``.env`` view carries them.
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"BITBUCKET_API_TOKEN": "tok", "BITBUCKET_EMAIL": "dev@example.com"},
    )

    block = init_ops._detect_project_forge_auth(  # noqa: SLF001
        tmp_path, settings=None, service_env=None
    )

    assert block["forge"] == "bitbucket"
    assert block["auth_ok"] is True
    assert block["auth"]["missing"] == []  # type: ignore[index]


@pytest.mark.unit
def test_detect_project_forge_auth_service_env_recompute_failure_is_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed env recompute degrades to ``os.environ``, never raising out."""
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _repo: "https://bitbucket.org/acme/widgets.git",
    )
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("BITBUCKET_EMAIL", "dev@example.com")

    def _raise() -> dict[str, str]:
        raise OSError("env file unreadable")

    monkeypatch.setattr("awf.service.config.local_service_environ", _raise)

    block = init_ops._detect_project_forge_auth(  # noqa: SLF001
        tmp_path, settings=None, service_env=None
    )

    # Recompute failed; the per-forge ``os.environ`` fallback still verifies auth.
    assert block["forge"] == "bitbucket"
    assert block["auth_ok"] is True


@pytest.mark.unit
def test_detect_project_forge_auth_recompute_interpolation_error_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Compose interpolation error during recompute degrades to ``os.environ``.

    ``local_service_environ`` merges the Compose ``.env``; a malformed interpolation
    raises :class:`ComposeEnvInterpolationError` (a ``ValueError``). That must degrade
    to the documented per-forge ``os.environ`` fallback like ``OSError`` does, not
    bubble up into a generic ``detection_error`` block that skips forge auth.
    """
    from awf.service.environment import ComposeEnvInterpolationError

    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _repo: "https://bitbucket.org/acme/widgets.git",
    )
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("BITBUCKET_EMAIL", "dev@example.com")

    def _raise() -> dict[str, str]:
        raise ComposeEnvInterpolationError("MISSING_VAR", "MISSING_VAR is required")

    monkeypatch.setattr("awf.service.config.local_service_environ", _raise)

    block = init_ops._detect_project_forge_auth(  # noqa: SLF001
        tmp_path, settings=None, service_env=None
    )

    # Recompute failed; the per-forge ``os.environ`` fallback still verifies auth.
    assert block["forge"] == "bitbucket"
    assert block["auth_ok"] is True
    assert "detection_error" not in block


@pytest.mark.unit
def test_explicit_project_forge_reads_pinned_value_from_disk(tmp_path: Path) -> None:
    """A pinned ``forge:`` in an on-disk profile is read (not the drafted default)."""
    awf_dir = tmp_path / ".awf"
    awf_dir.mkdir()
    (awf_dir / "workspace.yml").write_text("forge: bitbucket\n", encoding="utf-8")

    assert init_ops._explicit_project_forge(tmp_path) == "bitbucket"  # noqa: SLF001


@pytest.mark.unit
def test_explicit_project_forge_reads_nested_awf_section(tmp_path: Path) -> None:
    """The ``forge:`` under a top-level ``awf:`` mapping is honored too."""
    awf_dir = tmp_path / ".awf"
    awf_dir.mkdir()
    (awf_dir / "workspace.yml").write_text("awf:\n  forge: github\n", encoding="utf-8")

    assert init_ops._explicit_project_forge(tmp_path) == "github"  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize(
    "contents",
    [
        # No profile file at all is handled by the missing-path branch (see below).
        "forge: 123\n",  # non-string forge value -> "auto"
        "awf: not-a-mapping\n",  # awf section is not a mapping -> "auto"
        "- just\n- a\n- list\n",  # top-level is not a mapping -> "auto"
        "",  # empty file parses to ``None`` -> "auto"
        ": : invalid : :\n",  # malformed YAML -> "auto"
    ],
)
def test_explicit_project_forge_falls_back_to_auto(tmp_path: Path, contents: str) -> None:
    """Missing key, wrong shape, or unreadable profile defers to URL detection."""
    awf_dir = tmp_path / ".awf"
    awf_dir.mkdir()
    (awf_dir / "workspace.yml").write_text(contents, encoding="utf-8")

    assert init_ops._explicit_project_forge(tmp_path) == "auto"  # noqa: SLF001


@pytest.mark.unit
def test_explicit_project_forge_returns_auto_without_profile(tmp_path: Path) -> None:
    """No on-disk workspace profile defers to URL-host detection."""
    assert init_ops._explicit_project_forge(tmp_path) == "auto"  # noqa: SLF001


@pytest.mark.unit
def test_forge_guidance_lines_cover_github_missing_gh_and_unknown_auth() -> None:
    lines = init_ops._forge_guidance_lines(  # noqa: SLF001
        {
            "forge": "github",
            "host": "github.com",
            "host_detected": True,
            "auth": {"gh_present": False, "github_readiness": "unknown"},
        }
    )

    assert "Detected forge: github." in lines
    assert any("Install the GitHub CLI (gh)" in line for line in lines)
    assert any("GitHub auth not checked" in line for line in lines)


@pytest.mark.unit
def test_forge_guidance_lines_detection_error_does_not_claim_missing_remote() -> None:
    """A detection-error block surfaces a retry hint, not the add-a-remote advice."""
    lines = init_ops._forge_guidance_lines(  # noqa: SLF001
        init_ops._forge_detection_error_block()  # noqa: SLF001
    )

    assert any("Forge auth check could not run" in line for line in lines)
    assert not any("No `origin` remote detected" in line for line in lines)


@pytest.mark.unit
def test_forge_guidance_lines_no_origin_advises_adding_remote() -> None:
    """The genuine no-origin neutral block still advises adding a git remote."""
    lines = init_ops._forge_guidance_lines(  # noqa: SLF001
        init_ops._neutral_forge_block()  # noqa: SLF001
    )

    assert any("No `origin` remote detected" in line for line in lines)
    assert not any("Forge auth check could not run" in line for line in lines)


@pytest.mark.unit
def test_guided_project_onboarding_rejects_unknown_template(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(init_ops.typer, "prompt", lambda *_args, **_kwargs: "missing")

    with pytest.raises(typer.Exit) as excinfo:
        init_ops._prompt_project_onboarding_choices(  # noqa: SLF001
            tmp_path,
            preview=_preview(template="generic"),
            include_smoke_request=False,
            supported_templates=("generic",),
            egress_mode_type=EgressMode,
            preview_factory=lambda *_args, **_kwargs: _preview(template="generic"),
            customize_preview=lambda preview, **_kwargs: preview,
        )

    assert excinfo.value.exit_code == 2
    assert "unsupported onboarding template" in capsys.readouterr().err


@pytest.mark.unit
def test_guided_project_onboarding_rejects_unknown_egress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    responses = iter(["generic", "sideways"])
    monkeypatch.setattr(init_ops.typer, "prompt", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(typer.Exit) as excinfo:
        init_ops._prompt_project_onboarding_choices(  # noqa: SLF001
            tmp_path,
            preview=_preview(template="generic"),
            include_smoke_request=False,
            supported_templates=("generic",),
            egress_mode_type=EgressMode,
            preview_factory=lambda *_args, **_kwargs: _preview(template="generic"),
            customize_preview=lambda preview, **_kwargs: preview,
        )

    assert excinfo.value.exit_code == 2
    assert "unsupported egress mode" in capsys.readouterr().err


@pytest.mark.unit
def test_guided_project_onboarding_rebuilds_preview_and_collects_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = iter(["python", "open", "needs package downloads", "pytest -q"])
    confirms = iter([True, False, True])
    captured: dict[str, object] = {}

    def _customize(preview: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return preview

    monkeypatch.setattr(init_ops.typer, "prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(init_ops.typer, "confirm", lambda *_args, **_kwargs: next(confirms))

    preview, wants_write = init_ops._prompt_project_onboarding_choices(  # noqa: SLF001
        tmp_path,
        preview=_preview(template="generic"),
        include_smoke_request=True,
        supported_templates=("generic", "python"),
        egress_mode_type=EgressMode,
        preview_factory=lambda *_args, **_kwargs: _preview(template="python"),
        customize_preview=_customize,
    )

    assert preview.draft.template == "python"
    assert wants_write is True
    assert captured == {
        "egress_mode": EgressMode.open,
        "open_explanation": "needs package downloads",
        "validation_commands": ["pytest -q"],
    }


@pytest.mark.unit
def test_service_compose_env_path_helpers_cover_rejections(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("include:\n  - ./docker/compose/local-service.yml\n", encoding="utf-8")
    compose_env = tmp_path / ".env"
    unrelated_env = tmp_path / "other" / ".env"
    unrelated_env.parent.mkdir()

    assert (
        init_ops._trusted_service_compose_env_file_from_verified_paths(  # noqa: SLF001
            compose_file, compose_env
        )
        == compose_env
    )
    assert (
        init_ops._trusted_service_compose_env_file_from_verified_paths(  # noqa: SLF001
            compose_file.with_name("compose.yml"), compose_env
        )
        is None
    )
    assert (
        init_ops._trusted_service_compose_env_file_from_verified_paths(  # noqa: SLF001
            compose_file, unrelated_env
        )
        is None
    )
    assert init_ops._compose_root_env_file(Path("docker/compose/.env")) is None  # noqa: SLF001


@pytest.mark.unit
def test_env_seed_small_helpers_cover_negative_edges(tmp_path: Path) -> None:
    root_env = tmp_path / ".env"
    compose_env = tmp_path / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True)
    root_env.write_text("AWF_API_TOKEN=root\n", encoding="utf-8")

    assert init_ops._init_env_overlay_source(compose_env, tmp_path / "wrong.example") is None  # noqa: SLF001
    assert init_ops._env_assignment_line_with_key("not an assignment", "KEY") == "not an assignment"  # noqa: SLF001
    assert init_ops._env_seed_has_meaningful_leading_context(["", "# comment", "KEY=1"]) is True  # noqa: SLF001
    assert init_ops._env_seed_has_meaningful_leading_context(["", "KEY=1"]) is False  # noqa: SLF001
    assert init_ops._env_value_has_same_line_closing_quote('"a\\"b"', '"') is True  # noqa: SLF001
    assert init_ops._env_value_has_same_line_closing_quote('"unterminated', '"') is False  # noqa: SLF001
    assert init_ops._env_context_looks_like_file_header(["# one", "# two"]) is True  # noqa: SLF001
    assert init_ops._env_comment_looks_key_specific("# API token", "AWF_API_TOKEN") is True  # noqa: SLF001
    assert init_ops._env_comment_looks_key_specific("# generic", "AWF") is False  # noqa: SLF001
    assert init_ops._env_contents_have_multiline_values("KEY=value\\\ncontinued\n")  # noqa: SLF001
    assert init_ops._env_contents_have_multiline_values("KEY='unterminated\n")  # noqa: SLF001
    assert not init_ops._env_contents_have_multiline_values("KEY='closed'\n")  # noqa: SLF001
    assert init_ops._env_context_has_key_specific_comment(
        ["# AWF database URL"], "AWF_DATABASE_URL"
    )  # noqa: SLF001
    assert init_ops._env_context_has_file_header_marker(["# .env defaults live here"])  # noqa: SLF001
    assert init_ops._env_context_looks_like_section_header(["# one", "# two"])  # noqa: SLF001
    assert init_ops._env_context_is_single_adjacent_comment(["# one"])  # noqa: SLF001
    assert init_ops._env_context_has_non_comment_note(["operator note"])  # noqa: SLF001
    assert not init_ops._env_seed_has_meaningful_leading_context(["", ""])  # noqa: SLF001


@pytest.mark.unit
def test_env_context_split_and_merge_preserve_header_and_duplicate_context() -> None:
    assert init_ops._split_env_file_header_context(  # noqa: SLF001
        ["# key comment\n"],
        "AWF_API_TOKEN",
        seed_has_leading_context=True,
    ) == ([], ["# key comment\n"])
    assert init_ops._split_env_file_header_context(  # noqa: SLF001
        ["# file header\n", "\n", "# key comment\n"],
        "AWF_API_TOKEN",
        seed_has_leading_context=False,
    ) == (["# file header\n", "\n"], ["# key comment\n"])
    assert init_ops._split_env_file_header_context(  # noqa: SLF001
        ["# .env defaults\n", "# more\n"],
        "AWF_API_TOKEN",
        seed_has_leading_context=True,
    ) == (["# .env defaults\n", "# more\n"], [])
    assert init_ops._split_env_file_header_context(  # noqa: SLF001
        ["operator note\n", "# key comment\n"],
        "AWF_API_TOKEN",
        seed_has_leading_context=False,
    ) == ([], ["operator note\n", "# key comment\n"])

    merged, overlay_keys = init_ops._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"# seed header\nKEY=seed\n",
        b"# overlay header\n# key docs\nKEY=overlay\n# section\n# more\nKEY=old\nKEY=new\nTAIL=1\n",
    )

    assert overlay_keys == ("TAIL",)
    assert b"KEY=overlay\n" not in merged
    assert b"KEY=new\n" in merged
    assert b"TAIL=1\n" in merged


@pytest.mark.unit
def test_env_seed_merge_rejects_non_utf8_and_multiline_values() -> None:
    with pytest.raises(init_ops._EnvSeedMergeError, match="UTF-8"):  # noqa: SLF001
        init_ops._merge_env_seed_contents_with_overlay_keys(b"\xff", b"KEY=1\n")  # noqa: SLF001
    with pytest.raises(init_ops._EnvSeedMergeError, match="multi-line"):  # noqa: SLF001
        init_ops._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
            b"KEY='unterminated\n",
            b"OTHER=1\n",
        )


@pytest.mark.unit
def test_env_seed_file_error_and_overlay_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    missing_example = tmp_path / ".env.example"
    assert init_ops._seed_env_file(env_file, missing_example) == ("no_example", None, ())  # noqa: SLF001

    env_example = tmp_path / "compose.env.example"
    overlay = tmp_path / "root.env"
    env_example.write_text("KEY=compose", encoding="utf-8")
    overlay.write_text("OTHER=root\n", encoding="utf-8")
    action, error, overlay_keys = init_ops._seed_env_file(  # noqa: SLF001
        env_file, env_example, env_overlay=overlay
    )

    assert action == "wrote_from_example"
    assert error is None
    assert overlay_keys == ("OTHER",)
    assert env_file.read_text(encoding="utf-8") == "KEY=compose\nOTHER=root\n"

    env_file.unlink()
    overlay.write_bytes(b"KEY='unterminated\n")
    action, error, overlay_keys = init_ops._seed_env_file(  # noqa: SLF001
        env_file, env_example, env_overlay=overlay
    )

    assert action == "write_failed"
    assert error is not None
    assert error["operation"] == "merge_overlay"
    assert overlay_keys == ()

    assert "could not read" in init_ops._init_env_warning(  # noqa: SLF001
        {
            "operation": "read_example",
            "path": "example",
            "env_file": "env",
            "env_example": "example",
            "message": "missing",
        }
    )
    payload: dict[str, object] = {}
    init_ops._add_init_env_overlay_keys(payload, ("A", "B"))  # noqa: SLF001
    assert payload["env_overlay_keys"] == ["A", "B"]


@pytest.mark.unit
def test_service_compose_env_path_helpers_reject_bad_compose_or_parent(tmp_path: Path) -> None:
    compose_dir = tmp_path / "docker" / "compose"
    compose_dir.mkdir(parents=True)
    compose_file = compose_dir / "local-service.yml"
    compose_env = compose_dir / ".env"
    sibling_env = tmp_path / "sibling" / ".env"
    sibling_env.parent.mkdir()

    assert (
        init_ops._trusted_service_compose_env_file(  # noqa: SLF001
            tmp_path / "other.yml",
            compose_env,
        )
        is None
    )
    assert (
        init_ops._trusted_service_compose_env_file(  # noqa: SLF001
            compose_file,
            sibling_env,
        )
        is None
    )


@pytest.mark.unit
def test_merge_env_seed_contents_wrapper_returns_bytes() -> None:
    merged = init_ops._merge_env_seed_contents(  # noqa: SLF001
        b"KEY=seed\n",
        b"OTHER=overlay\n",
    )

    assert merged == b"KEY=seed\nOTHER=overlay\n"


@pytest.mark.unit
def test_seed_env_file_cleans_up_partial_file_when_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_example.write_text("KEY=value\n", encoding="utf-8")

    class _FailingHandle:
        def __enter__(self) -> _FailingHandle:
            env_file.write_text("", encoding="utf-8")
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, _data: bytes) -> int:
            raise OSError("disk full")

    original_open = Path.open

    def _open(path: Path, *args: object, **kwargs: object) -> object:
        if path == env_file and args and args[0] == "xb":
            return _FailingHandle()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    action, error, overlay_keys = init_ops._seed_env_file(env_file, env_example)  # noqa: SLF001

    assert action == "write_failed"
    assert error is not None
    assert error["operation"] == "write_env"
    assert overlay_keys == ()
    assert not env_file.exists()


@pytest.mark.unit
def test_init_display_path_and_warning_cover_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        init_ops.os.path,
        "relpath",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("drive mismatch")),
    )

    absolute = tmp_path / "repo"
    assert init_ops._init_display_path(absolute) == str(absolute)  # noqa: SLF001
    assert "could not merge" in init_ops._init_env_warning(  # noqa: SLF001
        {
            "operation": "merge_overlay",
            "path": "root/.env",
            "env_file": "docker/compose/.env",
            "env_example": "docker/compose/.env.example",
            "message": "bad syntax",
        }
    )
