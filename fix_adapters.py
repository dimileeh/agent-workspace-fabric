import os
import re

files_to_fix = [
    "tests/unit/runtime/_monitor_runner_fixtures.py",
    "tests/unit/runtime/test_release_pr_monitor.py",
    "tests/unit/control/test_executor_error_paths.py",
    "tests/integration/runtime/test_pr_monitor_runner.py",
    "tests/integration/runtime/test_pr_monitor_recovery_cycle.py",
    "scripts/salvage_workspace.py"
]

for filepath in files_to_fix:
    with open(filepath, 'r') as f:
        content = f.read()

    # Match @property followed by def provider(self) -> str:
    # and replace with def get_provider(self, model: str | None) -> str:
    # ensuring indentation is preserved.
    content = re.sub(
        r'([ \t]*)@property\n[ \t]*def provider\(self\) -> str:',
        r'\1def get_provider(self, model: str | None) -> str:',
        content
    )

    with open(filepath, 'w') as f:
        f.write(content)

