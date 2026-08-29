"""Persistent user annotations, Xiaxia replies, and independent thoughts."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from flask import Blueprint, current_app, jsonify, request
from psycopg.types.json import Jsonb

import database as db
import operations
from auth import action_required, api_or_session_required, web_api_required
from reading import _chapter_blocks, _json_safe, _text_across_blocks, _uuid_or_none
from locator_bridge import (
    BRIDGE_VERSION,
    LocatorBridgeError,
    bridge_state,
    legacy_locator_seed,
    map_engine_selection,
    validate_backfill_locator,
)


annotations_bp = Blueprint("annotations", __name__)


USER_ANNOTATION_FIELDS = """
    a.id, a.book_id, a.chapter_id, a.selected_text,
    a.start_block_id, a.start_offset, a.end_block_id, a.end_offset,
    a.prefix_text, a.suffix_text, a.comment, a.status,
    a.owner, a.content_type,
    a.created_at, a.updated_at,
    ar.response as xiaxia_response, ar.owner as reply_owner,
    ar.content_type as reply_content_type, ar.created_at as reply_created_at,
    ar.updated_at as reply_updated_at
"""

XIA_THOUGHT_FIELDS = """
    xt.id, xt.book_id, xt.chapter_id, xt.scope, xt.mark_type, xt.content,
    xt.selected_text, xt.start_block_id, xt.start_offset,
    xt.end_block_id, xt.end_offset, xt.prefix_text, xt.suffix_text,
    xt.owner, xt.content_type, xt.created_at, xt.updated_at,
    tur.id as user_reply_id, tur.response as user_response,
    tur.owner as user_reply_owner, tur.content_type as user_reply_content_type,
    tur.created_at as user_reply_created_at, tur.updated_at as user_reply_updated_at
