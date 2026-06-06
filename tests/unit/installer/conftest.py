"""Shared fixtures for black-box tests of ``packaging/install.sh``.

The installer is a standalone bash script. These fixtures drive it through
``subprocess`` as a black box with a hermetic environment: a temp ``HOME``, a
``PATH`` whose first entry is a directory of stub executables (``uname``, ``uv``,
``pipx``, ``awf``), and fixture manifest/wheel files referenced via ``file://``
URLs and local paths. The real ``sha256sum``/``shasum`` and coreutils still
resolve from the system ``PATH`` so checksum verification exercises real hashing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "packaging" / "install.sh"
UNINSTALLER = REPO_ROOT / "packaging" / "uninstall.sh"
REPOSITORY_URL = "https://github.com/dimileeh/aira-agent-workspace-fabric"


@pytest.fixture
def installer_path() -> Path:
    """Absolute path to the checked-in installer under test."""
    return INSTALLER


@pytest.fixture
def uninstaller_path() -> Path:
    """Absolute path to the checked-in uninstaller under test."""
    return UNINSTALLER


class InstallerHarness:
    """Builds stub binaries and runs the installer in a hermetic environment."""

    def __init__(self, root: Path) -> None:
        """Create the temp bin/home layout rooted at ``root``."""
        self.root = root
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.log_file = root / "stub-calls.log"
        self.log_file.write_text("", encoding="utf-8")

    # -- stub builders -------------------------------------------------

    def _stub_contents(self, name: str, behavior: str) -> str:
        """Return the full text of a logging stub for ``name`` running ``behavior``.

        Factored out so the same stub body can be written directly to ``PATH``
        (``_write_stub``) or embedded inside a fake uv-installer script
        (``write_uv_installer``) that drops it into ``$UV_INSTALL_DIR`` at run
        time. The argv log line targets the absolute ``log_file`` path so a stub
        created mid-run by the installer still records into ``calls()``.
        """
        return (
            f'#!/usr/bin/env bash\nprintf \'%s\\n\' "{name} $*" >> "{self.log_file}"\n{behavior}\n'
        )

    def _write_stub(self, name: str, behavior: str, *, directory: Path | None = None) -> Path:
        """Write an executable stub that logs its argv then runs ``behavior``."""
        target_dir = directory if directory is not None else self.bin_dir
        path = target_dir / name
        path.write_text(self._stub_contents(name, behavior), encoding="utf-8")
        path.chmod(0o755)
        return path

    def _uv_behavior(
        self,
        *,
        install_rc: int = 0,
        uninstall_rc: int = 0,
        self_uninstall_rc: int = 0,
        list_output: str = "",
        tool_bin_dir: str | None = None,
    ) -> str:
        """Return the ``case``-based body of the ``uv`` CLI stub.

        Shared by ``add_uv`` (writes the stub onto ``PATH``) and
        ``write_uv_installer`` (embeds the stub so a bootstrap drops a runnable
        ``uv`` into ``$UV_INSTALL_DIR``), so a bootstrapped uv behaves exactly
        like one the harness placed directly.

        ``self_uninstall_rc`` drives ``uv self uninstall``, which the hosted
        uninstaller (``packaging/uninstall.sh``) invokes once an AWF
        uv-ownership marker proves AWF bootstrapped uv. The stub's argv log line
        (``uv self uninstall``) is recorded by the shared logging preamble, so
        tests assert on ``calls()`` rather than a bespoke sentinel.
        """
        bin_dir_expr = (
            json.dumps(tool_bin_dir) if tool_bin_dir is not None else '"$HOME/.local/bin"'
        )
        return (
            'case "$1 $2" in\n'
            '  "tool install")\n'
            f"    if [ {install_rc} -ne 0 ]; then\n"
            "      printf '%s\\n' 'uv: simulated install failure' >&2\n"
            f"      exit {install_rc}\n"
            "    fi\n"
            "    exit 0 ;;\n"
            '  "tool uninstall")\n'
            "    printf '%s\\n' \"uv-tool-uninstall-env UV_TOOL_BIN_DIR=${UV_TOOL_BIN_DIR:-<unset>}\""
            ' >> "$AWF_STUB_LOG"\n'
            f"    exit {uninstall_rc} ;;\n"
            '  "self uninstall")\n'
            f"    exit {self_uninstall_rc} ;;\n"
            '  "tool list")\n'
            f"    printf '%b' {json.dumps(list_output)}\n"
            "    exit 0 ;;\n"
            '  "tool dir")\n'
            '    case "$*" in\n'
            f"      *--bin*) printf '%s\\n' {bin_dir_expr} ;;\n"
            "      *) printf '%s\\n' \"$HOME/.local/share/uv/tools\" ;;\n"
            "    esac\n"
            "    exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac"
        )

    def add_uname(self, system: str, machine: str) -> None:
        """Stub ``uname -s``/``uname -m`` with fixed platform values."""
        behavior = (
            'case "$1" in\n'
            f"  -s) printf '%s\\n' \"{system}\" ;;\n"
            f"  -m) printf '%s\\n' \"{machine}\" ;;\n"
            f"  *) printf '%s\\n' \"{system}\" ;;\n"
            "esac"
        )
        self._write_stub("uname", behavior)

    def add_uv(
        self,
        *,
        install_rc: int = 0,
        uninstall_rc: int = 0,
        self_uninstall_rc: int = 0,
        list_output: str = "",
        tool_bin_dir: str | None = None,
    ) -> None:
        """Stub the ``uv`` CLI subcommands the installer uses.

        ``uv tool dir --bin`` reports the directory uv links executables into,
        which uv derives from ``UV_TOOL_BIN_DIR``/``XDG_BIN_HOME`` and defaults to
        ``~/.local/bin``. ``tool_bin_dir`` overrides that to simulate a uv
        configured to install elsewhere. ``tool uninstall`` additionally records
        a ``uv-tool-uninstall-env UV_TOOL_BIN_DIR=<value|<unset>>`` line so tests
        can assert the installer re-exports the install-time bin dir on uninstall.
        ``self_uninstall_rc`` drives ``uv self uninstall``, which the hosted
        uninstaller runs to remove an AWF-bootstrapped uv.
        """
        self._write_stub(
            "uv",
            self._uv_behavior(
                install_rc=install_rc,
                uninstall_rc=uninstall_rc,
                self_uninstall_rc=self_uninstall_rc,
                list_output=list_output,
                tool_bin_dir=tool_bin_dir,
            ),
        )

    def write_uv_installer(self, *, succeeds: bool = True, path: Path | None = None) -> Path:
        """Write a fake uv-installer script — the ``AWF_UV_INSTALLER`` seam target.

        The official uv installer honors ``UV_INSTALL_DIR``; the bootstrap step
        sets it and prepends it to ``PATH``. This fake mirrors that contract
        hermetically (no network, no real release):

        * ``succeeds=True`` drops a working ``uv`` stub (the same body
          ``add_uv`` uses) into ``$UV_INSTALL_DIR`` so the bootstrap's
          post-install ``command -v uv`` / ``uv tool install`` resolve it.
        * ``succeeds=False`` runs to a clean exit but installs nothing, modeling
          a bootstrap that ran yet yielded no runnable ``uv`` (the
          ``UV_BOOTSTRAP_FAILED`` "still not on PATH" guard).

        A *missing* installer (download failure) is modeled by pointing
        ``AWF_UV_INSTALLER`` at a non-existent file, without this helper.
        """
        installer = path if path is not None else (self.root / "uv-installer.sh")
        # First line of either body logs a ``uv-installer`` sentinel into the
        # absolute ``calls()`` file the moment the script runs. This makes seam
        # *invocation* observable independently of its filesystem side-effect, so
        # the dry-run test can prove the bootstrap was planned-not-run via call
        # absence rather than relying solely on ``~/.local/bin/uv`` not existing.
        # The sentinel also records UV_NO_MODIFY_PATH so a test can assert the
        # bootstrap suppresses the uv installer's shell-profile edits, mirroring how
        # ``uv tool uninstall`` records ``UV_TOOL_BIN_DIR``.
        invoke_sentinel = (
            "printf '%s\\n' \"uv-installer $* "
            'UV_NO_MODIFY_PATH=${UV_NO_MODIFY_PATH:-<unset>}" '
            f'>> "{self.log_file}"\n'
        )
        if succeeds:
            uv_stub = self._stub_contents("uv", self._uv_behavior())
            body = (
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                f"{invoke_sentinel}"
                'target="${UV_INSTALL_DIR:?UV_INSTALL_DIR must be set by the bootstrap}"\n'
                'mkdir -p "$target"\n'
                "cat > \"$target/uv\" <<'AWF_UV_STUB_EOF'\n"
                f"{uv_stub}"
                "AWF_UV_STUB_EOF\n"
                'chmod 755 "$target/uv"\n'
            )
        else:
            body = (
                "#!/usr/bin/env bash\n"
                "# Fake uv installer that runs cleanly but installs no uv binary.\n"
                f"{invoke_sentinel}"
                "exit 0\n"
            )
        installer.write_text(body, encoding="utf-8")
        installer.chmod(0o755)
        return installer

    def add_pipx(
        self,
        *,
        install_rc: int = 0,
        uninstall_rc: int = 0,
        list_output: str = "",
        bin_dir: str | None = None,
    ) -> None:
        """Stub the ``pipx`` CLI subcommands the installer uses.

        ``pipx environment --value PIPX_BIN_DIR`` reports the directory pipx
        links console scripts into, which pipx derives from ``PIPX_BIN_DIR`` and
        defaults to ``~/.local/bin``. ``bin_dir`` overrides that to simulate a
        pipx configured to install elsewhere. ``uninstall`` additionally records
        a ``pipx-uninstall-env PIPX_BIN_DIR=<value|<unset>>`` line so tests can
        assert the installer re-exports the install-time bin dir on uninstall.
        """
        bin_dir_expr = json.dumps(bin_dir) if bin_dir is not None else '"$HOME/.local/bin"'
        behavior = (
            'case "$1" in\n'
            "  install)\n"
            f"    if [ {install_rc} -ne 0 ]; then\n"
            "      printf '%s\\n' 'pipx: simulated install failure' >&2\n"
            f"      exit {install_rc}\n"
            "    fi\n"
            "    exit 0 ;;\n"
            "  uninstall)\n"
            "    printf '%s\\n' \"pipx-uninstall-env PIPX_BIN_DIR=${PIPX_BIN_DIR:-<unset>}\""
            ' >> "$AWF_STUB_LOG"\n'
            f"    exit {uninstall_rc} ;;\n"
            "  list)\n"
            f"    printf '%b' {json.dumps(list_output)}\n"
            "    exit 0 ;;\n"
            "  environment)\n"
            '    case "$*" in\n'
            f"      *PIPX_BIN_DIR*) printf '%s\\n' {bin_dir_expr} ;;\n"
            "      *) printf '%s\\n' \"\" ;;\n"
            "    esac\n"
            "    exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac"
        )
        self._write_stub("pipx", behavior)

    def add_awf(
        self,
        *,
        directory: Path | None = None,
        rc: int = 0,
        version_rc: int | None = None,
        help_rc: int | None = None,
        version: str = "0.1.0",
    ) -> Path:
        """Place an ``awf`` stub on ``PATH`` or in ``directory``.

        The stub models the two commands installer verification cares about:
        ``awf --version`` reports a release identity and ``awf --help`` follows a
        separate exit status so tests can model a matching-version binary that is
        still non-runnable. ``rc`` remains the default for both commands to keep
        existing stale/broken executable tests concise.
        """
        version_exit = rc if version_rc is None else version_rc
        help_exit = rc if help_rc is None else help_rc
        version_output = shlex.quote(f"awf {version}")
        behavior = (
            'case "$1" in\n'
            "  --version)\n"
            f"    printf '%s\\n' {version_output}\n"
            f"    exit {version_exit} ;;\n"
            f"  *) exit {help_exit} ;;\n"
            "esac"
        )
        return self._write_stub("awf", behavior, directory=directory)

    def add_curl(self, *, rc: int = 0) -> None:
        """Stub ``curl`` to record its argv (download flags) then exit ``rc``.

        Placed first on ``PATH``, this shadows the system ``curl`` so a test can
        assert which flags the installer hands the downloader — e.g. the
        redirect-protocol pins that keep an ``https://`` fetch from being bounced
        to plain ``http://`` past the ``INSECURE_URL`` guard.
        """
        self._write_stub("curl", f"exit {rc}")

    def add_head(self, *, rc: int = 0) -> None:
        """Stub ``head`` to echo only its first input line then exit ``rc``.

        ``extract_manifest_channel`` pipes ``sed`` into ``head -n 1``. The real
        ``head`` closes its input after one line, so under ``set -o pipefail`` a
        producer that keeps writing takes SIGPIPE and the whole pipeline exits
        non-zero. Forcing a non-zero ``head`` exit reproduces that race
        deterministically; ``head`` is only used by that one channel pipeline, so
        the stub does not perturb any other install step.
        """
        behavior = f"IFS= read -r _line || true\nprintf '%s\\n' \"$_line\"\nexit {rc}"
        self._write_stub("head", behavior)

    # -- manifest / wheel fixtures ------------------------------------

    def write_wheel(self, *, version: str = "0.1.0") -> tuple[Path, str]:
        """Write a deterministic fake wheel and return its path + true sha256."""
        name = f"agent_workspace_fabric-{version}-py3-none-any.whl"
        wheel = self.root / name
        wheel.write_bytes(b"PK\x03\x04 fake awf wheel " + version.encode() + b"\n")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        return wheel, digest

    def write_manifest(
        self,
        *,
        wheel: Path,
        sha256: str,
        version: str = "0.1.0",
        channel: str = "stable",
        include_sdist: bool = True,
        wheel_url: str | None = None,
        wheel_name: str | None = None,
        tag: str | None = None,
        package: str | None = "agent-workspace-fabric",
        wheel_signatures: list[dict[str, object]] | None = None,
        include_version: bool = True,
        include_tag: bool = True,
    ) -> Path:
        """Write a T11-shaped manifest pointing at the fixture wheel.

        ``wheel_name`` overrides the artifact's ``name`` field (which the
        installer uses as the download-destination basename) so tests can model
        a malformed/compromised manifest; it defaults to the wheel's basename.
        ``tag`` overrides the ``source.tag`` field (defaults to ``v{version}``)
        so tests can model a manifest whose declared release tag disagrees with
        its version or with the requested ``--version`` pin. ``package`` overrides
        the top-level ``package`` field (defaults to ``agent-workspace-fabric``);
        pass a different name to model a manifest misattached to another package,
        or ``None`` to omit the field entirely (a legacy/hand-authored manifest).
        ``wheel_signatures`` fills the wheel artifact's ``signatures`` array
        (defaults to empty) so tests can model a release-signed manifest whose
        signature objects carry their own ``kind``/``name``/``url`` keys; with
        sorted keys these sort before the artifact's own ``url``.
        ``include_version``/``include_tag`` drop the top-level ``version`` and the
        ``source.tag`` fields respectively (both default present) so tests can model
        a legacy/hand-authored manifest that omits the version evidence
        ``verify_version`` and ``verify_artifact_name`` cross-check against a pin.
        """
        artifacts = [
            {
                "kind": "wheel",
                "name": wheel_name if wheel_name is not None else wheel.name,
                "platform": {"arch": "any", "os": "any", "python": ">=3.12"},
                "sha256": sha256,
                "signatures": wheel_signatures if wheel_signatures is not None else [],
                "url": wheel_url if wheel_url is not None else f"file://{wheel}",
            }
        ]
        if include_sdist:
            sdist_name = f"agent_workspace_fabric-{version}.tar.gz"
            artifacts.append(
                {
                    "kind": "sdist",
                    "name": sdist_name,
                    "platform": {"arch": "source", "os": "source", "python": ">=3.12"},
                    "sha256": "0" * 64,
                    "signatures": [],
                    "url": f"file://{wheel.parent / sdist_name}",
                }
            )
        source: dict[str, object] = {
            "commit": None,
            "repository": REPOSITORY_URL,
        }
        if include_tag:
            source["tag"] = tag if tag is not None else f"v{version}"
        manifest: dict[str, object] = {
            "artifacts": artifacts,
            "channel": channel,
            "generated_at": "2026-05-29T00:00:00Z",
            "schema_version": 1,
            "source": source,
        }
        if include_version:
            manifest["version"] = version
        if package is not None:
            manifest["package"] = package
        path = self.root / "awf-install-manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    # -- uninstaller seed fixtures ------------------------------------

    def awf_home(self) -> Path:
        """The hermetic ``AWF_HOME`` the uninstaller's state lanes target.

        Mirrors the production default (``~/.awf``) but rooted under the temp
        ``HOME`` so a test can seed config/state/secret paths and assert which
        the uninstaller removes — and, crucially, which it preserves. Passed to
        the uninstaller via the ``AWF_HOME`` env seam.
        """
        return self.home / ".awf"

    def uv_marker_path(self) -> Path:
        """The hermetic ``AWF_UV_MARKER`` path the uv-removal lane gates on."""
        return self.awf_home() / "uv-bootstrap.marker"

    def write_uv_marker(self, *, uv_bin_dir: str | None = None) -> Path:
        """Seed the AWF uv-ownership marker that authorizes ``--remove-uv``.

        ``install.sh``'s ``bootstrap_uv`` writes this deterministic marker
        (``installed_by=awf`` plus ``uv_bin_dir=<dir>``) only after AWF actually
        bootstraps uv, so its presence is the uninstaller's proof that the uv on
        the box is AWF-owned and safe to remove.
        """
        marker = self.uv_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        resolved_bin = uv_bin_dir if uv_bin_dir is not None else str(self.home / ".local" / "bin")
        marker.write_text(f"installed_by=awf\nuv_bin_dir={resolved_bin}\n", encoding="utf-8")
        return marker

    def write_awf_config(self, *, content: str = "install_channel: stable\n") -> Path:
        """Seed the non-secret host setup config ``${AWF_HOME}/config.yml``."""
        config = self.awf_home() / "config.yml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(content, encoding="utf-8")
        return config

    def write_awf_state(self) -> Path:
        """Seed the host runtime/state dir ``${AWF_HOME}/service`` with a file."""
        state = self.awf_home() / "service"
        state.mkdir(parents=True, exist_ok=True)
        (state / "runtime.json").write_text("{}\n", encoding="utf-8")
        return state

    def write_awf_secret(self) -> Path:
        """Seed a plain-file secret store under ``${AWF_HOME}`` to prove it survives.

        The uninstaller preserves credentials by default and its state lanes
        target an explicit allowlist (only ``config.yml`` and ``service/``), so a
        secrets path beside them must outlive every purge lane. Tests assert this
        file still exists after ``--purge-config``/``--purge-state``/``--all``.
        """
        secret = self.awf_home() / "secrets.yml"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("openai: sk-REDACTED-not-a-real-token\n", encoding="utf-8")
        secret.chmod(0o600)
        return secret

    # -- runner --------------------------------------------------------

    def shadowed_system_path(self) -> str:
        """Build a ``PATH`` where ``awf``/``uv``/``pipx`` read as genuinely missing.

        A test that omits ``add_uv``/``add_pipx`` (or the ``awf`` stub) must
        exercise the tool-absent path, while keeping every *other* system tool
        reachable. We cannot drop the whole directory that ships a managed tool:
        under Homebrew ``uv``/``pipx`` co-locate with coreutils in
        ``/usr/local/bin`` (Intel) or ``/opt/homebrew/bin`` (Apple Silicon), so
        excluding the directory would silently strip ``sha256sum``/``shasum``/
        ``awk`` that the installer's checksum step relies on. Instead we keep
        directories that ship no managed tool as-is and, for those that do,
        symlink their non-managed executables into a shadow dir — so the managed
        tool alone disappears. Tests that need a managed tool present add their
        own stub, which sits first on ``PATH`` and shadows it.
        """
        managed_tools = ("awf", "uv", "pipx")
        shadow_dir = self.root / "syspath-shadow"
        kept: list[str] = []
        shadow_insert_at: int | None = None
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            directory = Path(entry)
            if not any((directory / tool).exists() for tool in managed_tools):
                kept.append(entry)
                continue
            # Remember the original priority slot of the *first* managed-tool
            # directory. Everything iterated before it is non-managed and thus
            # already appended, so ``len(kept)`` here is exactly that slot in
            # ``kept``. Reinserting the shadow dir there (rather than at the end)
            # keeps co-located non-managed tools — e.g. GNU ``sha256sum`` sharing
            # ``/opt/homebrew/bin`` with ``uv`` — at their original PATH rank, so a
            # lower-priority entry can't silently shadow them with another variant.
            if shadow_insert_at is None:
                shadow_insert_at = len(kept)
            shadow_dir.mkdir(parents=True, exist_ok=True)
            for child in directory.iterdir():
                link = shadow_dir / child.name
                # ``follow_symlinks=False`` so an already-shadowed name is
                # detected even when it points at a *broken* symlink. The naive
                # ``link.exists()`` follows the link, reports a dangling target as
                # absent, and re-attempts ``symlink_to`` on the next managed-tool
                # directory that ships a same-named entry — raising
                # ``FileExistsError``. First-on-PATH still wins.
                if child.name not in managed_tools and not link.exists(follow_symlinks=False):
                    link.symlink_to(child)
        if shadow_insert_at is not None:
            kept.insert(shadow_insert_at, str(shadow_dir))
        return os.pathsep.join(kept)

    def run(
        self,
        args: list[str],
        *,
        manifest: Path | None = None,
        extra_env: dict[str, str] | None = None,
        stdin: str | None = None,
        script: Path = INSTALLER,
    ) -> subprocess.CompletedProcess[str]:
        """Run a packaging script with a hermetic env and captured output.

        ``script`` selects which checked-in script runs (the installer by
        default, or ``UNINSTALLER`` for the hosted uninstaller). The same
        hermetic ``HOME``/``PATH``/stub-log environment drives both, so the
        uninstaller's managed-removal, state-cleanup, and uv-removal lanes are
        exercised against the same stub ``uv``/``pipx``/``awf`` the installer
        tests use.

        ``stdin`` feeds the process standard input (used to confirm/decline the
        interactive uv-bootstrap prompt under the
        ``AWF_INSTALL_FORCE_INTERACTIVE`` seam, or the uninstaller's destructive
        confirmation under ``AWF_UNINSTALL_FORCE_INTERACTIVE``). When it is
        ``None`` the child's stdin is ``/dev/null`` so ``[ -t 0 ]`` is
        deterministically non-TTY and the non-interactive decision path is
        exercised regardless of how pytest was launched.
        """
        system_path = self.shadowed_system_path()
        env: dict[str, str] = {
            "PATH": f"{self.bin_dir}{os.pathsep}{system_path}",
            "HOME": str(self.home),
            "AWF_STUB_LOG": str(self.log_file),
        }
        if manifest is not None:
            env["AWF_INSTALL_MANIFEST"] = str(manifest)
        if extra_env is not None:
            env.update(extra_env)
        run_kwargs: dict[str, object] = {
            "env": env,
            "cwd": str(self.root),
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if stdin is None:
            run_kwargs["stdin"] = subprocess.DEVNULL
        else:
            run_kwargs["input"] = stdin
        return subprocess.run(["bash", str(script), *args], **run_kwargs)  # type: ignore[call-overload]

    def calls(self) -> list[str]:
        """Return the recorded stub invocations (one ``name args`` per line)."""
        return [line for line in self.log_file.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def harness(tmp_path: Path) -> InstallerHarness:
    """Provide a fresh installer harness rooted under a temp dir."""
    return InstallerHarness(tmp_path)
