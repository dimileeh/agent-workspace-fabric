#!/usr/bin/env python3
"""Release installer smoke: verify checksum-before-install on real artifacts.

This generates a *smoke* install manifest from the published
``awf-install-manifest.json`` by rewriting every artifact ``url`` to a local
``file://`` path under the built ``dist/`` directory, while preserving each
artifact's recorded ``sha256``/``name``/``kind`` and the manifest's top-level
identity fields. Pointed at that smoke manifest, ``packaging/install.sh``
fetches the real built wheel bytes locally and verifies the manifest-pinned
``sha256`` *before* any install mutation — proving checksum-before-install on
the actual release artifacts without depending on a live GitHub Release.

``--run`` executes the generated ``install.sh --dry-run`` invocation(s) and
asserts the installer printed "Checksum verified" and made no install mutation.
``--channel`` is derived from the manifest so an unpinned prerelease smoke does
not trip the installer's ``CHANNEL_MISMATCH`` guard.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALLER = REPO_ROOT / "packaging" / "install.sh"
CHECKSUM_VERIFIED_MARKER = "Checksum verified"


class SmokeError(RuntimeError):
    """Raised when the installer smoke fails to verify an artifact."""


def build_smoke_manifest(manifest: dict[str, Any], dist_dir: Path) -> dict[str, Any]:
    """Return a copy of ``manifest`` with artifact URLs pointing at local dist files.

    Every artifact ``url`` is rewritten to ``file://`` the resolved
    ``dist_dir/<name>`` path; ``name``/``kind``/``sha256`` and the manifest's
    top-level fields are preserved so the recorded checksum is still verified
    against the real built wheel bytes. The input manifest is not mutated.
    """
    smoke = copy.deepcopy(manifest)
    artifacts = smoke.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise SmokeError("manifest 'artifacts' must be a list")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or "name" not in artifact:
            raise SmokeError("manifest artifact is missing a 'name'")
        local_path = (dist_dir / str(artifact["name"])).resolve()
        artifact["url"] = f"file://{local_path}"
    return smoke


def smoke_invocation(
    installer: Path,
    manifest_path: Path,
    *,
    channel: str,
    method: str = "uv",
) -> tuple[list[str], dict[str, str]]:
    """Return the argv + env for a checksum-before-install dry-run via install.sh.

    The ``--channel`` is the manifest's channel, threaded in from the in-memory
    smoke manifest, so a prerelease smoke does not trip the installer's channel
    cross-check. ``manifest_path`` is the on-disk manifest the installer reads.
    """
    argv = [
        "bash",
        str(installer),
        "--dry-run",
        "--method",
        method,
        "--channel",
        channel,
    ]
    env = {"AWF_INSTALL_MANIFEST": str(manifest_path)}
    return argv, env


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _parser()
    args = parser.parse_args(argv)
    methods: list[str] = args.method or ["uv"]

    try:
        manifest = _load_manifest(args.manifest)
        smoke = build_smoke_manifest(manifest, args.dist_dir)
        channel = _manifest_channel(smoke)
        _write_smoke_manifest(smoke, args.smoke_manifest_out)
        for method in methods:
            invocation, env = smoke_invocation(
                args.installer, args.smoke_manifest_out, channel=channel, method=method
            )
            _print_invocation(invocation, env)
            if args.run:
                _run_invocation(invocation, env)
    except (OSError, SmokeError, ValueError) as exc:
        parser.error(str(exc))

    return 0


def _parser() -> argparse.ArgumentParser:
    """Create the command-line parser for the release smoke."""
    parser = argparse.ArgumentParser(
        description="Generate and optionally run the installer checksum smoke.",
    )
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--smoke-manifest-out", type=Path, required=True)
    parser.add_argument(
        "--method",
        action="append",
        choices=["uv", "pipx"],
        help="installer method to smoke (repeatable; default: uv).",
    )
    parser.add_argument("--installer", type=Path, default=DEFAULT_INSTALLER)
    parser.add_argument(
        "--run",
        action="store_true",
        help="execute each generated install.sh dry-run and assert checksum verification.",
    )
    return parser


def _manifest_channel(manifest: dict[str, Any]) -> str:
    """Return the manifest's top-level channel, defaulting to stable."""
    channel = manifest.get("channel", "stable")
    if not isinstance(channel, str) or not channel:
        raise SmokeError(f"manifest channel must be a non-empty string: {channel!r}")
    return channel


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load a manifest JSON object from disk."""
    if not manifest_path.is_file():
        raise SmokeError(f"manifest file does not exist: {manifest_path}")
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SmokeError(f"manifest must be a JSON object: {manifest_path}")
    return loaded


def _write_smoke_manifest(manifest: dict[str, Any], output: Path) -> None:
    """Write the smoke manifest JSON with stable formatting."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_invocation(invocation: list[str], env: dict[str, str]) -> None:
    """Print a human-readable, copy-pasteable smoke command."""
    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    print(f"$ {env_prefix} {shlex.join(invocation)}".strip())


def _run_invocation(invocation: list[str], env: dict[str, str]) -> None:
    """Execute one install.sh dry-run and assert checksum-before-install."""
    completed = subprocess.run(
        invocation,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        raise SmokeError(
            f"installer smoke failed (exit {completed.returncode}): {' '.join(invocation)}"
        )
    if CHECKSUM_VERIFIED_MARKER not in completed.stdout:
        raise SmokeError(
            f"installer did not report checksum verification before install: {' '.join(invocation)}"
        )


if __name__ == "__main__":
    sys.exit(main())
