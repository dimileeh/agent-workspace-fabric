#!/usr/bin/env python3
"""Generate the AWF installer release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_PACKAGE = "agent-workspace-fabric"
DEFAULT_PYTHON_REQUIREMENT = ">=3.12"
SCHEMA_VERSION = 1
CHANNELS = {"auto", "stable", "prerelease"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CHECKSUM_READ_SIZE = 64 * 1024
FINAL_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.post\d+)?$")
PRERELEASE_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.dev\d+)(?:\.post\d+)?$",
)


class ManifestError(ValueError):
    """Raised when release manifest inputs are invalid."""


def resolve_channel(version: str, requested_channel: str) -> str:
    """Resolve the release channel for a package version."""
    if requested_channel not in CHANNELS:
        raise ValueError(
            f"channel must be one of {', '.join(sorted(CHANNELS))}; got {requested_channel!r}",
        )
    if version.startswith("v"):
        raise ValueError("version must be a package version without a leading 'v'")

    if _is_prerelease_version(version):
        resolved = "prerelease"
    elif _is_final_version(version):
        resolved = "stable"
    else:
        raise ValueError(
            "version must be a final version like 0.1.0 or a prerelease like 0.2.0rc1",
        )

    if requested_channel == "auto":
        return resolved
    if requested_channel == "stable" and resolved != "stable":
        raise ValueError("stable channel requires a final version")
    if requested_channel == "prerelease" and resolved != "prerelease":
        raise ValueError("prerelease channel requires a prerelease version")
    return requested_channel


def build_manifest(
    *,
    dist_dir: Path,
    checksums_file: Path,
    version: str,
    tag: str,
    repository_url: str,
    channel: str,
    generated_at: str | None,
    package: str = DEFAULT_PACKAGE,
    commit: str | None = None,
) -> dict[str, Any]:
    """Build an AWF install manifest from release artifacts."""
    resolved_channel = resolve_channel(version, channel)
    generated_timestamp = (
        _validate_generated_at(generated_at)
        if generated_at is not None
        else _default_generated_at()
    )
    normalized_repository_url = _normalize_repository_url(repository_url)
    _validate_tag(tag, version)

    distribution_files = _distribution_files(dist_dir)
    _validate_distribution_metadata(distribution_files, package=package, version=version)
    checksums = _parse_checksums(checksums_file)
    _validate_checksum_coverage(distribution_files, checksums)
    _validate_checksum_content(distribution_files, checksums)

    return {
        "artifacts": [
            _artifact_entry(
                file=file,
                checksum=checksums[file.name],
                repository_url=normalized_repository_url,
                tag=tag,
            )
            for file in distribution_files
        ],
        "channel": resolved_channel,
        "generated_at": generated_timestamp,
        "package": package,
        "schema_version": SCHEMA_VERSION,
        "source": {
            "commit": commit,
            "repository": normalized_repository_url,
            "tag": tag,
        },
        "version": version,
    }


def write_manifest(manifest: dict[str, Any], output: Path) -> None:
    """Write manifest JSON with stable formatting."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _parser()
    args = parser.parse_args(argv)
    skip_reason = _github_actions_skip_reason(args.tag)
    if skip_reason is not None:
        try:
            _remove_skipped_output(args.output)
        except OSError as exc:
            parser.error(f"could not remove skipped manifest output: {exc}")
        print(f"SKIP: {skip_reason}")
        return 0

    try:
        manifest = build_manifest(
            dist_dir=args.dist_dir,
            checksums_file=args.checksums_file,
            version=args.version,
            tag=args.tag,
            repository_url=args.repository_url,
            channel=args.channel,
            generated_at=args.generated_at,
            package=args.package,
            commit=_resolve_commit(args.commit),
        )
    except (OSError, ManifestError, ValueError) as exc:
        parser.error(str(exc))

    write_manifest(manifest, args.output)
    print(f"OK: wrote {args.output}")
    return 0


def _parser() -> argparse.ArgumentParser:
    """Create the command-line parser for manifest generation."""
    parser = argparse.ArgumentParser(description="Generate awf-install-manifest.json.")
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--checksums-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="auto")
    parser.add_argument("--generated-at")
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--commit")
    return parser


