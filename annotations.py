"""Persistent user highlights, notes, Xiaxia replies, and status transitions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from flask import Blueprint, jsonify, request

import database as db
from auth import api_or_session_required
from reading import _json_safe, _uuid_or_none


annotations_bp = Blueprint("annotations", __name__)


@annotations_bp.get("/api/books/<uuid:book_id>/chapters/<uuid:chapter_id>/annotations")
@api_or_session_required
def list_chapter_annotations(book_id: UUID, chapter_id: UUID):
    rows = db.fetch_all(
        """
        select a.id, a.book_id, a.chapter_id, a.selected_text,
               a.start_block_id, a.start_offset, a.end_block_id, a.end_offset,
               a.prefix_text, a.suffix_text, a.comment, a.status,
               a.created_at, a.updated_at,
               ar.response as xiaxia_response, ar.created_at as reply_created_at,
               ar.updated_at as reply_updated_at
        from annotations a
        left join annotation_replies ar on ar.annotation_id = a.id
        where a.book_id = %s and a.chapter_id = %s
        order by a.created_at
        """,
        (book_id, chapter_id),
    )
    return jsonify({"annotations": [_json_safe(row) for row in rows]})


@annotations_bp.post("/api/annotations")
@api_or_session_required
def create_annotation():
    payload = request.get_json(silent=True) or {}
    book_id = _uuid_or_none(payload.get("book_id"))
    chapter_id = _uuid_or_none(payload.get("chapter_id"))
    selected_text = _clean_selected_text(payload.get("selected_text"), 20_000)
    start_block_id = _clean_block_id(payload.get("start_block_id"))
    end_block_id = _clean_block_id(payload.get("end_block_id"))
    comment = _clean_text(payload.get("comment"), 20_000, allow_empty=True)
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
        "select id from chapters where id = %s and book_id = %s", (chapter_id, book_id)
    )
    if not chapter:
        return jsonify({"error": "chapter_not_found"}), 404

    row = db.execute(
        """
        insert into annotations (
            book_id, chapter_id, selected_text,
            start_block_id, start_offset, end_block_id, end_offset,
            prefix_text, suffix_text, comment, status
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        returning id, book_id, chapter_id, selected_text,
                  start_block_id, start_offset, end_block_id, end_offset,
                  prefix_text, suffix_text, comment, status, created_at, updated_at
        """,
        (
            book_id,
            chapter_id,
            selected_text,
            start_block_id,
            start_offset,
            end_block_id,
            end_offset,
            prefix_text,
            suffix_text,
            comment,
        ),
    )
    result = _json_safe(row)
    result["xiaxia_response"] = None
    return jsonify({"annotation": result}), 201


@annotations_bp.get("/api/annotations/pending")
@api_or_session_required
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
@api_or_session_required
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
                last_chapter_read = excluded.last_chapter_read,
                last_annotation_seen = excluded.last_annotation_seen,
                updated_at = now()
            """,
            (row["book_id"], row["chapter_id"], annotation_id),
        )
    return jsonify({"annotation": _json_safe(row)})


@annotations_bp.post("/api/annotations/<uuid:annotation_id>/reply")
@api_or_session_required
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
                last_chapter_read = excluded.last_chapter_read,
                last_annotation_seen = excluded.last_annotation_seen,
                updated_at = now()
            """,
            (annotation["book_id"], annotation["chapter_id"], annotation_id),
        )
    result = _json_safe(reply)
    result["status"] = "replied"
    return jsonify({"reply": result})


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
    if len(text) > 64 or not text.startswith("b") or not text[1:].isdigit():
        return ""
    return text
