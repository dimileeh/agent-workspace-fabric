"""External-account clarification auth staging tests."""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from awf.db.enums import AgentRuntime
from awf.node.compose_manager import (
    AuthMount,
    ComposeManager,
    WorkspaceComposeSpec,
    upgrade_persisted_clarification_service,
)
from awf.node.stack_launcher_auth_helpers import legacy_clarification_entrypoint

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    """Provide a compose manager rooted in the test temp directory."""
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


def _spec(tmp_path: Path, **overrides: object) -> WorkspaceComposeSpec:
    base = {
        "workspace_id": "ws_test123",
        "worktree_host_path": tmp_path / "worktree",
        "postgres_password": "deterministic-for-test",
    }
    base.update(overrides)
    return WorkspaceComposeSpec(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_upgrade_persisted_clarification_stages_external_account_subject_token(
    tmp_path: Path,
) -> None:
    """Legacy clarification copies external-account ADC dependencies under its home."""
    adc = tmp_path / "external-account-adc.json"
    subject_token = tmp_path / "subject-token"
    adc_target = "/run/awf/secrets/google/external-account-adc.json"
    subject_token_target = "/run/awf/secrets/google/subject-token"
    non_normalized_subject_token_target = "/run/awf/secrets/google/./subject-token"
    adc.write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"file": non_normalized_subject_token_target},
            }
        ),
        encoding="utf-8",
    )
    subject_token.write_text("subject-token", encoding="utf-8")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {
                            "CLAUDE_CODE_USE_VERTEX": "1",
                            "GOOGLE_APPLICATION_CREDENTIALS": adc_target,
                        },
                        "volumes": [
                            f"{adc}:{adc_target}:ro",
                            f"{subject_token}:{subject_token_target}:ro",
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_external_account",
            agent_runtime=AgentRuntime.claude_code,
        )
        == ()
    )

    clarification = yaml.safe_load(compose_file.read_text(encoding="utf-8"))["services"][
        "clarification"
    ]
    assert clarification["environment"] == {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "GOOGLE_APPLICATION_CREDENTIALS": "/home/agent/.awf/clarification-auth/0",
        "AWF_CLARIFICATION_AUTH_TARGET_0": "/home/agent/.awf/clarification-auth/0",
        "AWF_CLARIFICATION_AUTH_TARGET_1": "/home/agent/.awf/clarification-auth/1",
        "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
            [[subject_token_target, "/home/agent/.awf/clarification-auth/1"]]
        ),
    }
    assert clarification["volumes"] == [
        f"{adc}:/run/awf/clarification-auth/0:ro",
        f"{subject_token}:/run/awf/clarification-auth/1:ro",
    ]
    assert "credential_source" in clarification["entrypoint"][2]
    assert (
        "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES"
        in (clarification["entrypoint"][2])
    )
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_subject_token = tmp_path / "clarification-auth" / "subject-token"
    (source_root / "0").write_text(adc.read_text(encoding="utf-8"), encoding="utf-8")
    (source_root / "1").write_text("subject-token", encoding="utf-8")
    entrypoint = clarification["entrypoint"]
    subprocess.run(
        [
            entrypoint[0],
            entrypoint[1],
            entrypoint[2].replace("/run/awf/clarification-auth", str(source_root)),
            entrypoint[3],
            "true",
        ],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_subject_token),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [[subject_token_target, str(staged_subject_token)]]
            ),
        },
    )
    assert json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"]["file"] == str(
        staged_subject_token
    )


