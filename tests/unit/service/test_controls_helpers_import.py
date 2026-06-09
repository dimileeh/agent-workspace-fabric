from __future__ import annotations

import subprocess
import sys


def test_controls_helpers_import_does_not_create_circular_import() -> None:
    import_code = (
        "import importlib; "
        "importlib.import_module('awf.service.controls_helpers'); "
        "importlib.import_module('awf.service.controls')"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            import_code,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
