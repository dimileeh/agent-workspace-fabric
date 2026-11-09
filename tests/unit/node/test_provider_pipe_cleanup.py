"""Inherited stdout/stderr writers must not pin config cleanup or cancellation."""

import contextlib
import json
import os
import signal
import subprocess
import sys
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = wrapper.communicate(timeout=7)
        assert wrapper.returncode == (143 if cancel else 0), stderr.decode()
        if not cancel:
            assert b"cli finished" in stdout
        assert not Path((tmp_path / "config.path").read_text()).exists()
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
