import re
import glob
import os

files = [
    "tests/unit/runtime/test_release_pr_monitor.py",
    "tests/integration/runtime/test_pr_monitor_runner.py",
    "tests/integration/runtime/test_pr_monitor_recovery_cycle.py",
    "tests/unit/control/test_executor_error_paths.py",
    "scripts/salvage_workspace.py"
]

for file in files:
    if not os.path.exists(file):
        continue
    with open(file, "r") as f:
        content = f.read()
    
    # We want to replace `@property\n    def name(self)` with `@property\n    def provider(self) -> str:\n        return "fake"\n\n    @property\n    def name(self)`
    # Or similarly for other indentation levels.

    new_content = re.sub(
        r"(\s*)@property\n\s*def name\(self\)",
        r'\1@property\n\1def provider(self) -> str:\n\1    return "fake"\n\1@property\n\1def name(self)',
        content
    )

    if new_content != content:
        with open(file, "w") as f:
            f.write(new_content)
        print(f"Patched {file}")
    else:
        print(f"No match in {file}")

