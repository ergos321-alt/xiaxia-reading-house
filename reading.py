"""Book import, bookshelf, reader, progress, and Xiaxia context APIs."""

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
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote
from uuid import UUID, uuid4

from bs4 import BeautifulSoup, Tag
from flask import Blueprint, Response, current_app, jsonify, request, url_for
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

import database as db
import storage as object_storage
from auth import action_required, api_or_session_required, web_api_required
from epub_parser import BookParseError, parse_uploaded_book_path
from source_first import SourceFirstFailure, build_text_index, create_epub_publication


reading_bp = Blueprint("reading", __name__)
logger = logging.getLogger(__name__)


BOOK_FIELDS = """
    b.id, b.title, b.author, b.format, b.source_filename,
    b.cover_asset_path, b.chapter_count, b.toc,
    b.publication_ready, b.reader_engine, b.text_index_status,
    b.text_index_failure_code, b.publication_validated_at,
    b.text_index_updated_at, b.locator_bridge_status,
    b.locator_bridge_version, b.locator_bridge_failure_code,
    b.locator_bridge_updated_at, b.created_at, b.updated_at
"""

ACTION_CHUNK_TARGET_CHARS = 6000
ACTION_CHUNK_MAX_CHARS = 8000
IMPORT_DEADLINE_SECONDS = 105
UPLOAD_STREAM_CHUNK_BYTES = 1024 * 1024
_IMPORT_LOCK = threading.BoundedSemaphore(1)


class ImportDeadlineExceeded(RuntimeError):
    """Cooperative deadline reached before the Gunicorn hard timeout."""


@reading_bp.get("/api/books")
@api_or_session_required
def list_books():
    rows = db.fetch_all(
        f"""
        select {BOOK_FIELDS},
               rp.chapter_id as progress_chapter_id,
               rp.chapter_index as progress_chapter_index,
               coalesce(rp.percentage, ppr.progression * 100) as progress_percentage,
               c.title as current_chapter,
               ppr.locator as publication_locator,
               ppr.progression as publication_progression
        from books b
        left join reading_progress rp on rp.book_id = b.id
        left join publication_reading_progress ppr on ppr.book_id = b.id
        left join chapters c on c.id = rp.chapter_id
        order by greatest(
            coalesce(rp.updated_at, '-infinity'::timestamptz),
            coalesce(ppr.updated_at, '-infinity'::timestamptz),
            b.created_at
        ) desc
        """
    )
    return jsonify({"books": [_serialize_book(row) for row in rows]})


@reading_bp.post("/api/books")
@web_api_required
def upload_book():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file_required"}), 400
    filename = (
        upload.filename.replace("\\", "/")
        .rsplit("/", 1)[-1]
        .replace("\x00", "")[:255]
    )
    if not filename:
        return jsonify({"error": "invalid_filename"}), 400
    if not _IMPORT_LOCK.acquire(blocking=False):
        return (
            jsonify(
                {
                    "error": "import_resource_exhausted",
                    "message": "当前已有一本书正在导入，请等待完成后再试",
                }
            ),
            429,
        )
    try:
        return _upload_book_locked(upload, filename)
    finally:
        _IMPORT_LOCK.release()


def _upload_book_locked(upload: Any, filename: str):
    if (
        PurePosixPath(filename).suffix.lower() == ".epub"
        and current_app.config["READER_ENGINE_ENABLED"]
    ):
        try:
            book = create_epub_publication(upload, filename)
        except SourceFirstFailure as exc:
            payload = {"error": exc.code, "message": exc.message, **exc.data}
            return jsonify(payload), exc.status
        return jsonify({"book": _serialize_book(book)}), 201
    return _upload_legacy_book_locked(upload, filename)