def _github_actions_skip_reason(tag: str) -> str | None:
    """Return a skip reason when Actions is not running for the release tag."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None

    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    ref_type = os.environ.get("GITHUB_REF_TYPE", "")
    if not ref_name:
        return "GitHub Actions ref <unknown> is not a release tag"
    if ref_type:
        if ref_type == "tag" and ref_name == tag:
            return None
        return f"GitHub Actions ref {ref_name or '<unknown>'} ({ref_type}) is not a release tag"

    if ref_name and ref_name != tag:
        return f"GitHub Actions ref {ref_name} is not a release tag"
    return None


def _remove_skipped_output(output: Path) -> None:
    """Remove stale manifest output when generation is intentionally skipped."""
    try:
        output.unlink()
    except FileNotFoundError:
        return


def _resolve_commit(explicit_commit: str | None) -> str | None:
    """Resolve source commit provenance for CLI-generated manifests."""
    if explicit_commit is not None:
        return explicit_commit
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    return os.environ.get("GITHUB_SHA") or None


def _is_final_version(version: str) -> bool:
    """Return whether the package version is a supported final release."""
    return FINAL_VERSION_RE.match(version) is not None


def _is_prerelease_version(version: str) -> bool:
    """Return whether the package version is a supported prerelease."""
    return PRERELEASE_VERSION_RE.match(version) is not None


def _default_generated_at() -> str:
    """Return a UTC timestamp, honoring SOURCE_DATE_EPOCH when present."""
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch:
        try:
            seconds = int(source_date_epoch)
        except ValueError as exc:
            raise ManifestError("SOURCE_DATE_EPOCH must be an integer Unix timestamp") from exc
        try:
            return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OSError, OverflowError) as exc:
            raise ManifestError(f"Invalid SOURCE_DATE_EPOCH timestamp: {exc}") from exc
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_generated_at(value: str) -> str:
    """Validate and normalize an explicit UTC RFC3339 timestamp."""
    if not GENERATED_AT_RE.match(value):
        raise ManifestError("generated_at must be UTC RFC3339 format: YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ManifestError(
            "generated_at must be UTC RFC3339 format: YYYY-MM-DDTHH:MM:SSZ"
        ) from exc
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_repository_url(repository_url: str) -> str:
    """Normalize and validate the GitHub repository URL for artifact links."""
    normalized = repository_url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ManifestError("repository URL must be a pinned GitHub repository HTTPS URL")
    if parsed.params or parsed.query or parsed.fragment:
        raise ManifestError("repository URL must not include params, query, or fragment")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ManifestError("repository URL must identify a GitHub owner and repository")
    owner, repo = parts
    return f"https://github.com/{owner}/{repo}"


def _validate_tag(tag: str, version: str) -> None:
    """Require the release tag to pin the package version."""
    if tag != f"v{version}":
        raise ManifestError(f"tag must pin the package version as v{version}")


def _distribution_files(dist_dir: Path) -> list[Path]:
    """Return sorted distribution artifacts after validating their types."""
    if not dist_dir.is_dir():
        raise ManifestError(f"dist directory does not exist: {dist_dir}")

    files = sorted(
        (path for path in dist_dir.iterdir() if path.is_file()), key=lambda path: path.name
    )
    if not files:
        raise ManifestError(f"dist directory has no artifacts: {dist_dir}")

    for file in files:
        _artifact_kind(file)
    return files


def _validate_distribution_metadata(files: list[Path], *, package: str, version: str) -> None:
    """Ensure distribution filenames match the requested package and version."""
    expected_package = _normalize_distribution_package(package)
    expected_version = _normalize_distribution_version(version)
    for file in files:
        artifact_package, artifact_version = _distribution_package_version(file)
        if (
            _normalize_distribution_package(artifact_package) != expected_package
            or _normalize_distribution_version(artifact_version) != expected_version
        ):
            raise ManifestError(
                "distribution artifact does not match requested package/version: "
                f"{file.name} (found {artifact_package} {artifact_version}, "
                f"expected {package} {version})"
            )


def _distribution_package_version(file: Path) -> tuple[str, str]:
    """Extract package and version metadata from a distribution artifact name."""
    name = file.name
    if name.endswith(".whl"):
        return _wheel_package_version(name)
    if name.endswith(".tar.gz"):
        return _sdist_package_version(name)
    raise ManifestError(f"unexpected distribution artifact: {name}")


def _wheel_package_version(name: str) -> tuple[str, str]:
    """Extract package and version metadata from a wheel filename."""
    parts = name[:-4].split("-")
    if len(parts) not in {5, 6}:
        raise ManifestError(f"malformed wheel distribution artifact: {name}")
    return parts[0], parts[1]


def _sdist_package_version(name: str) -> tuple[str, str]:
    """Extract package and version metadata from an sdist filename."""
    try:
        package, version = name[:-7].rsplit("-", maxsplit=1)
    except ValueError as exc:
        raise ManifestError(f"malformed sdist distribution artifact: {name}") from exc
    if not package or not version:
        raise ManifestError(f"malformed sdist distribution artifact: {name}")
    return package, version


def _normalize_distribution_package(package: str) -> str:
    """Normalize a package name using distribution filename rules."""
    return re.sub(r"[-_.]+", "-", package).lower()


def _normalize_distribution_version(version: str) -> str:
    """Normalize a version string for distribution filename comparison."""
    return version.lower()


def _parse_checksums(checksums_file: Path) -> dict[str, str]:
    """Parse SHA-256 checksums by artifact filename."""
    if not checksums_file.is_file():
        raise ManifestError(f"checksums file does not exist: {checksums_file}")

    checksums: dict[str, str] = {}
    for line_number, line in enumerate(checksums_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ManifestError(f"malformed checksum line {line_number}: expected sha256 and path")
        digest, raw_path = parts
        if not SHA256_RE.match(digest):
            raise ManifestError(
                f"malformed checksum line {line_number}: sha256 must be 64 hex chars"
            )
        filename = Path(raw_path.strip().lstrip("*")).name
        if not filename:
            raise ManifestError(f"malformed checksum line {line_number}: missing artifact path")
        if filename in checksums:
            raise ManifestError(f"duplicate checksum for {filename}")
        checksums[filename] = digest.lower()
    return checksums


def _validate_checksum_coverage(files: list[Path], checksums: dict[str, str]) -> None:
    """Ensure checksums exactly cover the discovered distribution artifacts."""
    artifact_names = {file.name for file in files}
    checksum_names = set(checksums)

    missing = sorted(artifact_names - checksum_names)
    if missing:
        raise ManifestError("missing checksum for distribution artifact(s): " + ", ".join(missing))

    stale = sorted(checksum_names - artifact_names)
    if stale:
        raise ManifestError(
            "checksum references absent distribution artifact(s): " + ", ".join(stale)
        )


def _validate_checksum_content(files: list[Path], checksums: dict[str, str]) -> None:
    """Verify artifact bytes match the supplied SHA-256 checksums."""
    for file in files:
        sha256 = hashlib.sha256()
        with file.open("rb") as artifact:
            while chunk := artifact.read(CHECKSUM_READ_SIZE):
                sha256.update(chunk)
        actual = sha256.hexdigest()
        expected = checksums[file.name]
        if actual != expected:
            raise ManifestError(f"checksum does not match distribution artifact: {file.name}")


def _artifact_entry(
    *,
    file: Path,
    checksum: str,
    repository_url: str,
    tag: str,
) -> dict[str, Any]:
    """Build one manifest artifact entry for a distribution file."""
    kind = _artifact_kind(file)
    return {
        "kind": kind,
        "name": file.name,
        "platform": _platform_metadata(kind),
        "sha256": checksum,
        "signatures": [],
        "url": f"{repository_url}/releases/download/{tag}/{file.name}",
    }


def _artifact_kind(file: Path) -> str:
    """Classify a distribution artifact as a wheel or sdist."""
    name = file.name
    if name.endswith(".whl"):
        return "wheel"
    if name.endswith(".tar.gz"):
        return "sdist"
    raise ManifestError(f"unexpected distribution artifact: {name}")


def _platform_metadata(kind: str) -> dict[str, str]:
    """Return install platform metadata for an artifact kind."""
    if kind == "wheel":
        return {
            "arch": "any",
            "os": "any",
            "python": DEFAULT_PYTHON_REQUIREMENT,
        }
    return {
        "arch": "source",
        "os": "source",
        "python": DEFAULT_PYTHON_REQUIREMENT,
    }


if __name__ == "__main__":
    sys.exit(main())
