import re

with open("tests/unit/control/test_executor.py", "r") as f:
    content = f.read()

assert_logic = """            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None"""

content = content.replace(
    "            ws = await WorkspaceRepository(s).get(ws_id)\n            assert ws is not None\n            assert ws.status == WorkspaceStatus.completed.value",
    assert_logic
)

with open("tests/unit/control/test_executor.py", "w") as f:
    f.write(content)
print("Patched test_executor.py")
