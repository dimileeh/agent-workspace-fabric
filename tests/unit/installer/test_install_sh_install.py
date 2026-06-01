"""Install-method, fallback, reachability, and dependency edge cases."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_install_method_failure_preserves_reason_and_tool_stderr(
    harness: InstallerHarness,
) -> None:
    """A failing ``uv tool install`` surfaces both the token and tool stderr."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(install_rc=7)
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "INSTALL_METHOD_FAILED" in result.stderr
    # The underlying tool failure must not be swallowed.
    assert "simulated install failure" in result.stderr


@pytest.mark.unit
def test_pipx_method_uses_pipx_not_uv(harness: InstallerHarness) -> None:
    """``--method pipx`` installs through pipx and never invokes uv install."""
    harness.add_uname("Darwin", "arm64")
    harness.add_uv()
    harness.add_pipx()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--method", "pipx"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert "pipx install" in joined
    assert "uv tool install" not in joined


@pytest.mark.unit
def test_reachability_failure_does_not_claim_success(harness: InstallerHarness) -> None:
    """A successful install command with no runnable awf reports the failure."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds" but produces no awf binary
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
    # The exact shell fix must be printed so the user can recover.
    assert "export PATH=" in result.stderr
    # The advice must not falsely claim awf *is* installed in the bin dir: it was
    # never found there, so "awf is installed in <dir>, which is not on your
    # PATH" would directly contradict the "no awf binary was found" failure the
    # user sees on the next line. The not-found branch must use an honest,
    # context-aware opening instead.
    assert "is installed in" not in result.stderr


@pytest.mark.unit
def test_default_install_rejects_stale_path_awf_when_bin_dir_empty(
    harness: InstallerHarness,
) -> None:
    """A stale PATH awf cannot mask a default install that produced no binary."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds" but produces no awf in uv's bin dir
    harness.add_awf(version="9.9.9")  # unrelated stale binary already on PATH
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
    assert "export PATH=" in result.stderr
    assert "Installed agent-workspace-fabric" not in result.stdout


@pytest.mark.unit
def test_default_install_rejects_glob_expansion_in_path_awf_version(
    harness: InstallerHarness,
) -> None:
    """Glob characters in ``awf --version`` cannot fake a version match."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds" but produces no awf in uv's bin dir
    harness.add_awf(version="*")  # stale PATH binary with glob-like version output
    (harness.root / "0.1.0").write_text("cwd glob match\n", encoding="utf-8")
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
    assert "Installed agent-workspace-fabric" not in result.stdout


@pytest.mark.unit
def test_default_install_accepts_matching_path_awf_when_bin_dir_empty(
    harness: InstallerHarness,
) -> None:
    """The default fallback still accepts the newly installed awf on PATH."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # bin-dir prediction misses where the tool linked awf
    harness.add_awf(version="0.1.0")  # matching newly installed binary on PATH
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr
    advice = result.stdout + result.stderr
    assert "export PATH=" not in advice
    assert "fish_add_path" not in advice


@pytest.mark.unit
def test_default_install_bin_off_path_is_reachable_with_advice(
    harness: InstallerHarness,
) -> None:
    """A default install into ~/.local/bin off PATH succeeds with PATH advice.

    With no ``--install-dir``, uv/pipx install into ``~/.local/bin``. When that
    directory is not on PATH the install is still valid: reachability must fall
    back to the default bin dir and succeed with PATH advice, not fail with
    ``AWF_NOT_REACHABLE``.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds"
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin)  # awf lands in the default bin, off PATH
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr
    # The user still gets actionable PATH advice for the off-PATH default bin.
    assert "export PATH=" in result.stderr
    assert str(default_bin) in result.stderr


@pytest.mark.unit
def test_found_binary_not_runnable_reports_broken_not_path(
    harness: InstallerHarness,
) -> None:
    """A found-but-non-runnable awf reports a broken install, not a PATH gap.

    When the installed binary exists in the bin dir but ``--help`` fails, the
    failure is a broken/incomplete install — not a missing PATH entry. The
    advice opening must say so rather than the misleading default
    "awf is installed in <dir>, which is not on your PATH", which would send the
    user to edit PATH for a binary that is present but unusable.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds"
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin, rc=1)  # found in bin dir but non-runnable
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
    # Assert the overridden opening specifically: "was found at" and "the install
    # may be incomplete" are printed only by this branch's context-aware opening,
    # never by the trailing fail() message ("awf installed at ... is not
    # runnable") — so they prove the opening changed, not just the failure text.
    assert "was found at" in result.stderr
    assert "the install may be incomplete" in result.stderr
    # The misleading default opening must not appear for a found-but-broken binary.
    assert "is installed in" not in result.stderr
    # The PATH export line is still emitted as a best-effort recovery hint.
    assert "export PATH=" in result.stderr


