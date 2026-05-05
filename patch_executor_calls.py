import re

with open("src/awf/control/executor.py", "r") as f:
    content = f.read()

# 1. Update in execute for validation
content = re.sub(
    r'(setup_result = await self._validation.run_profile_phases\()',
    r'await self._update_subphase(workspace_id, "validation")\n            \1',
    content,
    count=1
)

# 2. Update in _run_agent_task_with_optional_planning for agent without planning
content = re.sub(
    r'(        if not planning.required:\n            )(result = await adapter.run\()',
    r'\1await self._update_subphase(workspace.id, "agent")\n            \2',
    content,
    count=1
)

# 3. Update for planning
content = re.sub(
    r'(        plan_result = await adapter.run\()',
    r'await self._update_subphase(workspace.id, "planning")\n\1',
    content,
    count=1
)

# 4. Update for agent execution
content = re.sub(
    r'(            execute_result = await adapter.run\()',
    r'await self._update_subphase(workspace.id, "agent")\n\1',
    content,
    count=1
)

# 5. Update for conformance compare
content = re.sub(
    r'(            compare_result = await adapter.run\()',
    r'await self._update_subphase(workspace.id, "conformance")\n\1',
    content,
    count=1
)

with open("src/awf/control/executor.py", "w") as f:
    f.write(content)
print("Patched executor subphase calls")
