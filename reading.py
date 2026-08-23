"""Book import, bookshelf, reader, progress, and Xiaxia context APIs."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from bs4 import BeautifulSoup
from flask import Blueprint, Response, jsonify, request, url_for
from psycopg.types.json import Jsonb

import database as db
import storage as object_storage
from auth import api_or_session_required
from epub_parser import BookParseError, parse_uploaded_book


reading_bp = Blueprint("reading", __name__)


BOOK_FIELDS = """
    b.id, b.title, b.author, b.format, b.source_filename,
    b.cover_asset_path, b.chapter_count, b.toc, b.created_at, b.updated_at
"""


@reading_bp.get("/api/books")
@api_or_session_required
def list_books():
    rows = db.fetch_all(
        f"""
        select {BOOK_FIELDS},
               rp.chapter_id as progress_chapter_id,
               rp.chapter_index as progress_chapter_index,
               rp.percentage as progress_percentage,
               c.title as current_chapter
        from books b
        left join reading_progress rp on rp.book_id = b.id
        left join chapters c on c.id = rp.chapter_id
        order by coalesce(rp.updated_at, b.created_at) desc
        """
    )
    return jsonify({"books": [_serialize_book(row) for row in rows]})


@reading_bp.post("/api/books")
@api_or_session_required
def upload_book():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file_required"}), 400
    filename = (
        upload.filename.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")[:255]
    )
    if not filename:
        return jsonify({"error": "invalid_filename"}), 400
    raw = upload.read()
    try:
        parsed = parse_uploaded_book(filename, raw)
    except BookParseError as exc:
        return jsonify({"error": "book_parse_failed", "message": str(exc)}), 422

    book_id = uuid4()
    source_media_type = (
        "application/epub+zip" if parsed.format == "epub" else "text/plain"
    )
    source_object_path = f"books/{book_id}/source/source.{parsed.format}"
    uploaded_objects: list[str] = []
    try:
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

            object_storage.upload_bytes(source_object_path, raw, source_media_type)
            uploaded_objects.append(source_object_path)

            asset_records: list[tuple[Any, str]] = []
            for asset in parsed.assets:
                object_path = object_storage.object_path_for_asset(
                    book_id, asset.path, asset.path == parsed.cover_asset_path
                )
                object_storage.upload_bytes(object_path, asset.data, asset.media_type)
                uploaded_objects.append(object_path)
                asset_records.append((asset, object_path))

            book = conn.execute(
                """
                insert into books (
                    id, title, author, format, source_filename, source_sha256,
                    source_object_path, source_media_type, source_byte_size,
                    cover_asset_path, chapter_count, toc
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id, title, author, format, source_filename,
                          cover_asset_path, chapter_count, toc, created_at, updated_at
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
                    len(raw),
                    parsed.cover_asset_path,
                    len(parsed.chapters),
                    Jsonb(parsed.toc),
                ),
            ).fetchone()

            for asset, object_path in asset_records:
                conn.execute(
                    """
                    insert into book_assets (
                        book_id, asset_path, object_path, media_type, byte_size
                    ) values (%s, %s, %s, %s, %s)
                    """,
                    (
                        book_id,
                        asset.path,
                        object_path,
                        asset.media_type,
                        len(asset.data),
                    ),
                )

            first_chapter = None
            for chapter in parsed.chapters:
                inserted = conn.execute(
                    """
                    insert into chapters (
                        book_id, chapter_index, title, href, content_html,
                        content_text, word_count
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    returning id, chapter_index
                    """,
                    (
                        book_id,
                        chapter.chapter_index,
                        chapter.title,
                        chapter.href,
                        chapter.content_html,
                        chapter.content_text,
                        chapter.word_count,
                    ),
                ).fetchone()
                if first_chapter is None:
                    first_chapter = inserted

            if first_chapter:
                conn.execute(
                    """
                    insert into reading_progress (
                        book_id, chapter_id, chapter_index, position, percentage
                    ) values (%s, %s, %s, %s, 0)
                    """,
                    (
                        book_id,
                        first_chapter["id"],
                        first_chapter["chapter_index"],
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
                "insert into ai_reading_state (book_id) values (%s)", (book_id,)
            )
    except object_storage.ObjectStorageError:
        _cleanup_uploaded_objects(uploaded_objects)
        return jsonify({"error": "private_storage_unavailable"}), 502
    except Exception:
        _cleanup_uploaded_objects(uploaded_objects)
        raise

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
    payload = _serialize_book(book)
    payload["chapters"] = [_json_safe(row) for row in chapters]
    payload["progress"] = _json_safe(progress) if progress else None
    return jsonify({"book": payload})


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
        return jsonify({"error": "chapter_not_found"}), 404
    payload = _json_safe(chapter)
    payload["content_html"] = _materialize_asset_urls(chapter["content_html"], book_id)
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
@api_or_session_required
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
        sanitized_position = {
            "block_id": str(position.get("block_id") or "b000001")[:64],
            "char_offset": max(0, int(position.get("char_offset") or 0)),
            "scroll_fraction": max(
                0.0, min(1.0, float(position.get("scroll_fraction") or 0))
            ),
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
    return jsonify({"progress": _json_safe(row)})


@reading_bp.get("/api/reading/state")
@api_or_session_required
def get_reading_state():
    current = db.fetch_one(
        f"""
        select {BOOK_FIELDS}, rp.chapter_id, rp.chapter_index, rp.position,
               rp.percentage, rp.updated_at as progress_updated_at,
               c.title as current_chapter,
               ais.last_chapter_read, ais.last_annotation_seen,
               ais.updated_at as ai_updated_at,
               (select count(*) from annotations a
                where a.book_id = b.id and a.status = 'pending') as pending_count
        from reading_progress rp
        join books b on b.id = rp.book_id
        join chapters c on c.id = rp.chapter_id
        left join ai_reading_state ais on ais.book_id = b.id
        order by rp.updated_at desc limit 1
        """
    )
    if not current:
        return jsonify({"current": None, "message": "书架中还没有阅读进度"})
    return jsonify({"current": _serialize_reading_state(current)})


@reading_bp.get("/api/reading/context")
@api_or_session_required
def get_reading_context():
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

    if not chapter_id:
        current = db.fetch_one(
            "select chapter_id from reading_progress order by updated_at desc limit 1"
        )
        chapter_id = current["chapter_id"] if current else None
    if not chapter_id:
        return jsonify({"error": "no_current_reading"}), 404

    chapter = db.fetch_one(
        """
        select c.id, c.book_id, c.chapter_index, c.title, c.content_text,
               c.word_count, b.title as book_title, b.author as book_author,
               b.chapter_count
        from chapters c join books b on b.id = c.book_id
        where c.id = %s
        """,
        (chapter_id,),
    )
    if not chapter:
        return jsonify({"error": "chapter_not_found"}), 404
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
               a.comment, a.status, a.created_at, a.updated_at,
               ar.response as xiaxia_response
        from annotations a
        left join annotation_replies ar on ar.annotation_id = a.id
        where a.chapter_id = %s
        order by a.created_at
        """,
        (chapter_id,),
    )
    response: dict[str, Any] = {
        "book": {
            "id": str(chapter["book_id"]),
            "title": chapter["book_title"],
            "author": chapter["book_author"],
            "chapter_count": chapter["chapter_count"],
        },
        "chapter": {
            "id": str(chapter["id"]),
            "chapter_index": chapter["chapter_index"],
            "title": chapter["title"],
            "full_text": chapter["content_text"],
            "word_count": chapter["word_count"],
        },
        "reading_progress": _json_safe(progress) if progress else None,
        "annotations": [_json_safe(row) for row in chapter_annotations],
        "focus_annotation": _json_safe(annotation) if annotation else None,
    }
    if include_adjacent:
        response["adjacent_chapters"] = _adjacent_chapters(
            chapter["book_id"], chapter["chapter_index"]
        )
    return jsonify(response)


@reading_bp.post("/api/ai/progress")
@api_or_session_required
def save_ai_progress():
    payload = request.get_json(silent=True) or {}
    book_id = _uuid_or_none(payload.get("book_id"))
    chapter_id = _uuid_or_none(payload.get("chapter_id"))
    annotation_id = _uuid_or_none(payload.get("last_annotation_seen"))
    if not book_id:
        return jsonify({"error": "book_id_required"}), 400
    if chapter_id and not db.fetch_one(
        "select id from chapters where id = %s and book_id = %s", (chapter_id, book_id)
    ):
        return jsonify({"error": "chapter_not_found"}), 404
    if annotation_id and not db.fetch_one(
        "select id from annotations where id = %s and book_id = %s",
        (annotation_id, book_id),
    ):
        return jsonify({"error": "annotation_not_found"}), 404
    state = db.execute(
        """
        insert into ai_reading_state (
            book_id, last_chapter_read, last_annotation_seen
        ) values (%s, %s, %s)
        on conflict (book_id) do update set
            last_chapter_read = coalesce(excluded.last_chapter_read, ai_reading_state.last_chapter_read),
            last_annotation_seen = coalesce(excluded.last_annotation_seen, ai_reading_state.last_annotation_seen),
            updated_at = now()
        returning book_id, last_chapter_read, last_annotation_seen, updated_at
        """,
        (book_id, chapter_id, annotation_id),
    )
    return jsonify({"ai_reading_state": _json_safe(state)})


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


def _materialize_asset_urls(fragment: str, book_id: UUID) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for image in soup.select("img[data-asset-path]"):
        path = image.get("data-asset-path")
        if path:
            image["src"] = url_for(
                "reading.get_asset", book_id=book_id, asset_path=path
            )
            image["loading"] = "lazy"
    return str(soup)


def _cleanup_uploaded_objects(object_paths: list[str]) -> None:
    try:
        object_storage.delete_objects(object_paths)
    except object_storage.ObjectStorageError:
        # Preserve the original upload/database error. A failed rollback cleanup
        # is visible in Supabase Storage and can be removed by book UUID prefix.
        pass


def _serialize_book(row: dict[str, Any]) -> dict[str, Any]:
    field_names = (
        "id",
        "title",
        "author",
        "format",
        "source_filename",
        "chapter_count",
        "toc",
        "created_at",
        "updated_at",
        "progress_chapter_id",
        "progress_chapter_index",
        "progress_percentage",
        "current_chapter",
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
    return result


def _serialize_reading_state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "book": _serialize_book(row),
        "user_progress": {
            "chapter_id": str(row["chapter_id"]),
            "chapter_index": row["chapter_index"],
            "chapter_title": row["current_chapter"],
            "position": row["position"],
            "percentage": float(row["percentage"]),
            "updated_at": row["progress_updated_at"].isoformat(),
        },
        "xiaxia_progress": {
            "last_chapter_read": str(row["last_chapter_read"])
            if row.get("last_chapter_read")
            else None,
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
