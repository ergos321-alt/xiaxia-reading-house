"""Source-first EPUB publication creation and independent text indexing."""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import resource
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

import database as db
import storage as object_storage
from epub_parser import (
    BookParseError,
    PublicationInspection,
    inspect_epub_publication_path,
    parse_uploaded_book_path,
)


logger = logging.getLogger(__name__)
SOURCE_STREAM_CHUNK_BYTES = 1024 * 1024
COVER_STREAM_CHUNK_BYTES = 256 * 1024
MAX_COVER_BYTES = 20 * 1024 * 1024
TEXT_INDEX_DEADLINE_SECONDS = 105
_TEXT_INDEX_LOCK = threading.BoundedSemaphore(1)


@dataclass(slots=True)
class SourceFirstFailure(RuntimeError):
    code: str
    message: str
    status: int
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def create_epub_publication(upload: Any, filename: str) -> dict[str, Any]:
    """Persist a browser-readable publication without building its text index."""
    started = time.perf_counter()
    book_id = uuid4()
    source_object_path = f"books/{book_id}/source/source.epub"
    uploaded_objects: list[str] = []
    with tempfile.TemporaryDirectory(prefix="rh-source-first-") as temp_dir:
        temp_root = Path(temp_dir)
        source_path = temp_root / "source.epub"
        source_size, source_sha256 = _spool_upload(upload, source_path)
        _log(
            "publication_receive",
            book_id=book_id,
            source_sha256=source_sha256,
            stage="receive_source",
            duration_ms=_duration_ms(started),
        )
        try:
            inspection = inspect_epub_publication_path(
                filename, source_path, source_sha256=source_sha256
            )
        except BookParseError as exc:
            _log(
                "publication_failed",
                book_id=book_id,
                source_sha256=source_sha256,
                stage="minimal_validation",
                error_code=exc.code,
                duration_ms=_duration_ms(started),
            )
            raise SourceFirstFailure(exc.code, str(exc), 422) from exc

        try:
            existing = db.fetch_one(
                """
                select id, title, text_index_status
                from books where source_sha256 = %s
                """,
                (source_sha256,),
            )
        except Exception as exc:
            raise SourceFirstFailure(
                "database_write_failed",
                "暂时无法核对书架，本次没有保存任何文件",
                500,
            ) from exc
        if existing:
            raise SourceFirstFailure(
                "book_already_exists",
                "这本书已经在书架中，可以重新整理夏夏阅读索引",
                409,
                {
                    "book_id": str(existing["id"]),
                    "title": existing["title"],
                    "text_index_status": existing.get("text_index_status"),
                    "retryable": existing.get("text_index_status") == "failed",
                },
            )

        cover_record = None
        try:
            object_storage.upload_file(
                source_object_path, source_path, "application/epub+zip"
            )
            uploaded_objects.append(source_object_path)
        except object_storage.ObjectStorageError as exc:
            _cleanup([source_object_path])
            raise SourceFirstFailure(
                "storage_upload_failed",
                "原始 EPUB 无法安全保存，本次没有创建书籍",
                502,
            ) from exc

        if inspection.cover_archive_path and inspection.cover_byte_size <= MAX_COVER_BYTES:
            try:
                cover_record = _persist_optional_cover(
                    book_id, source_path, temp_root, inspection
                )
                if cover_record:
                    uploaded_objects.append(cover_record[1])
            except Exception:
                # Cover is decorative. An invalid or unavailable cover never
                # changes publication readiness.
                logger.warning(
                    "source_first optional_cover_skipped book_id=%s", book_id,
                    exc_info=True,
                )
                cover_record = None

        try:
            with db.transaction() as conn:
                book = conn.execute(
                    """
                    insert into books (
                        id, title, author, format, source_filename, source_sha256,
                        source_object_path, source_media_type, source_byte_size,
                        cover_asset_path, chapter_count, toc,
                        publication_ready, reader_engine, text_index_status,
                        publication_validated_at, text_index_updated_at
                    ) values (
                        %s, %s, %s, 'epub', %s, %s, %s,
                        'application/epub+zip', %s, %s, 0, %s,
                        true, 'foliate', 'pending', now(), now()
                    )
                    returning id, title, author, format, source_filename,
                              cover_asset_path, chapter_count, toc,
                              publication_ready, reader_engine, text_index_status,
                              text_index_failure_code, publication_validated_at,
                              text_index_updated_at, created_at, updated_at
                    """,
                    (
                        book_id,
                        inspection.title,
                        inspection.author,
                        filename,
                        source_sha256,
                        source_object_path,
                        source_size,
                        cover_record[0] if cover_record else None,
                        Jsonb([]),
                    ),
                ).fetchone()
                if cover_record:
                    conn.execute(
                        """
                        insert into book_assets (
                            book_id, asset_path, object_path, media_type, byte_size
                        ) values (%s, %s, %s, %s, %s)
                        """,
                        (
                            book_id,
                            cover_record[0],
                            cover_record[1],
                            cover_record[2],
                            cover_record[3],
                        ),
                    )
        except UniqueViolation as exc:
            _cleanup(uploaded_objects)
            existing = db.fetch_one(
                "select id, title, text_index_status from books where source_sha256 = %s",
                (source_sha256,),
            )
            raise SourceFirstFailure(
                "book_already_exists",
                "这本书已经在书架中，可以重新整理夏夏阅读索引",
                409,
                {
                    "book_id": str(existing["id"]) if existing else None,
                    "title": existing["title"] if existing else inspection.title,
                    "text_index_status": (
                        existing.get("text_index_status") if existing else None
                    ),
                },
            ) from exc
        except Exception as exc:
            _cleanup(uploaded_objects)
            logger.exception("source_first database create failed book_id=%s", book_id)
            raise SourceFirstFailure(
                "database_write_failed",
                "书籍记录无法安全创建，已清理本次原始文件",
                500,
            ) from exc

        _log(
            "publication_ready",
            book_id=book_id,
            source_sha256=source_sha256,
            publication_ready=True,
            text_index_status="pending",
            stage="publication_commit",
            duration_ms=_duration_ms(started),
            chapter_count=0,
            block_count=0,
            archive_item_count=inspection.archive_item_count,
            xhtml_count=inspection.xhtml_item_count,
            image_count=inspection.image_item_count,
            font_count=inspection.font_item_count,
        )
        return dict(book)


