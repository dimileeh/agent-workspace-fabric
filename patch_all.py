import re
import glob
import os

files = glob.glob("tests/**/*.py", recursive=True) + ["scripts/salvage_workspace.py"]

for file in files:
    if not os.path.exists(file):
        continue
    with open(file, "r") as f:
        content = f.read()

    # Find all places with `@property\n\s*def name(self) -> AgentRuntime`
    # and if they don't have `def provider`, add it right above.
    
    parts = []
    
    last_idx = 0
    for match in re.finditer(r'([ \t]*)@property\n[ \t]*def name\(self\)', content):
        start = match.start()
        # Look around to see if `def provider` exists within a few lines.
        # Check backwards 500 characters and forwards 500 characters.
        context = content[max(0, start - 500):min(len(content), start + 500)]
        if "def provider" not in context:
            parts.append(content[last_idx:start])
            indent = match.group(1)
            parts.append(f'{indent}@property\n{indent}def provider(self) -> str:\n{indent}    return "fake"\n\n')
            last_idx = start
            
    if last_idx > 0:
        parts.append(content[last_idx:])
        new_content = "".join(parts)
        with open(file, "w") as f:
            f.write(new_content)
        print(f"Patched {file}")