@pytest.mark.unit
def test_default_install_verifies_installed_binary_not_path_shadow(
    harness: InstallerHarness,
) -> None:
    """A default install must not let an older PATH awf shadow verification.

    With no ``--install-dir``, uv/pipx install into ``~/.local/bin``. When an
    unrelated awf sits earlier on PATH, verification must inspect the freshly
    installed binary in the default bin dir and still surface PATH advice for it,
    not silently pass on the shadowing binary and report success without advice.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()  # older, unrelated awf earlier on PATH (stub bin dir)
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin)  # freshly installed binary, off PATH
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    assert "export PATH=" in advice
    assert str(default_bin) in advice


@pytest.mark.unit
def test_default_install_binary_verified_even_when_path_awf_is_broken(
    harness: InstallerHarness,
) -> None:
    """The default-bin binary is verified, not a broken awf earlier on PATH.

    A non-runnable awf on PATH must not make verification fail when the freshly
    installed ``~/.local/bin/awf`` is itself runnable.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf(rc=1)  # broken awf earlier on PATH
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin)  # runnable freshly installed binary
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr


@pytest.mark.unit
def test_uv_install_uses_uv_tool_bin_dir_for_reachability(harness: InstallerHarness) -> None:
    """A uv configured to link executables elsewhere is honored, not mis-reported.

    When uv installs into a configured bin dir (UV_TOOL_BIN_DIR / XDG_BIN_HOME /
    XDG_DATA_HOME) instead of ``~/.local/bin``, reachability must consult
    ``uv tool dir --bin`` rather than a hard-coded ``~/.local/bin``; otherwise a
    successful uv install is falsely reported ``AWF_NOT_REACHABLE`` and PATH
    advice points at the wrong directory.
    """
    harness.add_uname("Linux", "x86_64")
    uv_bin = harness.root / "uv-tool-bin"
    uv_bin.mkdir()
    harness.add_uv(tool_bin_dir=str(uv_bin))
    # awf landed in uv's configured bin dir, which is not on PATH nor ~/.local/bin.
    harness.add_awf(directory=uv_bin)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--shell", "bash"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr
    # PATH advice (the dir is off-PATH) points at uv's real bin dir, not ~/.local/bin.
    advice = result.stdout + result.stderr
    assert str(uv_bin) in advice
    assert "/.local/bin" not in advice


@pytest.mark.unit
def test_off_path_install_resolves_bin_dir_once(harness: InstallerHarness) -> None:
    """A successful off-PATH install resolves the bin dir with a single subprocess.

    ``verify_awf`` resolves the bin dir via ``uv tool dir --bin`` and threads the
    cached value into ``print_path_advice`` instead of letting it re-resolve. A
    successful install that lands off PATH (so PATH advice is printed) must spawn
    ``uv tool dir --bin`` exactly once, not twice.
    """
    harness.add_uname("Linux", "x86_64")
    uv_bin = harness.root / "uv-tool-bin"
    uv_bin.mkdir()
    harness.add_uv(tool_bin_dir=str(uv_bin))
    # awf lands in uv's configured bin dir, off PATH, so advice is emitted.
    harness.add_awf(directory=uv_bin)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--shell", "bash"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    # Advice still points at uv's real bin dir...
    assert str(uv_bin) in result.stdout + result.stderr
    # ...but the bin dir is resolved with a single `uv tool dir --bin` subprocess.
    tool_dir_calls = [call for call in harness.calls() if call.startswith("uv tool dir")]
    assert len(tool_dir_calls) == 1, harness.calls()


