import re

with open("tests/unit/service/test_provider_recovery.py", "r") as f:
    content = f.read()

# remove the appended imports and put them at the top
appended_imports_re = re.compile(r'\nfrom awf\.service\.provider_recovery import \([^)]+\)\nfrom awf\.db\.models import Workspace\n', re.MULTILINE)
match = appended_imports_re.search(content)

if match:
    imports_str = match.group(0)
    content = content[:match.start()] + content[match.end():]
    
    # split imports
    content = imports_str + "\n" + content

with open("tests/unit/service/test_provider_recovery.py", "w") as f:
    f.write(content)
