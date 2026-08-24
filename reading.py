"""Book import, bookshelf, reader, progress, and Xiaxia context APIs."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from bs4 import BeautifulSoup
from flask import Blueprint, Response, jsonify, request, url_for
from psycopg.types.json import Jsonb

import database as db
import storage as object_storage
from auth import action_required, api_or_session_required, web_api_required
from epub_parser import BookParseError, parse_uploaded_book


reading_bp = Blueprint("reading", __name__)


BOOK_FIELDS = """
    b.id, b.title, b.author, b.format, b.source_filename,
    b.cover_asset_path, b.chapter_count, b.toc, b.created_at, b.updated_at
"""

ACTION_CHUNK_TARGET_CHARS = 6000
ACTION_CHUNK_MAX_CHARS = 8000


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
@web_api_required
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
    if not rows and not db.fetch_one("select id from books where id = %s", (book_id,)):
        return jsonify({"error": "book_not_found"}), 404
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
                  cover_asset_path, chapter_count, toc, created_at, updated_at
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
        "select id, title, source_object_path from books where id = %s", (book_id,)
    )
    if not book:
        return jsonify({"error": "book_not_found"}), 404
    assets = db.fetch_all(
        "select object_path from book_assets where book_id = %s order by object_path",
        (book_id,),
    )
    object_paths = [book["source_object_path"]] + [row["object_path"] for row in assets]
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


@reading_bp.get("/api/reading/state")
@api_or_session_required
def get_reading_state():
    current = db.fetch_one(
        f"""
        select {BOOK_FIELDS}, rp.chapter_id, rp.chapter_index, rp.position,
               rp.percentage, rp.updated_at as progress_updated_at,
               c.title as current_chapter,
               ais.last_chapter_read, ais.last_chunk_index, ais.last_chunk_id,
               ais.last_block_id,
               ais.chapter_completed, ais.last_annotation_seen,
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
            return jsonify({"error": "chapter_not_found"}), 404
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
