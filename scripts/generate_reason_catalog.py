import sys
import os

# Add src to sys.path to import awf
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from awf.service.doctor.reasons import _REASON_TEXT

def generate_catalog():
    lines = [
        "# AWF Reason and Error Code Catalog\n",
        "This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.\n"
    ]
    
    # Filter out "OK" reasons or those with empty problem (message) or action
    reasons = {k: v for k, v in _REASON_TEXT.items() if v.likely_cause and v.action and v.message}
    
    for key in sorted(reasons.keys()):
        val = reasons[key]
        lines.append(f"### {key}")
        lines.append(f"**Problem:** {val.message}")
        lines.append(f"**Likely Cause:** {val.likely_cause}")
        lines.append(f"**Operator Fix:** {val.action}")
        if val.related_command:
            lines.append(f"**Related Command:** `{val.related_command}`")
        if val.docs_link:
            if val.docs_link.startswith("http"):
                lines.append(f"**Docs Link:** [{val.docs_link}]({val.docs_link})")
            elif val.docs_link.startswith("docs/REASON_CATALOG.md#"):
                anchor = val.docs_link.split("#", 1)[1]
                lines.append(f"**Docs Link:** [{val.docs_link}](#{anchor})")
            else:
                lines.append(f"**Docs Link:** [{val.docs_link}]({val.docs_link})")
        lines.append("")
        
    return "\n".join(lines)

if __name__ == "__main__":
    catalog = generate_catalog()
    catalog_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'docs', 'REASON_CATALOG.md'))
    with open(catalog_path, 'w') as f:
        f.write(catalog)
    print(f"Generated {catalog_path}")
