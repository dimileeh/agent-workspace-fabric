import pytest
import subprocess
from collections import namedtuple

CompletedProcess = namedtuple('CompletedProcess', ['returncode', 'stdout', 'stderr'])
original_run = subprocess.run

def _mock_run(args, **kwargs):
    if len(args) > 0 and args[0] == "docker" and any("command -v" in str(a) for a in args):
        return CompletedProcess(returncode=0, stdout="/usr/bin/cli\n", stderr="")
    return original_run(args, **kwargs)

subprocess.run = _mock_run
