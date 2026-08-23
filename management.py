"""V1.1 reading-trace management, preview/commit, replies, and undo APIs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from flask import Blueprint, jsonify, request
from psycopg.types.json import Jsonb

import database as db
import operations
from annotations import (
    XIA_THOUGHT_FIELDS,
    _clean_mark_type,
    _clean_text,
    _thought_anchor,
)
from auth import action_required, web_api_required
from reading import _json_safe, _uuid_or_none


management_bp = Blueprint("management", __name__)


@management_bp.get("/api/xiaxia/thoughts")
@action_required
def list_xiaxia_thoughts():
    book_id = _uuid_or_none(request.args.get("book_id"))
    chapter_id = _uuid_or_none(request.args.get("chapter_id"))
    if not book_id:
        return jsonify({"error": "book_id_required"}), 400
    page, page_size, error = _pagination()
    if error:
        return error
    if chapter_id and not db.fetch_one(
        "select id from chapters where id = %s and book_id = %s",
        (chapter_id, book_id),
    ):
        return jsonify({"error": "chapter_not_found"}), 404
    total_row = db.fetch_one(
        """
        select count(*) as total from xiaxia_thoughts
        where owner = 'xiaxia' and content_type = 'xiaxia_thought'
          and book_id = %s and (%s::uuid is null or chapter_id = %s::uuid)
        """,
        (book_id, chapter_id, chapter_id),
    )
    rows = db.fetch_all(
        f"""
        select {XIA_THOUGHT_FIELDS}, b.title as book_title,
               c.title as chapter_title, c.chapter_index
        from xiaxia_thoughts xt
        join books b on b.id = xt.book_id
        join chapters c on c.id = xt.chapter_id
        left join thought_user_replies tur on tur.thought_id = xt.id
        where xt.owner = 'xiaxia' and xt.content_type = 'xiaxia_thought'
          and xt.book_id = %s and (%s::uuid is null or xt.chapter_id = %s::uuid)
        order by c.chapter_index, xt.created_at, xt.id
        limit %s offset %s
        """,
        (book_id, chapter_id, chapter_id, page_size, (page - 1) * page_size),
    )
    total = int(total_row["total"] if total_row else 0)
    return jsonify(
        {
            "thoughts": [_thought_payload(row) for row in rows],
            "pagination": _pagination_payload(page, page_size, total),
        }
    )


@management_bp.patch("/api/xiaxia/thoughts/<uuid:thought_id>")
@action_required
def update_xiaxia_thought(thought_id: UUID):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_payload"}), 400
    editable_fields = {
        "content", "mark_type", "scope", "block_id", "selected_text",
        "start_block_id", "start_offset", "end_block_id", "end_offset",
    }
    if not editable_fields.intersection(payload):
        return jsonify({"error": "thought_fields_required"}), 400
    with db.transaction() as conn:
        existing = conn.execute(
            """
            select * from xiaxia_thoughts
            where id = %s and owner = 'xiaxia' and content_type = 'xiaxia_thought'
            for update
            """,
            (thought_id,),
        ).fetchone()
        if not existing:
            return jsonify({"error": "xiaxia_thought_not_found"}), 404
        content = existing["content"]
        mark_type = existing["mark_type"]
        if "content" in payload:
            content = _clean_text(payload.get("content"), 50_000)
            if not content:
                return jsonify({"error": "content_required"}), 400
        if "mark_type" in payload:
            mark_type = _clean_mark_type(payload.get("mark_type"))
            if not mark_type:
                return jsonify({"error": "invalid_mark_type"}), 400

        anchor_fields = {
            "scope", "block_id", "selected_text", "start_block_id", "start_offset",
            "end_block_id", "end_offset",
        }
        scope = existing["scope"]
        anchor = {key: existing.get(key) for key in (
            "selected_text", "start_block_id", "start_offset", "end_block_id",
            "end_offset", "prefix_text", "suffix_text",
        )}
        if anchor_fields.intersection(payload):
            scope = str(payload.get("scope") or existing["scope"]).strip().lower()
            if scope not in {"range", "block", "chapter"}:
                return jsonify({"error": "invalid_thought_scope"}), 400
            chapter = conn.execute(
                "select content_html from chapters where id = %s and book_id = %s",
                (existing["chapter_id"], existing["book_id"]),
            ).fetchone()
            merged = {**dict(existing), **payload}
            try:
                anchor = _thought_anchor(scope, merged, chapter["content_html"])
            except ValueError as exc:
                return jsonify({"error": "invalid_thought_anchor", "message": str(exc)}), 400
        updated = conn.execute(
            """
            update xiaxia_thoughts set
                scope = %s, mark_type = %s, content = %s, selected_text = %s,
                start_block_id = %s, start_offset = %s, end_block_id = %s,
                end_offset = %s, prefix_text = %s, suffix_text = %s,
                updated_at = now()
            where id = %s
            returning *
            """,
            (
                scope, mark_type, content, anchor["selected_text"],
                anchor["start_block_id"], anchor["start_offset"],
                anchor["end_block_id"], anchor["end_offset"],
                anchor["prefix_text"], anchor["suffix_text"], thought_id,
            ),
        ).fetchone()
        operations.record(
            conn,
            actor="xiaxia",
            operation_type="update_thought",
            target_type="xiaxia_thought",
            target_id=thought_id,
            book_id=existing["book_id"],
            chapter_id=existing["chapter_id"],
            previous_records=[operations.snapshot(existing, "xiaxia_thought")],
            new_records=[operations.snapshot(updated, "xiaxia_thought")],
        )
    return jsonify({"xiaxia_thought": _json_safe(updated)})


@management_bp.delete("/api/xiaxia/thoughts/<uuid:thought_id>")
@action_required
def delete_xiaxia_thought(thought_id: UUID):
    with db.transaction() as conn:
        thought = conn.execute(
            """
            select * from xiaxia_thoughts
            where id = %s and owner = 'xiaxia' and content_type = 'xiaxia_thought'
            for update
            """,
            (thought_id,),
        ).fetchone()
        if not thought:
            return jsonify({"error": "xiaxia_thought_not_found"}), 404
        user_reply = conn.execute(
            "select * from thought_user_replies where thought_id = %s", (thought_id,)
        ).fetchone()
        snapshots = [operations.snapshot(thought, "xiaxia_thought")]
        if user_reply:
            snapshots.append(operations.snapshot(user_reply, "user_reply"))
        conn.execute("delete from xiaxia_thoughts where id = %s", (thought_id,))
        operations.record(
            conn,
            actor="xiaxia", operation_type="delete_thought",
            target_type="xiaxia_thought", target_id=thought_id,
            book_id=thought["book_id"], chapter_id=thought["chapter_id"],
            previous_records=snapshots,
        )
    return jsonify({"deleted": True, "thought_id": str(thought_id), "deleted_user_reply": bool(user_reply)})


@management_bp.post("/api/xiaxia/thoughts/batch-delete")
@action_required
def delete_xiaxia_thoughts():
    payload = request.get_json(silent=True) or {}
    book_id = _uuid_or_none(payload.get("book_id"))
    chapter_id = _uuid_or_none(payload.get("chapter_id"))
    if not book_id:
        return jsonify({"error": "book_id_required"}), 400
    with db.transaction() as conn:
        thoughts = list(conn.execute(
            """
            select * from xiaxia_thoughts
            where owner = 'xiaxia' and content_type = 'xiaxia_thought'
              and book_id = %s and (%s::uuid is null or chapter_id = %s::uuid)
            order by created_at for update
            """,
            (book_id, chapter_id, chapter_id),
        ).fetchall())
        if not thoughts:
            return jsonify({"deleted_count": 0, "thought_ids": []})
        ids = [row["id"] for row in thoughts]
        replies = list(conn.execute(
            "select * from thought_user_replies where thought_id = any(%s)", (ids,)
        ).fetchall())
        snapshots = [operations.snapshot(row, "xiaxia_thought") for row in thoughts]
        snapshots.extend(operations.snapshot(row, "user_reply") for row in replies)
        conn.execute(
            """
            delete from xiaxia_thoughts
            where owner = 'xiaxia' and content_type = 'xiaxia_thought'
              and id = any(%s)
            """,
            (ids,),
        )
        operations.record(
            conn,
            actor="xiaxia", operation_type="batch_delete",
            target_type="xiaxia_thought", target_id=None,
            book_id=book_id, chapter_id=chapter_id,
            previous_records=snapshots,
        )
    return jsonify({"deleted_count": len(ids), "thought_ids": [str(item) for item in ids]})


@management_bp.post("/api/xiaxia/thoughts/preview")
@action_required
def preview_xiaxia_thoughts():
    payload = request.get_json(silent=True) or {}
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 50:
        return jsonify({"error": "candidates_must_have_1_to_50_items"}), 400
    batch_id = uuid4()
    results = []
    with db.transaction() as conn:
        for raw in candidates:
            candidate = raw if isinstance(raw, dict) else {}
            result = _validate_candidate(conn, candidate)
            candidate_id = uuid4()
            stored_payload = result.get("normalized_payload") or _json_safe(candidate)
            conn.execute(
                """
                insert into xiaxia_thought_candidates (
                    id, batch_id, book_id, chapter_id, request_payload,
                    validation_status, validation_error, matched_text, validated_anchor
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    candidate_id, batch_id, result["book_id"], result["chapter_id"],
                    Jsonb(stored_payload), result["validation_status"],
                    result.get("validation_error"), result.get("matched_text", ""),
                    Jsonb(result.get("anchor") or {}),
                ),
            )
            results.append(
                {
                    "candidate_id": str(candidate_id),
                    "validation_status": result["validation_status"],
                    "validation_error": result.get("validation_error"),
                    "matched_text": result.get("matched_text", ""),
                    "anchor": result.get("anchor") or {},
                }
            )
    return jsonify({"batch_id": str(batch_id), "candidates": results})