@pytest.mark.unit
def test_clarification_rewrites_staged_external_account_subject_token(
    manager: ComposeManager, tmp_path: Path
) -> None:
    """The copied external-account ADC points at the copied subject token."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    original_subject_token = "/run/awf/secrets/google/subject-token"
    non_normalized_subject_token = "/run/awf/secrets/google/./subject-token"
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_subject_token = tmp_path / "clarification-auth" / "subject-token"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {"file": non_normalized_subject_token},
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("subject-token", encoding="utf-8")
    parsed = yaml.safe_load(
        manager.render(
            _spec(
                tmp_path,
                clarification_enabled=True,
                clarification_agent_environment=(
                    ("GOOGLE_APPLICATION_CREDENTIALS", str(staged_adc)),
                ),
                clarification_auth_mounts=(
                    AuthMount(source="/host/adc.json", target=str(staged_adc)),
                    AuthMount(
                        source="/host/subject-token",
                        target=str(staged_subject_token),
                    ),
                ),
                clarification_external_account_subject_token_file_rewrites=(
                    (original_subject_token, str(staged_subject_token)),
                ),
            )
        ).compose_file.read_text()
    )
    clarification = parsed["services"]["clarification"]

    assert json.loads(
        clarification["environment"][
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES"
        ]
    ) == [[original_subject_token, str(staged_subject_token)]]
    entrypoint = clarification["entrypoint"]
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))
    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_subject_token),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [[original_subject_token, str(staged_subject_token)]]
            ),
        },
    )

    assert json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"]["file"] == str(
        staged_subject_token
    )


@pytest.mark.unit
def test_clarification_rewrites_staged_external_account_executable_paths(
    manager: ComposeManager, tmp_path: Path
) -> None:
    """The rendered entrypoint rewrites copied executable ADC dependencies."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    helper_target = "/run/awf/secrets/google/external-account-helper"
    non_normalized_helper_target = "/run/awf/secrets/google/./external-account-helper"
    output_target = "/run/awf/secrets/google/external-account-output.json"
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_helper = tmp_path / "clarification-auth" / "external-account-helper"
    staged_output = tmp_path / "clarification-auth" / "external-account-output.json"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": (
                            f"printf '%s' {helper_target}-backup && "
                            f"{non_normalized_helper_target} --output={output_target} | jq -c ."
                        ),
                        "output_file": output_target,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("{}", encoding="utf-8")
    parsed = yaml.safe_load(
        manager.render(
            _spec(
                tmp_path,
                clarification_enabled=True,
                clarification_agent_environment=(
                    ("GOOGLE_APPLICATION_CREDENTIALS", str(staged_adc)),
                ),
                clarification_auth_mounts=(
                    AuthMount(source="/host/adc.json", target=str(staged_adc)),
                    AuthMount(source="/host/helper", target=str(staged_helper)),
                    AuthMount(source="/host/output", target=str(staged_output)),
                ),
                clarification_external_account_subject_token_file_rewrites=(
                    (helper_target, str(staged_helper)),
                    (output_target, str(staged_output)),
                ),
            )
        ).compose_file.read_text()
    )
    entrypoint = parsed["services"]["clarification"]["entrypoint"]
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_output),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [output_target, str(staged_output)],
                ]
            ),
        },
    )

    executable = json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"][
        "executable"
    ]
    assert executable == {
        "command": (
            f"printf '%s' {helper_target}-backup && "
            f"{staged_helper} --output={staged_output} | jq -c ."
        ),
        "output_file": str(staged_output),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command_suffix", "expected_command_suffix"),
    (
        ('--subject-token="{subject_token}"', '--subject-token="{staged_subject_token}"'),
        ("--subject-token={escaped_subject_token}", "--subject-token={staged_subject_token}"),
    ),
)
def test_clarification_rewrites_executable_subject_token_path(
    manager: ComposeManager,
    tmp_path: Path,
    command_suffix: str,
    expected_command_suffix: str,
) -> None:
    """Executable credential paths retain whitespace while being staged."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    helper_target = "/run/awf/secrets/google/external-account-helper"
    subject_token_target = "/run/awf/secrets/google/subject token"
    escaped_subject_token = subject_token_target.replace(" ", r"\ ")
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_helper = tmp_path / "clarification-auth" / "external-account-helper"
    staged_subject_token = tmp_path / "clarification-auth" / "subject-token"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": f"{helper_target} "
                        + command_suffix.format(
                            subject_token=subject_token_target,
                            escaped_subject_token=escaped_subject_token,
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("subject-token", encoding="utf-8")
    entrypoint = yaml.safe_load(
        manager.render(
            _spec(
                tmp_path,
                clarification_enabled=True,
                clarification_agent_environment=(
                    ("GOOGLE_APPLICATION_CREDENTIALS", str(staged_adc)),
                ),
                clarification_auth_mounts=(
                    AuthMount(source="/host/adc.json", target=str(staged_adc)),
                    AuthMount(source="/host/helper", target=str(staged_helper)),
                    AuthMount(
                        source="/host/subject-token",
                        target=str(staged_subject_token),
                    ),
                ),
                clarification_external_account_subject_token_file_rewrites=(
                    (helper_target, str(staged_helper)),
                    (subject_token_target, str(staged_subject_token)),
                ),
            )
        ).compose_file.read_text()
    )["services"]["clarification"]["entrypoint"]
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_subject_token),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [subject_token_target, str(staged_subject_token)],
                ]
            ),
        },
    )

    executable = json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"][
        "executable"
    ]
    assert executable["command"] == (
        f"{staged_helper} "
        + expected_command_suffix.format(staged_subject_token=staged_subject_token)
    )


@pytest.mark.unit
@pytest.mark.parametrize("operator", (":-", "-", ":=", "=", ":+"))
def test_clarification_rewrites_defaulted_executable_token_path(
    manager: ComposeManager,
    tmp_path: Path,
    operator: str,
) -> None:
    """Parameter-expanded executable paths point at the staged fallback token."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    helper_target = "/run/awf/secrets/google/external-account-helper"
    fallback_token_target = "/run/awf/secrets/google/fallback-token"
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_helper = tmp_path / "clarification-auth" / "external-account-helper"
    staged_fallback_token = tmp_path / "clarification-auth" / "fallback-token"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": (
                            f"sh -c '{helper_target} --token-file "
                            f'"${{MY_TOKEN_FILE{operator}{fallback_token_target}}}"\''
                        )
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("fallback-token", encoding="utf-8")
    entrypoint = yaml.safe_load(
        manager.render(
            _spec(
                tmp_path,
                clarification_enabled=True,
                clarification_agent_environment=(
                    ("GOOGLE_APPLICATION_CREDENTIALS", str(staged_adc)),
                ),
                clarification_auth_mounts=(
                    AuthMount(source="/host/adc.json", target=str(staged_adc)),
                    AuthMount(source="/host/helper", target=str(staged_helper)),
                    AuthMount(
                        source="/host/fallback-token",
                        target=str(staged_fallback_token),
                    ),
                ),
                clarification_external_account_subject_token_file_rewrites=(
                    (helper_target, str(staged_helper)),
                    (fallback_token_target, str(staged_fallback_token)),
                ),
            )
        ).compose_file.read_text()
    )["services"]["clarification"]["entrypoint"]
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_fallback_token),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [fallback_token_target, str(staged_fallback_token)],
                ]
            ),
        },
    )

    executable = json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"][
        "executable"
    ]
    assert executable["command"] == (
        f"sh -c '{staged_helper} --token-file "
        f'"${{MY_TOKEN_FILE{operator}{staged_fallback_token}}}"\''
    )


