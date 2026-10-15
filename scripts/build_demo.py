#!/usr/bin/env python3
"""Build the static demo site into docs/ for GitHub Pages.

The real app needs the Python server. The demo swaps the server for demo.js, which answers the page's /api/ calls in the
browser with made-up players. Run this again after changing the page, then commit docs/.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"
SHARED = ["app.js", "celebrate.js", "ics.js", "sun.js", "teams.js", "theme.js", "styles.css", "demo.js"]


def build(out=OUT, root=ROOT):
    out = Path(out)
    out.mkdir(exist_ok=True)
    for name in SHARED:
        shutil.copyfile(root / name, out / name)
    page = (root / "index.html").read_text(encoding="utf-8")
    page = re.sub(r'(src|href)="/([A-Za-z0-9_.-]+\.(?:js|css))"', r'\1="\2"', page)        # relative, so it works under /pickup-poll/
    marker = '<script src="app.js" defer></script>'
    if marker not in page:
        raise SystemExit("index.html no longer has the app.js script tag")
    page = page.replace(marker, '<script src="demo.js" defer></script>\n  ' + marker, 1)     # demo.js must run first
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return sorted(p.name for p in out.iterdir())


if __name__ == "__main__":
    print("built docs/:", ", ".join(build()))
    sys.exit(0)
