from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_cloudbuild_publishes_only_core_owned_images_from_main_sha() -> None:
    path = ROOT / "cloudbuild.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    core_tag = "${_ARTIFACT_REPOSITORY}/awf-core:rc-$COMMIT_SHA"

    assert config["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert config["timeout"] == "3600s"
    assert "docker/control-plane.Dockerfile" in text
    assert "docker/agent-runtime.Dockerfile" in text
    assert core_tag in text
    assert "${_ARTIFACT_REPOSITORY}/awf-agent-runtime:rc-$COMMIT_SHA" in text
    # build-core only tags locally; publish requires an explicit docker push step.
    push_core_steps = [
        step for step in config["steps"] if isinstance(step, dict) and step.get("id") == "push-core"
    ]
    assert len(push_core_steps) == 1
    assert push_core_steps[0].get("args") == ["push", core_tag]
    assert "kubectl" not in text
    assert "gcloud container" not in text
    assert "aira-agent" not in text
    assert "aira-web" not in text


def test_cloudbuild_publishes_agent_runtime_multi_arch() -> None:
    """Agent-runtime must ship linux/amd64+linux/arm64 (PRD §18.5 / Dockerfile contract)."""
    path = ROOT / "cloudbuild.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")

    runtime_steps = [
        step
        for step in config["steps"]
        if "docker/agent-runtime.Dockerfile" in step.get("args", [])
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
    assert "${_ARTIFACT_REPOSITORY}/awf-agent-runtime:rc-$COMMIT_SHA" in text
    # Multi-arch images are published via buildx --push, not a separate docker push.
    assert "push-agent-runtime" not in {
        step.get("id") for step in config["steps"] if isinstance(step, dict)
    }
