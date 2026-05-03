import re
from pathlib import Path

CATALOG_PATH = Path("docs/REASON_CATALOG.md")
catalog_content = CATALOG_PATH.read_text()
documented_codes: set[str] = set(re.findall(r"^###\s+([A-Z0-9_]+)", catalog_content, re.MULTILINE))

from tests.unit.docs.test_catalog_coverage import ALLOWLIST

for code in ALLOWLIST:
    if code in documented_codes:
        print(f"Documented but in ALLOWLIST: {code}")
