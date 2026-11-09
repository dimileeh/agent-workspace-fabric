"""Inherited stdout/stderr writers must not pin config cleanup or cancellation."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import awf.adapters.provider_config as runtime
from awf.common.agent_execution import resolve_execution


@pytest.mark.parametrize("cancel", [False, True])
def test_runtime_bounds_inherited_pipe_drain_and_kills_process_group(tmp_path, cancel):
    selected = resolve_execution(
        agent="codex", provider="ollama", model="coder", allow_unverified=True
    )
    # The background tool ignores TERM and holds both inherited output pipes.
    descendant = (
        "import os,signal,time,pathlib; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "pathlib.Path('writer.pid').write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import os,pathlib,signal,subprocess,sys,time\n"
        "pathlib.Path('config.path').write_text(os.environ['CODEX_HOME'])\n"
        "pathlib.Path('leader.pid').write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}])\n"
        "while not pathlib.Path('writer.pid').exists(): time.sleep(.01)\n"
        "print('cli finished', flush=True)\n"
        + ("os.kill(os.getppid(),signal.SIGTERM)\ntime.sleep(60)\n" if cancel else "")
    )
    script = (
        "import runpy,sys; scope=runpy.run_path(sys.argv[1]); "
        "scope['main'].__globals__['probe_connection']=lambda *a:{'ok':True}; "
        "sys.argv=['provider_config.py',*sys.argv[2:]]; raise SystemExit(scope['main']())"
    )
    home = tmp_path / "runtime-home"
    runtime_root = home / ".awf" / "provider-config"
    runtime_root.mkdir(parents=True)
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            runtime.__file__,
            json.dumps(selected.model_dump()),
            sys.executable,
            str(agent),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(home),
            "TMPDIR": str(os_temp),
            "CODEX_HOME": str(tmp_path / "no-native-auth"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = wrapper.communicate(timeout=7)
        assert wrapper.returncode == (143 if cancel else 0), stderr.decode()
        if not cancel:
            assert b"cli finished" in stdout
        config_path = Path((tmp_path / "config.path").read_text())
        assert config_path.parent == runtime_root
        assert not config_path.exists()
        assert list(runtime_root.iterdir()) == []
        assert list(os_temp.iterdir()) == []
        for name in ("leader.pid", "writer.pid"):
            stat = Path("/proc") / (tmp_path / name).read_text() / "stat"
            assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        # Contain the pre-fix reproduction as well as a failing implementation.
        for name in ("leader.pid", "writer.pid"):
            pid = tmp_path / name
            if pid.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid.read_text()), signal.SIGKILL)
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.communicate(timeout=3)


def test_inherited_pipes_flush_redacted_partial_output_before_group_cleanup(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    secret = "synthetic-provider-credential"
    descendant = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "pathlib.Path('writer.pid').write_text(str(os.getpid())); time.sleep(60)"
    )
    leader = (
        "import os,pathlib,subprocess,sys,time\n"
        "pathlib.Path('leader.pid').write_text(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}])\n"
        "while not pathlib.Path('writer.pid').exists(): time.sleep(.01)\n"
        "sys.stdout.write('out: '+os.environ['AWF_SELECTED_PROVIDER_KEY'])\n"
        "sys.stderr.write('err: '+os.environ['AWF_SELECTED_PROVIDER_KEY'])\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
        "raise SystemExit(23)\n"
    )
    try:
        result = runtime.run_cli_redacted(
            [sys.executable, "-c", leader], {"AWF_SELECTED_PROVIDER_KEY": secret}
        )
        assert result == 23
        captured = capsys.readouterr()
        assert captured.out == "out: <redacted>"
        assert captured.err == "err: <redacted>"
        for name in ("leader.pid", "writer.pid"):
            stat = Path("/proc") / (tmp_path / name).read_text() / "stat"
            # SIGKILL delivery to a descendant is asynchronous to the leader's wait.
            deadline = time.monotonic() + 2
            while True:
                try:
                    if stat.read_text().split()[2] == "Z":
                        break
                except FileNotFoundError:
                    break
                assert time.monotonic() < deadline, "provider process survived group cleanup"
                time.sleep(0.01)
    finally:
        for name in ("leader.pid", "writer.pid"):
            pid = tmp_path / name
            if pid.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid.read_text()), signal.SIGKILL)


def test_output_failure_kills_term_resistant_cli_and_closes_pipes(tmp_path, monkeypatch):
    started = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        started.append(process)
        return process

    ready = tmp_path / "ready"
    cli = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )

    def interrupted_select(_selector, timeout):
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "CLI did not install its TERM handler"
        raise OSError("output stream interrupted")

    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime.selectors.DefaultSelector, "select", interrupted_select)
    try:
        with pytest.raises(OSError, match="^output stream interrupted$"):
            runtime.run_cli_redacted([sys.executable, "-c", cli], {})
        assert len(started) == 1
        process = started[0]
        assert process.returncode == -signal.SIGKILL
        assert process.stdout.closed and process.stderr.closed
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        for process in started:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()
