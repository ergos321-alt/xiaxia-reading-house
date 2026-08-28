#!/usr/bin/env python3
"""Exercise the Phase 2 source-first path with real EPUBs and local sinks.

The production service function is used for receive, minimal validation,
source/cover persistence, and book creation. Supabase Storage and PostgreSQL
are replaced only by bounded local sinks so this script never needs secrets or
mutates production. The independent text parser then reads the persisted raw
EPUB with asset extraction disabled.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import resource
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source_first  # noqa: E402
from epub_parser import parse_uploaded_book_path  # noqa: E402


def rss_mb() -> float:
    with open("/proc/self/statm", encoding="ascii") as status:
        pages = int(status.read().split()[1])
    return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 2)


def peak_rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        pass
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)


class LocalConnection:
    def __init__(self) -> None:
        self.book: dict[str, Any] | None = None
        self.cover_rows = 0

    def execute(self, query: str, params=()):
        normalized = " ".join(query.lower().split())
        if "insert into books" in normalized:
            self.book = {
                "id": params[0],
                "title": params[1],
                "author": params[2],
                "format": "epub",
                "source_filename": params[3],
                "cover_asset_path": params[7],
                "chapter_count": 0,
                "toc": [],
                "publication_ready": True,
                "reader_engine": "foliate",
                "text_index_status": "pending",
                "text_index_failure_code": None,
                "publication_validated_at": None,
                "text_index_updated_at": None,
                "created_at": None,
                "updated_at": None,
            }
            return LocalCursor(self.book)
        if "insert into book_assets" in normalized:
            self.cover_rows += 1
            return LocalCursor(None)
        raise AssertionError(f"unexpected validation SQL: {normalized[:120]}")


class LocalCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


def validate(path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    checkpoints = {"before_import_rss_mb": rss_mb()}
    original_fetch = source_first.db.fetch_one
    original_transaction = source_first.db.transaction
    original_upload = source_first.object_storage.upload_file
    original_delete = source_first.object_storage.delete_objects
    original_object_path = source_first.object_storage.object_path_for_asset

    with tempfile.TemporaryDirectory(prefix="rh-source-first-real-") as temp_dir:
        sink_root = Path(temp_dir) / "private-storage"
        sink_root.mkdir()
        connection = LocalConnection()

        @contextmanager
        def transaction():
            yield connection

        def upload_file(object_path: str, file_path: Path, _media_type: str):
            target = sink_root / object_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with Path(file_path).open("rb") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=256 * 1024)

        def delete_objects(object_paths: list[str]):
            for object_path in object_paths:
                (sink_root / object_path).unlink(missing_ok=True)

        source_first.db.fetch_one = lambda *_args, **_kwargs: None
        source_first.db.transaction = transaction
        source_first.object_storage.upload_file = upload_file
        source_first.object_storage.delete_objects = delete_objects
        source_first.object_storage.object_path_for_asset = (
            lambda book_id, asset_path, is_cover, digest: (
                f"books/{book_id}/cover/{digest[:16]}-{Path(asset_path).name}"
            )
        )
        try:
            with path.open("rb") as stream:
                publication = source_first.create_epub_publication(
                    FileStorage(stream=stream, filename=path.name), path.name
                )
        finally:
            source_first.db.fetch_one = original_fetch
            source_first.db.transaction = original_transaction
            source_first.object_storage.upload_file = original_upload
            source_first.object_storage.delete_objects = original_delete
            source_first.object_storage.object_path_for_asset = original_object_path

        checkpoints["after_publication_rss_mb"] = rss_mb()
        source_path = next((sink_root / "books").glob("*/source/source.epub"))
        publication_duration_ms = round((time.perf_counter() - started) * 1000)

        text_started = time.perf_counter()
        digest = hashlib.sha256()
        with source_path.open("rb") as source:
            while chunk := source.read(256 * 1024):
                digest.update(chunk)
        parsed = parse_uploaded_book_path(
            path.name,
            source_path,
            source_sha256=digest.hexdigest(),
            include_assets=False,
        )
        checkpoints["after_text_parse_rss_mb"] = rss_mb()
        block_count = sum(
            chapter.content_html.count("data-block-id=")
            for chapter in parsed.chapters
        )
        chapter_count = len(parsed.chapters)
        text_duration_ms = round((time.perf_counter() - text_started) * 1000)
        archive_facts = {
            "archive_uncompressed_size": parsed.archive_uncompressed_size,
            "archive_item_count": parsed.archive_item_count,
            "xhtml_count": parsed.xhtml_item_count,
            "image_count": parsed.image_item_count,
            "font_count": parsed.font_item_count,
            "epub_version": parsed.epub_version,
        }
        del parsed
        gc.collect()
        checkpoints["after_close_rss_mb"] = rss_mb()

    return {
        "file": path.name,
        "archive_compressed_size": path.stat().st_size,
        **archive_facts,
        "publication_result": "PASS",
        "publication_ready": bool(publication["publication_ready"]),
        "reader_engine": publication["reader_engine"],
        "initial_text_index_status": publication["text_index_status"],
        "source_persisted": True,
        "publication_duration_ms": publication_duration_ms,
        "text_index_result": "PASS",
        "text_index_duration_ms": text_duration_ms,
        "chapter_count": chapter_count,
        "block_count": block_count,
        "asset_rows_for_web": connection.cover_rows,
        "full_asset_extraction": False,
        **checkpoints,
        "peak_rss_mb": peak_rss_mb(),
        "storage_database_mode": "bounded local sinks; production adapters unit-tested",
        "workaround": "NONE",
        "total_duration_ms": round((time.perf_counter() - started) * 1000),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("epub", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.epub), ensure_ascii=False))


if __name__ == "__main__":
    main()