@management_bp.post("/api/xiaxia/thoughts/commit")
@action_required
def commit_xiaxia_thoughts():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("candidate_ids")
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 50:
        return jsonify({"error": "candidate_ids_must_have_1_to_50_items"}), 400
    results = []
    committed = []
    with db.transaction() as conn:
        for raw_id in raw_ids:
            candidate_id = _uuid_or_none(raw_id)
            if not candidate_id:
                results.append({"candidate_id": str(raw_id), "status": "failed", "error": "invalid_candidate_id"})
                continue
            candidate = conn.execute(
                """
                select * from xiaxia_thought_candidates
                where id = %s for update
                """,
                (candidate_id,),
            ).fetchone()
            error = _candidate_commit_error(candidate)
            if error:
                results.append({"candidate_id": str(candidate_id), "status": "failed", "error": error})
                continue
            raw = candidate["request_payload"]
            anchor = candidate["validated_anchor"]
            thought = conn.execute(
                """
                insert into xiaxia_thoughts (
                    book_id, chapter_id, scope, mark_type, content, selected_text,
                    start_block_id, start_offset, end_block_id, end_offset,
                    prefix_text, suffix_text
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    candidate["book_id"], candidate["chapter_id"], raw["scope"],
                    _clean_mark_type(raw.get("mark_type")),
                    _clean_text(raw.get("content"), 50_000),
                    anchor.get("selected_text", ""), anchor.get("start_block_id"),
                    anchor.get("start_offset"), anchor.get("end_block_id"),
                    anchor.get("end_offset"), anchor.get("prefix_text", ""),
                    anchor.get("suffix_text", ""),
                ),
            ).fetchone()
            conn.execute(
                "update xiaxia_thought_candidates set committed_thought_id = %s where id = %s",
                (thought["id"], candidate_id),
            )
            committed.append(operations.snapshot(thought, "xiaxia_thought"))
            results.append({"candidate_id": str(candidate_id), "status": "committed", "thought_id": str(thought["id"])})
        if committed:
            first = committed[0]
            operations.record(
                conn,
                actor="xiaxia", operation_type="batch_create",
                target_type="xiaxia_thought", target_id=None,
                book_id=first.get("book_id"), chapter_id=first.get("chapter_id"),
                new_records=committed,
            )
    return jsonify({"results": results, "committed_count": len(committed), "failed_count": len(results) - len(committed)})


@management_bp.patch("/api/annotations/<uuid:annotation_id>/reply")
@action_required
def update_annotation_reply(annotation_id: UUID):
    payload = request.get_json(silent=True) or {}
    response_text = _clean_text(payload.get("response"), 50_000)
    if not response_text:
        return jsonify({"error": "response_required"}), 400
    with db.transaction() as conn:
        existing = conn.execute(
            """
            select ar.*, a.book_id, a.chapter_id
            from annotation_replies ar join annotations a on a.id = ar.annotation_id
            where ar.annotation_id = %s and ar.owner = 'xiaxia'
              and ar.content_type = 'xiaxia_reply' for update
            """,
            (annotation_id,),
        ).fetchone()
        if not existing:
            return jsonify({"error": "annotation_reply_not_found"}), 404
        updated = conn.execute(
            """
            update annotation_replies set response = %s, updated_at = now()
            where annotation_id = %s returning *
            """,
            (response_text, annotation_id),
        ).fetchone()
        operations.record(
            conn, actor="xiaxia", operation_type="update_reply",
            target_type="xiaxia_reply", target_id=existing["id"],
            book_id=existing["book_id"], chapter_id=existing["chapter_id"],
            previous_records=[operations.snapshot(existing, "xiaxia_reply")],
            new_records=[operations.snapshot(updated, "xiaxia_reply")],
        )
    return jsonify({"reply": _json_safe(updated)})


@management_bp.delete("/api/annotations/<uuid:annotation_id>/reply")
@action_required
def delete_annotation_reply(annotation_id: UUID):
    with db.transaction() as conn:
        existing = conn.execute(
            """
            select ar.*, a.book_id, a.chapter_id
            from annotation_replies ar join annotations a on a.id = ar.annotation_id
            where ar.annotation_id = %s and ar.owner = 'xiaxia'
              and ar.content_type = 'xiaxia_reply' for update
            """,
            (annotation_id,),
        ).fetchone()
        if not existing:
            return jsonify({"error": "annotation_reply_not_found"}), 404
        conn.execute("delete from annotation_replies where id = %s", (existing["id"],))
        annotation = conn.execute(
            """
            update annotations set status = 'seen', updated_at = now()
            where id = %s returning id, status, updated_at
            """,
            (annotation_id,),
        ).fetchone()
        operations.record(
            conn, actor="xiaxia", operation_type="delete_reply",
            target_type="xiaxia_reply", target_id=existing["id"],
            book_id=existing["book_id"], chapter_id=existing["chapter_id"],
            previous_records=[operations.snapshot(existing, "xiaxia_reply")],
        )
    return jsonify({"deleted": True, "annotation_id": str(annotation_id), "annotation_status": annotation["status"]})


@management_bp.post("/api/xiaxia/thoughts/<uuid:thought_id>/reply")
@web_api_required
def create_user_reply(thought_id: UUID):
    return _write_user_reply(thought_id, create_only=True)


@management_bp.patch("/api/xiaxia/thoughts/<uuid:thought_id>/reply")
@web_api_required
def update_user_reply(thought_id: UUID):
    return _write_user_reply(thought_id, create_only=False)


def _write_user_reply(thought_id: UUID, create_only: bool):
    payload = request.get_json(silent=True) or {}
    response_text = _clean_text(payload.get("response"), 50_000)
    if not response_text:
        return jsonify({"error": "response_required"}), 400
    with db.transaction() as conn:
        thought = conn.execute(
            "select id, book_id, chapter_id from xiaxia_thoughts where id = %s",
            (thought_id,),
        ).fetchone()
        if not thought:
            return jsonify({"error": "xiaxia_thought_not_found"}), 404
        existing = conn.execute(
            "select * from thought_user_replies where thought_id = %s for update",
            (thought_id,),
        ).fetchone()
        if create_only and existing:
            return jsonify({"error": "user_reply_already_exists"}), 409
        if not create_only and not existing:
            return jsonify({"error": "user_reply_not_found"}), 404
        if existing:
            reply = conn.execute(
                "update thought_user_replies set response = %s, updated_at = now() where id = %s returning *",
                (response_text, existing["id"]),
            ).fetchone()
            operation_type = "update_reply"
        else:
            reply = conn.execute(
                "insert into thought_user_replies (thought_id, response) values (%s, %s) returning *",
                (thought_id, response_text),
            ).fetchone()
            operation_type = "create_reply"
        operations.record(
            conn, actor="user", operation_type=operation_type,
            target_type="user_reply", target_id=reply["id"],
            book_id=thought["book_id"], chapter_id=thought["chapter_id"],
            previous_records=[operations.snapshot(existing, "user_reply")] if existing else [],
            new_records=[operations.snapshot(reply, "user_reply")],
        )
    return jsonify({"user_reply": _json_safe(reply)}), (200 if existing else 201)


@management_bp.delete("/api/xiaxia/thoughts/<uuid:thought_id>/reply")
@web_api_required
def delete_user_reply(thought_id: UUID):
    with db.transaction() as conn:
        existing = conn.execute(
            """
            select tur.*, xt.book_id, xt.chapter_id
            from thought_user_replies tur join xiaxia_thoughts xt on xt.id = tur.thought_id
            where tur.thought_id = %s and tur.owner = 'user'
              and tur.content_type = 'user_reply' for update
            """,
            (thought_id,),
        ).fetchone()
        if not existing:
            return jsonify({"error": "user_reply_not_found"}), 404
        conn.execute("delete from thought_user_replies where id = %s", (existing["id"],))
        operations.record(
            conn, actor="user", operation_type="delete_reply",
            target_type="user_reply", target_id=existing["id"],
            book_id=existing["book_id"], chapter_id=existing["chapter_id"],
            previous_records=[operations.snapshot(existing, "user_reply")],
        )
    return jsonify({"deleted": True, "thought_id": str(thought_id)})


@management_bp.get("/api/management/traces")
@web_api_required
def list_reading_traces():
    book_id = _uuid_or_none(request.args.get("book_id"))
    chapter_id = _uuid_or_none(request.args.get("chapter_id"))
    owner = request.args.get("owner", "all")
    content_type = request.args.get("content_type", "all")
    if owner not in {"all", "user", "xiaxia"}:
        return jsonify({"error": "invalid_owner"}), 400
    allowed_types = {"all", "user_annotation", "xiaxia_thought", "xiaxia_reply", "user_reply"}
    if content_type not in allowed_types:
        return jsonify({"error": "invalid_content_type"}), 400
    page, page_size, error = _pagination(default_size=50)
    if error:
        return error
    items: list[dict[str, Any]] = []
    if owner in {"all", "user"} and content_type in {"all", "user_annotation"}:
        items.extend(_trace_annotations(book_id, chapter_id))
    if owner in {"all", "xiaxia"} and content_type in {"all", "xiaxia_thought"}:
        items.extend(_trace_thoughts(book_id, chapter_id))
    if owner in {"all", "xiaxia"} and content_type in {"all", "xiaxia_reply"}:
        items.extend(_trace_xiaxia_replies(book_id, chapter_id))
    if owner in {"all", "user"} and content_type in {"all", "user_reply"}:
        items.extend(_trace_user_replies(book_id, chapter_id))
    items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item["id"])), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return jsonify({"traces": items[start:start + page_size], "pagination": _pagination_payload(page, page_size, total)})


@management_bp.post("/api/management/traces/batch-delete")
@web_api_required
def batch_delete_user_traces():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        return jsonify({"error": "items_must_have_1_to_200_entries"}), 400
    confirm_replies = payload.get("confirm_xiaxia_replies") is True
    deleted = []
    snapshots = []
    first_book = first_chapter = None
    with db.transaction() as conn:
        for item in items:
            content_type = item.get("content_type") if isinstance(item, dict) else None
            item_id = _uuid_or_none(item.get("id") if isinstance(item, dict) else None)
            if content_type not in {"user_annotation", "user_reply"} or not item_id:
                return jsonify({"error": "only_user_owned_traces_can_be_deleted"}), 403
            if content_type == "user_annotation":
                row = conn.execute("select * from annotations where id = %s and owner = 'user' for update", (item_id,)).fetchone()
                if not row:
                    return jsonify({"error": "user_annotation_not_found", "id": str(item_id)}), 404
                reply = conn.execute("select * from annotation_replies where annotation_id = %s", (item_id,)).fetchone()
                if reply and not confirm_replies:
                    return jsonify({"error": "xiaxia_reply_confirmation_required", "annotation_id": str(item_id)}), 409
                snapshots.append(operations.snapshot(row, "user_annotation"))
                if reply:
                    snapshots.append(operations.snapshot(reply, "xiaxia_reply"))
                conn.execute("delete from annotations where id = %s", (item_id,))
                first_book = first_book or row["book_id"]
                first_chapter = first_chapter or row["chapter_id"]
            else:
                row = conn.execute(
                    """
                    select tur.*, xt.book_id, xt.chapter_id from thought_user_replies tur
                    join xiaxia_thoughts xt on xt.id = tur.thought_id
                    where tur.id = %s and tur.owner = 'user' for update
                    """,
                    (item_id,),
                ).fetchone()
                if not row:
                    return jsonify({"error": "user_reply_not_found", "id": str(item_id)}), 404
                snapshots.append(operations.snapshot(row, "user_reply"))
                conn.execute("delete from thought_user_replies where id = %s", (item_id,))
                first_book = first_book or row["book_id"]
                first_chapter = first_chapter or row["chapter_id"]
            deleted.append({"id": str(item_id), "content_type": content_type})
        operations.record(
            conn, actor="user", operation_type="batch_delete",
            target_type="reading_trace", target_id=None,
            book_id=first_book, chapter_id=first_chapter,
            previous_records=snapshots,
        )
    return jsonify({"deleted_count": len(deleted), "deleted": deleted})


@management_bp.post("/api/actions/undo")
@action_required
def undo_last_reading_action():
    return _undo("xiaxia")


@management_bp.post("/api/management/undo")
@web_api_required
def undo_last_user_action():
    return _undo("user")


def _undo(actor: str):
    try:
        operation = operations.undo_last(actor)
    except operations.UndoConflict as exc:
        return jsonify({"error": "undo_conflict", "message": str(exc)}), 409
    if not operation:
        return jsonify({"error": "nothing_to_undo"}), 404
    return jsonify({"undone": True, "operation": operation})


def _validate_candidate(conn: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    book_id = _uuid_or_none(candidate.get("book_id"))
    chapter_id = _uuid_or_none(candidate.get("chapter_id"))
    scope = str(candidate.get("scope") or "").strip().lower()
    content = _clean_text(candidate.get("content"), 50_000)
    mark_type = _clean_mark_type(candidate.get("mark_type"))
    base = {"book_id": book_id, "chapter_id": chapter_id, "validation_status": "invalid"}
    if not book_id or not chapter_id:
        return {**base, "validation_error": "book_id_and_chapter_id_required"}
    if scope not in {"range", "block", "chapter"}:
        return {**base, "validation_error": "invalid_thought_scope"}
    if not content:
        return {**base, "validation_error": "content_required"}
    if not mark_type:
        return {**base, "validation_error": "invalid_mark_type"}
    chapter = conn.execute(
        "select id, content_html from chapters where id = %s and book_id = %s",
        (chapter_id, book_id),
    ).fetchone()
    if not chapter:
        return {
            **base,
            "book_id": None,
            "chapter_id": None,
            "validation_error": "chapter_not_found",
        }
    try:
        anchor = _thought_anchor(scope, candidate, chapter["content_html"])
    except ValueError as exc:
        return {**base, "validation_error": str(exc)}
    return {
        **base,
        "validation_status": "valid",
        "validation_error": None,
        "matched_text": anchor["selected_text"],
        "anchor": anchor,
        "normalized_payload": {
            **candidate,
            "book_id": str(book_id),
            "chapter_id": str(chapter_id),
            "scope": scope,
            "mark_type": mark_type,
            "content": content,
        },
    }


def _candidate_commit_error(candidate: dict[str, Any] | None) -> str | None:
    if not candidate:
        return "candidate_not_found"
    if candidate["validation_status"] != "valid":
        return "candidate_not_valid"
    if candidate.get("committed_thought_id"):
        return "candidate_already_committed"
    expires = candidate.get("expires_at")
    if expires and expires < datetime.now(UTC):
        return "candidate_expired"
    return None


def _thought_payload(row: dict[str, Any]) -> dict[str, Any]:
    result = _json_safe(row)
    result["thought_id"] = result.pop("id")
    result["book"] = {"book_id": result.get("book_id"), "title": result.pop("book_title", None)}
    result["chapter"] = {
        "chapter_id": result.get("chapter_id"),
        "chapter_index": result.pop("chapter_index", None),
        "title": result.pop("chapter_title", None),
    }
    result["anchor"] = {
        key: result.get(key)
        for key in ("scope", "start_block_id", "start_offset", "end_block_id", "end_offset")
    }
    result["thought_content"] = result.get("content")
    return result


def _pagination(default_size: int = 20):
    try:
        page = max(1, int(request.args.get("page", "1")))
        page_size = min(100, max(1, int(request.args.get("page_size", str(default_size)))))
    except ValueError:
        return 0, 0, (jsonify({"error": "invalid_pagination"}), 400)
    return page, page_size, None


def _pagination_payload(page: int, page_size: int, total: int) -> dict[str, Any]:
    page_count = max(1, (total + page_size - 1) // page_size)
    return {"page": page, "page_size": page_size, "total": total, "page_count": page_count, "has_next": page < page_count}


def _trace_annotations(book_id: UUID | None, chapter_id: UUID | None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select a.*, b.title as book_title, c.title as chapter_title, c.chapter_index,
               ar.response as xiaxia_response
        from annotations a join books b on b.id = a.book_id
        join chapters c on c.id = a.chapter_id
        left join annotation_replies ar on ar.annotation_id = a.id
        where (%s::uuid is null or a.book_id = %s::uuid)
          and (%s::uuid is null or a.chapter_id = %s::uuid)
        """,
        (book_id, book_id, chapter_id, chapter_id),
    )
    return [_json_safe(row) for row in rows]


