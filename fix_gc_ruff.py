with open("tests/unit/service/test_gc.py", "r") as f:
    lines = f.readlines()

imports = []
code = []

in_import_block = False
for line in lines:
    if line.startswith("import ") or line.startswith("from "):
        imports.append(line)
        if line.endswith("(\n"):
            in_import_block = True
    elif in_import_block:
        imports.append(line)
        if line.startswith(")"):
            in_import_block = False
    elif line.strip() == "" and not in_import_block and not code:
        pass # skip leading blank lines
    else:
        code.append(line)

final_content = "".join(imports) + "\n" + "".join(code)
with open("tests/unit/service/test_gc.py", "w") as f:
    f.write(final_content)
