from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_cloudbuild_publishes_only_core_owned_images_from_main_sha() -> None:
    path = ROOT / "cloudbuild.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")

    assert config["options"]["logging"] == "CLOUD_LOGGING_ONLY"
    assert config["timeout"] == "3600s"
    assert len(config["steps"]) == 4
    assert "docker/control-plane.Dockerfile" in text
    assert "docker/agent-runtime.Dockerfile" in text
    assert "${_ARTIFACT_REPOSITORY}/awf-core:rc-$COMMIT_SHA" in text
    assert "${_ARTIFACT_REPOSITORY}/awf-agent-runtime:rc-$COMMIT_SHA" in text
    assert "kubectl" not in text
    assert "gcloud container" not in text
    assert "aira-agent" not in text
    assert "aira-web" not in text
