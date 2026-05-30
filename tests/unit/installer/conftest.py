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
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "packaging" / "install.sh"
REPOSITORY_URL = "https://github.com/dimileeh/aira-agent-workspace-fabric"


@pytest.fixture
def installer_path() -> Path:
    """Absolute path to the checked-in installer under test."""
    return INSTALLER


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

    def _write_stub(self, name: str, behavior: str, *, directory: Path | None = None) -> Path:
        """Write an executable stub that logs its argv then runs ``behavior``."""
        target_dir = directory if directory is not None else self.bin_dir
        path = target_dir / name
        body = (
            f'#!/usr/bin/env bash\nprintf \'%s\\n\' "{name} $*" >> "{self.log_file}"\n{behavior}\n'
        )
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

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
        list_output: str = "",
    ) -> None:
        """Stub the ``uv`` CLI subcommands the installer uses."""
        behavior = (
            'case "$1 $2" in\n'
            '  "tool install")\n'
            f"    if [ {install_rc} -ne 0 ]; then\n"
            "      printf '%s\\n' 'uv: simulated install failure' >&2\n"
            f"      exit {install_rc}\n"
            "    fi\n"
            "    exit 0 ;;\n"
            '  "tool uninstall")\n'
            f"    exit {uninstall_rc} ;;\n"
            '  "tool list")\n'
            f"    printf '%s' {json.dumps(list_output)}\n"
            "    exit 0 ;;\n"
            '  "tool dir")\n'
            "    printf '%s\\n' \"$HOME/.local/share/uv/tools\"\n"
            "    exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac"
        )
        self._write_stub("uv", behavior)

    def add_pipx(
        self,
        *,
        install_rc: int = 0,
        uninstall_rc: int = 0,
        list_output: str = "",
    ) -> None:
        """Stub the ``pipx`` CLI subcommands the installer uses."""
        behavior = (
            'case "$1" in\n'
            "  install)\n"
            f"    if [ {install_rc} -ne 0 ]; then\n"
            "      printf '%s\\n' 'pipx: simulated install failure' >&2\n"
            f"      exit {install_rc}\n"
            "    fi\n"
            "    exit 0 ;;\n"
            "  uninstall)\n"
            f"    exit {uninstall_rc} ;;\n"
            "  list)\n"
            f"    printf '%s' {json.dumps(list_output)}\n"
            "    exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac"
        )
        self._write_stub("pipx", behavior)

    def add_awf(self, *, directory: Path | None = None) -> Path:
        """Place a runnable ``awf`` stub on ``PATH`` or in ``directory``."""
        return self._write_stub("awf", "exit 0", directory=directory)

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
    ) -> Path:
        """Write a T11-shaped manifest pointing at the fixture wheel."""
        artifacts = [
            {
                "kind": "wheel",
                "name": wheel.name,
                "platform": {"arch": "any", "os": "any", "python": ">=3.12"},
                "sha256": sha256,
                "signatures": [],
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
        manifest = {
            "artifacts": artifacts,
            "channel": channel,
            "generated_at": "2026-05-29T00:00:00Z",
            "package": "agent-workspace-fabric",
            "schema_version": 1,
            "source": {"commit": None, "repository": REPOSITORY_URL, "tag": f"v{version}"},
            "version": version,
        }
        path = self.root / "awf-install-manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    # -- runner --------------------------------------------------------

    def run(
        self,
        args: list[str],
        *,
        manifest: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the installer with a hermetic env and captured output."""
        # Keep system coreutils on PATH but drop any directory that already ships
        # a real ``awf`` (e.g. the dev virtualenv bin) so reachability/uninstall
        # tests see only the stub binaries this harness places.
        system_path = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and not (Path(entry) / "awf").exists()
        )
        env: dict[str, str] = {
            "PATH": f"{self.bin_dir}{os.pathsep}{system_path}",
            "HOME": str(self.home),
            "AWF_STUB_LOG": str(self.log_file),
        }
        if manifest is not None:
            env["AWF_INSTALL_MANIFEST"] = str(manifest)
        if extra_env is not None:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(INSTALLER), *args],
            env=env,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[str]:
        """Return the recorded stub invocations (one ``name args`` per line)."""
        return [line for line in self.log_file.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def harness(tmp_path: Path) -> InstallerHarness:
    """Provide a fresh installer harness rooted under a temp dir."""
    return InstallerHarness(tmp_path)
