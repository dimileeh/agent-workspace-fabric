import re

with open("src/awf/cli/main.py", "r") as f:
    content = f.read()

# Add the constants
provider_help_defs = """_PROVIDER_HELP = (
    "Repeatable provider strictness check: github, codex, claude_code, "
    "gemini, opencode, or docker."
)
_PROVIDER_HELP_PASSTHROUGH = (
    "Repeatable provider strictness check passed through to local "
    "service bootstrap: github, codex, claude_code, gemini, opencode, "
    "or docker."
)
"""

content = content.replace('_DX_HELP = "DX smoke proof: validate local service, profile, and PR path."\n', '_DX_HELP = "DX smoke proof: validate local service, profile, and PR path."\n' + provider_help_defs + '\n')

# Replace the occurrences
old_help_normal = """        help=(
            "Repeatable provider strictness check: github, codex, claude_code, "
            "gemini, opencode, or docker."
        ),"""
new_help_normal = """        help=_PROVIDER_HELP,"""

content = content.replace(old_help_normal, new_help_normal)

old_help_passthrough = """        help=(
            "Repeatable provider strictness check passed through to local "
            "service bootstrap: github, codex, claude_code, gemini, opencode, "
            "or docker."
        ),"""
new_help_passthrough = """        help=_PROVIDER_HELP_PASSTHROUGH,"""

content = content.replace(old_help_passthrough, new_help_passthrough)

with open("src/awf/cli/main.py", "w") as f:
    f.write(content)