@pytest.mark.unit
def test_pipx_install_uses_pipx_bin_dir_for_reachability(harness: InstallerHarness) -> None:
    """A pipx configured to link scripts elsewhere is honored, not mis-reported.

    pipx installs console scripts into ``PIPX_BIN_DIR`` (defaulting to
    ``~/.local/bin``). When that is configured to a non-default directory,
    reachability must consult ``pipx environment --value PIPX_BIN_DIR`` rather
    than a hard-coded ``~/.local/bin``; otherwise a successful pipx install is
    falsely reported ``AWF_NOT_REACHABLE`` and PATH advice points at the wrong
    directory (the uv path resolves its bin dir, so pipx must too).
    """
    harness.add_uname("Linux", "x86_64")
    pipx_bin = harness.root / "pipx-bin"
    pipx_bin.mkdir()
    harness.add_pipx(bin_dir=str(pipx_bin))
    # awf landed in pipx's configured bin dir, which is not on PATH nor ~/.local/bin.
    harness.add_awf(directory=pipx_bin)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--method", "pipx", "--shell", "bash"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr
    # PATH advice (the dir is off-PATH) points at pipx's real bin dir, not ~/.local/bin.
    advice = result.stdout + result.stderr
    assert str(pipx_bin) in advice
    assert "/.local/bin" not in advice


@pytest.mark.unit
def test_install_dir_verifies_installed_binary_not_path_shadow(
    harness: InstallerHarness,
) -> None:
    """With --install-dir, an older awf on PATH must not shadow verification.

    The freshly installed binary lives at ``${INSTALL_DIR}/awf`` (off PATH) while
    an unrelated awf sits earlier on PATH. Verification must inspect the install
    location and still surface PATH advice for it, not silently pass on the
    shadowing binary and report success without advice.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()  # older, unrelated awf earlier on PATH (stub bin dir)
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    harness.add_awf(directory=install_dir)  # freshly installed binary, off PATH
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    assert "export PATH=" in advice
    assert str(install_dir) in advice


@pytest.mark.unit
def test_install_dir_binary_verified_even_when_path_awf_is_broken(
    harness: InstallerHarness,
) -> None:
    """The install-dir binary is verified, not a broken awf earlier on PATH.

    A non-runnable awf on PATH must not make verification fail when the freshly
    installed ``${INSTALL_DIR}/awf`` is itself runnable.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf(rc=1)  # broken awf earlier on PATH
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    harness.add_awf(directory=install_dir)  # runnable freshly installed binary
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr


@pytest.mark.unit
def test_install_dir_missing_binary_fails_even_with_stale_path_awf(
    harness: InstallerHarness,
) -> None:
    """A stale PATH awf cannot mask a missing install-dir binary.

    When ``--install-dir`` produced no awf, verification must fail with
    ``AWF_NOT_REACHABLE`` rather than falsely passing on an unrelated awf that
    happens to be earlier on PATH.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds" but produces no awf in the install dir
    harness.add_awf()  # unrelated awf earlier on PATH
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
    assert "export PATH=" in result.stderr


@pytest.mark.unit
def test_invalid_method_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An unsupported ``--method`` value is rejected before any work."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_pipx()
    result = harness.run(["--method", "conda"])

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    # Rejected during arg parsing: no platform probe or install tool is invoked.
    assert harness.calls() == []


@pytest.mark.unit
def test_invalid_channel_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An unsupported ``--channel`` value is rejected before any work."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_pipx()
    result = harness.run(["--channel", "nightly"])

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    # Rejected during arg parsing: no platform probe or install tool is invoked.
    assert harness.calls() == []


@pytest.mark.unit
def test_empty_version_equals_form_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """``--version=`` must not silently degrade a pinned install to latest.

    ``--version`` is the trust-boundary pin that selects a specific manifest/tag.
    An empty value (e.g. ``--version=`` or an empty wrapper variable) would leave
    ``VERSION`` empty and resolve ``releases/latest`` — a mutable install the user
    did not ask for. Reject it with ``BAD_USAGE`` before resolving the manifest.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--version="], manifest=manifest)

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    assert "--version" in result.stderr
    # Rejected during arg parsing: no platform probe, manifest fetch, or install.
    assert harness.calls() == []


