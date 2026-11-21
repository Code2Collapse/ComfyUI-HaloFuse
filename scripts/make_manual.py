"""Generate the HaloFuse PDF manuals from Markdown — run at every release.

Renders the Markdown with markdown-it-py and prints to PDF with headless Edge or
Chrome, so no Pandoc or LaTeX is needed. Defaults to docs/manual.md ->
docs/manual.pdf; the runtime repo's USER-GUIDE.pdf is rendered the same way with
--source/--output.

Usage (any Python with markdown-it-py, e.g. ComfyUI's embedded Python):

    python scripts/make_manual.py
    python scripts/make_manual.py --source ../runtime/USER-GUIDE.md --output ../runtime/USER-GUIDE.pdf

The PDF title is the document's first "# " heading. Before rendering, the source
is refused if it names the cloud provider (user-facing docs are HaloFuse only).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "manual.md"
OUTPUT = ROOT / "docs" / "manual.pdf"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

CSS = """
@page { size: A4; margin: 22mm 18mm; }
body { font-family: 'Segoe UI', system-ui, sans-serif; font-size: 10.5pt;
       line-height: 1.5; color: #1a1a1a; max-width: 46em; margin: 0 auto; }
h1 { font-size: 20pt; border-bottom: 2px solid #7c5cff; padding-bottom: 6px; }
h2 { font-size: 14pt; margin-top: 1.6em; border-bottom: 1px solid #ddd;
     padding-bottom: 3px; }
h3 { font-size: 11.5pt; margin-top: 1.3em; }
code { font-family: Consolas, monospace; font-size: 9.5pt;
       background: #f4f2fa; padding: 1px 4px; border-radius: 3px; }
pre { background: #f4f2fa; border: 1px solid #e0dcee; border-radius: 4px;
      padding: 8px 10px; overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; }
th { background: #f4f2fa; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #ddd; }
img { max-width: 100%; border: 1px solid #ddd; border-radius: 4px; }
h2, h3 { page-break-after: avoid; }
pre, table { page-break-inside: avoid; }
"""


def find_browser() -> str:
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    sys.exit("No Edge or Chrome found for PDF printing; install one or adjust BROWSERS.")


def version_from_pyproject() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


# Words that must not appear in a user-facing manual (NEW_WORK section 1).
# Provider / original-project words the manual must not contain, held as SHA-256 so this public script does not
# spell them out (the same approach as the tests' provider-domain check). A token matches when its lower-case
# form is listed, or when it starts with the listed case-sensitive five-letter brand prefix.
_FORBIDDEN_WORD_SHA256 = frozenset({
    "57e9f891304a9efece2564bc54afaf7463218e0627eb47898f438a42c3345aad",
    "a815aeb11c309bd999fa4806388180bbd448977cf62faa28adeba4c18e72e3af",
    "a8cc6d6767ce34c26b8e95f5f44822be2b9e1d6a39f1e1b972d90c6426dd2cd3",
    "fd49c86e53639b035ca1eb9bb765bdb419e1ee6dd5b15df8ae52ed850c401d93",
})
_FORBIDDEN_PREFIX5_SHA256 = frozenset({"529bc3b07127ecb7e53a4dcf1991d9152c24537d919178022b2c42657f79a26b"})


def forbidden_words(text: str) -> list[str]:
    """Masked positions of every forbidden token (never the word itself)."""
    hits = []
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9-]*", text):
        tok = m.group(0)
        if (hashlib.sha256(tok.lower().encode()).hexdigest() in _FORBIDDEN_WORD_SHA256
                or hashlib.sha256(tok[:5].encode()).hexdigest() in _FORBIDDEN_PREFIX5_SHA256):
            hits.append(f"<forbidden word>@{m.start()}")
    return hits


_RELATIVE_LINK = re.compile(r'<a href="(?!https?:|mailto:|#)[^"]*">(.*?)</a>', re.S)


def unlink_relative(html: str) -> str:
    """Relative links point at repository files that do not travel with the PDF, and through <base> they would be
    printed as file:/// URIs carrying this computer's folder names. Keep their text, drop the link."""
    return _RELATIVE_LINK.sub(r"\1", html)


def title_of(md_text: str) -> str:
    m = re.search(r"^#\s+(.+)$", md_text, re.M)
    return m.group(1).strip() if m else "HaloFuse"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=SOURCE)
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args()
    source, output = args.source.resolve(), args.output.resolve()

    md_text = source.read_text(encoding="utf-8")
    bad = forbidden_words(md_text)
    if bad:
        sys.exit(f"{source.name} names the provider ({', '.join(bad[:10])}); fix the text before rendering.")
    version = version_from_pyproject()
    if source == SOURCE.resolve() and version not in md_text[:300]:
        print(f"warning: the manual's header does not mention version {version}")

    body = unlink_relative(MarkdownIt("commonmark").enable("table").render(md_text))
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        # Relative image paths (docs/images/...) resolve against the Markdown file's folder, not the temp dir.
        f"<base href='{source.parent.as_uri()}/'>"
        f"<title>{title_of(md_text)}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )

    with tempfile.TemporaryDirectory(prefix="halofuse-manual-") as tmp:
        html_path = Path(tmp) / "manual.html"
        html_path.write_text(html, encoding="utf-8")
        browser = find_browser()
        result = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={output}", html_path.as_uri()],
            capture_output=True, text=True, timeout=120,
        )
        if not output.is_file():
            sys.exit(f"PDF was not produced.\nstdout: {result.stdout}\nstderr: {result.stderr}")
    print(f"wrote {output} ({output.stat().st_size} bytes) for version {version}")


if __name__ == "__main__":
    main()
