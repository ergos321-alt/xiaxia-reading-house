"""Create a disposable hostile EPUB used only by the foliate POC security test."""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


MIMETYPE = "application/epub+zip"
CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
PACKAGE = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">xiaxia-security-fixture</dc:identifier>
    <dc:title>Security Fixture</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="evil" href="evil.js" media-type="text/javascript"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
NAV = """<!doctype html><html xmlns="http://www.w3.org/1999/xhtml"><body>
<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>
<li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body></html>
"""
CHAPTER = """<!doctype html><html xmlns="http://www.w3.org/1999/xhtml"><head>
<script src="evil.js"></script><script>parent.__EPUB_SCRIPT_EXECUTED = true</script>
</head><body onload="parent.__EPUB_HANDLER_EXECUTED = true">
<p id="safe">Readable security fixture text.</p>
<a href="javascript:parent.__EPUB_LINK_EXECUTED=true">unsafe link</a>
<iframe srcdoc="&lt;script&gt;parent.__EPUB_IFRAME_EXECUTED=true&lt;/script&gt;"></iframe>
</body></html>
"""
EVIL = "parent.__EPUB_EXTERNAL_SCRIPT_EXECUTED = true"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w") as archive:
        archive.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("EPUB/package.opf", PACKAGE)
        archive.writestr("EPUB/nav.xhtml", NAV)
        archive.writestr("EPUB/chapter.xhtml", CHAPTER)
        archive.writestr("EPUB/evil.js", EVIL)


if __name__ == "__main__":
    main()