@pytest.mark.unit
def test_legacy_clarification_entrypoint_rewrites_external_account_executable_paths(
    tmp_path: Path,
) -> None:
    """Copied executable ADC configuration refers only to copied dependencies."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    helper_target = "/run/awf/secrets/google/external-account-helper"
    non_normalized_helper_target = "/run/awf/secrets/google/./external-account-helper"
    output_target = "/run/awf/secrets/google/external-account-output.json"
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_helper = tmp_path / "clarification-auth" / "external-account-helper"
    staged_output = tmp_path / "clarification-auth" / "external-account-output.json"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": (
                            f"printf '%s' {helper_target}-backup && "
                            f"{non_normalized_helper_target} --output={output_target} | jq -c ."
                        ),
                        "output_file": output_target,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("{}", encoding="utf-8")
    entrypoint = legacy_clarification_entrypoint(
        3, rewrite_external_account_subject_token_file=True
    )
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_output),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [output_target, str(staged_output)],
                ]
            ),
        },
    )

    executable = json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"][
        "executable"
    ]
    assert executable == {
        "command": (
            f"printf '%s' {helper_target}-backup && "
            f"{staged_helper} --output={staged_output} | jq -c ."
        ),
        "output_file": str(staged_output),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command_suffix", "expected_command_suffix"),
    (
        ('--subject-token="{subject_token}"', '--subject-token="{staged_subject_token}"'),
        ("--subject-token={escaped_subject_token}", "--subject-token={staged_subject_token}"),
    ),
)
def test_legacy_clarification_entrypoint_rewrites_executable_subject_token_path(
    tmp_path: Path,
    command_suffix: str,
    expected_command_suffix: str,
) -> None:
    """Executable credential paths retain whitespace while being staged."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    helper_target = "/run/awf/secrets/google/external-account-helper"
    subject_token_target = "/run/awf/secrets/google/subject token"
    escaped_subject_token = subject_token_target.replace(" ", r"\ ")
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_helper = tmp_path / "clarification-auth" / "external-account-helper"
    staged_subject_token = tmp_path / "clarification-auth" / "subject-token"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": f"{helper_target} "
                        + command_suffix.format(
                            subject_token=subject_token_target,
                            escaped_subject_token=escaped_subject_token,
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("subject-token", encoding="utf-8")
    entrypoint = legacy_clarification_entrypoint(
        3, rewrite_external_account_subject_token_file=True
    )
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_subject_token),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [subject_token_target, str(staged_subject_token)],
                ]
            ),
        },
    )

    executable = json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"][
        "executable"
    ]
    assert executable["command"] == (
        f"{staged_helper} "
        + expected_command_suffix.format(staged_subject_token=staged_subject_token)
    )


