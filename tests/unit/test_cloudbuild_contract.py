"""Static contract tests for AWF Core Cloud Build producer config.

Provenance-carrier schema (config labels on
`${_ARTIFACT_REPOSITORY}/awf-core-provenance:build-$BUILD_ID`):

- awf.build.id              — Cloud Build `$BUILD_ID`
- awf.git.commit            — exact lowercase 40-char hex `$COMMIT_SHA`
- awf.source.repository     — `$REPO_FULL_NAME` (fail-closed if empty)
- awf.core.digest           — from core Buildx `--metadata-file`
- awf.agent.runtime.digest  — multi-arch index from runtime `--metadata-file`

Only the carrier is listed in top-level `images:` so Cloud Build records its
immutable digest in `results.images` without re-pushing the pre-pushed
multi-arch runtime manifest list. Core and runtime stay `rc-$COMMIT_SHA`
outputs published via Buildx `--push` with direct metadata digest capture.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]

_BINFMT_DIGEST_REF = re.compile(r"^tonistiigi/binfmt(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_HELPER_IMAGE_DIGEST_REF = re.compile(r".+@sha256:[0-9a-f]{64}$")

_CARRIER_TAG = "${_ARTIFACT_REPOSITORY}/awf-core-provenance:build-$BUILD_ID"
_CORE_TAG = "${_ARTIFACT_REPOSITORY}/awf-core:rc-$COMMIT_SHA"
_RUNTIME_TAG = "${_ARTIFACT_REPOSITORY}/awf-agent-runtime:rc-$COMMIT_SHA"

_FORBIDDEN_AUTH_PRINT = re.compile(
    r"(?i)(echo\s+(\$[A-Z0-9_]*TOKEN|\$[A-Z0-9_]*PASSWORD|\$[A-Z0-9_]*SECRET)|"
    r"printenv\s+[A-Z0-9_]*TOKEN|docker\s+login\s+.*--password[^\-])"
)


def _load() -> tuple[dict[str, Any], str]:
    path = ROOT / "cloudbuild.yaml"
    text = path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    assert isinstance(config, dict)
    return config, text


def _steps(config: dict[str, Any]) -> list[dict[str, Any]]:
    steps = config.get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _step_by_id(config: dict[str, Any], step_id: str) -> dict[str, Any]:
    matches = [step for step in _steps(config) if step.get("id") == step_id]
    assert len(matches) == 1, f"expected exactly one step id={step_id!r}"
    return matches[0]


def _flatten_args(step: dict[str, Any]) -> str:
    args = step.get("args", [])
    if isinstance(args, list):
        return "\n".join(str(part) for part in args)
    return str(args)


def test_cloudbuild_publishes_core_and_runtime_rc_tags() -> None:
    config, text = _load()

    assert config["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert config["timeout"] == "3600s"
    assert "docker/control-plane.Dockerfile" in text
    assert "docker/agent-runtime.Dockerfile" in text
    assert _CORE_TAG in text
    assert _RUNTIME_TAG in text
    assert "kubectl" not in text
    assert "gcloud container" not in text
    assert "aira-agent" not in text
    assert "aira-web" not in text
    assert "secretmanager" not in text.lower()
    assert "gke" not in text.lower()
    assert "kubectl" not in text


def test_cloudbuild_images_lists_only_provenance_carrier() -> None:
    config, _text = _load()
    images = config.get("images")
    assert images == [_CARRIER_TAG]
    # Pre-pushed product tags must not appear in images (manifest-list safety).
    assert _CORE_TAG not in images
    assert _RUNTIME_TAG not in images


def test_cloudbuild_core_and_runtime_push_via_buildx_metadata_file() -> None:
    config, text = _load()

    core = _step_by_id(config, "build-and-push-core")
    core_args = core.get("args", [])
    assert core_args[0:2] == ["buildx", "build"]
    assert "--push" in core_args
    assert "--metadata-file" in core_args
    assert _CORE_TAG in core_args
    assert "docker/control-plane.Dockerfile" in core_args

    runtime = _step_by_id(config, "build-and-push-agent-runtime")
    runtime_args = runtime.get("args", [])
    assert runtime_args[0:2] == ["buildx", "build"]
    assert "--push" in runtime_args
    assert "--metadata-file" in runtime_args
    assert _RUNTIME_TAG in runtime_args

    # Digests must not be taken from mutable-tag inspect/pull as authority.
    assert "docker inspect" not in text
    assert re.search(r"docker\s+pull\s+.*rc-\$COMMIT_SHA", text) is None
    assert "push-core" not in {step.get("id") for step in _steps(config)}


def test_cloudbuild_publishes_agent_runtime_multi_arch() -> None:
    """Agent-runtime must ship linux/amd64+linux/arm64 (PRD §18.5 / Dockerfile contract)."""
    config, text = _load()

    runtime_steps = [
        step for step in _steps(config) if "docker/agent-runtime.Dockerfile" in step.get("args", [])
    ]
    assert len(runtime_steps) == 1
    args = runtime_steps[0]["args"]
    assert args[0] == "buildx"
    assert "build" in args
    assert "--platform" in args
    platform = args[args.index("--platform") + 1]
    assert "linux/amd64" in platform
    assert "linux/arm64" in platform
    assert "--push" in args
    assert _RUNTIME_TAG in text
    assert "push-agent-runtime" not in {step.get("id") for step in _steps(config)}
    # Carrier-only images list — runtime must not be re-pushed by Cloud Build.
    assert _RUNTIME_TAG not in config.get("images", [])


def test_cloudbuild_pins_privileged_binfmt_by_digest() -> None:
    """Privileged qemu/binfmt helper must not float on a mutable tag."""
    config, _text = _load()

    qemu_steps = [step for step in _steps(config) if step.get("id") == "setup-qemu"]
    assert len(qemu_steps) == 1
    args = qemu_steps[0].get("args", [])
    assert args[:3] == ["run", "--privileged", "--rm"]
    image = args[3]
    assert _BINFMT_DIGEST_REF.match(image), (
        f"setup-qemu must pin tonistiigi/binfmt by digest, got {image!r}"
    )


def test_cloudbuild_provenance_carrier_validates_then_builds() -> None:
    config, text = _load()
    step_ids = [step.get("id") for step in _steps(config)]

    assert "build-and-push-core" in step_ids
    assert "build-and-push-agent-runtime" in step_ids
    assert "prepare-provenance-carrier" in step_ids
    assert "build-provenance-carrier" in step_ids

    core_idx = step_ids.index("build-and-push-core")
    runtime_idx = step_ids.index("build-and-push-agent-runtime")
    prepare_idx = step_ids.index("prepare-provenance-carrier")
    build_idx = step_ids.index("build-provenance-carrier")
    assert prepare_idx > core_idx
    assert prepare_idx > runtime_idx
    assert build_idx > prepare_idx

    prepare = _step_by_id(config, "prepare-provenance-carrier")
    prepare_name = str(prepare.get("name", ""))
    assert _HELPER_IMAGE_DIGEST_REF.match(prepare_name), (
        f"prepare-provenance-carrier helper image must be digest-pinned, got {prepare_name!r}"
    )
    prepare_blob = _flatten_args(prepare)
    assert "scripts/cloudbuild_provenance.py" in prepare_blob
    assert "--metadata-file" in text  # digests come from metadata files
    assert "awf-core.metadata.json" in text
    assert "awf-agent-runtime.metadata.json" in text
    assert "$BUILD_ID" in prepare_blob or "BUILD_ID" in prepare_blob
    assert "$COMMIT_SHA" in prepare_blob or "COMMIT_SHA" in prepare_blob
    assert "REPO_FULL_NAME" in prepare_blob

    build = _step_by_id(config, "build-provenance-carrier")
    build_blob = _flatten_args(build)
    assert "docker/awf-core-provenance.Dockerfile" in build_blob
    assert _CARRIER_TAG in build_blob or "CARRIER_TAG" in build_blob
    assert "awf.build.id" in build_blob or "AWF_BUILD_ID" in build_blob
    # After create-buildx-builder --use (docker-container), plain `docker build`
    # can leave the image only in BuildKit cache. Carrier must buildx --load into
    # the local Docker store so Cloud Build top-level `images:` can push it.
    assert "buildx build" in build_blob
    assert "--builder default" in build_blob
    assert "--load" in build_blob
    # Carrier is tagged locally; Cloud Build `images:` performs the recorded push.
    assert "--push" not in build_blob


def test_cloudbuild_documents_carrier_schema() -> None:
    _config, text = _load()
    for key in (
        "awf.build.id",
        "awf.git.commit",
        "awf.source.repository",
        "awf.core.digest",
        "awf.agent.runtime.digest",
        "awf-core-provenance",
    ):
        assert key in text


def test_cloudbuild_does_not_print_credentials() -> None:
    _config, text = _load()
    assert _FORBIDDEN_AUTH_PRINT.search(text) is None
    assert "printenv" not in text.lower() or "TOKEN" not in text
