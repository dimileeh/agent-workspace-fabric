from pathlib import Path


def test_sdk_stance_in_docs() -> None:
    """
    Ensure the SDK stance is explicitly documented in the key documentation files
    for the v0.1 release.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent.parent

    docs_to_check = ["README.md", "docs/CLIENT_SURFACES.md", "docs/MCP_CLIENT_PARITY.md"]

    expected_surfaces_phrase = "REST, CLI, and MCP are the supported client surfaces for v0.1"
    expected_warning_phrase = "do not import internal AWF modules"

    for doc_path in docs_to_check:
        full_path = repo_root / doc_path
        assert full_path.exists(), f"Expected documentation file missing: {doc_path}"

        content = full_path.read_text(encoding="utf-8")

        assert expected_surfaces_phrase.lower() in content.lower(), (
            f"Missing required client surfaces stance in {doc_path}. "
            f"Expected phrase containing: '{expected_surfaces_phrase}'"
        )

        assert expected_warning_phrase.lower() in content.lower(), (
            f"Missing required warning against importing internal modules in {doc_path}. "
            f"Expected phrase containing: '{expected_warning_phrase}'"
        )