@pytest.mark.unit
def test_empty_version_space_form_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """``--version ""`` (empty wrapper variable) is rejected the same way."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--version", ""], manifest=manifest)

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    assert "--version" in result.stderr
    assert harness.calls() == []


@pytest.mark.unit
def test_empty_install_dir_equals_form_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """``--install-dir=`` must not silently fall back to the default bin directory.

    ``--install-dir`` scopes where uv/pipx link the executable, how ``awf``
    reachability is verified, and which bin dir uninstall re-exports. Every
    downstream use gates on ``[ -n "$INSTALL_DIR" ]``, so an empty value (e.g.
    ``--install-dir=`` or an empty wrapper variable) is indistinguishable from
    omitting the flag and silently mutates uv/pipx's default bin dir instead of the
    caller's intended isolated location. Reject it with ``BAD_USAGE`` at parse time.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir="], manifest=manifest)

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    assert "--install-dir" in result.stderr
    # Rejected during arg parsing: no platform probe, manifest fetch, or install.
    assert harness.calls() == []


@pytest.mark.unit
def test_empty_install_dir_space_form_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """``--install-dir ""`` (empty wrapper variable) is rejected the same way."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", ""], manifest=manifest)

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    assert "--install-dir" in result.stderr
    assert harness.calls() == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_version",
    ["../../../evil", "1.2.3/../../etc", "/etc/passwd", "..", "v1 2"],
)
def test_version_with_path_traversal_is_a_bad_usage_error(
    harness: InstallerHarness, bad_version: str
) -> None:
    """A ``--version`` carrying a slash/traversal is rejected before any fetch.

    ``VERSION`` is interpolated directly into the manifest URL path segment
    (``resolve_manifest`` builds ``.../releases/download/v${VERSION}/...``). A
    value with a slash or ``..`` would escape that segment; against a ``file://``
    base (the ``AWF_INSTALL_BASE_URL`` test seam) it could redirect the manifest
    fetch to an arbitrary local path before the sha256 gate runs. The installer
    constrains ``--version`` to a plain version token, rejecting such values with
    ``BAD_USAGE`` during arg parsing — before the platform probe or any fetch.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--version", bad_version], manifest=manifest)

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    assert "--version" in result.stderr
    # Rejected during arg parsing: no platform probe, manifest fetch, or install.
    assert harness.calls() == []


@pytest.mark.unit
def test_traversal_version_rejected_before_file_base_url_fetch(
    harness: InstallerHarness,
) -> None:
    """A traversal ``--version`` is refused before any ``file://`` manifest fetch.

    This exercises the exact seam the reviewer named: with ``AWF_INSTALL_BASE_URL``
    set to a ``file://`` base and no ``AWF_INSTALL_MANIFEST`` override,
    ``resolve_manifest`` builds ``<base>/releases/download/v${VERSION}/...`` and
    ``fetch`` ``cp``s from that local path — so a traversal ``${VERSION}`` could
    otherwise redirect the read outside the base. The parse-time guard rejects it
    with ``BAD_USAGE`` before the platform probe or any fetch, so the ``file://``
    read never happens: no manifest is resolved and no subprocess is invoked.
    """
    base = harness.root / "seam"
    base.mkdir(parents=True, exist_ok=True)
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()

    result = harness.run(
        ["--version", "../../../evil"],
        extra_env={"AWF_INSTALL_BASE_URL": f"file://{base}"},
    )

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
    assert "--version" in result.stderr
    # Rejected at parse time: the file:// manifest fetch never runs.
    assert harness.calls() == []
    assert "Resolved manifest" not in result.stdout