"""


@annotations_bp.get("/api/books/<uuid:book_id>/chapters/<uuid:chapter_id>/annotations")
@api_or_session_required
def list_chapter_annotations(book_id: UUID, chapter_id: UUID):
    annotations = db.fetch_all(
        f"""
        select {USER_ANNOTATION_FIELDS}
        from annotations a
        left join annotation_replies ar on ar.annotation_id = a.id
        where a.book_id = %s and a.chapter_id = %s
        order by a.created_at
        """,
        (book_id, chapter_id),
    )
    thoughts = db.fetch_all(
        f"""
        select {XIA_THOUGHT_FIELDS}
        from xiaxia_thoughts xt
        left join thought_user_replies tur on tur.thought_id = xt.id
        where xt.book_id = %s and xt.chapter_id = %s
        order by xt.created_at
        """,
        (book_id, chapter_id),
    )
    return jsonify(
        {
            "annotations": [_json_safe(row) for row in annotations],
            "xiaxia_thoughts": [_json_safe(row) for row in thoughts],
            "sync_token": _latest_sync_token(annotations, thoughts),
        }
    )


@annotations_bp.get("/api/books/<uuid:book_id>/annotations")
@api_or_session_required
def list_book_annotations(book_id: UUID):
    filter_name = request.args.get("filter", "all").strip().lower()
    filters = {
        "all": "true",
        "highlights": "a.comment = ''",
        "comments": "a.comment <> ''",
        "replies": "ar.annotation_id is not null",
    }
    if filter_name not in filters:
        return jsonify({"error": "invalid_annotation_filter"}), 400

    annotations = db.fetch_all(
        f"""
        select {USER_ANNOTATION_FIELDS},
               c.title as chapter_title, c.chapter_index
        from annotations a
        join chapters c on c.id = a.chapter_id
        left join annotation_replies ar on ar.annotation_id = a.id
        where a.book_id = %s and {filters[filter_name]}
        order by c.chapter_index, a.created_at
        """,
        (book_id,),
    )
    thoughts: list[dict[str, Any]] = []
    if filter_name == "all":
        thoughts = db.fetch_all(
            f"""
            select {XIA_THOUGHT_FIELDS},
                   c.title as chapter_title, c.chapter_index
            from xiaxia_thoughts xt
            join chapters c on c.id = xt.chapter_id
            left join thought_user_replies tur on tur.thought_id = xt.id
            where xt.book_id = %s
            order by c.chapter_index, xt.created_at
            """,
            (book_id,),
        )
    return jsonify(
        {
            "filter": filter_name,
            "annotations": [_json_safe(row) for row in annotations],
            "xiaxia_thoughts": [_json_safe(row) for row in thoughts],
            "counts": {
                "annotations": len(annotations),
                "xiaxia_thoughts": len(thoughts),
            },
        }
    )


@annotations_bp.post("/api/annotations")
@web_api_required
def create_annotation():
    payload = request.get_json(silent=True) or {}
    book_id = _uuid_or_none(payload.get("book_id"))
    comment = _clean_text(payload.get("comment"), 20_000, allow_empty=True)
    engine_locator = payload.get("engine_locator")
    if engine_locator is not None:
        if not current_app.config.get("DUAL_ANCHOR_ENABLED", False):
            return jsonify({"error": "dual_anchor_disabled"}), 409
        if not book_id or not isinstance(engine_locator, dict):
            return jsonify({"error": "incomplete_annotation"}), 400
        try:
            mapped = map_engine_selection(book_id, engine_locator)
        except LocatorBridgeError as exc:
            return jsonify({"error": exc.code, "message": exc.message}), exc.status
        chapter_id = mapped["chapter_id"]
        selected_text = mapped["selected_text"]
        start_block_id = mapped["start_block_id"]
        start_offset = mapped["start_offset"]
        end_block_id = mapped["end_block_id"]
        end_offset = mapped["end_offset"]
        prefix_text = mapped["prefix_text"]
        suffix_text = mapped["suffix_text"]
        engine_locator = mapped["engine_locator"]
    else:
        chapter_id = _uuid_or_none(payload.get("chapter_id"))
        selected_text = _clean_selected_text(payload.get("selected_text"), 20_000)
        start_block_id = _clean_block_id(payload.get("start_block_id"))
        end_block_id = _clean_block_id(payload.get("end_block_id"))
        prefix_text = _clean_text(payload.get("prefix_text"), 500, allow_empty=True)
        suffix_text = _clean_text(payload.get("suffix_text"), 500, allow_empty=True)
        try:
            start_offset = int(payload.get("start_offset"))
            end_offset = int(payload.get("end_offset"))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_offsets"}), 400
        if not all((book_id, chapter_id, selected_text, start_block_id, end_block_id)):
            return jsonify({"error": "incomplete_annotation"}), 400
        if start_offset < 0 or end_offset < 0:
            return jsonify({"error": "invalid_offsets"}), 400
    chapter = db.fetch_one(
        "select id, content_html from chapters where id = %s and book_id = %s", (chapter_id, book_id)
    )
    if not chapter:
        return jsonify({"error": "chapter_not_found"}), 404
    if not _annotation_anchor_matches(
        chapter["content_html"], selected_text, start_block_id, start_offset,
        end_block_id, end_offset,
    ):
        return jsonify({"error": "locator_mapping_validation_failed"}), 422

    with db.transaction() as conn:
        row = conn.execute(
            """
            insert into annotations (
                book_id, chapter_id, selected_text,
                start_block_id, start_offset, end_block_id, end_offset,
                prefix_text, suffix_text, comment, status,
                engine_locator, engine_locator_version, engine_anchor_verified_at
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending',
                      %s, %s, case when %s is null then null else now() end)
            returning *
            """,
            (
                book_id, chapter_id, selected_text, start_block_id, start_offset,
                end_block_id, end_offset, prefix_text, suffix_text, comment,
                Jsonb(engine_locator) if engine_locator else None,
                BRIDGE_VERSION if engine_locator else None,
                Jsonb(engine_locator) if engine_locator else None,
            ),
        ).fetchone()
        operations.record(
            conn, actor="user", operation_type="create_annotation",
            target_type="user_annotation", target_id=row["id"],
            book_id=book_id, chapter_id=chapter_id,
            new_records=[operations.snapshot(row, "user_annotation")],
        )
    result = _json_safe(row)
    result["xiaxia_response"] = None
    return jsonify({"annotation": result}), 201


@annotations_bp.get("/api/books/<uuid:book_id>/engine-traces")
@web_api_required
def list_engine_traces(book_id: UUID):
    annotations = db.fetch_all(
        """
        select a.*, ar.response as xiaxia_response, c.href as chapter_href
        from annotations a
        join chapters c on c.id = a.chapter_id
        left join annotation_replies ar on ar.annotation_id = a.id
        where a.book_id = %s order by a.created_at
        """,
        (book_id,),
    )
    thoughts = db.fetch_all(
        """
        select xt.*, tur.response as user_response, c.href as chapter_href
        from xiaxia_thoughts xt
        join chapters c on c.id = xt.chapter_id
        left join thought_user_replies tur on tur.thought_id = xt.id
        where xt.book_id = %s order by xt.created_at
        """,
        (book_id,),
    )
    if not annotations and not thoughts and not db.fetch_one("select id from books where id = %s", (book_id,)):
        return jsonify({"error": "book_not_found"}), 404
    return jsonify(
        {
            "locator_bridge": _json_safe(bridge_state(book_id) or {}),
            "annotations": [_engine_trace_payload(book_id, row, "annotation") for row in annotations],
            "xiaxia_thoughts": [_engine_trace_payload(book_id, row, "thought") for row in thoughts],
        }
    )


@annotations_bp.put("/api/books/<uuid:book_id>/engine-traces/<record_type>/<uuid:record_id>/locator")
@web_api_required
def backfill_engine_trace(book_id: UUID, record_type: str, record_id: UUID):
    if not current_app.config.get("DUAL_ANCHOR_ENABLED", False):
        return jsonify({"error": "dual_anchor_disabled"}), 409
    payload = request.get_json(silent=True) or {}
    locator = payload.get("engine_locator")
    table = {"annotation": "annotations", "thought": "xiaxia_thoughts"}.get(record_type)
    if not table or not isinstance(locator, dict):
        return jsonify({"error": "locator_backfill_failed"}), 400
    with db.transaction() as conn:
        record = conn.execute(
            f"select * from {table} where id = %s and book_id = %s for update",
            (record_id, book_id),
        ).fetchone()
        if not record:
            return jsonify({"error": "reading_trace_not_found"}), 404
        try:
            sanitized = validate_backfill_locator(book_id, record, locator)
        except LocatorBridgeError as exc:
            return jsonify({"error": exc.code, "message": exc.message}), exc.status
        row = conn.execute(
            f"""
            update {table} set engine_locator = %s,
                engine_locator_version = %s,
                engine_anchor_verified_at = now(), updated_at = now()
            where id = %s returning *
            """,
            (Jsonb(sanitized), BRIDGE_VERSION, record_id),
        ).fetchone()
    return jsonify({"record": _json_safe(row)})


@annotations_bp.patch("/api/annotations/<uuid:annotation_id>")
@web_api_required
def update_annotation(annotation_id: UUID):
    payload = request.get_json(silent=True) or {}
    if "comment" not in payload:
        return jsonify({"error": "comment_required"}), 400
    comment = _clean_text(payload.get("comment"), 20_000, allow_empty=True)
    with db.transaction() as conn:
        existing = conn.execute(
            "select * from annotations where id = %s for update", (annotation_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "annotation_not_found"}), 404
        conn.execute(
            """
            update annotations set comment = %s,
                status = case when status = 'seen' then 'pending' else status end,
                updated_at = now() where id = %s
            """,
            (comment, annotation_id),
        )
        row = conn.execute(
            f"""
            select {USER_ANNOTATION_FIELDS} from annotations a
            left join annotation_replies ar on ar.annotation_id = a.id
            where a.id = %s
            """,
            (annotation_id,),
        ).fetchone()
        operations.record(
            conn, actor="user", operation_type="update_annotation",
            target_type="user_annotation", target_id=annotation_id,
            book_id=existing["book_id"], chapter_id=existing["chapter_id"],
            previous_records=[operations.snapshot(existing, "user_annotation")],
            new_records=[operations.snapshot(row, "user_annotation")],
        )
    return jsonify({"annotation": _json_safe(row)})


@annotations_bp.delete("/api/annotations/<uuid:annotation_id>")
@web_api_required
def delete_annotation(annotation_id: UUID):
    with db.transaction() as conn:
        existing = conn.execute(
            "select * from annotations where id = %s for update", (annotation_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "annotation_not_found"}), 404
        reply = conn.execute(
            "select * from annotation_replies where annotation_id = %s",
            (annotation_id,),
        ).fetchone()
        # annotation_replies.annotation_id uses ON DELETE CASCADE. The delete
        # therefore removes any reply atomically and cannot leave an orphan.
        conn.execute("delete from annotations where id = %s", (annotation_id,))
        snapshots = [operations.snapshot(existing, "user_annotation")]
        if reply:
            snapshots.append(operations.snapshot(reply, "xiaxia_reply"))
        operations.record(
            conn, actor="user", operation_type="delete_annotation",
            target_type="user_annotation", target_id=annotation_id,
            book_id=existing["book_id"], chapter_id=existing["chapter_id"],
            previous_records=snapshots,
        )
    return jsonify(
        {
            "deleted": True,
            "annotation_id": str(annotation_id),
            "deleted_reply": bool(reply),
        }
    )


@annotations_bp.get("/api/annotations/pending")
@action_required
def pending_annotations():
    requested_book = _uuid_or_none(request.args.get("book_id"))
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 100)
    except ValueError:
        return jsonify({"error": "invalid_limit"}), 400
    rows = db.fetch_all(
        """
        select a.id, a.book_id, a.chapter_id, a.selected_text,
               a.start_block_id, a.start_offset, a.end_block_id, a.end_offset,
               a.prefix_text, a.suffix_text, a.comment, a.status,
               a.created_at, a.updated_at,
               b.title as book_title, b.author as book_author,
               c.title as chapter_title, c.chapter_index
        from annotations a
        join books b on b.id = a.book_id
        join chapters c on c.id = a.chapter_id
        where a.status = 'pending' and (%s::uuid is null or a.book_id = %s::uuid)
        order by a.created_at
        limit %s
        """,
        (requested_book, requested_book, limit),
    )
    return jsonify(
        {
            "annotations": [_json_safe(row) for row in rows],
            "count": len(rows),
        }
    )


@annotations_bp.post("/api/annotations/<uuid:annotation_id>/seen")
@action_required
def mark_annotation_seen(annotation_id: UUID):
    with db.transaction() as conn:
        row = conn.execute(
            """
            update annotations
            set status = case when status = 'pending' then 'seen' else status end,
                updated_at = now()
            where id = %s
            returning id, book_id, chapter_id, status, updated_at
            """,
            (annotation_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "annotation_not_found"}), 404
        conn.execute(
            """
            insert into ai_reading_state (book_id, last_chapter_read, last_annotation_seen)
            values (%s, %s, %s)
            on conflict (book_id) do update set
                last_chunk_index = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then null
                    else ai_reading_state.last_chunk_index
                end,
                last_chunk_id = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then null
                    else ai_reading_state.last_chunk_id
                end,
                last_block_id = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then null
                    else ai_reading_state.last_block_id
                end,
                chapter_completed = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then false
                    else ai_reading_state.chapter_completed
                end,
                last_chapter_read = excluded.last_chapter_read,
                last_annotation_seen = excluded.last_annotation_seen,
                updated_at = now()
            """,
            (row["book_id"], row["chapter_id"], annotation_id),
        )
    return jsonify({"annotation": _json_safe(row)})


@annotations_bp.post("/api/annotations/<uuid:annotation_id>/reply")
@action_required
def reply_to_annotation(annotation_id: UUID):
    payload = request.get_json(silent=True) or {}
    response_text = _clean_text(payload.get("response"), 50_000)
    if not response_text:
        return jsonify({"error": "response_required"}), 400

    with db.transaction() as conn:
        annotation = conn.execute(
            "select id, book_id, chapter_id from annotations where id = %s for update",
            (annotation_id,),
        ).fetchone()
        if not annotation:
            return jsonify({"error": "annotation_not_found"}), 404
        previous_reply = conn.execute(
            "select * from annotation_replies where annotation_id = %s for update",
            (annotation_id,),
        ).fetchone()
        reply = conn.execute(
            """
            insert into annotation_replies (annotation_id, response)
            values (%s, %s)
            on conflict (annotation_id) do update set
                response = excluded.response,
                updated_at = now()
            returning id, annotation_id, response, created_at, updated_at
            """,
            (annotation_id, response_text),
        ).fetchone()
        conn.execute(
            "update annotations set status = 'replied', updated_at = now() where id = %s",
            (annotation_id,),
        )
        conn.execute(
            """
            insert into ai_reading_state (book_id, last_chapter_read, last_annotation_seen)
            values (%s, %s, %s)
            on conflict (book_id) do update set
                last_chunk_index = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then null
                    else ai_reading_state.last_chunk_index
                end,
                last_chunk_id = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then null
                    else ai_reading_state.last_chunk_id
                end,
                last_block_id = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then null
                    else ai_reading_state.last_block_id
                end,
                chapter_completed = case
                    when ai_reading_state.last_chapter_read is distinct from excluded.last_chapter_read then false
                    else ai_reading_state.chapter_completed
                end,
                last_chapter_read = excluded.last_chapter_read,
                last_annotation_seen = excluded.last_annotation_seen,
                updated_at = now()
            """,
            (annotation["book_id"], annotation["chapter_id"], annotation_id),
        )
        operations.record(
            conn, actor="xiaxia",
            operation_type="update_reply" if previous_reply else "create_reply",
            target_type="xiaxia_reply", target_id=reply["id"],
            book_id=annotation["book_id"], chapter_id=annotation["chapter_id"],
            previous_records=[operations.snapshot(previous_reply, "xiaxia_reply")] if previous_reply else [],
            new_records=[operations.snapshot(reply, "xiaxia_reply")],
        )
    result = _json_safe(reply)
    result["status"] = "replied"
    return jsonify({"reply": result})


@annotations_bp.post("/api/xiaxia/thoughts")
@action_required
def create_xiaxia_thought():
    payload = request.get_json(silent=True) or {}
    book_id = _uuid_or_none(payload.get("book_id"))
    chapter_id = _uuid_or_none(payload.get("chapter_id"))
    scope = str(payload.get("scope") or "").strip().lower()
    mark_type = _clean_mark_type(payload.get("mark_type"))
    content = _clean_text(payload.get("content"), 50_000)
    if not book_id or not chapter_id or scope not in {"range", "block", "chapter"}:
        return jsonify({"error": "invalid_thought_scope"}), 400
    if not content:
        return jsonify({"error": "content_required"}), 400
    if not mark_type:
        return jsonify({"error": "invalid_mark_type"}), 400
    chapter = db.fetch_one(
        "select id, book_id, content_html from chapters where id = %s and book_id = %s",
        (chapter_id, book_id),
    )
    if not chapter:
        return jsonify({"error": "chapter_not_found"}), 404

    try:
        anchor = _thought_anchor(scope, payload, chapter["content_html"])
    except ValueError as exc:
        return jsonify({"error": "invalid_thought_anchor", "message": str(exc)}), 400
    with db.transaction() as conn:
        row = conn.execute(
            """
            insert into xiaxia_thoughts (
                book_id, chapter_id, scope, mark_type, content, selected_text,
                start_block_id, start_offset, end_block_id, end_offset,
                prefix_text, suffix_text
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning *
            """,
            (
                book_id, chapter_id, scope, mark_type, content,
                anchor["selected_text"], anchor["start_block_id"],
                anchor["start_offset"], anchor["end_block_id"],
                anchor["end_offset"], anchor["prefix_text"], anchor["suffix_text"],
            ),
        ).fetchone()
        operations.record(
            conn, actor="xiaxia", operation_type="create_thought",
            target_type="xiaxia_thought", target_id=row["id"],
            book_id=book_id, chapter_id=chapter_id,
            new_records=[operations.snapshot(row, "xiaxia_thought")],
        )
    return jsonify({"xiaxia_thought": _action_trace_payload(row)}), 201


def _thought_anchor(scope: str, payload: dict[str, Any], content_html: str):
    empty = {
        "selected_text": "",
        "start_block_id": None,
        "start_offset": None,
        "end_block_id": None,
        "end_offset": None,
        "prefix_text": "",
        "suffix_text": "",
    }
    if scope == "chapter":
        return empty
    blocks = _chapter_blocks(content_html)
    block_map = {block["block_id"]: block for block in blocks}
    if scope == "block":
        block_id = _clean_block_id(
            payload.get("block_id") or payload.get("start_block_id")
        )
        block = block_map.get(block_id)
        if not block:
            raise ValueError("block_id does not exist in this chapter")
        text = block["text"]
        return {
            "selected_text": text,
            "start_block_id": block_id,
            "start_offset": 0,
            "end_block_id": block_id,
            "end_offset": len(text),
            "prefix_text": "",
            "suffix_text": "",
        }

    start_block_id = _clean_block_id(payload.get("start_block_id"))
    end_block_id = _clean_block_id(payload.get("end_block_id"))
    try:
        start_offset = int(payload.get("start_offset"))
        end_offset = int(payload.get("end_offset"))
    except (TypeError, ValueError) as exc:
        raise ValueError("range offsets must be integers") from exc
    selected = _text_across_blocks(
        blocks, start_block_id, start_offset, end_block_id, end_offset
    )
    if not selected.strip():
        raise ValueError("range is empty or outside the chapter")
    supplied = _clean_selected_text(payload.get("selected_text"), 20_000)
    if supplied and _normalize_text(supplied) != _normalize_text(selected):
        raise ValueError("selected_text does not match the supplied block offsets")
    start_text = block_map[start_block_id]["text"]
    end_text = block_map[end_block_id]["text"]
    return {
        "selected_text": selected,
        "start_block_id": start_block_id,
        "start_offset": start_offset,
        "end_block_id": end_block_id,
        "end_offset": end_offset,
        "prefix_text": start_text[max(0, start_offset - 120) : start_offset],
        "suffix_text": end_text[end_offset : end_offset + 120],
    }


def _latest_sync_token(*collections: list[dict[str, Any]]) -> str:
    values = []
    for collection in collections:
        for row in collection:
            for field in (
                "updated_at", "reply_updated_at", "user_reply_updated_at", "created_at"
            ):
                value = row.get(field)
                if value is not None:
                    values.append(
                        value.isoformat() if hasattr(value, "isoformat") else str(value)
                    )
    return max(values, default="")


def _annotation_anchor_matches(
    content_html: str,
    selected_text: str,
    start_block_id: str,
    start_offset: int,
    end_block_id: str,
    end_offset: int,
) -> bool:
    actual = _text_across_blocks(
        _chapter_blocks(content_html), start_block_id, start_offset,
        end_block_id, end_offset,
    )
    return bool(actual.strip()) and _normalize_text(actual) == _normalize_text(selected_text)


def _engine_trace_payload(book_id: UUID, row: dict[str, Any], record_type: str) -> dict[str, Any]:
    payload = _json_safe(row)
    if row.get("engine_locator") or not row.get("start_block_id"):
        return payload
    try:
        payload["locator_seed"] = legacy_locator_seed(book_id, row)
    except LocatorBridgeError as exc:
        payload["locator_seed_error"] = exc.code
    return payload


def _action_trace_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Keep renderer-only fields out of the unchanged Custom GPT contract."""
    payload = _json_safe(row)
    for field in ("engine_locator", "engine_locator_version", "engine_anchor_verified_at"):
        payload.pop(field, None)
    return payload


def _clean_text(value: Any, max_length: int, allow_empty: bool = False) -> str:
    if value is None:
        return "" if allow_empty else ""
    cleaned = str(value).replace("\x00", "").strip()
    return cleaned[:max_length]


def _clean_selected_text(value: Any, max_length: int) -> str:
    cleaned = str(value or "").replace("\x00", "")[:max_length]
    return cleaned if cleaned.strip() else ""


def _clean_block_id(value: Any) -> str:
    text = str(value or "")
    if len(text) > 64 or not re.fullmatch(r"b\d+", text):
        return ""
    return text


def _clean_mark_type(value: Any) -> str:
    text = str(value or "thought").strip().lower()
    return text if re.fullmatch(r"[a-z0-9_-]{1,64}", text) else ""


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
