"""Unit tests for Cloud Build provenance-carrier helper.

Carrier schema (image config labels on
`${_ARTIFACT_REPOSITORY}/awf-core-provenance:build-$BUILD_ID`):

- awf.build.id              — Cloud Build BUILD_ID (non-empty)
- awf.git.commit            — exact lowercase 40-char hex COMMIT_SHA
- awf.source.repository     — source repository identity (REPO_FULL_NAME)
- awf.core.digest           — sha256:<64-hex> from core Buildx metadata-file
- awf.agent.runtime.digest  — sha256:<64-hex> multi-arch index from runtime
                              Buildx metadata-file
- awf.core.console.digest   — sha256:<64-hex> from console Buildx metadata-file

Digests must come from Buildx `--metadata-file` of the push operation, never
from a later mutable-tag inspect/pull. Top-level Cloud Build `images:` lists
only this carrier so `results.images` records its immutable digest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cloudbuild_provenance import (
    LABEL_AGENT_RUNTIME_DIGEST,
    LABEL_BUILD_ID,
    LABEL_CORE_CONSOLE_DIGEST,
    LABEL_CORE_DIGEST,
    LABEL_GIT_COMMIT,
    LABEL_SOURCE_REPOSITORY,
    ProvenanceError,
    bind_provenance,
    carrier_docker_build_argv,
    carrier_image_ref,
    extract_digest_from_metadata,
    main,
    write_bindings_env,
    write_carrier_build_script,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

_COMMIT = "a" * 40
_BUILD_ID = "build-abc-123"
_REPO = "dimileeh/agent-workspace-fabric"
_CORE_DIGEST = "sha256:" + ("b" * 64)
_RUNTIME_DIGEST = "sha256:" + ("c" * 64)
_CONSOLE_DIGEST = "sha256:" + ("d" * 64)


def _metadata(digest: str) -> dict[str, object]:
    return {"containerimage.digest": digest}


def _valid_bind_kwargs() -> dict[str, str]:
    return {
        "build_id": _BUILD_ID,
        "commit_sha": _COMMIT,
        "source_repository": _REPO,
        "core_digest": _CORE_DIGEST,
        "agent_runtime_digest": _RUNTIME_DIGEST,
        "core_console_digest": _CONSOLE_DIGEST,
    }


def test_extract_digest_from_metadata_dict() -> None:
    assert extract_digest_from_metadata(_metadata(_CORE_DIGEST)) == _CORE_DIGEST


def test_extract_digest_from_metadata_json_file(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    path.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    assert extract_digest_from_metadata(path) == _RUNTIME_DIGEST


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"containerimage.digest": ""},
        {"containerimage.digest": "sha256:dead"},
        {"containerimage.digest": "sha256:" + ("X" * 64)},
        {"containerimage.digest": f" {_CORE_DIGEST} "},
        {"containerimage.digest": f"registry.example/{_CORE_DIGEST}"},
        {"containerimage.digest": "latest"},
    ],
)
def test_extract_digest_rejects_missing_or_malformed(payload: dict[str, object]) -> None:
    with pytest.raises(ProvenanceError):
        extract_digest_from_metadata(payload)


def test_bind_provenance_accepts_valid_bindings() -> None:
    bindings = bind_provenance(**_valid_bind_kwargs())
    assert bindings.labels() == {
        LABEL_BUILD_ID: _BUILD_ID,
        LABEL_GIT_COMMIT: _COMMIT,
        LABEL_SOURCE_REPOSITORY: _REPO,
        LABEL_CORE_DIGEST: _CORE_DIGEST,
        LABEL_AGENT_RUNTIME_DIGEST: _RUNTIME_DIGEST,
        LABEL_CORE_CONSOLE_DIGEST: _CONSOLE_DIGEST,
    }
    assert len(bindings.labels()) == 6


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"commit_sha": "A" * 40}, "commit"),
        ({"commit_sha": "a" * 39}, "commit"),
        ({"commit_sha": "a" * 41}, "commit"),
        ({"build_id": ""}, "build"),
        ({"build_id": "   "}, "build"),
        ({"build_id": "id with space"}, "build"),
        ({"build_id": "repo/path"}, "build"),
        ({"build_id": "tag:extra"}, "build"),
        ({"build_id": "digest@sha256"}, "build"),
        ({"build_id": ".leading-dot"}, "build"),
        ({"build_id": "-leading-hyphen"}, "build"),
        # carrier_image_ref prefixes "build-" (6 chars); Docker tags max at 128.
        ({"build_id": "x" * 123}, "build"),
        ({"build_id": "x" * 129}, "build"),
        ({"source_repository": ""}, "source"),
        ({"source_repository": "   "}, "source"),
        ({"source_repository": "https://x:y@github.com/org/repo"}, "source"),
        ({"source_repository": "ghp_notarealtokenbutlookslikeone"}, "source"),
        ({"core_digest": "awf-core:rc-deadbeef"}, "digest"),
        ({"core_digest": f" {_CORE_DIGEST}"}, "digest"),
        ({"agent_runtime_digest": "sha256:" + ("d" * 63)}, "digest"),
        ({"core_console_digest": f" {_CONSOLE_DIGEST}"}, "digest"),
        ({"core_console_digest": f"{_CONSOLE_DIGEST} "}, "digest"),
        ({"core_console_digest": "sha256:" + ("e" * 63)}, "digest"),
        ({"core_console_digest": "sha256:" + ("E" * 64)}, "digest"),
        ({"core_console_digest": "latest"}, "digest"),
        ({"core_console_digest": ""}, "digest"),
    ],
)
def test_bind_provenance_rejects_invalid_bindings(
    kwargs: dict[str, str],
    match: str,
) -> None:
    base = _valid_bind_kwargs()
    base.update(kwargs)
    with pytest.raises(ProvenanceError, match=match):
        bind_provenance(**base)


def test_carrier_image_ref_uses_build_id_tag() -> None:
    ref = carrier_image_ref(
        artifact_repository="us-docker.pkg.dev/proj/repo",
        build_id=_BUILD_ID,
    )
    assert ref == f"us-docker.pkg.dev/proj/repo/awf-core-provenance:build-{_BUILD_ID}"


def test_bind_from_metadata_files_round_trip(tmp_path: Path) -> None:
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    console_meta = tmp_path / "console.json"
    core_meta.write_text(json.dumps(_metadata(_CORE_DIGEST)), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    console_meta.write_text(json.dumps(_metadata(_CONSOLE_DIGEST)), encoding="utf-8")

    bindings = bind_provenance(
        build_id=_BUILD_ID,
        commit_sha=_COMMIT,
        source_repository=_REPO,
        core_digest=extract_digest_from_metadata(core_meta),
        agent_runtime_digest=extract_digest_from_metadata(runtime_meta),
        core_console_digest=extract_digest_from_metadata(console_meta),
    )
    assert bindings.labels()[LABEL_CORE_DIGEST] == _CORE_DIGEST
    assert bindings.labels()[LABEL_AGENT_RUNTIME_DIGEST] == _RUNTIME_DIGEST
    assert bindings.labels()[LABEL_CORE_CONSOLE_DIGEST] == _CONSOLE_DIGEST


def test_helper_module_documents_carrier_schema() -> None:
    """Schema keys must stay documented next to the producer contract."""
    helper = (REPO_ROOT / "scripts" / "cloudbuild_provenance.py").read_text(encoding="utf-8")
    for key in (
        LABEL_BUILD_ID,
        LABEL_GIT_COMMIT,
        LABEL_SOURCE_REPOSITORY,
        LABEL_CORE_DIGEST,
        LABEL_AGENT_RUNTIME_DIGEST,
        LABEL_CORE_CONSOLE_DIGEST,
        "awf-core-provenance",
        "metadata-file",
    ):
        assert key in helper


def test_carrier_docker_build_argv_embeds_contract_labels() -> None:
    bindings = bind_provenance(**_valid_bind_kwargs())
    tag = carrier_image_ref(
        artifact_repository="us-docker.pkg.dev/proj/repo",
        build_id=_BUILD_ID,
    )
    argv = carrier_docker_build_argv(bindings=bindings, carrier_tag=tag)
    assert argv[:6] == [
        "buildx",
        "build",
        "--builder",
        "default",
        "--load",
        "--file",
    ]
    assert "docker/awf-core-provenance.Dockerfile" in argv
    assert tag in argv
    assert f"{LABEL_BUILD_ID}={_BUILD_ID}" in argv
    assert f"{LABEL_GIT_COMMIT}={_COMMIT}" in argv
    assert f"{LABEL_CORE_DIGEST}={_CORE_DIGEST}" in argv
    assert f"{LABEL_AGENT_RUNTIME_DIGEST}={_RUNTIME_DIGEST}" in argv
    assert f"{LABEL_CORE_CONSOLE_DIGEST}={_CONSOLE_DIGEST}" in argv
    assert argv[-1] == "docker/"
    assert "--push" not in argv


def test_write_bindings_env_and_prepare_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    console_meta = tmp_path / "console.json"
    core_meta.write_text(json.dumps(_metadata(_CORE_DIGEST)), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    console_meta.write_text(json.dumps(_metadata(_CONSOLE_DIGEST)), encoding="utf-8")
    out_env = tmp_path / "awf-provenance.env"
    out_script = tmp_path / "awf-provenance-build.sh"

    bindings = bind_provenance(**_valid_bind_kwargs())
    tag = carrier_image_ref(
        artifact_repository="us-docker.pkg.dev/proj/repo",
        build_id=_BUILD_ID,
    )
    write_bindings_env(out_env, bindings=bindings, carrier_tag=tag)
    env_text = out_env.read_text(encoding="utf-8")
    assert f"CARRIER_TAG='{tag}'" in env_text
    assert f"AWF_CORE_DIGEST='{_CORE_DIGEST}'" in env_text
    assert f"AWF_CORE_CONSOLE_DIGEST='{_CONSOLE_DIGEST}'" in env_text

    rc = main(
        [
            "prepare",
            "--build-id",
            _BUILD_ID,
            "--commit-sha",
            _COMMIT,
            "--source-repository",
            _REPO,
            "--artifact-repository",
            "us-docker.pkg.dev/proj/repo",
            "--core-metadata",
            str(core_meta),
            "--runtime-metadata",
            str(runtime_meta),
            "--console-metadata",
            str(console_meta),
            "--output-env",
            str(out_env),
            "--output-build-script",
            str(out_script),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert _CORE_DIGEST in captured.err
    assert _CONSOLE_DIGEST in captured.err
    assert "TOKEN" not in captured.err
    assert "password" not in captured.err.lower()
    script_text = out_script.read_text(encoding="utf-8")
    assert "exec 'docker'" in script_text
    assert "'--load'" in script_text
    assert tag in script_text
    assert f"{LABEL_CORE_CONSOLE_DIGEST}={_CONSOLE_DIGEST}" in script_text


def test_prepare_cli_rejects_bad_metadata(tmp_path: Path) -> None:
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    console_meta = tmp_path / "console.json"
    core_meta.write_text(json.dumps({"containerimage.digest": "latest"}), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    console_meta.write_text(json.dumps(_metadata(_CONSOLE_DIGEST)), encoding="utf-8")
    out_env = tmp_path / "awf-provenance.env"
    out_script = tmp_path / "awf-provenance-build.sh"

    rc = main(
        [
            "prepare",
            "--build-id",
            _BUILD_ID,
            "--commit-sha",
            _COMMIT,
            "--source-repository",
            _REPO,
            "--artifact-repository",
            "us-docker.pkg.dev/proj/repo",
            "--core-metadata",
            str(core_meta),
            "--runtime-metadata",
            str(runtime_meta),
            "--console-metadata",
            str(console_meta),
            "--output-env",
            str(out_env),
            "--output-build-script",
            str(out_script),
        ]
    )
    assert rc == 1
    assert not out_env.exists()
    assert not out_script.exists()


def test_prepare_cli_rejects_bad_console_metadata(tmp_path: Path) -> None:
    """Missing/malformed console digest must fail closed with no side effects."""
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    console_meta = tmp_path / "console.json"
    core_meta.write_text(json.dumps(_metadata(_CORE_DIGEST)), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    console_meta.write_text(
        json.dumps({"containerimage.digest": f" {_CONSOLE_DIGEST} "}),
        encoding="utf-8",
    )
    out_env = tmp_path / "awf-provenance.env"
    out_script = tmp_path / "awf-provenance-build.sh"

    rc = main(
        [
            "prepare",
            "--build-id",
            _BUILD_ID,
            "--commit-sha",
            _COMMIT,
            "--source-repository",
            _REPO,
            "--artifact-repository",
            "us-docker.pkg.dev/proj/repo",
            "--core-metadata",
            str(core_meta),
            "--runtime-metadata",
            str(runtime_meta),
            "--console-metadata",
            str(console_meta),
            "--output-env",
            str(out_env),
            "--output-build-script",
            str(out_script),
        ]
    )
    assert rc == 1
    assert not out_env.exists()
    assert not out_script.exists()


def test_prepare_cli_rejects_missing_console_metadata_file(tmp_path: Path) -> None:
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    core_meta.write_text(json.dumps(_metadata(_CORE_DIGEST)), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    out_env = tmp_path / "awf-provenance.env"
    out_script = tmp_path / "awf-provenance-build.sh"
    missing_console = tmp_path / "missing-console.metadata.json"

    rc = main(
        [
            "prepare",
            "--build-id",
            _BUILD_ID,
            "--commit-sha",
            _COMMIT,
            "--source-repository",
            _REPO,
            "--artifact-repository",
            "us-docker.pkg.dev/proj/repo",
            "--core-metadata",
            str(core_meta),
            "--runtime-metadata",
            str(runtime_meta),
            "--console-metadata",
            str(missing_console),
            "--output-env",
            str(out_env),
            "--output-build-script",
            str(out_script),
        ]
    )
    assert rc == 1
    assert not out_env.exists()
    assert not out_script.exists()


def test_prepare_cli_oserror_on_env_write_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing parent dir / unwritable path must exit 1, not traceback."""
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    console_meta = tmp_path / "console.json"
    core_meta.write_text(json.dumps(_metadata(_CORE_DIGEST)), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    console_meta.write_text(json.dumps(_metadata(_CONSOLE_DIGEST)), encoding="utf-8")
    missing_parent = tmp_path / "no-such-dir" / "awf-provenance.env"
    out_script = tmp_path / "awf-provenance-build.sh"

    rc = main(
        [
            "prepare",
            "--build-id",
            _BUILD_ID,
            "--commit-sha",
            _COMMIT,
            "--source-repository",
            _REPO,
            "--artifact-repository",
            "us-docker.pkg.dev/proj/repo",
            "--core-metadata",
            str(core_meta),
            "--runtime-metadata",
            str(runtime_meta),
            "--console-metadata",
            str(console_meta),
            "--output-env",
            str(missing_parent),
            "--output-build-script",
            str(out_script),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "provenance error:" in captured.err
    assert "Traceback" not in captured.err
    assert not out_script.exists()


def test_write_carrier_build_script_uses_helper_argv(tmp_path: Path) -> None:
    bindings = bind_provenance(**_valid_bind_kwargs())
    tag = carrier_image_ref(
        artifact_repository="us-docker.pkg.dev/proj/repo",
        build_id=_BUILD_ID,
    )
    script = tmp_path / "awf-provenance-build.sh"
    write_carrier_build_script(script, bindings=bindings, carrier_tag=tag)
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n")
    assert "set -eu\n" in text
    assert "exec 'docker'" in text
    assert "'buildx'" in text
    assert "'--builder'" in text
    assert "'default'" in text
    assert "'--load'" in text
    assert "docker/awf-core-provenance.Dockerfile" in text
    assert f"{LABEL_BUILD_ID}={_BUILD_ID}" in text
    assert f"{LABEL_CORE_DIGEST}={_CORE_DIGEST}" in text
    assert f"{LABEL_CORE_CONSOLE_DIGEST}={_CONSOLE_DIGEST}" in text
    assert "'--push'" not in text
    assert "--push" not in text


def test_carrier_image_ref_rejects_empty_or_trailing_slash_repo() -> None:
    with pytest.raises(ProvenanceError, match="artifact repository"):
        carrier_image_ref(artifact_repository="", build_id=_BUILD_ID)
    with pytest.raises(ProvenanceError, match="artifact repository"):
        carrier_image_ref(artifact_repository="us-docker.pkg.dev/proj/repo/", build_id=_BUILD_ID)


def test_carrier_image_ref_rejects_credential_looking_repo() -> None:
    with pytest.raises(ProvenanceError, match="credential-looking"):
        carrier_image_ref(
            artifact_repository="https://user:password@registry.example/repo",
            build_id=_BUILD_ID,
        )


def test_prepare_cli_rejects_credential_artifact_repo_without_leaking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Credential-bearing artifact repos must fail before tag/log emission."""
    core_meta = tmp_path / "core.json"
    runtime_meta = tmp_path / "runtime.json"
    console_meta = tmp_path / "console.json"
    core_meta.write_text(json.dumps(_metadata(_CORE_DIGEST)), encoding="utf-8")
    runtime_meta.write_text(json.dumps(_metadata(_RUNTIME_DIGEST)), encoding="utf-8")
    console_meta.write_text(json.dumps(_metadata(_CONSOLE_DIGEST)), encoding="utf-8")
    out_env = tmp_path / "awf-provenance.env"
    out_script = tmp_path / "awf-provenance-build.sh"
    credential_repo = "https://user:password@registry.example/repo"

    rc = main(
        [
            "prepare",
            "--build-id",
            _BUILD_ID,
            "--commit-sha",
            _COMMIT,
            "--source-repository",
            _REPO,
            "--artifact-repository",
            credential_repo,
            "--core-metadata",
            str(core_meta),
            "--runtime-metadata",
            str(runtime_meta),
            "--console-metadata",
            str(console_meta),
            "--output-env",
            str(out_env),
            "--output-build-script",
            str(out_script),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "provenance error:" in captured.err
    assert "credential-looking" in captured.err
    assert "user:password" not in captured.err
    assert "password" not in captured.err.lower()
    assert credential_repo not in captured.err
    assert not out_env.exists()
    assert not out_script.exists()


@pytest.mark.parametrize(
    "build_id",
    [
        "repo/path",
        "tag:extra",
        "digest@sha256",
        ".dot",
        "-hyphen",
        # "build-" + 123 chars exceeds Docker's 128-char tag limit.
        "x" * 123,
        "x" * 129,
    ],
)
def test_carrier_image_ref_rejects_non_docker_tag_build_id(build_id: str) -> None:
    with pytest.raises(ProvenanceError, match="Docker tag"):
        carrier_image_ref(
            artifact_repository="us-docker.pkg.dev/proj/repo",
            build_id=build_id,
        )


def test_carrier_image_ref_accepts_max_build_id_for_prefixed_tag() -> None:
    """Exact boundary: build- (6) + 122-char id == 128-char Docker tag."""
    build_id = "x" * 122
    ref = carrier_image_ref(
        artifact_repository="us-docker.pkg.dev/proj/repo",
        build_id=build_id,
    )
    tag = ref.rsplit(":", 1)[1]
    assert tag == f"build-{build_id}"
    assert len(tag) == 128