@pytest.mark.unit
def test_prerelease_style_version_is_accepted(harness: InstallerHarness) -> None:
    """A PEP 440 pre-release token (letters + digits + dots) is a valid pin.

    The path-traversal guard must constrain ``--version`` without rejecting
    legitimate release tags such as ``1.2.3rc1``.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf(version="1.2.3rc1")
    wheel, digest = harness.write_wheel(version="1.2.3rc1")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="1.2.3rc1")

    result = harness.run(["--version", "1.2.3rc1"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_manifest_without_wheel_artifact_is_invalid(harness: InstallerHarness) -> None:
    """A manifest missing a wheel artifact fails with ``MANIFEST_INVALID``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    manifest = harness.root / "awf-install-manifest.json"
    manifest.write_text(
        '{\n  "artifacts": [],\n  "schema_version": 1,\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "MANIFEST_INVALID" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_signed_wheel_manifest_with_kind_bearing_signatures_installs(
    harness: InstallerHarness,
) -> None:
    """A release-signed wheel still parses when signatures carry a ``kind`` key.

    The T11 manifest sorts keys, so the wheel artifact's ``url`` follows its
    ``signatures`` array. Once release signing fills that array with objects
    that include their own ``kind``/``name``/``url`` fields, a parser that ends
    the wheel block on any ``kind`` line — or captures the first ``name``/``url``
    it sees — would stop before (or clobber) the artifact's real ``url`` and fail
    a valid signed manifest with ``MANIFEST_INVALID``. Anchoring field matches to
    the wheel object's own indentation keeps the nested signature object inert.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_signatures=[
            {
                "kind": "sigstore",
                "name": "decoy-should-not-win",
                "url": "https://example.invalid/decoy-signature",
            }
        ],
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "MANIFEST_INVALID" not in result.stderr
    # The wheel was resolved from the manifest and handed to the install method,
    # proving the nested signature "kind" did not abort wheel parsing.
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_manifest_wheel_name_with_path_traversal_is_rejected(harness: InstallerHarness) -> None:
    """A wheel name that escapes ``WORK_DIR`` aborts before any download."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    # A compromised manifest names the wheel with a traversal sequence so that
    # download_artifact's ${WORK_DIR}/${ARTIFACT_NAME} resolves outside the temp
    # dir; the url still points at the real fixture so an unguarded fetch would
    # actually write (and clobber) the escape target.
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, wheel_name="../awf-pwned.txt")
    tmpdir = harness.root / "tmp"
    tmpdir.mkdir()
    # WORK_DIR is ${TMPDIR}/awf-install.XXXXXX, so ../awf-pwned.txt lands here.
    escape_target = tmpdir / "awf-pwned.txt"

    result = harness.run([], manifest=manifest, extra_env={"TMPDIR": str(tmpdir)})

    assert result.returncode != 0
    assert "MANIFEST_INVALID" in result.stderr
    # The traversal name must be rejected before fetch writes the artifact.
    assert not escape_target.exists()
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_manifest_wheel_name_traversal_rejected_even_in_dry_run(
    harness: InstallerHarness,
) -> None:
    """Dry-run also rejects a traversal wheel name before writing the artifact."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, wheel_name="../../awf-pwned.txt")
    tmpdir = harness.root / "tmp"
    tmpdir.mkdir()
    # ${WORK_DIR}/../../awf-pwned.txt resolves up two levels to harness.root.
    escape_target = harness.root / "awf-pwned.txt"

    result = harness.run(["--dry-run"], manifest=manifest, extra_env={"TMPDIR": str(tmpdir)})

    assert result.returncode != 0
    assert "MANIFEST_INVALID" in result.stderr
    assert not escape_target.exists()


@pytest.mark.unit
def test_missing_manifest_source_is_unavailable(harness: InstallerHarness) -> None:
    """A manifest path that does not exist fails with ``MANIFEST_UNAVAILABLE``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()

    result = harness.run([], manifest=harness.root / "missing-manifest.json")

    assert result.returncode != 0
    assert "MANIFEST_UNAVAILABLE" in result.stderr