@pytest.mark.unit
@pytest.mark.parametrize("operator", (":-", "-", ":=", "=", ":+", "+"))
def test_legacy_clarification_entrypoint_rewrites_defaulted_executable_token_path(
    tmp_path: Path,
    operator: str,
) -> None:
    """Parameter-expanded executable paths point at the staged fallback token."""
    source_root = tmp_path / "clarification-source"
    source_root.mkdir()
    helper_target = "/run/awf/secrets/google/external-account-helper"
    fallback_token_target = "/run/awf/secrets/google/fallback-token"
    staged_adc = tmp_path / "clarification-auth" / "adc.json"
    staged_helper = tmp_path / "clarification-auth" / "external-account-helper"
    staged_fallback_token = tmp_path / "clarification-auth" / "fallback-token"
    (source_root / "0").write_text(
        json.dumps(
            {
                "type": "external_account",
                "credential_source": {
                    "executable": {
                        "command": (
                            f"sh -c '{helper_target} --token-file "
                            f'"${{MY_TOKEN_FILE{operator}{fallback_token_target}}}"\''
                        )
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("fallback-token", encoding="utf-8")
    entrypoint = legacy_clarification_entrypoint(
        3, rewrite_external_account_subject_token_file=True
    )
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "GOOGLE_APPLICATION_CREDENTIALS": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_adc),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_fallback_token),
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [fallback_token_target, str(staged_fallback_token)],
                ]
            ),
        },
    )

    executable = json.loads(staged_adc.read_text(encoding="utf-8"))["credential_source"][
        "executable"
    ]
    assert executable["command"] == (
        f"sh -c '{staged_helper} --token-file \"${{MY_TOKEN_FILE{operator}{staged_fallback_token}}}\"'"
    )


@pytest.mark.unit
def test_clarification_rewrites_staged_aws_profile_paths(
    manager: ComposeManager, tmp_path: Path
) -> None:
    """The copied Bedrock profile points at copied helper and token mounts."""
    source_root = tmp_path / "clarification-source"
    source_aws = source_root / "0"
    source_aws.mkdir(parents=True)
    helper_target = "/run/awf/secrets/aws-helper"
    token_target = "/run/awf/secrets/subject token"
    staged_aws = tmp_path / "clarification-auth" / "aws"
    staged_helper = tmp_path / "clarification-auth" / "aws-helper"
    staged_token = tmp_path / "clarification-auth" / "web-identity-token"
    (source_aws / "config").write_text(
        "[profile awf-bedrock]\n"
        f"credential_process = {helper_target} --token-file={token_target.replace(' ', r'\ ')}\n"
        f"web_identity_token_file = {token_target}\n",
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("token", encoding="utf-8")
    parsed = yaml.safe_load(
        manager.render(
            _spec(
                tmp_path,
                clarification_enabled=True,
                clarification_agent_environment=(
                    ("AWS_PROFILE", "awf-bedrock"),
                    ("AWS_CONFIG_FILE", str(staged_aws / "config")),
                ),
                clarification_auth_mounts=(
                    AuthMount(source="/host/aws", target=str(staged_aws)),
                    AuthMount(source="/host/aws-helper", target=str(staged_helper)),
                    AuthMount(source="/host/web-identity-token", target=str(staged_token)),
                ),
                clarification_aws_profile_path_rewrites=(
                    (helper_target, str(staged_helper)),
                    (token_target, str(staged_token)),
                ),
            )
        ).compose_file.read_text()
    )
    clarification = parsed["services"]["clarification"]

    assert json.loads(
        clarification["environment"]["AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES"]
    ) == [[helper_target, str(staged_helper)], [token_target, str(staged_token)]]
    entrypoint = clarification["entrypoint"]
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))
    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "AWS_PROFILE": "awf-bedrock",
            "AWS_CONFIG_FILE": str(staged_aws / "config"),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_aws),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_token),
            "AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES": json.dumps(
                [[helper_target, str(staged_helper)], [token_target, str(staged_token)]]
            ),
        },
    )

    configuration = configparser.RawConfigParser(interpolation=None)
    configuration.read(staged_aws / "config", encoding="utf-8")
    assert configuration.get("profile awf-bedrock", "credential_process") == (
        f"{staged_helper} --token-file={staged_token}"
    )
    assert configuration.get("profile awf-bedrock", "web_identity_token_file") == str(staged_token)


