import re

with open("src/awf/control/executor.py", "r") as f:
    content = f.read()

content = content.replace(
    '            await self._update_subphase(workspace_id, "validation")\n            setup_result = await self._validation.run_profile_phases(',
    '            setup_result = await self._validation.run_profile_phases('
)

content = content.replace(
    '                val_result = await self._validation.run_profile_phases(\n',
    '                await self._update_subphase(workspace_id, "validation")\n                val_result = await self._validation.run_profile_phases(\n'
)

with open("src/awf/control/executor.py", "w") as f:
    f.write(content)
print("Patched validation subphase")
