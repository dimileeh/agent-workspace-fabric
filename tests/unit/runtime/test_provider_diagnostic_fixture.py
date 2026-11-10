"""The diagnostic oracle lives in AWF, outside the editable fixture."""

import subprocess
import sys

from awf.runtime.provider_diagnostic_fixture import (
    FIXTURE_VERSION,
    PROMPT,
    seed_fixture,
    verify_fixture,
)


def test_fixture_challenge_is_fresh_and_not_in_prompt(tmp_path):
    seed_fixture(tmp_path)
    script = "from awf.runtime.provider_diagnostic_fixture import challenge; challenge()"
    a = subprocess.check_output([sys.executable, "-c", script], text=True).strip().split("=")[1]
    b = subprocess.check_output([sys.executable, "-c", script], text=True).strip().split("=")[1]
    assert a != b and a not in PROMPT
    assert FIXTURE_VERSION
    assert not verify_fixture(tmp_path, a)
    (tmp_path / "solution.py").write_text(
        f'def answer(value):\n    return {a!r} + ":" + value[::-1]\n'
    )
    assert verify_fixture(tmp_path, a)
    assert not verify_fixture(tmp_path, b)


def test_workspace_cannot_replace_verifier_or_hang_it(tmp_path):
    seed_fixture(tmp_path)
    (tmp_path / "test_solution.py").write_text("assert True")
    (tmp_path / "json.py").write_text('raise RuntimeError("spoof")')
    assert not verify_fixture(tmp_path, "a" * 32)
    (tmp_path / "solution.py").write_text("while True: pass")
    assert not verify_fixture(tmp_path, "a" * 32, timeout=0.1)


def test_fixture_rejects_symlink_candidate(tmp_path):
    seed_fixture(tmp_path)
    (tmp_path / "solution.py").unlink()
    (tmp_path / "solution.py").symlink_to("/etc/passwd")
    assert not verify_fixture(tmp_path, "a" * 32)


def test_fixture_cannot_forge_verifier_output_and_exit_zero(tmp_path):
    seed_fixture(tmp_path)
    nonce = "a" * 32
    (tmp_path / "solution.py").write_text(
        "import os\nprint("
        + repr('["' + nonce + ':fwa", "' + nonce + ':dnoces"]')
        + ", flush=True)\nos._exit(0)\n"
    )
    assert not verify_fixture(tmp_path, nonce)


def test_runtime_oracle_requires_nonroot_and_root_owned_immutable_files(monkeypatch):
    from types import SimpleNamespace

    from awf.runtime.provider_diagnostic_fixture import runtime_is_immutable

    parent = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0))
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0), parent=parent)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    monkeypatch.setattr("os.access", lambda *_a: False)
    assert runtime_is_immutable(path=path)
    monkeypatch.setattr("os.access", lambda *_a: True)
    assert not runtime_is_immutable(path=path)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert not runtime_is_immutable(path=path)


def test_standalone_fixture_command_contract(tmp_path):
    from pathlib import Path

    import awf.runtime.provider_diagnostic_fixture as fixture

    script = Path(fixture.__file__)
    base = [sys.executable, "-I", "-S", str(script)]
    assert (
        subprocess.check_output([*base, "--contract-version"], text=True).strip() == FIXTURE_VERSION
    )
    assert subprocess.check_output([*base, "challenge"], text=True).startswith("AWF_NONCE=")
    seed_fixture(tmp_path)
    nonce = "a" * 32
    (tmp_path / "solution.py").write_text(
        f'def answer(value: str) -> str:\n    return f"{nonce}:{{value[::-1]}}"\n'
    )
    assert subprocess.run([*base, "verify", str(tmp_path), nonce], check=False).returncode == 0
    assert subprocess.run([*base, "verify", str(tmp_path), "b" * 32], check=False).returncode == 1
    assert subprocess.run([*base, "unknown-command"], check=False).returncode == 64
    assert subprocess.run([*base, "--capability"], check=False).returncode == 78


def test_command_dispatch_and_capability_are_independent_of_candidate(
    monkeypatch, tmp_path, capsys
):
    import awf.runtime.provider_diagnostic_fixture as fixture

    assert fixture.main(["--contract-version"]) == 0
    assert capsys.readouterr().out.strip() == FIXTURE_VERSION
    monkeypatch.setattr(fixture, "runtime_is_immutable", lambda: True)
    assert fixture.main(["--capability"]) == 0
    assert capsys.readouterr().out.strip() == FIXTURE_VERSION
    monkeypatch.setattr(fixture, "runtime_is_immutable", lambda: False)
    assert fixture.main(["--capability"]) == 78
    assert fixture.main(["challenge"]) == 0
    assert capsys.readouterr().out.startswith("AWF_NONCE=")
    seed_fixture(tmp_path)
    assert fixture.main(["verify", str(tmp_path), "a" * 32]) == 1
    assert fixture.main(["bad"]) == 64
