"""Static contract tests for AWF Core Cloud Build producer config.

Provenance-carrier schema (config labels on
`${_ARTIFACT_REPOSITORY}/awf-core-provenance:build-$BUILD_ID`):

- awf.build.id              — Cloud Build `$BUILD_ID`
- awf.git.commit            — exact lowercase 40-char hex `$COMMIT_SHA`
- awf.source.repository     — `$REPO_FULL_NAME` (fail-closed if empty)
- awf.core.digest           — from core Buildx `--metadata-file`
- awf.agent.runtime.digest  — multi-arch index from runtime `--metadata-file`
- awf.core.console.digest   — from console Buildx `--metadata-file`

Only the carrier is listed in top-level `images:` so Cloud Build records its
immutable digest in `results.images` without re-pushing the pre-pushed
multi-arch runtime manifest list. Core, runtime, and hosted console stay
`rc-$COMMIT_SHA` outputs published via Buildx `--push` with direct metadata
digest capture.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_BINFMT_DIGEST_REF = re.compile(r"^tonistiigi/binfmt(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_HELPER_IMAGE_DIGEST_REF = re.compile(r".+@sha256:[0-9a-f]{64}$")

_CARRIER_TAG = "${_ARTIFACT_REPOSITORY}/awf-core-provenance:build-$BUILD_ID"
_CORE_TAG = "${_ARTIFACT_REPOSITORY}/awf-core:rc-$COMMIT_SHA"
_RUNTIME_TAG = "${_ARTIFACT_REPOSITORY}/awf-agent-runtime:rc-$COMMIT_SHA"
_CONSOLE_TAG = "${_ARTIFACT_REPOSITORY}/awf-core-console:rc-$COMMIT_SHA"

_HOSTED_BASE_PATH = "NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=/workspaces"
_HOSTED_API_BASE = "NEXT_PUBLIC_AWF_CONSOLE_API_BASE=/api/core-console"
_HOSTED_OPERATOR_BASE = "NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=/api/core-console"
_HOSTED_CONTEXT_QUERY_KEYS = "NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS=org_id,project_id"

_FORBIDDEN_AUTH_PRINT = re.compile(
    r"(?i)(echo\s+(\$[A-Z0-9_]*TOKEN|\$[A-Z0-9_]*PASSWORD|\$[A-Z0-9_]*SECRET)|"
    r"printenv\s+[A-Z0-9_]*TOKEN|docker\s+login\s+.*--password[^\-])"
)
_FORBIDDEN_SECRET_BUILD_ARG = re.compile(
    r"(?i)(AWF_API_TOKEN|TOKEN=|PASSWORD=|SECRET=|CREDENTIAL|API_KEY)"
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


def test_cloudbuild_publishes_core_runtime_and_console_rc_tags() -> None:
    config, text = _load()

    assert config["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert config["timeout"] == "3600s"
    assert "docker/control-plane.Dockerfile" in text
    assert "docker/agent-runtime.Dockerfile" in text
    assert "apps/console/Dockerfile" in text
    assert _CORE_TAG in text
    assert _RUNTIME_TAG in text
    assert _CONSOLE_TAG in text
    assert "kubectl" not in text
    assert "gcloud container" not in text
    assert "aira-agent" not in text
    assert "aira-web" not in text
    assert "secretmanager" not in text.lower()
    assert "gke" not in text.lower()


def test_cloudbuild_images_lists_only_provenance_carrier() -> None:
    config, _text = _load()
    images = config.get("images")
    assert images == [_CARRIER_TAG]
    # Pre-pushed product tags must not appear in images (manifest-list safety).
    assert _CORE_TAG not in images
    assert _RUNTIME_TAG not in images
    assert _CONSOLE_TAG not in images


def test_cloudbuild_core_runtime_and_console_push_via_buildx_metadata_file() -> None:
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

    console = _step_by_id(config, "build-and-push-core-console")
    console_args = console.get("args", [])
    assert console_args[0:2] == ["buildx", "build"]
    assert "--push" in console_args
    assert "--metadata-file" in console_args
    assert "/workspace/awf-core-console.metadata.json" in console_args
    assert _CONSOLE_TAG in console_args
    assert "apps/console/Dockerfile" in console_args
    # Single-platform like core — no multi-arch platform flag for console.
    assert "--platform" not in console_args

    # Digests must not be taken from mutable-tag inspect/pull as authority.
    assert "docker inspect" not in text
    assert re.search(r"docker\s+pull\s+.*rc-\$COMMIT_SHA", text) is None
    assert "push-core" not in {step.get("id") for step in _steps(config)}


def test_cloudbuild_console_uses_hosted_public_path_build_args() -> None:
    config, text = _load()
    console = _step_by_id(config, "build-and-push-core-console")
    console_args = [str(part) for part in console.get("args", [])]

    assert console_args.count("--build-arg") == 4
    assert _HOSTED_BASE_PATH in console_args
    assert _HOSTED_API_BASE in console_args
    assert _HOSTED_OPERATOR_BASE in console_args
    assert _HOSTED_CONTEXT_QUERY_KEYS in console_args
    # Public path settings only — never tokens/credentials/tenant data.
    assert _FORBIDDEN_SECRET_BUILD_ARG.search("\n".join(console_args)) is None
    assert "AWF_API_TOKEN" not in text
    assert "TOKEN=" not in text


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
    assert "build-and-push-core-console" in step_ids
    assert "prepare-provenance-carrier" in step_ids
    assert "build-provenance-carrier" in step_ids

    core_idx = step_ids.index("build-and-push-core")
    runtime_idx = step_ids.index("build-and-push-agent-runtime")
    console_idx = step_ids.index("build-and-push-core-console")
    prepare_idx = step_ids.index("prepare-provenance-carrier")
    build_idx = step_ids.index("build-provenance-carrier")
    assert prepare_idx > core_idx
    assert prepare_idx > runtime_idx
    assert prepare_idx > console_idx
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
    assert "awf-core-console.metadata.json" in text
    assert "--console-metadata" in prepare_blob
    assert "$BUILD_ID" in prepare_blob or "BUILD_ID" in prepare_blob
    assert "$COMMIT_SHA" in prepare_blob or "COMMIT_SHA" in prepare_blob
    assert "REPO_FULL_NAME" in prepare_blob
    assert "--output-build-script" in prepare_blob
    assert "awf-provenance-build.sh" in prepare_blob

    build = _step_by_id(config, "build-provenance-carrier")
    build_blob = _flatten_args(build)
    # Carrier docker argv (labels, --builder default, --load) comes from the
    # Python helper via the generated script — do not duplicate it in YAML.
    assert "awf-provenance-build.sh" in build_blob
    assert "docker/awf-core-provenance.Dockerfile" not in build_blob
    assert "--label" not in build_blob
    assert "awf.build.id=" not in build_blob
    # Script is executed by bash; Cloud Build top-level images: performs the push.
    assert "--push" not in build_blob


def test_cloudbuild_documents_carrier_schema() -> None:
    _config, text = _load()
    for key in (
        "awf.build.id",
        "awf.git.commit",
        "awf.source.repository",
        "awf.core.digest",
        "awf.agent.runtime.digest",
        "awf.core.console.digest",
        "awf-core-provenance",
    ):
        assert key in text


def test_cloudbuild_does_not_print_credentials() -> None:
    _config, text = _load()
    assert _FORBIDDEN_AUTH_PRINT.search(text) is None
    assert "printenv" not in text.lower()
