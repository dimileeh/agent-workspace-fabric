#!/usr/bin/env python3
"""Generate or verify the stable OpenAPI spec artifact.

Usage:
    python scripts/generate_openapi.py           # Write openapi.json to repo root
    python scripts/generate_openapi.py --check    # Exit 1 if generated spec diverges

Exit codes:
    0 — success (spec generated or matches checked-in file)
    1 — drift detected (--check mode) or generation error
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_JSON = REPO_ROOT / "openapi.json"

sys.path.insert(0, str(REPO_ROOT / "src"))


def get_openapi_spec() -> dict:
    from awf.api.app import create_app

    app = create_app(use_lifespan=False)
    return app.openapi()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate or verify the AWF OpenAPI spec artifact.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that the generated spec matches the checked-in openapi.json. Exit 1 on drift.",
    )
    args = parser.parse_args()

    try:
        spec = get_openapi_spec()
    except Exception as exc:
        print(
            f"ERROR: Failed to generate OpenAPI spec: {exc}\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            file=sys.stderr,
        )
        sys.exit(1)

    current = json.dumps(spec, indent=2, sort_keys=True)

    if args.check:
        if not OPENAPI_JSON.exists():
            print(
                "ERROR: openapi.json not found. Run `python scripts/generate_openapi.py` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        checked_in = OPENAPI_JSON.read_text()
        if current.strip() != checked_in.strip():
            print(
                "ERROR: openapi.json has drifted from the current app spec. "
                "Run `python scripts/generate_openapi.py` to update it.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("OK: openapi.json matches the current app spec.")
    else:
        OPENAPI_JSON.write_text(current + "\n")
        print(f"OK: Wrote openapi.json ({len(current)} bytes)")


if __name__ == "__main__":
    main()
