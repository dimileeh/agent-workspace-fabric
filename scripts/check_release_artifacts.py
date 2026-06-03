#!/usr/bin/env python3
"""Verify published release artifacts do not drift from the built distributions.

This is the release-workflow drift gate. It re-derives the AWF install manifest
from the built ``dist/*`` files and the ``python-distribution-sha256.txt``
checksum file (reusing ``generate_install_manifest.build_manifest``), pinning
``generated_at`` to the published manifest's own value so the comparison is
deterministic, then asserts the re-derived manifest equals the published one. A
mismatch prints a precise, field-level diff and exits non-zero.

Because the re-derivation runs the generator's own validation, this transitively
re-verifies the checksum bytes against the dist artifacts, the checksum coverage
and filenames, the channel/version/tag, and each artifact ``sha256``/``url``/
``name`` — without duplicating any of that logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow direct execution (``python scripts/check_release_artifacts.py``) to import
# the sibling generator module, which lives in the ``scripts`` package. Running a
# script by path puts the script's own directory on sys.path, not the repo root,
# so ``import scripts`` would otherwise fail; pytest imports this module as
# ``scripts.check_release_artifacts`` and is unaffected by the redundant insert.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_install_manifest import (
    DEFAULT_PACKAGE,
    ManifestError,
    build_manifest,
)


def load_published_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and minimally validate the published manifest JSON."""
    if not manifest_path.is_file():
        raise ManifestError(f"manifest file does not exist: {manifest_path}")
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {manifest_path} ({exc})") from exc
    if not isinstance(loaded, dict):
        raise ManifestError(f"manifest must be a JSON object: {manifest_path}")
    return loaded


def check_release_artifacts(
    *,
    dist_dir: Path,
    checksums_file: Path,
    manifest_path: Path,
    version: str | None = None,
    tag: str | None = None,
    repository_url: str | None = None,
) -> None:
    """Raise ManifestError if the published manifest drifts from the dist artifacts.

    Release metadata (version, tag, repository URL) defaults to the published
    manifest when the corresponding flag is omitted, so the gate can run from the
    manifest alone; an explicit flag that disagrees with the published manifest
    surfaces as drift through the field-level comparison.
    """
    published = load_published_manifest(manifest_path)

    resolved_version = version if version is not None else _required_str(published, "version")
    source = _required_mapping(published, "source")
    resolved_tag = tag if tag is not None else _required_str(source, "tag")
    resolved_repository = (
        repository_url if repository_url is not None else _required_str(source, "repository")
    )
    resolved_channel = _required_str(published, "channel")
    package = published.get("package", DEFAULT_PACKAGE)
    commit = source.get("commit")
    generated_at = _required_str(published, "generated_at")

    expected = build_manifest(
        dist_dir=dist_dir,
        checksums_file=checksums_file,
        version=resolved_version,
        tag=resolved_tag,
        repository_url=resolved_repository,
        channel=resolved_channel,
        generated_at=generated_at,
        package=str(package),
        commit=commit,
    )

    differences = _manifest_differences(published, expected)
    if differences:
        joined = "\n  ".join(differences)
        raise ManifestError(
            "published release manifest drifts from the built distributions:\n  " + joined
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        check_release_artifacts(
            dist_dir=args.dist_dir,
            checksums_file=args.checksums_file,
            manifest_path=args.manifest,
            version=args.version,
            tag=args.tag,
            repository_url=args.repository_url,
        )
    except (OSError, ManifestError, ValueError) as exc:
        parser.error(str(exc))

    print(f"OK: {args.manifest} matches the built distributions")
    return 0


def _parser() -> argparse.ArgumentParser:
    """Create the command-line parser for the drift gate."""
    parser = argparse.ArgumentParser(
        description="Verify the published install manifest matches the built dist/*.",
    )
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--checksums-file", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--tag")
    parser.add_argument("--repository-url")
    return parser


def _required_str(mapping: dict[str, Any], key: str) -> str:
    """Return a required string field from a manifest mapping."""
    if key not in mapping:
        raise ManifestError(f"manifest is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, str):
        raise ManifestError(f"manifest field {key} must be a string")
    return value


def _required_mapping(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a required object field from a manifest mapping."""
    if key not in mapping:
        raise ManifestError(f"manifest is missing required field: {key}")
    value = mapping[key]
    if not isinstance(value, dict):
        raise ManifestError(f"manifest field {key} must be an object")
    return value


def _manifest_differences(published: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Return human-readable field-level differences between two manifests."""
    differences: list[str] = []
    _scalar_differences(
        "", _without_artifacts(published), _without_artifacts(expected), differences
    )
    _duplicate_name_differences(published, differences)
    _artifact_differences(
        _artifacts_by_name(published),
        _artifacts_by_name(expected),
        differences,
    )
    return differences


def _without_artifacts(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the manifest mapping without its ``artifacts`` list."""
    return {key: value for key, value in manifest.items() if key != "artifacts"}


def _scalar_differences(
    prefix: str,
    published: dict[str, Any],
    expected: dict[str, Any],
    differences: list[str],
) -> None:
    """Record dotted-path differences for nested scalar/object fields."""
    for key in sorted(set(published) | set(expected)):
        path = f"{prefix}{key}"
        published_value = published.get(key)
        expected_value = expected.get(key)
        if isinstance(published_value, dict) and isinstance(expected_value, dict):
            _scalar_differences(f"{path}.", published_value, expected_value, differences)
        elif published_value != expected_value:
            differences.append(f"{path}: published={published_value!r} expected={expected_value!r}")


def _duplicate_name_differences(manifest: dict[str, Any], differences: list[str]) -> None:
    """Record drift for any artifact name that appears more than once.

    The re-derived ``expected`` manifest has one entry per built dist file, so a
    repeated name in the published manifest is always drift. Indexing by name
    (``_artifacts_by_name``) would otherwise silently collapse duplicates to the
    last entry, letting an extra earlier wheel object with a stale/broken URL
    escape the comparison entirely.
    """
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        return
    counts: dict[str, int] = {}
    for index, artifact in enumerate(artifacts):
        if isinstance(artifact, dict):
            name = str(artifact.get("name", f"<artifact {index}>"))
            counts[name] = counts.get(name, 0) + 1
    for name in sorted(counts):
        if counts[name] > 1:
            differences.append(
                f"artifact {name}: appears {counts[name]} times in manifest (duplicate entries)"
            )


def _artifacts_by_name(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a manifest's artifacts by filename."""
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for index, artifact in enumerate(artifacts):
        if isinstance(artifact, dict):
            name = artifact.get("name", f"<artifact {index}>")
            indexed[str(name)] = artifact
    return indexed


def _artifact_differences(
    published: dict[str, dict[str, Any]],
    expected: dict[str, dict[str, Any]],
    differences: list[str],
) -> None:
    """Record per-artifact differences keyed by filename."""
    for name in sorted(set(published) | set(expected)):
        published_artifact = published.get(name)
        expected_artifact = expected.get(name)
        if published_artifact is None:
            differences.append(f"artifact {name}: present in dist but missing from manifest")
            continue
        if expected_artifact is None:
            differences.append(f"artifact {name}: in manifest but absent from dist")
            continue
        _scalar_differences(
            f"artifact {name}: ",
            published_artifact,
            expected_artifact,
            differences,
        )


if __name__ == "__main__":
    sys.exit(main())
