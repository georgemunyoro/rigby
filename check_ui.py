"""Parse the control UI's JavaScript.

The page is a Python string, so a stray escape becomes a real newline at import
and silently breaks the whole script -- the page still renders, but nothing
works and no error is visible. Cheap to check, expensive to debug.

    uv run python check_ui.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rigby.control import page


def main() -> int:
    html = page()
    if "__SLIDERS__" in html or "__OPTS__" in html:
        print("FAIL: placeholders were not substituted")
        return 1
    try:
        js = html.split("<script>")[1].split("</script>")[0]
    except IndexError:
        print("FAIL: no <script> block in page")
        return 1

    for i, line in enumerate(js.split("\n"), 1):
        if line.count("'") % 2 or line.count('"') % 2:
            print(f"FAIL: unbalanced quotes on JS line {i}: {line.strip()[:80]}")
            return 1

    node = shutil.which("node")
    if not node:
        print(f"ok (quote balance only, {len(js)} bytes) -- node not installed")
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        tmp = Path(f.name)
    try:
        r = subprocess.run([node, "--check", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode:
            print("FAIL: JS does not parse\n" + (r.stderr or r.stdout))
            return 1
    finally:
        tmp.unlink(missing_ok=True)

    print(f"ok: page renders, {len(js)} bytes of JS parse cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
