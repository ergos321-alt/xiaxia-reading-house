"""Read-only structural inventory for real EPUB POC corpus files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile


XHTML_TYPES = {"application/xhtml+xml", "text/html"}
IMAGE_PREFIX = "image/"
FONT_TYPES = {
    "application/font-sfnt",
    "application/font-woff",
    "application/vnd.ms-opentype",
    "font/otf",
    "font/ttf",
    "font/woff",
    "font/woff2",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def inspect_epub(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        uncompressed = sum(info.file_size for info in infos)
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next(
            element
            for element in container.iter()
            if local_name(element.tag) == "rootfile"
        )
        opf_path = rootfile.attrib["full-path"]
        opf = ET.fromstring(archive.read(opf_path))
        opf_dir = PurePosixPath(opf_path).parent
        manifest = {}
        for element in opf.iter():
            if local_name(element.tag) != "item":
                continue
            href = element.attrib.get("href", "")
            resolved = str(opf_dir / unquote(href.split("#", 1)[0]))
            manifest[element.attrib.get("id", "")] = {
                "href": href,
                "path": resolved,
                "media_type": element.attrib.get("media-type", ""),
                "properties": element.attrib.get("properties", "").split(),
            }
        spine = [
            element.attrib.get("idref", "")
            for element in opf.iter()
            if local_name(element.tag) == "itemref"
        ]
        xhtml = [item for item in manifest.values() if item["media_type"] in XHTML_TYPES]
        images = [item for item in manifest.values() if item["media_type"].startswith(IMAGE_PREFIX)]
        fonts = [item for item in manifest.values() if item["media_type"] in FONT_TYPES]
        nav = next((item for item in manifest.values() if "nav" in item["properties"]), None)
        ncx = next((item for item in manifest.values() if item["media_type"] == "application/x-dtbncx+xml"), None)
        internal_links = 0
        noterefs = 0
        encoded_hrefs = 0
        missing_manifest_items = sum(
            1 for item in manifest.values() if item["path"] not in names
        )
        for item in xhtml:
            if item["path"] not in names:
                continue
            text = archive.read(item["path"]).decode("utf-8", errors="replace")
            hrefs = re.findall(r"\bhref\s*=\s*['\"]([^'\"]+)", text, re.I)
            internal_links += sum(
                1 for href in hrefs
                if href.startswith("#") or ".xhtml" in href.lower() or ".html" in href.lower()
            )
            encoded_hrefs += sum(1 for href in hrefs if "%" in href)
            noterefs += len(re.findall(r"(?:epub:type|role)\s*=\s*['\"][^'\"]*(?:noteref|doc-noteref)", text, re.I))
        return {
            "file": path.name,
            "size": path.stat().st_size,
            "uncompressed_total": uncompressed,
            "item_count": len(infos),
            "epub_version": opf.attrib.get("version", "unknown"),
            "manifest_items": len(manifest),
            "spine_items": len(spine),
            "xhtml_count": len(xhtml),
            "images": len(images),
            "fonts": len(fonts),
            "nav_type": "NAV" if nav else "NCX" if ncx else "fallback",
            "internal_link_refs": internal_links,
            "noteref_refs": noterefs,
            "encoded_hrefs": encoded_hrefs,
            "missing_manifest_items": missing_manifest_items,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps(
        [inspect_epub(path) for path in args.files],
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
