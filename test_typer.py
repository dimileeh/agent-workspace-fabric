import typer

_COMMON_HELP_TEXT = """
For first-time users: the recommended first path is to run `awf init`
to verify prerequisites and bootstrap your local service stack, followed by
`awf init <path>` to prepare your project repository. See docs/CLI_REFERENCE.md
for more details.

Safety defaults & Dry-run: Commands that modify local state default to
dry-runs or previews unless explicit write flags are passed.

Mutates: Local state (.env, .awf/), Docker Compose stacks, and Git/GitHub
via the async worker.
"""

app = typer.Typer(
    name="awf",
    help=f"Aira Agent Workspace Fabric — CLI operator surface.\n{_COMMON_HELP_TEXT}",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

@app.command("init", help=f"Bootstrap AWF on this machine, or run local onboarding checks for a project path.\n{_COMMON_HELP_TEXT}")
def app_init():
    pass

if __name__ == "__main__":
    app()

@app.command("other")
def other():
    pass