def build_text_index(book_id: UUID) -> dict[str, Any]:
    """Build chapters atomically; failures only change the readiness state."""
    if not _TEXT_INDEX_LOCK.acquire(blocking=False):
        raise SourceFirstFailure(
            "text_index_resource_limit",
            "另一本书正在整理，请稍后再试",
            429,
        )
    started = time.perf_counter()
    source_sha256 = None
    try:
        book = db.fetch_one(
            """
            select id, title, format, source_filename, source_sha256,
                   source_object_path, publication_ready, text_index_status
            from books where id = %s
            """,
            (book_id,),
        )
        if not book:
            raise SourceFirstFailure("book_not_found", "没有找到这本书", 404)
        source_sha256 = book["source_sha256"]
        if not book["publication_ready"] or not book.get("source_object_path"):
            raise SourceFirstFailure(
                "publication_not_ready", "这本书没有可用的原始 EPUB", 409
            )
        if book["format"] != "epub":
            raise SourceFirstFailure(
                "text_index_unsupported_format", "该格式不使用 EPUB 文本整理", 422
            )
        if book["text_index_status"] == "ready":
            return _readiness_row(book_id)

        claimed = db.execute(
            """
            update books
            set text_index_status = 'processing',
                text_index_failure_code = null,
                text_index_failure_detail = null,
                text_index_updated_at = now()
            where id = %s and (
                text_index_status <> 'processing'
                or text_index_updated_at < now() - interval '15 minutes'
            )
            returning id
            """,
            (book_id,),
        )
        if not claimed:
            raise SourceFirstFailure(
                "text_index_in_progress", "夏夏正在整理这本书", 409
            )

        with tempfile.TemporaryDirectory(prefix="rh-text-index-") as temp_dir:
            temp_root = Path(temp_dir)
            source_path = temp_root / "source.epub"
            _log_index(book_id, source_sha256, "source_download", started)
            try:
                object_storage.download_to_file(
                    book["source_object_path"], source_path
                )
            except object_storage.ObjectStorageError as exc:
                return _fail_index(
                    book_id,
                    source_sha256,
                    "text_index_internal_error",
                    "source_download_failed",
                    started,
                    exc,
                )

            deadline = time.perf_counter() + TEXT_INDEX_DEADLINE_SECONDS
            try:
                parsed = parse_uploaded_book_path(
                    book["source_filename"],
                    source_path,
                    deadline,
                    source_sha256=source_sha256,
                    include_assets=False,
                    stage_observer=lambda stage: _log_index(
                        book_id, source_sha256, stage, started
                    ),
                )
            except BookParseError as exc:
                error_code = _parse_failure_code(exc.code)
                return _fail_index(
                    book_id,
                    source_sha256,
                    error_code,
                    exc.code,
                    started,
                    exc,
                )
            except MemoryError as exc:
                return _fail_index(
                    book_id,
                    source_sha256,
                    "text_index_resource_limit",
                    "memory_error",
                    started,
                    exc,
                )

            chapter_paths = _stage_chapters(parsed.chapters, temp_root)
            chapter_ids = [uuid4() for _chapter in parsed.chapters]
            block_count = sum(
                html_path.read_text(encoding="utf-8").count("data-block-id=")
                for html_path, _text_path in chapter_paths
            )
            try:
                with db.transaction() as conn:
                    locked = conn.execute(
                        "select id, text_index_status from books where id = %s for update",
                        (book_id,),
                    ).fetchone()
                    if not locked:
                        raise RuntimeError("book_deleted_during_text_index")
                    traces = conn.execute(
                        """
                        select
                          (select count(*) from annotations where book_id = %s)
                          + (select count(*) from xiaxia_thoughts where book_id = %s)
                          as anchored_count
                        """,
                        (book_id, book_id),
                    ).fetchone()
                    existing_chapters = conn.execute(
                        "select count(*) as total from chapters where book_id = %s",
                        (book_id,),
                    ).fetchone()
                    if (
                        int(existing_chapters["total"] or 0) > 0
                        and int(traces["anchored_count"] or 0) > 0
                    ):
                        raise SourceFirstFailure(
                            "text_index_mapping_failed",
                            "已有稳定书页锚点，不能自动重建索引",
                            409,
                        )
                    # A prior failed transaction should leave no rows. This
                    # cleanup is the idempotent safety net for older partial data.
                    conn.execute("delete from reading_progress where book_id = %s", (book_id,))
                    conn.execute("delete from ai_reading_state where book_id = %s", (book_id,))
                    conn.execute("delete from chapters where book_id = %s", (book_id,))
                    db.execute_many(
                        conn,
                        """
                        insert into chapters (
                            id, book_id, chapter_index, title, href,
                            content_html, content_text, word_count
                        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            (
                                chapter_ids[index],
                                book_id,
                                chapter.chapter_index,
                                chapter.title,
                                chapter.href,
                                chapter_paths[index][0].read_text(encoding="utf-8"),
                                chapter_paths[index][1].read_text(encoding="utf-8"),
                                chapter.word_count,
                            )
                            for index, chapter in enumerate(parsed.chapters)
                        ),
                    )
                    conn.execute(
                        """
                        insert into reading_progress (
                            book_id, chapter_id, chapter_index, position, percentage
                        ) values (%s, %s, 0, %s, 0)
                        """,
                        (
                            book_id,
                            chapter_ids[0],
                            Jsonb(
                                {
                                    "block_id": "b000001",
                                    "char_offset": 0,
                                    "scroll_fraction": 0,
                                }
                            ),
                        ),
                    )
                    conn.execute(
                        "insert into ai_reading_state (book_id) values (%s)",
                        (book_id,),
                    )
                    conn.execute(
                        """
                        update books
                        set chapter_count = %s, toc = %s,
                            text_index_status = 'ready',
                            text_index_failure_code = null,
                            text_index_failure_detail = null,
                            text_index_updated_at = now()
                        where id = %s
                        returning id, title, publication_ready, reader_engine,
                                  text_index_status, text_index_failure_code,
                                  text_index_updated_at, chapter_count
                        """,
                        (len(parsed.chapters), Jsonb(parsed.toc), book_id),
                    ).fetchone()
            except SourceFirstFailure as exc:
                return _fail_index(
                    book_id,
                    source_sha256,
                    exc.code,
                    "existing_anchor_guard",
                    started,
                    exc,
                )
            except Exception as exc:
                return _fail_index(
                    book_id,
                    source_sha256,
                    "text_index_internal_error",
                    "database_transaction_failed",
                    started,
                    exc,
                )

            _log(
                "text_index_ready",
                book_id=book_id,
                source_sha256=source_sha256,
                publication_ready=True,
                text_index_status="ready",
                stage="text_index_commit",
                duration_ms=_duration_ms(started),
                chapter_count=len(parsed.chapters),
                block_count=block_count,
            )
            if os.getenv("DUAL_ANCHOR_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    from locator_bridge import persist_locator_bridge

                    persist_locator_bridge(book_id, source_path=source_path)
                except Exception:
                    # Bridge readiness is a third independent fact. It must
                    # never turn a committed text index back into a failure.
                    logger.exception("locator bridge build deferred book_id=%s", book_id)
            return _readiness_row(book_id)
    finally:
        gc.collect()
        _TEXT_INDEX_LOCK.release()


def _persist_optional_cover(
    book_id: UUID,
    source_path: Path,
    temp_root: Path,
    inspection: PublicationInspection,
) -> tuple[str, str, str, int] | None:
    if not inspection.cover_archive_path or not inspection.cover_asset_path:
        return None
    cover_path = temp_root / "cover"
    digest = hashlib.sha256()
    total = 0
    with zipfile.ZipFile(source_path) as archive:
        with archive.open(inspection.cover_archive_path) as source:
            with cover_path.open("wb") as output:
                while chunk := source.read(COVER_STREAM_CHUNK_BYTES):
                    digest.update(chunk)
                    output.write(chunk)
                    total += len(chunk)
                    if total > MAX_COVER_BYTES:
                        cover_path.unlink(missing_ok=True)
                        return None
    if not total:
        return None
    object_path = object_storage.object_path_for_asset(
        book_id,
        inspection.cover_asset_path,
        True,
        digest.hexdigest(),
    )
    try:
        object_storage.upload_file(
            object_path,
            cover_path,
            inspection.cover_media_type or "application/octet-stream",
        )
    except Exception:
        _cleanup([object_path])
        raise
    return (
        inspection.cover_asset_path,
        object_path,
        inspection.cover_media_type or "application/octet-stream",
        total,
    )


def _stage_chapters(chapters: list[Any], temp_root: Path) -> list[tuple[Path, Path]]:
    staged: list[tuple[Path, Path]] = []
    for index, chapter in enumerate(chapters):
        html_path = temp_root / f"chapter-{index:06d}.html"
        text_path = temp_root / f"chapter-{index:06d}.txt"
        html_path.write_text(chapter.content_html, encoding="utf-8")
        text_path.write_text(chapter.content_text, encoding="utf-8")
        chapter.content_html = ""
        chapter.content_text = ""
        staged.append((html_path, text_path))
    gc.collect()
    return staged


def _fail_index(
    book_id: UUID,
    source_sha256: str,
    error_code: str,
    detail: str,
    started: float,
    exc: Exception,
) -> dict[str, Any]:
    logger.exception(
        "text_index failed book_id=%s code=%s stage=%s",
        book_id,
        error_code,
        detail,
        exc_info=exc,
    )
    row = db.execute(
        """
        update books
        set text_index_status = 'failed',
            text_index_failure_code = %s,
            text_index_failure_detail = %s,
            text_index_updated_at = now()
        where id = %s
        returning id, title, publication_ready, reader_engine,
                  text_index_status, text_index_failure_code,
                  text_index_updated_at, chapter_count
        """,
        (error_code, detail[:500], book_id),
    )
    _log(
        "text_index_failed",
        book_id=book_id,
        source_sha256=source_sha256,
        publication_ready=True,
        text_index_status="failed",
        stage=detail,
        duration_ms=_duration_ms(started),
        error_code=error_code,
    )
    return dict(row or {
        "id": book_id,
        "publication_ready": True,
        "text_index_status": "failed",
        "text_index_failure_code": error_code,
    })


def _readiness_row(book_id: UUID) -> dict[str, Any]:
    row = db.fetch_one(
        """
        select id, title, publication_ready, reader_engine,
               text_index_status, text_index_failure_code,
               text_index_updated_at, chapter_count,
               locator_bridge_status, locator_bridge_version,
               locator_bridge_failure_code, locator_bridge_updated_at
        from books where id = %s
        """,
        (book_id,),
    )
    return dict(row or {})


def _parse_failure_code(parser_code: str) -> str:
    return {
        "epub_import_timeout": "text_index_timeout",
        "no_readable_content": "text_index_no_readable_text",
    }.get(parser_code, "text_index_parse_failed")


def _spool_upload(upload: Any, target: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with target.open("wb") as output:
        while chunk := upload.stream.read(SOURCE_STREAM_CHUNK_BYTES):
            output.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _cleanup(paths: list[str]) -> None:
    try:
        object_storage.delete_objects(paths)
    except object_storage.ObjectStorageError:
        logger.exception("source_first storage cleanup failed object_count=%s", len(paths))


def _duration_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _rss_mb() -> float:
    try:
        with open("/proc/self/statm", encoding="ascii") as status:
            resident_pages = int(status.read().split()[1])
        return round(
            resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 2
        )
    except (OSError, ValueError, IndexError):
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(value / 1024, 2)


def _peak_rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        pass
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / 1024, 2)


def _log_index(
    book_id: UUID, source_sha256: str, stage: str, started: float
) -> None:
    _log(
        "text_index_stage",
        book_id=book_id,
        source_sha256=source_sha256,
        publication_ready=True,
        text_index_status="processing",
        stage=stage,
        duration_ms=_duration_ms(started),
    )


def _log(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        **fields,
        "rss_mb": _rss_mb(),
        "peak_rss_mb": _peak_rss_mb(),
    }
    logger.info("source_first %s", json.dumps(payload, ensure_ascii=False, default=str))