@pytest.mark.unit
def test_legacy_clarification_entrypoint_rewrites_staged_aws_profile_paths(
    tmp_path: Path,
) -> None:
    """Persisted-stack clarification rewrites copied AWS profile configuration."""
    source_root = tmp_path / "clarification-source"
    source_aws = source_root / "0"
    source_aws.mkdir(parents=True)
    helper_target = "/run/awf/secrets/aws-helper"
    staged_aws = tmp_path / "clarification-auth" / "aws"
    staged_helper = tmp_path / "clarification-auth" / "aws-helper"
    (source_aws / "config").write_text(
        f"[profile awf-bedrock]\ncredential_process = {helper_target} --json\n",
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    entrypoint = legacy_clarification_entrypoint(2, rewrite_aws_profile_paths=True)
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "AWS_PROFILE": "awf-bedrock",
            "AWS_CONFIG_FILE": str(staged_aws / "config"),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_aws),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES": json.dumps(
                [[helper_target, str(staged_helper)]]
            ),
        },
    )

    configuration = configparser.RawConfigParser(interpolation=None)
    configuration.read(staged_aws / "config", encoding="utf-8")
    assert configuration.get("profile awf-bedrock", "credential_process") == (
        f"{staged_helper} --json"
    )


@pytest.mark.unit
def test_legacy_clarification_entrypoint_rewrites_escaped_aws_credential_process_path(
    tmp_path: Path,
) -> None:
    """Escaped credential-process option paths point at staged auth files."""
    source_root = tmp_path / "clarification-source"
    source_aws = source_root / "0"
    source_aws.mkdir(parents=True)
    helper_target = "/run/awf/secrets/aws-helper"
    token_target = "/run/awf/secrets/subject token"
    staged_aws = tmp_path / "clarification-auth" / "aws"
    staged_helper = tmp_path / "clarification-auth" / "aws-helper"
    staged_token = tmp_path / "clarification-auth" / "subject-token"
    (source_aws / "config").write_text(
        "[profile awf-bedrock]\n"
        f"credential_process = {helper_target} --token-file={token_target.replace(' ', r'\ ')}\n",
        encoding="utf-8",
    )
    (source_root / "1").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_root / "2").write_text("token", encoding="utf-8")
    entrypoint = legacy_clarification_entrypoint(3, rewrite_aws_profile_paths=True)
    script = entrypoint[2].replace("/run/awf/clarification-auth", str(source_root))

    subprocess.run(
        [entrypoint[0], entrypoint[1], script, entrypoint[3], "true"],
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "AWS_PROFILE": "awf-bedrock",
            "AWS_CONFIG_FILE": str(staged_aws / "config"),
            "AWF_CLARIFICATION_AUTH_TARGET_0": str(staged_aws),
            "AWF_CLARIFICATION_AUTH_TARGET_1": str(staged_helper),
            "AWF_CLARIFICATION_AUTH_TARGET_2": str(staged_token),
            "AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES": json.dumps(
                [
                    [helper_target, str(staged_helper)],
                    [token_target, str(staged_token)],
                ]
            ),
        },
    )

    configuration = configparser.RawConfigParser(interpolation=None)
    configuration.read(staged_aws / "config", encoding="utf-8")
    assert configuration.get("profile awf-bedrock", "credential_process") == (
        f"{staged_helper} --token-file={staged_token}"
    )


@pytest.mark.unit
def test_upgrade_persisted_clarification_stages_aws_profile_helper(tmp_path: Path) -> None:
    """Persisted clarification carries active AWS profile helper rewrites forward."""
    aws_home = tmp_path / "aws"
    aws_home.mkdir()
    helper = tmp_path / "aws-helper"
    helper_target = "/run/awf/secrets/aws-helper"
    (aws_home / "config").write_text(
        f"[profile awf-bedrock]\ncredential_process = {helper_target} --json\n",
        encoding="utf-8",
    )
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {
                            "CLAUDE_CODE_USE_BEDROCK": "1",
                            "AWS_PROFILE": "awf-bedrock",
                        },
                        "volumes": [
                            f"{aws_home}:/home/agent/.aws:ro",
                            f"{helper}:{helper_target}:ro",
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert (
        upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_aws_profile",
            agent_runtime=AgentRuntime.claude_code,
        )
        == ()
    )

    clarification = yaml.safe_load(compose_file.read_text(encoding="utf-8"))["services"][
        "clarification"
    ]
    assert clarification["environment"]["AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES"] == (
        json.dumps([[helper_target, "/home/agent/.awf/clarification-auth/1"]])
    )
    assert clarification["volumes"] == [
        f"{aws_home}:/run/awf/clarification-auth/0:ro",
        f"{helper}:/run/awf/clarification-auth/1:ro",
    ]
