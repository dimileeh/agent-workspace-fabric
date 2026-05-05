import re

with open("tests/unit/db/test_repository_coverage.py", "r") as f:
    content = f.read()

assert_logic = """
    assert closed_again.closed_at == first_closed_at
    await session.refresh(workspace)
    assert workspace.last_log_at is not None
    assert workspace.last_activity_at is not None
"""

content = content.replace(
    "    assert closed_again.closed_at == first_closed_at\n",
    assert_logic
)

with open("tests/unit/db/test_repository_coverage.py", "w") as f:
    f.write(content)
print("Patched test_repository_coverage.py")
