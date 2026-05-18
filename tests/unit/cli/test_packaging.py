import tomllib
from pathlib import Path


def test_packaging_metadata() -> None:
    """Test that the pyproject.toml package name and entrypoints match the plan."""
    pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    # 1. project.name = "aira-awf"
    assert data["project"]["name"] == "aira-awf"

    # 2. project.scripts.awf exists as the canonical operator CLI
    scripts = data["project"].get("scripts", {})
    assert "awf" in scripts
    assert "awf-watchdog" not in scripts


def test_readme_install_paths() -> None:
    """Test that the README explicitly documents the primary install paths."""
    readme_path = Path(__file__).parents[3] / "docs" / "GETTING_STARTED.md"
    content = readme_path.read_text()

    # 3. README explicitly documents `uv tool install aira-awf` and `uv pip install aira-awf`
    assert "uv tool install aira-awf" in content, "README must document uv tool install"
    assert "uv pip install aira-awf" in content, "README must document uv pip install"

    # 4. README preserves the `git clone` path
    assert "git clone" in content, "README must preserve the git clone contributor path"