def _upload_legacy_book_locked(upload: Any, filename: str):
    started_at = time.perf_counter()
    deadline = started_at + IMPORT_DEADLINE_SECONDS
    memory = _ImportMemoryProbe()
    stage = "request"
    suffix = PurePosixPath(filename).suffix.lower() or ".upload"
    with tempfile.TemporaryDirectory(prefix="rh-book-import-") as temp_dir:
        source_path = Path(temp_dir) / f"source{suffix}"
        source_size, source_sha256 = _spool_upload(upload, source_path)

        def observe(stage_name: str) -> None:
            memory.mark(stage_name)
            _log_import(
                "stage",
                filename=filename,
                archive_compressed_size=source_size,
                stage=stage_name,
                **memory.fields(),
            )

        _log_import(
            "started",
            filename=filename,
            archive_compressed_size=source_size,
            failure_stage=None,
            **memory.fields(),
        )
        stage = "parse"
        parse_started = time.perf_counter()
        observe("parse_before")
        try:
            parsed = parse_uploaded_book_path(
                filename,
                source_path,
                deadline,
                source_sha256=source_sha256,
                stage_observer=observe,
            )
        except BookParseError as exc:
            observe("parse_after")
            _log_import(
                "failed",
                filename=filename,
                archive_compressed_size=source_size,
                total_duration=_duration(started_at),
                failure_stage=stage,
                error_code=exc.code,
                **memory.fields(),
            )
            status = 408 if exc.code == "epub_import_timeout" else 422
            return jsonify({"error": exc.code, "message": str(exc)}), status
        observe("parse_after")
        parse_duration = _duration(parse_started)
        _log_import(
            "parsed",
            filename=filename,
            archive_compressed_size=source_size,
            archive_uncompressed_size=parsed.archive_uncompressed_size,
            archive_item_count=parsed.archive_item_count,
            xhtml_count=parsed.xhtml_item_count,
            image_count=parsed.image_item_count,
            font_count=parsed.font_item_count,
            epub_version=parsed.epub_version,
            manifest_item_count=parsed.manifest_item_count,
            spine_item_count=parsed.spine_item_count,
            chapter_count=len(parsed.chapters),
            asset_count=len(parsed.assets),
            parse_duration=parse_duration,
            warning_count=len(parsed.warnings),
            **memory.fields(),
        )
        chapter_content = _stage_normalized_chapters(
            parsed.chapters, Path(temp_dir)
        )
        gc.collect()
        _release_unused_heap()
        observe("normalized_ready")

        book_id = uuid4()
        source_media_type = (
            "application/epub+zip" if parsed.format == "epub" else "text/plain"
        )
        source_object_path = f"books/{book_id}/source/source.{parsed.format}"
        uploaded_objects: list[str] = []
        asset_records: list[tuple[Any, str]] = []
        asset_uploads: dict[str, tuple[str, str, str]] = {}
        for asset in parsed.assets:
            object_path = object_storage.object_path_for_asset(
                book_id,
                asset.path,
                asset.path == parsed.cover_asset_path,
                asset.content_sha256,
            )
            asset_records.append((asset, object_path))
            if parsed.format == "epub" and asset.archive_path:
                asset_uploads.setdefault(
                    object_path,
                    (object_path, asset.archive_path, asset.media_type),
                )

        try:
            stage = "duplicate_check"
            with db.transaction() as conn:
                existing = conn.execute(
                    "select id, title from books where source_sha256 = %s",
                    (parsed.source_sha256,),
                ).fetchone()
                if existing:
                    return (
                        jsonify(
                            {
                                "error": "book_already_exists",
                                "book_id": str(existing["id"]),
                                "title": existing["title"],
                            }
                        ),
                        409,
                    )

            stage = "storage"
            storage_started = time.perf_counter()
            observe("storage_before")
            object_storage.upload_file(
                source_object_path, source_path, source_media_type
            )
            uploaded_objects.append(source_object_path)
            if time.perf_counter() >= deadline:
                raise ImportDeadlineExceeded
            if asset_uploads:
                uploaded_objects.extend(
                    object_storage.upload_archive_entries(
                        source_path,
                        list(asset_uploads.values()),
                        max_workers=2,
                        deadline=deadline,
                    )
                )
            observe("storage_after")
            storage_duration = _duration(storage_started)
            if time.perf_counter() >= deadline:
                raise ImportDeadlineExceeded

            chapter_ids = [uuid4() for _chapter in parsed.chapters]
            first_chapter_id = chapter_ids[0]
            stage = "database"
            database_started = time.perf_counter()
            observe("database_before")
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
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        true, 'legacy', 'ready', now(), now()
                    )
                    returning id, title, author, format, source_filename,
                              cover_asset_path, chapter_count, toc,
                              publication_ready, reader_engine, text_index_status,
                              text_index_failure_code, publication_validated_at,
                              text_index_updated_at, created_at, updated_at
                    """,
                    (
                        book_id,
                        parsed.title,
                        parsed.author,
                        parsed.format,
                        parsed.source_filename,
                        parsed.source_sha256,
                        source_object_path,
                        source_media_type,
                        source_size,
                        parsed.cover_asset_path,
                        len(parsed.chapters),
                        Jsonb(parsed.toc),
                    ),
                ).fetchone()

                _bulk_execute(
                    conn,
                    """
                    insert into book_assets (
                        book_id, asset_path, object_path, media_type, byte_size
                    ) values (%s, %s, %s, %s, %s)
                    """,
                    (
                        (
                            book_id,
                            asset.path,
                            object_path,
                            asset.media_type,
                            asset.byte_size,
                        )
                        for asset, object_path in asset_records
                    ),
                )
                _bulk_execute(
                    conn,
                    """
                    insert into chapters (
                        id, book_id, chapter_index, title, href, content_html,
                        content_text, word_count
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        (
                            chapter_ids[index],
                            book_id,
                            chapter.chapter_index,
                            chapter.title,
                            chapter.href,
                            chapter_content[index][0].read_text(
                                encoding="utf-8"
                            ),
                            chapter_content[index][1].read_text(
                                encoding="utf-8"
                            ),
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
                        first_chapter_id,
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
            observe("database_after")
            database_duration = _duration(database_started)
        except ImportDeadlineExceeded:
            _cleanup_uploaded_objects(uploaded_objects)
            _log_import(
                "failed",
                filename=filename,
                archive_compressed_size=source_size,
                parse_duration=parse_duration,
                total_duration=_duration(started_at),
                failure_stage=stage,
                error_code="epub_import_timeout",
                **memory.fields(),
            )
            return (
                jsonify(
                    {
                        "error": "epub_import_timeout",
                        "message": "导入超过安全处理时间，未保存任何不完整书籍",
                    }
                ),
                408,
            )
        except object_storage.ObjectStorageDeadlineError as exc:
            uploaded_objects = sorted(
                set(uploaded_objects).union(exc.uploaded_paths)
            )
            _cleanup_uploaded_objects(uploaded_objects)
            _log_import(
                "failed",
                filename=filename,
                archive_compressed_size=source_size,
                parse_duration=parse_duration,
                total_duration=_duration(started_at),
                failure_stage=stage,
                error_code="epub_import_timeout",
                **memory.fields(),
            )
            return (
                jsonify(
                    {
                        "error": "epub_import_timeout",
                        "message": "导入超过安全处理时间，未保存任何不完整书籍",
                    }
                ),
                408,
            )
        except object_storage.ObjectStorageBatchError as exc:
            uploaded_objects = sorted(
                set(uploaded_objects).union(exc.uploaded_paths)
            )
            _cleanup_uploaded_objects(uploaded_objects)
            _log_import(
                "failed",
                filename=filename,
                archive_compressed_size=source_size,
                parse_duration=parse_duration,
                total_duration=_duration(started_at),
                failure_stage=stage,
                error_code="storage_upload_failed",
                **memory.fields(),
            )
            return (
                jsonify(
                    {
                        "error": "storage_upload_failed",
                        "message": "书籍资源保存失败，已撤销本次导入",
                    }
                ),
                502,
            )
        except UniqueViolation:
            _cleanup_uploaded_objects(uploaded_objects)
            existing = db.fetch_one(
                "select id, title from books where source_sha256 = %s",
                (parsed.source_sha256,),
            )
            if existing:
                return (
                    jsonify(
                        {
                            "error": "book_already_exists",
                            "book_id": str(existing["id"]),
                            "title": existing["title"],
                        }
                    ),
                    409,
                )
            return (
                jsonify(
                    {
                        "error": "database_write_failed",
                        "message": "书籍写入失败，已撤销本次导入",
                    }
                ),
                500,
            )
        except Exception:
            _cleanup_uploaded_objects(uploaded_objects)
            logger.exception(
                "book_import database_or_storage_failure stage=%s", stage
            )
            error_code = (
                "storage_upload_failed"
                if stage == "storage"
                else "database_write_failed"
            )
            status = 502 if stage == "storage" else 500
            _log_import(
                "failed",
                filename=filename,
                archive_compressed_size=source_size,
                parse_duration=parse_duration,
                total_duration=_duration(started_at),
                failure_stage=stage,
                error_code=error_code,
                **memory.fields(),
            )
            return (
                jsonify(
                    {
                        "error": error_code,
                        "message": "书籍导入失败，已撤销本次导入",
                    }
                ),
                status,
            )

        _log_import(
            "completed",
            filename=filename,
            archive_compressed_size=source_size,
            archive_uncompressed_size=parsed.archive_uncompressed_size,
            archive_item_count=parsed.archive_item_count,
            xhtml_count=parsed.xhtml_item_count,
            image_count=parsed.image_item_count,
            font_count=parsed.font_item_count,
            epub_version=parsed.epub_version,
            manifest_item_count=parsed.manifest_item_count,
            spine_item_count=parsed.spine_item_count,
            chapter_count=len(parsed.chapters),
            asset_count=len(parsed.assets),
            storage_upload_count=1 + len(asset_uploads),
            parse_duration=parse_duration,
            storage_duration=storage_duration,
            database_duration=database_duration,
            total_duration=_duration(started_at),
            failure_stage=None,
            warning_count=len(parsed.warnings),
            **memory.fields(),
        )
        return jsonify({"book": _serialize_book(book)}), 201


@reading_bp.get("/api/books/<uuid:book_id>")
@api_or_session_required
def get_book(book_id: UUID):
    book = db.fetch_one(
        f"select {BOOK_FIELDS} from books b where b.id = %s", (book_id,)
    )
    if not book:
        return jsonify({"error": "book_not_found"}), 404
    chapters = db.fetch_all(
        """
        select id, chapter_index, title, href, word_count
        from chapters where book_id = %s order by chapter_index
        """,
        (book_id,),
    )
    progress = db.fetch_one(
        """
        select chapter_id, chapter_index, position, percentage, updated_at
        from reading_progress where book_id = %s
        """,
        (book_id,),
    )
    publication_progress = db.fetch_one(
        """
        select engine, locator, progression, updated_at
        from publication_reading_progress where book_id = %s
        """,
        (book_id,),
    )
    payload = _serialize_book(book)
    payload["chapters"] = [_json_safe(row) for row in chapters]
    payload["progress"] = _json_safe(progress) if progress else None
    payload["publication_progress"] = (
        _json_safe(publication_progress) if publication_progress else None
    )
    return jsonify({"book": payload})


@reading_bp.get("/api/books/<uuid:book_id>/source-access")
@web_api_required
def get_publication_source_access(book_id: UUID):
    book = db.fetch_one(
        """
        select id, title, format, source_filename, source_sha256,
               source_object_path, publication_ready
        from books where id = %s
        """,
        (book_id,),
    )
    if not book:
        return jsonify({"error": "book_not_found"}), 404
    if (
        book["format"] != "epub"
        or not book["publication_ready"]
        or not book.get("source_object_path")
    ):
        return (
            jsonify(
                {
                    "error": "publication_not_ready",
                    "message": "这本书没有可供新阅读引擎打开的原始 EPUB",
                }
            ),
            409,
        )
    ttl = int(current_app.config["READER_SOURCE_SIGNED_URL_TTL"])
    if not 60 <= ttl <= 300:
        return jsonify({"error": "reader_source_configuration_invalid"}), 503
    try:
        signed_url = object_storage.create_signed_download_url(
            book["source_object_path"], ttl
        )
    except object_storage.ObjectStorageError:
        logger.exception("publication source access failed book_id=%s", book_id)
        return jsonify({"error": "publication_source_unavailable"}), 502
    response = jsonify(
        {
            "book": {
                "id": str(book["id"]),
                "title": book["title"],
                "filename": book["source_filename"],
                "source_sha256": book.get("source_sha256") or "",
            },
            "signed_url": signed_url,
            "expires_in": ttl,
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=ttl)
            ).isoformat(),
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@reading_bp.post("/api/books/<uuid:book_id>/text-index")
@web_api_required
def retry_text_index(book_id: UUID):
    try:
        book = build_text_index(book_id)
    except SourceFirstFailure as exc:
        return jsonify({"error": exc.code, "message": exc.message, **exc.data}), exc.status
    payload = _serialize_book(book)
    if book.get("text_index_status") != "failed":
        return jsonify({"book": payload})
    code = str(book.get("text_index_failure_code") or "text_index_internal_error")
    messages = {
        "text_index_parse_failed": "这本书可以阅读，但夏夏暂时无法解析正文",
        "text_index_timeout": "这本书可以阅读，但夏夏整理正文的时间过长",
        "text_index_resource_limit": "这本书可以阅读，但本次整理超出服务器资源",
        "text_index_no_readable_text": "这本书可以阅读，但没有提取到可供夏夏读取的文字",
        "text_index_mapping_failed": "这本书可以阅读，但现有书页锚点阻止了安全重建",
        "text_index_internal_error": "这本书可以阅读，但夏夏暂时无法完成整理",
    }
    status = {
        "text_index_timeout": 408,
        "text_index_resource_limit": 503,
        "text_index_internal_error": 500,
    }.get(code, 422)
    return (
        jsonify(
            {
                "error": code,
                "message": messages.get(code, messages["text_index_internal_error"]),
                "retryable": code != "text_index_mapping_failed",
                "publication_ready": True,
                "book": payload,
            }
        ),
        status,
    )


@reading_bp.post("/api/books/<uuid:book_id>/locator-bridge")
@web_api_required
def rebuild_locator_bridge(book_id: UUID):
    if not current_app.config.get("DUAL_ANCHOR_ENABLED", False):
        return jsonify({"error": "dual_anchor_disabled"}), 409
    from locator_bridge import LocatorBridgeError, persist_locator_bridge

    try:
        payload = request.get_json(silent=True) or {}
        bridge = persist_locator_bridge(
            book_id, promote_legacy=bool(payload.get("promote_legacy", False))
        )
    except LocatorBridgeError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return jsonify({"locator_bridge": _json_safe(bridge)})


@reading_bp.put("/api/books/<uuid:book_id>/publication-progress")
@web_api_required
def save_publication_progress(book_id: UUID):
    payload = request.get_json(silent=True) or {}
    locator = payload.get("locator")
    try:
        progression = max(0.0, min(1.0, float(payload.get("progression", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_publication_progress"}), 400
    if not isinstance(locator, dict):
        return jsonify({"error": "invalid_publication_locator"}), 400
    book = db.fetch_one(
        "select id, source_sha256, locator_bridge_version from books where id = %s and publication_ready = true",
        (book_id,),
    )
    if not book:
        return jsonify({"error": "publication_not_ready"}), 409
    sanitized = {
        "engine": "foliate-js",
        "href": str(locator.get("href") or "")[:2048],
        "cfi": str(locator.get("cfi") or "")[:8192],
        "progression": progression,
    }
    supplied_hash = str(locator.get("source_sha256") or "")
    if supplied_hash and supplied_hash != book["source_sha256"]:
        return jsonify({"error": "locator_source_mismatch"}), 409
    if supplied_hash:
        sanitized["source_sha256"] = supplied_hash
        sanitized["engine_adapter_version"] = 1
        sanitized["bridge_version"] = int(locator.get("bridge_version") or book.get("locator_bridge_version") or 1)
    if locator.get("section_index") is not None:
        try:
            sanitized["section_index"] = max(0, int(locator["section_index"]))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_publication_locator"}), 400
    if not sanitized["cfi"] and not sanitized["href"]:
        return jsonify({"error": "invalid_publication_locator"}), 400
    row = db.execute(
        """
        insert into publication_reading_progress (
            book_id, engine, locator, progression
        ) values (%s, 'foliate-js', %s, %s)
        on conflict (book_id) do update set
            engine = excluded.engine,
            locator = excluded.locator,
            progression = excluded.progression,
            updated_at = now()
        returning engine, locator, progression, updated_at
        """,
        (book_id, Jsonb(sanitized), progression),
    )
    return jsonify({"publication_progress": _json_safe(row)})


@reading_bp.get("/api/books/<uuid:book_id>/chapters")
@api_or_session_required
def list_book_chapters(book_id: UUID):
    rows = db.fetch_all(
        """
        select id as chapter_id, chapter_index, title, word_count
        from chapters where book_id = %s order by chapter_index
        """,
        (book_id,),
    )
    if not rows:
        book = db.fetch_one(
            """
            select id, publication_ready, text_index_status,
                   text_index_failure_code
            from books where id = %s
            """,
            (book_id,),
        )
        if not book:
            return jsonify({"error": "book_not_found"}), 404
        readiness = _text_readiness_response(book)
        if readiness:
            return readiness
    return jsonify(
        {"book_id": str(book_id), "chapters": [_json_safe(row) for row in rows]}
    )


@reading_bp.patch("/api/books/<uuid:book_id>")
@web_api_required
def update_book(book_id: UUID):
    payload = request.get_json(silent=True) or {}
    updates = []
    values: list[Any] = []
    for field in ("title", "author"):
        if field not in payload:
            continue
        value = _clean_book_field(payload.get(field))
        if not value:
            return jsonify({"error": f"{field}_required"}), 400
        updates.append(f"{field} = %s")
        values.append(value)
    if not updates:
        return jsonify({"error": "book_fields_required"}), 400
    values.append(book_id)
    row = db.execute(
        f"""
        update books set {", ".join(updates)}, updated_at = now()
        where id = %s
        returning id, title, author, format, source_filename,
                  cover_asset_path, chapter_count, toc,
                  publication_ready, reader_engine, text_index_status,
                  text_index_failure_code, publication_validated_at,
                  text_index_updated_at, created_at, updated_at
        """,
        values,
    )
    if not row:
        return jsonify({"error": "book_not_found"}), 404
    return jsonify({"book": _serialize_book(row)})


@reading_bp.delete("/api/books/<uuid:book_id>")
@web_api_required
def delete_book(book_id: UUID):
    """Delete one private book after removing every persisted Storage object."""
    book = db.fetch_one(
        "select id, title, source_object_path, locator_bridge_object_path from books where id = %s", (book_id,)
    )
    if not book:
        return jsonify({"error": "book_not_found"}), 404
    assets = db.fetch_all(
        "select object_path from book_assets where book_id = %s order by object_path",
        (book_id,),
    )
    object_paths = [
        path
        for path in (
            [book.get("source_object_path")]
            + [book.get("locator_bridge_object_path")]
            + [row["object_path"] for row in assets]
        )
        if path
    ]
    try:
        object_storage.delete_objects(object_paths)
    except object_storage.ObjectStorageError:
        # Keep the relational records when Storage cleanup cannot be confirmed.
        return jsonify({"error": "private_storage_cleanup_failed"}), 502
    deleted = db.execute(
        "delete from books where id = %s returning id, title", (book_id,)
    )
    if not deleted:
        return jsonify({"error": "book_delete_conflict"}), 409
    return jsonify(
        {
            "deleted": True,
            "book_id": str(book_id),
            "title": deleted["title"],
            "storage_objects_deleted": len(object_paths),
        }
    )


@reading_bp.get("/api/books/<uuid:book_id>/chapters/<uuid:chapter_id>")
@api_or_session_required
def get_chapter(book_id: UUID, chapter_id: UUID):
    chapter = db.fetch_one(
        """
        select id, book_id, chapter_index, title, href, content_html,
               content_text, word_count
        from chapters where id = %s and book_id = %s
        """,
        (chapter_id, book_id),
    )
    if not chapter:
        book = db.fetch_one(
            """
            select id, publication_ready, text_index_status,
                   text_index_failure_code
            from books where id = %s
            """,
            (book_id,),
        )
        readiness = _text_readiness_response(book) if book else None
        if readiness:
            return readiness
        return jsonify({"error": "chapter_not_found"}), 404
    target_hrefs = _internal_link_hrefs(chapter["content_html"])
    current_key = _chapter_href_key(str(chapter.get("href") or ""))
    other_hrefs = sorted(
        href for href in target_hrefs if _chapter_href_key(href) != current_key
    )
    link_targets = [chapter]
    if other_hrefs:
        link_targets.extend(
            db.fetch_all(
                """
                select id, href, content_html
                from chapters
                where book_id = %s and lower(href) = any(%s)
                """,
                (book_id, [_chapter_href_key(href) for href in other_hrefs]),
            )
        )
    payload = _json_safe(chapter)
    payload["content_html"] = _materialize_reader_html(
        chapter["content_html"], book_id, link_targets
    )
    return jsonify({"chapter": payload})


@reading_bp.get("/api/books/<uuid:book_id>/assets/<path:asset_path>")
@api_or_session_required
def get_asset(book_id: UUID, asset_path: str):
    asset = db.fetch_one(
        """
        select object_path, media_type, byte_size
        from book_assets where book_id = %s and asset_path = %s
        """,
        (book_id, asset_path),
    )
    if not asset:
        return jsonify({"error": "asset_not_found"}), 404
    try:
        binary = object_storage.download_bytes(asset["object_path"])
    except object_storage.ObjectStorageError:
        return jsonify({"error": "private_storage_unavailable"}), 502
    response = Response(binary, mimetype=asset["media_type"])
    response.headers["Cache-Control"] = "private, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Length"] = str(asset["byte_size"])
    return response


@reading_bp.put("/api/books/<uuid:book_id>/progress")
@web_api_required
def save_progress(book_id: UUID):
    payload = request.get_json(silent=True) or {}
    chapter_id = _uuid_or_none(payload.get("chapter_id"))
    position = payload.get("position")
    try:
        chapter_index = int(payload.get("chapter_index"))
        percentage = max(0.0, min(100.0, float(payload.get("percentage", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_progress"}), 400
    if chapter_id is None or not isinstance(position, dict):
        return jsonify({"error": "invalid_progress"}), 400
    chapter = db.fetch_one(
        "select id from chapters where id = %s and book_id = %s and chapter_index = %s",
        (chapter_id, book_id, chapter_index),
    )
    if not chapter:
        return jsonify({"error": "chapter_not_found"}), 404

    try:
        display_mode = str(position.get("display_mode") or "scroll")
        if display_mode not in {"scroll", "paginated"}:
            display_mode = "scroll"
        sanitized_position = {
            "block_id": str(position.get("block_id") or "b000001")[:64],
            "char_offset": max(0, int(position.get("char_offset") or 0)),
            "scroll_fraction": max(
                0.0, min(1.0, float(position.get("scroll_fraction") or 0))
            ),
            "display_mode": display_mode,
            "page_index": max(0, int(position.get("page_index") or 0)),
            "page_count": max(1, int(position.get("page_count") or 1)),
        }
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_progress_position"}), 400
    row = db.execute(
        """
        insert into reading_progress (
            book_id, chapter_id, chapter_index, position, percentage
        ) values (%s, %s, %s, %s, %s)
        on conflict (book_id) do update set
            chapter_id = excluded.chapter_id,
            chapter_index = excluded.chapter_index,
            position = excluded.position,
            percentage = excluded.percentage,
            updated_at = now()
        returning chapter_id, chapter_index, position, percentage, updated_at
        """,
        (book_id, chapter_id, chapter_index, Jsonb(sanitized_position), percentage),
    )
    # V2 records only the first meaningful reading milestone. The progress
    # response and its existing persistence contract remain unchanged.
    from memories import record_user_progress_milestone

    record_user_progress_milestone(
        book_id, chapter_id, percentage, row.get("updated_at")
    )
    return jsonify({"progress": _json_safe(row)})


def _current_reading_state(book_id: UUID | None = None) -> dict[str, Any] | None:
    book_filter = "and b.id = %s" if book_id else ""
    params = (book_id,) if book_id else ()
    row = db.fetch_one(
        f"""
        select {BOOK_FIELDS},
               rp.chapter_id as legacy_chapter_id,
               rp.chapter_index as legacy_chapter_index,
               rp.position as legacy_position,
               rp.percentage as legacy_percentage,
               rp.updated_at as legacy_updated_at,
               c.title as legacy_chapter_title,
               ppr.locator as publication_locator,
               ppr.progression as publication_progression,
               ppr.updated_at as publication_progress_updated_at,
               (b.reader_engine = 'foliate' and ppr.book_id is not null)
                   as publication_current,
               ais.last_chapter_read, ais.last_chunk_index, ais.last_chunk_id,
               ais.last_block_id,
               ais.chapter_completed, ais.last_annotation_seen,
               ais.updated_at as ai_updated_at,
               (select count(*) from annotations a
                where a.book_id = b.id and a.status = 'pending') as pending_count
        from books b
        left join reading_progress rp on rp.book_id = b.id
        left join publication_reading_progress ppr on ppr.book_id = b.id
        left join chapters c on c.id = rp.chapter_id
        left join ai_reading_state ais on ais.book_id = b.id
        where case
            when b.reader_engine = 'foliate' then ppr.updated_at
            else rp.updated_at
        end is not null
        {book_filter}
        order by case
            when b.reader_engine = 'foliate' then ppr.updated_at
            else rp.updated_at
        end desc
        limit 1
        """,
        params,
    )
    if not row:
        return None
    if not row.get("publication_current"):
        row["chapter_id"] = row.get("legacy_chapter_id", row.get("chapter_id"))
        row["chapter_index"] = row.get("legacy_chapter_index", row.get("chapter_index"))
        row["position"] = row.get("legacy_position", row.get("position"))
        row["percentage"] = row.get("legacy_percentage", row.get("percentage"))
        row["progress_updated_at"] = row.get(
            "legacy_updated_at", row.get("progress_updated_at")
        )
        row["current_chapter"] = row.get(
            "legacy_chapter_title", row.get("current_chapter")
        )
        row["_publication_current"] = False
        return row

    locator = row.get("publication_locator") or {}
    chapter_id = _publication_chapter_id(row["id"], locator)
    chapter = None
    if chapter_id:
        chapter = db.fetch_one(
            """
            select id, chapter_index, title, href
            from chapters where id = %s and book_id = %s
            """,
            (chapter_id, row["id"]),
        )
    row["chapter_id"] = chapter["id"] if chapter else None
    row["chapter_index"] = (
        chapter["chapter_index"] if chapter else locator.get("section_index")
    )
    row["current_chapter"] = chapter["title"] if chapter else None
    row["position"] = locator
    row["percentage"] = float(row.get("publication_progression") or 0) * 100
    row["progress_updated_at"] = row.get("publication_progress_updated_at")
    row["_publication_current"] = True
    return row


def _publication_chapter_id(book_id: UUID, locator: dict[str, Any]) -> UUID | None:
    try:
        from locator_bridge import (
            LocatorBridgeError,
            _bridge_chapter,
            _validated_bridge,
            canonical_href,
        )

        _, bridge = _validated_bridge(book_id, locator)
        chapter = _bridge_chapter(
            bridge,
            canonical_href(locator.get("href") or ""),
            locator.get("spine_index", locator.get("section_index")),
        )
        return UUID(str(chapter["chapter_id"]))
    except (LocatorBridgeError, KeyError, TypeError, ValueError):
        return None


def _publication_progress_payload(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": "foliate-js",
        "chapter_id": current.get("chapter_id"),
        "chapter_index": current.get("chapter_index"),
        "position": current.get("publication_locator") or {},
        "percentage": current.get("percentage", 0),
        "progression": current.get("publication_progression", 0),
        "updated_at": current.get("publication_progress_updated_at"),
    }


@reading_bp.get("/api/reading/state")
@api_or_session_required
def get_reading_state():
    current = _current_reading_state()
    if not current:
        return jsonify({"current": None, "message": "书架中还没有阅读进度"})
    return jsonify({"current": _serialize_reading_state(current)})


@reading_bp.get("/api/reading/context")
@api_or_session_required
def get_reading_context():
    requested_book_id = _uuid_or_none(request.args.get("book_id"))
    if requested_book_id:
        requested_book = db.fetch_one(
            """
            select id, publication_ready, text_index_status,
                   text_index_failure_code
            from books where id = %s
            """,
            (requested_book_id,),
        )
        if not requested_book:
            return jsonify({"error": "book_not_found"}), 404
        readiness = _text_readiness_response(requested_book)
        if readiness:
            return readiness
    annotation_id = _uuid_or_none(request.args.get("annotation_id"))
    chapter_id = _uuid_or_none(request.args.get("chapter_id"))
    include_adjacent = request.args.get("include_adjacent", "false").lower() == "true"

    annotation = None
    if annotation_id:
        annotation = db.fetch_one(
            """
            select a.*, ar.response as xiaxia_response, ar.created_at as reply_created_at
            from annotations a
            left join annotation_replies ar on ar.annotation_id = a.id
            where a.id = %s
            """,
            (annotation_id,),
        )
        if not annotation:
            return jsonify({"error": "annotation_not_found"}), 404
        chapter_id = annotation["chapter_id"]

    current = None
    if not chapter_id:
        current = _current_reading_state(requested_book_id)
        chapter_id = current["chapter_id"] if current else None
    if not chapter_id:
        if current and current.get("_publication_current"):
            return jsonify(
                {
                    "error": "publication_section_not_mapped",
                    "current": _serialize_reading_state(current),
                }
            ), 409
        return jsonify({"error": "no_current_reading"}), 404

    chapter = db.fetch_one(
        """
        select c.id, c.book_id, c.chapter_index, c.title, c.content_html,
               c.content_text, c.word_count,
               b.title as book_title, b.author as book_author,
               b.chapter_count
        from chapters c join books b on b.id = c.book_id
        where c.id = %s
        """,
        (chapter_id,),
    )
    if not chapter:
        return jsonify({"error": "chapter_not_found"}), 404
    if current and current.get("_publication_current"):
        progress = _publication_progress_payload(current)
    else:
        progress = db.fetch_one(
            """
            select chapter_id, chapter_index, position, percentage, updated_at
            from reading_progress where book_id = %s
            """,
            (chapter["book_id"],),
        )
    chapter_annotations = db.fetch_all(
        """
        select a.id, a.selected_text, a.start_block_id, a.start_offset,
               a.end_block_id, a.end_offset, a.prefix_text, a.suffix_text,
               a.comment, a.status, a.owner, a.content_type,
               a.created_at, a.updated_at,
               ar.response as xiaxia_response,
               ar.owner as reply_owner, ar.content_type as reply_content_type
        from annotations a
        left join annotation_replies ar on ar.annotation_id = a.id
        where a.chapter_id = %s
        order by a.created_at
        """,
        (chapter_id,),
    )
    xiaxia_thoughts = db.fetch_all(
        """
        select xt.id, xt.book_id, xt.chapter_id, xt.scope, xt.mark_type,
               xt.content, xt.selected_text, xt.start_block_id, xt.start_offset,
               xt.end_block_id, xt.end_offset, xt.prefix_text, xt.suffix_text,
               xt.owner, xt.content_type, xt.created_at, xt.updated_at,
               tur.id as user_reply_id, tur.response as user_response,
               tur.owner as user_reply_owner,
               tur.content_type as user_reply_content_type,
               tur.created_at as user_reply_created_at,
               tur.updated_at as user_reply_updated_at
        from xiaxia_thoughts xt
        left join thought_user_replies tur on tur.thought_id = xt.id
        where xt.chapter_id = %s
        order by xt.created_at
        """,
        (chapter_id,),
    )

    blocks = _chapter_blocks(chapter["content_html"])
    chunks = _chunk_chapter_blocks(blocks)
    focus_chunk_index = _anchor_chunk_index(annotation, chunks) if annotation else None
    raw_chunk_index = request.args.get("chunk_index")
    if raw_chunk_index is None or raw_chunk_index == "":
        chunk_index = focus_chunk_index if focus_chunk_index is not None else 0
    else:
        try:
            chunk_index = int(raw_chunk_index)
        except ValueError:
            return jsonify({"error": "invalid_chunk_index"}), 400
    if chunk_index < 0 or chunk_index >= len(chunks):
        return (
            jsonify(
                {
                    "error": "chunk_not_found",
                    "chunk_count": len(chunks),
                    "valid_chunk_index_min": 0,
                    "valid_chunk_index_max": len(chunks) - 1,
                }
            ),
            404,
        )
    current_chunk = chunks[chunk_index]
    annotations_in_chunk = [
        row
        for row in chapter_annotations
        if _anchor_chunk_index(row, chunks) == chunk_index
    ]
    thoughts_in_chunk = [
        row
        for row in xiaxia_thoughts
        if _anchor_chunk_index(row, chunks) == chunk_index
    ]
    chapter_payload: dict[str, Any] = {
        "id": str(chapter["id"]),
        "chapter_index": chapter["chapter_index"],
        "title": chapter["title"],
        "word_count": chapter["word_count"],
        "chunk_index": chunk_index,
        "chunk_id": _chunk_id(chapter["id"], chunk_index),
        "chunk_count": len(chunks),
        "has_next": chunk_index < len(chunks) - 1,
        "has_previous": chunk_index > 0,
        "chunk_text": current_chunk["text"],
        "chunk_character_count": len(current_chunk["text"]),
        "blocks": current_chunk["blocks"],
        "is_complete_chapter": len(chunks) == 1,
    }
    if len(chunks) == 1:
        # Preserve the original short-chapter response field for compatibility.
        chapter_payload["full_text"] = chapter["content_text"]
    response: dict[str, Any] = {
        "book": {
            "id": str(chapter["book_id"]),
            "title": chapter["book_title"],
            "author": chapter["book_author"],
            "chapter_count": chapter["chapter_count"],
        },
        "chapter": chapter_payload,
        "reading_progress": _json_safe(progress) if progress else None,
        "annotations": [_json_safe(row) for row in annotations_in_chunk],
        "xiaxia_thoughts": [_json_safe(row) for row in thoughts_in_chunk],
        "focus_annotation": _json_safe(annotation) if annotation else None,
        "focus_annotation_chunk_index": focus_chunk_index,
        "reading_instruction": (
            "This is the complete chapter."
            if len(chunks) == 1
            else "Read chunk_index 0 through chunk_count - 1 before claiming the chapter is complete."
        ),
    }
    if include_adjacent:
        response["adjacent_chapters"] = _adjacent_chapters(
            chapter["book_id"], chapter["chapter_index"]
        )
    return jsonify(response)


@reading_bp.post("/api/ai/progress")
@action_required
def save_ai_progress():
    payload = request.get_json(silent=True) or {}
    book_id = _uuid_or_none(payload.get("book_id"))
    chapter_id = _uuid_or_none(payload.get("chapter_id"))
    annotation_id = _uuid_or_none(payload.get("last_annotation_seen"))
    if not book_id:
        return jsonify({"error": "book_id_required"}), 400
    checkpoint_chapter = None
    if chapter_id:
        checkpoint_chapter = db.fetch_one(
            "select id, content_html from chapters where id = %s and book_id = %s",
            (chapter_id, book_id),
        )
        if not checkpoint_chapter:
            readiness_book = db.fetch_one(
                """
                select id, publication_ready, text_index_status,
                       text_index_failure_code
                from books where id = %s
                """,
                (book_id,),
            )
            readiness = _text_readiness_response(readiness_book)
            if readiness:
                return readiness
            return jsonify({"error": "chapter_not_found"}), 404
    else:
        readiness_book = db.fetch_one(
            """
            select id, publication_ready, text_index_status,
                   text_index_failure_code
            from books where id = %s
            """,
            (book_id,),
        )
        if not readiness_book:
            return jsonify({"error": "book_not_found"}), 404
        readiness = _text_readiness_response(readiness_book)
        if readiness:
            return readiness
    if annotation_id and not db.fetch_one(
        "select id from annotations where id = %s and book_id = %s",
        (annotation_id, book_id),
    ):
        return jsonify({"error": "annotation_not_found"}), 404
    chapter_completed_supplied = "chapter_completed" in payload
    if chapter_completed_supplied and not isinstance(
        payload["chapter_completed"], bool
    ):
        return jsonify({"error": "invalid_chapter_completed"}), 400
    try:
        chunk_index = (
            int(payload["chunk_index"])
            if payload.get("chunk_index") is not None
            else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_chunk_index"}), 400
    if chunk_index is not None and chunk_index < 0:
        return jsonify({"error": "invalid_chunk_index"}), 400
    supplied_chunk_id = str(payload.get("chunk_id") or "").strip()[:160] or None
    last_block_id = str(payload.get("last_block_id") or "")[:64] or None
    if (chunk_index is not None or supplied_chunk_id or last_block_id) and chapter_id is None:
        return jsonify({"error": "chapter_checkpoint_required"}), 400
    chapter_completed = payload.get("chapter_completed", False)
    if last_block_id and not chapter_completed and (
        not last_block_id.startswith("b") or not last_block_id[1:].isdigit()
    ):
        return jsonify({"error": "invalid_last_block_id"}), 400
    if chapter_completed and (chapter_id is None or chunk_index is None):
        return jsonify({"error": "completed_chapter_checkpoint_required"}), 400
    if (supplied_chunk_id or last_block_id) and chunk_index is None:
        return jsonify({"error": "chunk_index_required"}), 400

    resolved_chunk_id = None
    normalized_last_block = False
    if checkpoint_chapter and (chunk_index is not None or supplied_chunk_id or last_block_id):
        blocks = _chapter_blocks(checkpoint_chapter["content_html"])
        chunks = _chunk_chapter_blocks(blocks)
        if chunk_index is not None and chunk_index >= len(chunks):
            return jsonify(
                {"error": "chunk_not_found", "chunk_count": len(chunks)}
            ), 404
        if chapter_completed and chunk_index != len(chunks) - 1:
            return (
                jsonify(
                    {
                        "error": "chapter_not_fully_read",
                        "final_chunk_index": len(chunks) - 1,
                    }
                ),
                400,
            )
        if chunk_index is not None:
            resolved_chunk_id = _chunk_id(chapter_id, chunk_index)
            if supplied_chunk_id and supplied_chunk_id != resolved_chunk_id:
                return jsonify({"error": "chunk_chapter_mismatch"}), 400
            chunk_block_ids = {
                block["block_id"] for block in chunks[chunk_index]["blocks"]
            }
            if chapter_completed:
                # Completion is authoritative only at the final chunk. Normalize
                # a missing/stale last block to that chunk's final stable block.
                final_block_id = chunks[-1]["blocks"][-1]["block_id"]
                if last_block_id not in chunk_block_ids:
                    last_block_id = final_block_id
                    normalized_last_block = True
            elif last_block_id and last_block_id not in chunk_block_ids:
                return jsonify({"error": "last_block_not_in_chunk"}), 400
    elif supplied_chunk_id:
        return jsonify({"error": "chunk_checkpoint_required"}), 400
    state = db.execute(
        """
        insert into ai_reading_state (
            book_id, last_chapter_read, last_chunk_index, last_chunk_id, last_block_id,
            chapter_completed, last_annotation_seen
        ) values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (book_id) do update set
            last_chapter_read = coalesce(excluded.last_chapter_read, ai_reading_state.last_chapter_read),
            last_chunk_index = case
                when excluded.last_chapter_read is null then ai_reading_state.last_chunk_index
                when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read
                    then excluded.last_chunk_index
                else coalesce(excluded.last_chunk_index, ai_reading_state.last_chunk_index)
            end,
            last_chunk_id = case
                when excluded.last_chapter_read is null then ai_reading_state.last_chunk_id
                when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read
                    then excluded.last_chunk_id
                else coalesce(excluded.last_chunk_id, ai_reading_state.last_chunk_id)
            end,
            last_block_id = case
                when excluded.last_chapter_read is null then ai_reading_state.last_block_id
                when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read
                    then excluded.last_block_id
                else coalesce(excluded.last_block_id, ai_reading_state.last_block_id)
            end,
            chapter_completed = case
                when excluded.last_chapter_read is null then ai_reading_state.chapter_completed
                when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read
                    then excluded.chapter_completed
                when %s then excluded.chapter_completed
                else ai_reading_state.chapter_completed
            end,
            last_annotation_seen = coalesce(excluded.last_annotation_seen, ai_reading_state.last_annotation_seen),
            updated_at = now()
        returning book_id, last_chapter_read, last_chunk_index, last_chunk_id, last_block_id,
                  chapter_completed, last_annotation_seen, updated_at
        """,
        (
            book_id,
            chapter_id,
            chunk_index,
            resolved_chunk_id,
            last_block_id,
            chapter_completed,
            annotation_id,
            chapter_completed_supplied,
        ),
    )
    if chapter_completed and chapter_id is not None:
        # Keep the current checkpoint as-is and separately record the verified
        # final-chunk completion used by the V2 whole-book memory layer.
        from memories import record_ai_chapter_completion

        record_ai_chapter_completion(
            book_id,
            chapter_id,
            resolved_chunk_id,
            last_block_id,
            state.get("updated_at"),
        )
    return jsonify(
        {
            "ai_reading_state": _json_safe(state),
            "last_block_normalized": normalized_last_block,
        }
    )


def _chunk_id(chapter_id: UUID | str, chunk_index: int) -> str:
    """Return a deterministic ID tied to the same chapter/block chunk source."""
    return f"{chapter_id}:{chunk_index}"


def _chapter_blocks(content_html: str) -> list[dict[str, Any]]:
    """Return the persisted block IDs and their exact DOM text order."""
    soup = BeautifulSoup(content_html or "", "html.parser")
    blocks = []
    for block_order, element in enumerate(soup.select("[data-block-id]")):
        block_id = str(element.get("data-block-id") or "")
        text = element.get_text("", strip=False)
        if block_id and text.strip():
            blocks.append(
                {"block_id": block_id, "block_order": block_order, "text": text}
            )
    if not blocks:
        text = soup.get_text("", strip=False)
        blocks.append({"block_id": "b000001", "block_order": 0, "text": text})
    return blocks


def _text_across_blocks(
    blocks: list[dict[str, Any]],
    start_block_id: str,
    start_offset: int,
    end_block_id: str,
    end_offset: int,
) -> str:
    indexes = {block["block_id"]: index for index, block in enumerate(blocks)}
    if start_block_id not in indexes or end_block_id not in indexes:
        return ""
    start_index = indexes[start_block_id]
    end_index = indexes[end_block_id]
    if start_index > end_index or start_offset < 0 or end_offset < 0:
        return ""
    start_text = blocks[start_index]["text"]
    end_text = blocks[end_index]["text"]
    if start_offset > len(start_text) or end_offset > len(end_text):
        return ""
    if start_index == end_index:
        return start_text[start_offset:end_offset] if end_offset >= start_offset else ""
    selected = [start_text[start_offset:]]
    selected.extend(block["text"] for block in blocks[start_index + 1 : end_index])
    selected.append(end_text[:end_offset])
    return "\n".join(selected)


def _chunk_chapter_blocks(
    blocks: list[dict[str, Any]],
    target_chars: int = ACTION_CHUNK_TARGET_CHARS,
    max_chars: int = ACTION_CHUNK_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Group stable blocks, splitting only an individually oversized block."""
    segments = []
    for block in blocks:
        segments.extend(_split_block_segment(block, target_chars, max_chars))

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_length = 0
    for segment in segments:
        separator = 2 if current else 0
        segment_length = len(segment["text"])
        if current and current_length + separator + segment_length > target_chars:
            chunks.append(current)
            current = []
            current_length = 0
            separator = 0
        current.append(segment)
        current_length += separator + segment_length
    if current or not chunks:
        chunks.append(current)
    return [
        {"blocks": chunk, "text": "\n\n".join(item["text"] for item in chunk)}
        for chunk in chunks
    ]


def _split_block_segment(
    block: dict[str, Any], target_chars: int, max_chars: int
) -> list[dict[str, Any]]:
    text = block["text"]
    if len(text) <= max_chars:
        return [
            {
                "block_id": block["block_id"],
                "block_order": block["block_order"],
                "start_offset": 0,
                "end_offset": len(text),
                "text": text,
            }
        ]
    segments = []
    start = 0
    sentence_endings = set("。！？!?；;.!?\n")
    while len(text) - start > max_chars:
        ideal = min(start + target_chars, len(text))
        lower_bound = start + max(1, target_chars // 2)
        cut = ideal
        for position in range(ideal, lower_bound, -1):
            if text[position - 1] in sentence_endings:
                cut = position
                break
        if cut <= start:
            cut = min(start + max_chars, len(text))
        segments.append(
            {
                "block_id": block["block_id"],
                "block_order": block["block_order"],
                "start_offset": start,
                "end_offset": cut,
                "text": text[start:cut],
            }
        )
        start = cut
    segments.append(
        {
            "block_id": block["block_id"],
            "block_order": block["block_order"],
            "start_offset": start,
            "end_offset": len(text),
            "text": text[start:],
        }
    )
    return segments


def _anchor_chunk_index(
    anchor: dict[str, Any] | None, chunks: list[dict[str, Any]]
) -> int | None:
    if not anchor:
        return None
    if anchor.get("scope") == "chapter":
        return 0
    block_id = anchor.get("start_block_id")
    try:
        offset = int(anchor.get("start_offset") or 0)
    except (TypeError, ValueError):
        return None
    boundary_candidate = None
    for chunk_index, chunk in enumerate(chunks):
        for block in chunk["blocks"]:
            if block["block_id"] != block_id:
                continue
            if block["start_offset"] <= offset < block["end_offset"]:
                return chunk_index
            if offset == block["end_offset"]:
                boundary_candidate = chunk_index
    return boundary_candidate


def _text_readiness_response(book: dict[str, Any] | None):
    if not book or book.get("text_index_status") == "ready":
        return None
    status = str(book.get("text_index_status") or "pending")
    if status in {"pending", "processing"}:
        return (
            jsonify(
                {
                    "error": "text_index_not_ready",
                    "status": status,
                    "publication_ready": bool(book.get("publication_ready")),
                    "retryable": status == "pending",
                }
            ),
            409,
        )
    return (
        jsonify(
            {
                "error": "text_index_failed",
                "status": "failed",
                "failure_code": book.get("text_index_failure_code"),
                "publication_ready": bool(book.get("publication_ready")),
                "retryable": book.get("text_index_failure_code")
                != "text_index_mapping_failed",
            }
        ),
        409,
    )


def _clean_book_field(value: Any) -> str:
    cleaned = " ".join(str(value or "").replace("\x00", "").split())
    return cleaned[:1000]


def _adjacent_chapters(book_id: UUID, chapter_index: int) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select id, chapter_index, title, content_text as full_text, word_count
        from chapters
        where book_id = %s and chapter_index in (%s, %s)
        order by chapter_index
        """,
        (book_id, chapter_index - 1, chapter_index + 1),
    )
    return [_json_safe(row) for row in rows]


def _materialize_reader_html(
    fragment: str, book_id: UUID, chapters: list[dict[str, Any]] | None = None
) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for image in soup.select("img[data-asset-path]"):
        path = image.get("data-asset-path")
        if path:
            image["src"] = url_for(
                "reading.get_asset", book_id=book_id, asset_path=path
            )
            image["loading"] = "lazy"
    targets = {
        _chapter_href_key(str(row.get("href") or "")): row
        for row in (chapters or [])
    }
    for anchor in soup.select("a[data-epub-target-href]"):
        target_href = str(anchor.get("data-epub-target-href") or "")
        fragment_name = unquote(
            str(anchor.get("data-epub-target-fragment") or "")
        )
        link_kind = str(anchor.get("data-epub-link-kind") or "chapter")
        target = targets.get(_chapter_href_key(target_href))
        anchor["href"] = "#"
        anchor["data-rh-internal"] = "true"
        anchor["data-rh-link-kind"] = link_kind
        anchor["data-rh-target-fragment"] = fragment_name
        for attribute in (
            "data-epub-target-href",
            "data-epub-target-fragment",
            "data-epub-link-kind",
        ):
            anchor.attrs.pop(attribute, None)
        if not target:
            anchor["data-rh-target-missing"] = "true"
            anchor["data-rh-error-code"] = (
                "footnote_target_missing"
                if link_kind == "footnote"
                else "internal_link_target_missing"
            )
            continue
        block_id = _fragment_block_id(target["content_html"], fragment_name)
        if fragment_name and not block_id:
            anchor["data-rh-target-missing"] = "true"
            anchor["data-rh-error-code"] = (
                "footnote_target_missing"
                if link_kind == "footnote"
                else "internal_link_target_missing"
            )
            continue
        anchor["data-rh-chapter-id"] = str(target["id"])
        anchor["data-rh-block-id"] = block_id or "b000001"
    return str(soup)


def _internal_link_hrefs(fragment: str) -> set[str]:
    soup = BeautifulSoup(fragment, "html.parser")
    return {
        str(anchor.get("data-epub-target-href"))
        for anchor in soup.select("a[data-epub-target-href]")
        if anchor.get("data-epub-target-href")
    }


def _fragment_block_id(fragment: str, fragment_name: str) -> str | None:
    soup = BeautifulSoup(fragment, "html.parser")
    if not fragment_name:
        first = soup.select_one("[data-block-id]")
        return str(first.get("data-block-id")) if first else None
    marker = soup.find(
        lambda tag: isinstance(tag, Tag)
        and str(tag.get("data-epub-fragment") or "") == fragment_name
    )
    if marker is None:
        return None
    block = marker if marker.get("data-block-id") else marker.find_parent(attrs={"data-block-id": True})
    if block is None:
        block = marker.find_next(attrs={"data-block-id": True})
    return str(block.get("data-block-id")) if block else None


def _chapter_href_key(href: str) -> str:
    return unquote(href).replace("\\", "/").lstrip("/").lower()


def _cleanup_uploaded_objects(object_paths: list[str]) -> None:
    try:
        object_storage.delete_objects(object_paths)
    except object_storage.ObjectStorageError:
        # Preserve the original upload/database error. A failed rollback cleanup
        # is visible in Supabase Storage and can be removed by book UUID prefix.
        logger.exception(
            "book_import_cleanup_failed object_count=%s", len(object_paths)
        )


def _bulk_execute(
    conn: Any, query: str, rows: Iterable[tuple[Any, ...]]
) -> None:
    """Use psycopg pipeline-backed executemany; keep simple test doubles usable."""
    executemany = getattr(conn, "executemany", None)
    if callable(executemany):
        executemany(query, rows)
        return
    for row in rows:
        conn.execute(query, row)


def _spool_upload(upload: Any, target: Path) -> tuple[int, str]:
    """Copy the request stream to disk while hashing it with bounded memory."""
    digest = hashlib.sha256()
    byte_size = 0
    with target.open("wb") as destination:
        while chunk := upload.stream.read(UPLOAD_STREAM_CHUNK_BYTES):
            destination.write(chunk)
            digest.update(chunk)
            byte_size += len(chunk)
    return byte_size, digest.hexdigest()


def _stage_normalized_chapters(
    chapters: list[Any], temp_root: Path
) -> list[tuple[Path, Path]]:
    """Move normalized chapter bodies out of RAM until the DB transaction."""
    staged: list[tuple[Path, Path]] = []
    chapter_root = temp_root / "normalized-chapters"
    chapter_root.mkdir()
    for index, chapter in enumerate(chapters):
        html_path = chapter_root / f"{index:06d}.html"
        text_path = chapter_root / f"{index:06d}.txt"
        html_path.write_text(chapter.content_html, encoding="utf-8")
        text_path.write_text(chapter.content_text, encoding="utf-8")
        chapter.content_html = ""
        chapter.content_text = ""
        staged.append((html_path, text_path))
    return staged


def _release_unused_heap() -> None:
    """Best-effort Linux heap trim after large transient DOMs are released."""
    try:
        import ctypes

        trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
        if trim is not None:
            trim(0)
    except (AttributeError, OSError):
        return


def _current_rss_mb() -> float:
    try:
        with open("/proc/self/statm", encoding="ascii") as status:
            resident_pages = int(status.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return _peak_rss_mb()


def _peak_rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class _ImportMemoryProbe:
    """Collect lightweight process RSS checkpoints for structured logs."""

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self.mark("request")

    def mark(self, stage: str) -> None:
        self._values[f"{stage}_rss_mb"] = round(_current_rss_mb(), 2)

    def fields(self) -> dict[str, float]:
        return {
            **self._values,
            "peak_rss_mb": round(_peak_rss_mb(), 2),
        }


def _duration(started_at: float) -> float:
    return round(time.perf_counter() - started_at, 4)


def _log_import(event: str, **fields: Any) -> None:
    payload = {"event": f"book_import_{event}", **fields}
    logger.info("book_import %s", json.dumps(payload, ensure_ascii=False, default=str))


def _serialize_book(row: dict[str, Any]) -> dict[str, Any]:
    field_names = (
        "id",
        "title",
        "author",
        "format",
        "source_filename",
        "chapter_count",
        "toc",
        "publication_ready",
        "reader_engine",
        "text_index_status",
        "text_index_failure_code",
        "publication_validated_at",
        "text_index_updated_at",
        "locator_bridge_status",
        "locator_bridge_version",
        "locator_bridge_failure_code",
        "locator_bridge_updated_at",
        "created_at",
        "updated_at",
        "progress_chapter_id",
        "progress_chapter_index",
        "progress_percentage",
        "current_chapter",
        "publication_locator",
        "publication_progression",
    )
    result = _json_safe({name: row[name] for name in field_names if name in row})
    book_id = row["id"]
    result["cover_url"] = (
        url_for(
            "reading.get_asset",
            book_id=book_id,
            asset_path=row["cover_asset_path"],
        )
        if row.get("cover_asset_path")
        else None
    )
    status = str(row.get("text_index_status") or "ready")
    result["text_index_ready"] = status == "ready"
    result["ai_reading_note"] = {
        "pending": "夏夏正在整理这本书",
        "processing": "夏夏正在整理这本书",
        "failed": "这本书可以阅读，但夏夏暂时还不能完整读取",
    }.get(status, "")
    return result


def _serialize_reading_state(row: dict[str, Any]) -> dict[str, Any]:
    user_progress = {
        "chapter_id": str(row["chapter_id"]) if row.get("chapter_id") else None,
        "chapter_index": row.get("chapter_index"),
        "chapter_title": row.get("current_chapter"),
        "position": row.get("position") or {},
        "percentage": float(row.get("percentage") or 0),
        "updated_at": row["progress_updated_at"].isoformat()
        if row.get("progress_updated_at")
        else None,
    }
    if row.get("_publication_current"):
        locator = row.get("publication_locator") or {}
        user_progress.update(
            {
                "engine": "foliate-js",
                "progression": float(row.get("publication_progression") or 0),
                "section": {
                    "href": locator.get("href"),
                    "section_index": locator.get("section_index"),
                    "cfi": locator.get("cfi"),
                },
            }
        )
    return {
        "book": _serialize_book(row),
        "user_progress": user_progress,
        "xiaxia_progress": {
            "last_chapter_read": str(row["last_chapter_read"])
            if row.get("last_chapter_read")
            else None,
            "last_chunk_index": row.get("last_chunk_index"),
            "last_chunk_id": row.get("last_chunk_id"),
            "last_block_id": row.get("last_block_id"),
            "chapter_completed": bool(row.get("chapter_completed", False)),
            "last_annotation_seen": str(row["last_annotation_seen"])
            if row.get("last_annotation_seen")
            else None,
            "updated_at": row["ai_updated_at"].isoformat()
            if row.get("ai_updated_at")
            else None,
        },
        "pending_annotation_count": int(row["pending_count"]),
    }


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    return value


def _uuid_or_none(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