def _trace_thoughts(book_id: UUID | None, chapter_id: UUID | None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select xt.*, b.title as book_title, c.title as chapter_title, c.chapter_index,
               tur.response as user_response
        from xiaxia_thoughts xt join books b on b.id = xt.book_id
        join chapters c on c.id = xt.chapter_id
        left join thought_user_replies tur on tur.thought_id = xt.id
        where (%s::uuid is null or xt.book_id = %s::uuid)
          and (%s::uuid is null or xt.chapter_id = %s::uuid)
        """,
        (book_id, book_id, chapter_id, chapter_id),
    )
    return [_json_safe(row) for row in rows]


def _trace_xiaxia_replies(book_id: UUID | None, chapter_id: UUID | None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select ar.*, a.book_id, a.chapter_id, a.selected_text,
               b.title as book_title, c.title as chapter_title, c.chapter_index
        from annotation_replies ar join annotations a on a.id = ar.annotation_id
        join books b on b.id = a.book_id join chapters c on c.id = a.chapter_id
        where (%s::uuid is null or a.book_id = %s::uuid)
          and (%s::uuid is null or a.chapter_id = %s::uuid)
        """,
        (book_id, book_id, chapter_id, chapter_id),
    )
    return [_json_safe(row) for row in rows]


def _trace_user_replies(book_id: UUID | None, chapter_id: UUID | None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select tur.*, xt.book_id, xt.chapter_id, xt.selected_text,
               b.title as book_title, c.title as chapter_title, c.chapter_index
        from thought_user_replies tur join xiaxia_thoughts xt on xt.id = tur.thought_id
        join books b on b.id = xt.book_id join chapters c on c.id = xt.chapter_id
        where (%s::uuid is null or xt.book_id = %s::uuid)
          and (%s::uuid is null or xt.chapter_id = %s::uuid)
        """,
        (book_id, book_id, chapter_id, chapter_id),
    )
    return [_json_safe(row) for row in rows]
