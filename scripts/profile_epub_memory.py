#!/usr/bin/env python3
"""Profile real EPUBs with the production parser and bounded local sinks."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import resource
import sys
import tempfile
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from epub_parser import parse_uploaded_book_path  # noqa: E402


def rss_mb() -> float:
    with open("/proc/self/statm", encoding="ascii") as status:
        resident_pages = int(status.read().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def profile(path: Path) -> dict[str, object]:
    started = time.perf_counter()
    stages: dict[str, float] = {}

    def mark(stage: str) -> None:
        stages[f"{stage}_rss_mb"] = round(rss_mb(), 2)

    mark("parse_before")
    parse_started = time.perf_counter()
    parsed = parse_uploaded_book_path(
        path.name, path, stage_observer=mark
    )
    mark("parse_after")
    parse_duration = time.perf_counter() - parse_started

    with tempfile.TemporaryDirectory(prefix="rh-profile-") as temp_dir:
        temp_root = Path(temp_dir)
        staged: list[tuple[Path, Path]] = []
        for index, chapter in enumerate(parsed.chapters):
            html_path = temp_root / f"{index:06d}.html"
            text_path = temp_root / f"{index:06d}.txt"
            html_path.write_text(chapter.content_html, encoding="utf-8")
            text_path.write_text(chapter.content_text, encoding="utf-8")
            chapter.content_html = ""
            chapter.content_text = ""
            staged.append((html_path, text_path))
        gc.collect()
        mark("normalized_ready")

        storage_started = time.perf_counter()
        mark("storage_before")
        source_digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(256 * 1024):
                source_digest.update(chunk)
        unique_assets = {
            asset.content_sha256: asset
            for asset in parsed.assets
            if asset.content_sha256 and asset.archive_path
        }
        with zipfile.ZipFile(path) as archive:
            for asset in unique_assets.values():
                with archive.open(asset.archive_path) as source:
                    while source.read(256 * 1024):
                        pass
        mark("storage_after")
        storage_duration = time.perf_counter() - storage_started

        database_started = time.perf_counter()
        mark("database_before")
        persisted_bytes = 0
        for html_path, text_path in staged:
            persisted_bytes += len(html_path.read_bytes())
            persisted_bytes += len(text_path.read_bytes())
        mark("database_after")
        database_duration = time.perf_counter() - database_started

    return {
        "filename": path.name,
        "archive_compressed_size": path.stat().st_size,
        "archive_uncompressed_size": parsed.archive_uncompressed_size,
        "archive_item_count": parsed.archive_item_count,
        "xhtml_count": parsed.xhtml_item_count,
        "image_count": parsed.image_item_count,
        "font_count": parsed.font_item_count,
        "chapter_count": len(parsed.chapters),
        "asset_count": len(parsed.assets),
        "unique_asset_upload_count": len(unique_assets),
        "resident_asset_bytes": sum(len(asset.data or b"") for asset in parsed.assets),
        "persisted_chapter_bytes": persisted_bytes,
        "parse_duration": round(parse_duration, 4),
        "storage_sink_duration": round(storage_duration, 4),
        "database_sink_duration": round(database_duration, 4),
        "total_duration": round(time.perf_counter() - started, 4),
        **stages,
        "peak_rss_mb": round(peak_rss_mb(), 2),
        "note": "Storage and DB timings use bounded local sinks; production logs measure real providers.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("epub", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.epub:
        print(json.dumps(profile(path), ensure_ascii=False))


if __name__ == "__main__":
    main()
