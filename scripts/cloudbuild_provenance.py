#!/usr/bin/env python3
"""Cloud Build provenance-carrier bindings for AWF Core image digests.

Carrier image:
  `${_ARTIFACT_REPOSITORY}/awf-core-provenance:build-$BUILD_ID`

Image config labels (immutable build-ID → digest contract for awf-cloud):
  awf.build.id              — Cloud Build `$BUILD_ID` (non-empty)
  awf.git.commit            — exact lowercase 40-char hex `$COMMIT_SHA`
  awf.source.repository     — source repo identity (`$REPO_FULL_NAME`)
  awf.core.digest           — `sha256:<64-hex>` from core Buildx `--metadata-file`
  awf.agent.runtime.digest  — `sha256:<64-hex>` multi-arch **index** digest from
                              agent-runtime Buildx `--metadata-file`

Digests are taken from Buildx push metadata (`containerimage.digest`), never
from a later mutable-tag inspect/pull. Only the carrier is listed in Cloud
Build top-level `images:` so `results.images` records its digest without
re-pushing (and collapsing) the pre-pushed multi-arch runtime index.

This module never prints credentials or raw tokens — only validated digests,
SHAs, build IDs, and repository identity strings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LABEL_BUILD_ID = "awf.build.id"
LABEL_GIT_COMMIT = "awf.git.commit"
LABEL_SOURCE_REPOSITORY = "awf.source.repository"
LABEL_CORE_DIGEST = "awf.core.digest"
LABEL_AGENT_RUNTIME_DIGEST = "awf.agent.runtime.digest"

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CREDENTIAL_HINT_RE = re.compile(
    r"(?i)(://[^/\s]+:[^/\s]+@|\bghp_|\bgho_|\bghu_|\bghr_|\bgithub_pat_|\bglpat-|"
    r"\bxox[baprs]-|\bAKIA[0-9A-Z]{16}\b|\bASIA[0-9A-Z]{16}\b|\bBearer\s+[A-Za-z0-9._~+/=-]+)"
)


class ProvenanceError(ValueError):
    """Raised when provenance bindings or Buildx metadata are invalid."""


@dataclass(frozen=True)
class ProvenanceBindings:
    """Validated fields that become carrier image config labels."""

    build_id: str
    commit_sha: str
    source_repository: str
    core_digest: str
    agent_runtime_digest: str

    def labels(self) -> dict[str, str]:
        """Return the five contract labels with exact validated values."""
        return {
            LABEL_BUILD_ID: self.build_id,
            LABEL_GIT_COMMIT: self.commit_sha,
            LABEL_SOURCE_REPOSITORY: self.source_repository,
            LABEL_CORE_DIGEST: self.core_digest,
            LABEL_AGENT_RUNTIME_DIGEST: self.agent_runtime_digest,
        }


def extract_digest_from_metadata(metadata: Mapping[str, Any] | Path | str) -> str:
    """Return `containerimage.digest` from Buildx `--metadata-file` JSON.

    Accepts a parsed mapping, a filesystem path, or a JSON string. Rejects
    missing, non-string, whitespace-padded, or non-`sha256:<64-hex>` digests.
    """
    payload = _load_metadata(metadata)
    raw = payload.get("containerimage.digest")
    if not isinstance(raw, str):
        raise ProvenanceError("Buildx metadata missing containerimage.digest")
    return _validate_digest(raw, field="containerimage.digest")


def bind_provenance(
    *,
    build_id: str,
    commit_sha: str,
    source_repository: str,
    core_digest: str,
    agent_runtime_digest: str,
) -> ProvenanceBindings:
    """Validate and return provenance bindings for the carrier image labels."""
    return ProvenanceBindings(
        build_id=_validate_build_id(build_id),
        commit_sha=_validate_commit_sha(commit_sha),
        source_repository=_validate_source_repository(source_repository),
        core_digest=_validate_digest(core_digest, field="core digest"),
        agent_runtime_digest=_validate_digest(
            agent_runtime_digest,
            field="agent runtime digest",
        ),
    )


def carrier_image_ref(*, artifact_repository: str, build_id: str) -> str:
    """Return `${artifact}/awf-core-provenance:build-${BUILD_ID}`."""
    repo = artifact_repository.strip()
    if not repo:
        raise ProvenanceError("artifact repository must be non-empty")
    if repo.endswith("/"):
        raise ProvenanceError("artifact repository must not end with '/'")
    validated_build_id = _validate_build_id(build_id)
    return f"{repo}/awf-core-provenance:build-{validated_build_id}"


def carrier_docker_build_argv(
    *,
    bindings: ProvenanceBindings,
    carrier_tag: str,
    dockerfile: str = "docker/awf-core-provenance.Dockerfile",
    context: str = "docker/",
) -> list[str]:
    """Return `docker buildx build` argv (without the `docker` binary) for the carrier.

    Uses `--builder default --load` so the single-platform carrier lands in the
    local Docker store for Cloud Build top-level `images:` push (not BuildKit
    cache-only after a docker-container builder was selected with `--use`).
    """
    argv = [
        "buildx",
        "build",
        "--builder",
        "default",
        "--load",
        "--file",
        dockerfile,
        "--tag",
        carrier_tag,
    ]
    for key, value in bindings.labels().items():
        argv.extend(["--label", f"{key}={value}"])
    argv.append(context)
    return argv


def write_bindings_env(path: Path, *, bindings: ProvenanceBindings, carrier_tag: str) -> None:
    """Write shell-sourcable validated bindings (no secrets) for Cloud Build."""
    lines = [
        f"CARRIER_TAG={_shell_single_quote(carrier_tag)}",
        f"AWF_BUILD_ID={_shell_single_quote(bindings.build_id)}",
        f"AWF_GIT_COMMIT={_shell_single_quote(bindings.commit_sha)}",
        f"AWF_SOURCE_REPOSITORY={_shell_single_quote(bindings.source_repository)}",
        f"AWF_CORE_DIGEST={_shell_single_quote(bindings.core_digest)}",
        f"AWF_AGENT_RUNTIME_DIGEST={_shell_single_quote(bindings.agent_runtime_digest)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_carrier_build_script(
    path: Path,
    *,
    bindings: ProvenanceBindings,
    carrier_tag: str,
) -> None:
    """Write a bash script that builds the carrier via ``carrier_docker_build_argv``.

    Cloud Build's docker builder invokes this script so label / ``--load`` argv
    has a single source of truth in Python rather than a duplicated YAML list.
    """
    docker_argv = ["docker", *carrier_docker_build_argv(bindings=bindings, carrier_tag=carrier_tag)]
    quoted = " ".join(_shell_single_quote(part) for part in docker_argv)
    path.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "# Generated by cloudbuild_provenance.py — do not edit.\n"
        f"exec {quoted}\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint used by Cloud Build provenance carrier steps."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate Buildx metadata digests and emit provenance-carrier "
            "bindings. Never logs credentials or tokens."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser(
        "prepare",
        help=(
            "Validate metadata + bindings, write a shell env file, and emit a "
            "docker buildx --load script from carrier_docker_build_argv"
        ),
    )
    prepare.add_argument("--build-id", required=True)
    prepare.add_argument("--commit-sha", required=True)
    prepare.add_argument("--source-repository", required=True)
    prepare.add_argument("--artifact-repository", required=True)
    prepare.add_argument("--core-metadata", type=Path, required=True)
    prepare.add_argument("--runtime-metadata", type=Path, required=True)
    prepare.add_argument(
        "--output-env",
        type=Path,
        required=True,
        help="Path to write CARRIER_TAG + validated label values for bash source",
    )
    prepare.add_argument(
        "--output-build-script",
        type=Path,
        required=True,
        help="Path to write the carrier docker buildx --load bash script",
    )

    args = parser.parse_args(argv)
    if args.command == "prepare":
        return _cmd_prepare(args)
    raise ProvenanceError(f"unknown command: {args.command}")  # pragma: no cover


def _cmd_prepare(args: argparse.Namespace) -> int:
    try:
        core_digest = extract_digest_from_metadata(args.core_metadata)
        runtime_digest = extract_digest_from_metadata(args.runtime_metadata)
        bindings = bind_provenance(
            build_id=args.build_id,
            commit_sha=args.commit_sha,
            source_repository=args.source_repository,
            core_digest=core_digest,
            agent_runtime_digest=runtime_digest,
        )
        tag = carrier_image_ref(
            artifact_repository=args.artifact_repository,
            build_id=bindings.build_id,
        )
        write_bindings_env(args.output_env, bindings=bindings, carrier_tag=tag)
        write_carrier_build_script(
            args.output_build_script,
            bindings=bindings,
            carrier_tag=tag,
        )
    except (ProvenanceError, OSError) as exc:
        print(f"provenance error: {exc}", file=sys.stderr)
        return 1

    # Safe summary only — digests / SHA / build id / repo, never credentials.
    print(
        "provenance bindings ok "
        f"build_id={bindings.build_id} "
        f"commit={bindings.commit_sha} "
        f"source_repository={bindings.source_repository} "
        f"core_digest={bindings.core_digest} "
        f"agent_runtime_digest={bindings.agent_runtime_digest} "
        f"carrier={tag}",
        file=sys.stderr,
    )
    return 0


def _load_metadata(metadata: Mapping[str, Any] | Path | str) -> Mapping[str, Any]:
    if isinstance(metadata, Mapping):
        return metadata
    if isinstance(metadata, Path):
        try:
            raw_text = metadata.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProvenanceError(f"unable to read metadata file: {metadata}") from exc
        return _parse_metadata_json(raw_text, source=str(metadata))
    return _parse_metadata_json(metadata, source="<string>")


def _parse_metadata_json(raw_text: str, *, source: str) -> Mapping[str, Any]:
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"metadata is not valid JSON ({source}): {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProvenanceError(f"metadata must be a JSON object ({source})")
    return loaded


def _validate_digest(value: str, *, field: str) -> str:
    if value != value.strip():
        raise ProvenanceError(f"{field} must not have surrounding whitespace")
    if not _DIGEST_RE.fullmatch(value):
        raise ProvenanceError(f"{field} must match sha256:<64 lowercase hex>")
    return value


def _validate_commit_sha(value: str) -> str:
    if value != value.strip():
        raise ProvenanceError("commit SHA must not have surrounding whitespace")
    if not _COMMIT_SHA_RE.fullmatch(value):
        raise ProvenanceError("commit SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def _validate_build_id(value: str) -> str:
    if value != value.strip():
        raise ProvenanceError("build id must not have surrounding whitespace")
    if not value:
        raise ProvenanceError("build id must be non-empty")
    if not _BUILD_ID_RE.fullmatch(value):
        raise ProvenanceError("build id must be a valid Docker tag component")
    return value


def _validate_source_repository(value: str) -> str:
    if value != value.strip():
        raise ProvenanceError("source repository must not have surrounding whitespace")
    if not value:
        raise ProvenanceError("source repository must be non-empty")
    if _CREDENTIAL_HINT_RE.search(value):
        raise ProvenanceError("source repository must not contain credential-looking material")
    if not _REPO_ID_RE.fullmatch(value):
        raise ProvenanceError("source repository must be a simple path identity (e.g. org/repo)")
    return value


def _shell_single_quote(value: str) -> str:
    """Quote a validated value for `source`-able env files."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
