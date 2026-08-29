"""V2 after-reading memories without changing the V1 reading contracts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from psycopg.errors import UndefinedColumn, UndefinedTable
from psycopg.types.json import Jsonb

import database as db
from auth import action_required, web_api_required, web_required
from reading import _chapter_blocks, _json_safe


memories_bp = Blueprint("memories", __name__)

MATCH_PRIORITY = {"same_block": 1, "overlap": 2, "exact": 3}
MEMORY_EVENT_LABELS = {
    "started_reading": "我们从这里开始读",
    "first_shared_stop": "第一次在同一处停下来",
    "completed_reading": "我们一起读完了这本书",
    "back_cover_opened": "第一次打开封底",
}


@memories_bp.errorhandler(UndefinedTable)
@memories_bp.errorhandler(UndefinedColumn)
def memory_schema_unavailable(error):
    """Turn an unapplied V2 migration into an actionable business response."""
    current_app.logger.error("V2 reading-memory schema is unavailable: %s", error)
    return (
        jsonify(
            {
                "error": "v2_migration_required",
                "message": "读完以后所需的数据表尚未就绪，请先执行 005_v2_reading_memories.sql。",
                "migration": "migrations/005_v2_reading_memories.sql",
            }
        ),
        503,
    )


@memories_bp.get("/reader/<uuid:book_id>/after-reading")
@web_required
def after_reading_page(book_id: UUID):
    return render_template("after_reading.html", book_id=str(book_id))


@memories_bp.get("/api/books/<uuid:book_id>/back-cover")
@web_api_required
def get_back_cover(book_id: UUID):
    return _back_cover_response(book_id, "user")


@memories_bp.get("/api/xiaxia/books/<uuid:book_id>/back-cover")
@action_required
def get_xiaxia_back_cover(book_id: UUID):
    """Server-ready counterpart view; intentionally absent from V2 OpenAPI."""
    return _back_cover_response(book_id, "xiaxia")


@memories_bp.get("/api/books/<uuid:book_id>/shared-stops")
@web_api_required
def list_shared_stops(book_id: UUID):
    if not _book(book_id):
        return jsonify({"error": "book_not_found"}), 404
    state = _sync_memory_state(book_id)
    if not state.get("user_completed_at"):
        return jsonify({"error": "after_reading_locked"}), 409
    page, page_size = _pagination()
    stops = _shared_stops(book_id)
    _ensure_first_shared_stop_event(book_id, stops)
    start = (page - 1) * page_size
    items = stops[start : start + page_size]
    return jsonify(
        {
            "book_id": str(book_id),
            "shared_stops": [_public_stop(item) for item in items],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": len(stops),
                "page_count": max(1, (len(stops) + page_size - 1) // page_size),
            },
        }
    )


@memories_bp.post("/api/books/<uuid:book_id>/completion")
@web_api_required
def complete_book_for_user(book_id: UUID):
    with db.transaction() as conn:
        progress = conn.execute(
            """
            select rp.chapter_id, rp.chapter_index, rp.percentage, rp.updated_at,
                   b.chapter_count, b.reader_engine, b.source_sha256,
                   ppr.progression as publication_progression,
                   ppr.locator as publication_locator
            from reading_progress rp
            join books b on b.id = rp.book_id
            left join publication_reading_progress ppr on ppr.book_id = b.id
            where rp.book_id = %s
            for update of rp
            """,
            (book_id,),
        ).fetchone()
        legacy_at_end = False
        final_index = 0
        if progress:
            final_index = max(0, int(progress["chapter_count"]) - 1)
            legacy_at_end = (
                int(progress["chapter_index"]) == final_index
                and float(progress["percentage"]) >= 99.5
            )
        publication = None
        if progress and progress.get("reader_engine") == "foliate":
            publication = {
                "id": book_id,
                "reader_engine": progress.get("reader_engine"),
                "source_sha256": progress.get("source_sha256"),
                "progression": progress.get("publication_progression"),
                "locator": progress.get("publication_locator"),
            }
        if not progress and not publication:
            if not conn.execute("select id from books where id = %s", (book_id,)).fetchone():
                return jsonify({"error": "book_not_found", "message": "没有找到这本书。"}), 404
            return (
                jsonify(
                    {
                        "error": "reading_progress_not_found",
                        "message": "还没有可用于完成本书的阅读进度。",
                    }
                ),
                409,
            )
        locator = publication.get("locator") if publication else None
        foliate_at_end = bool(
            publication
            and publication.get("reader_engine") == "foliate"
            and publication.get("progression") is not None
            and float(publication["progression"]) >= 0.995
            and isinstance(locator, dict)
            and locator.get("source_sha256") == publication.get("source_sha256")
            and str(locator.get("cfi") or "").startswith("epubcfi(")
        )
        if not legacy_at_end and not foliate_at_end:
            return (
                jsonify(
                    {
                        "error": "book_not_at_end",
                        "message": "尚未读到最后一章末尾。",
                        "required_chapter_index": final_index,
                        "current_percentage": float(progress["percentage"]) if progress else float(publication.get("progression") or 0) * 100,
                    }
                ),
                409,
            )
        _ensure_state(conn, book_id)
        conn.execute(
            """
            update book_memory_state
            set user_completed_at = coalesce(user_completed_at, now())
            where book_id = %s and user_completed_at is null
            """,
            (book_id,),
        )
        state = _refresh_shared_completion(conn, book_id)
    return jsonify({"completion": _completion_payload(state)})


@memories_bp.put("/api/books/<uuid:book_id>/reflection")
@web_api_required
def write_user_reflection(book_id: UUID):
    return _write_reflection(book_id, "user")


@memories_bp.put("/api/xiaxia/books/<uuid:book_id>/reflection")
@action_required
def write_xiaxia_reflection(book_id: UUID):
    """Prepared for the later Action-schema phase; owner is server-enforced."""
    return _write_reflection(book_id, "xiaxia")


@memories_bp.put("/api/books/<uuid:book_id>/letter")
@web_api_required
def write_user_letter(book_id: UUID):
    return _write_letter(book_id, "user")


@memories_bp.put("/api/xiaxia/books/<uuid:book_id>/letter")
@action_required
def write_xiaxia_letter(book_id: UUID):
    """Prepared for the later Action-schema phase; owner is server-enforced."""
    return _write_letter(book_id, "xiaxia")


def record_user_progress_milestone(
    book_id: UUID,
    chapter_id: UUID,
    percentage: float,
    happened_at: datetime | None = None,
) -> None:
    """Record only the first meaningful progress write, never routine saves."""
    if percentage <= 0:
        return
    try:
        db.execute(
            """
            insert into reading_memory_events (
                book_id, event_type, actor, chapter_id, dedupe_key, happened_at
            ) values (%s, 'started_reading', 'user', %s, 'book', coalesce(%s, now()))
            on conflict (book_id, event_type, dedupe_key) do nothing
            returning id
            """,
            (book_id, chapter_id, happened_at),
        )
    except (UndefinedTable, UndefinedColumn) as error:
        # V2 timeline recording must never make the established V1 progress API fail.
        current_app.logger.warning("Skipped V2 user milestone before migration: %s", error)


def record_ai_chapter_completion(
    book_id: UUID,
    chapter_id: UUID,
    chunk_id: str | None,
    last_block_id: str | None,
    happened_at: datetime | None = None,
) -> None:
    """Persist a verified final-chunk checkpoint and refresh book completion."""
    try:
        with db.transaction() as conn:
            chapter = conn.execute(
                "select id from chapters where id = %s and book_id = %s",
                (chapter_id, book_id),
            ).fetchone()
            if not chapter:
                return
            _ensure_state(conn, book_id)
            conn.execute(
                """
                insert into ai_chapter_completions (
                    book_id, chapter_id, last_chunk_id, last_block_id, completed_at
                ) values (%s, %s, %s, %s, coalesce(%s, now()))
                on conflict (book_id, chapter_id) do update set
                    last_chunk_id = excluded.last_chunk_id,
                    last_block_id = excluded.last_block_id,
                    completed_at = least(
                        ai_chapter_completions.completed_at, excluded.completed_at
                    )
                """,
                (book_id, chapter_id, chunk_id, last_block_id, happened_at),
            )
            _refresh_ai_completion(conn, book_id)
            _refresh_shared_completion(conn, book_id)
    except (UndefinedTable, UndefinedColumn) as error:
        # The V2 completion ledger is additive; an unapplied migration must not
        # roll back the already verified Xiaxia checkpoint written by V1.1.
        current_app.logger.warning("Skipped V2 Xiaxia completion before migration: %s", error)


def _back_cover_response(book_id: UUID, perspective: str):
    book = _book(book_id)
    if not book:
        return jsonify({"error": "book_not_found"}), 404
    state = _sync_memory_state(book_id)
    owner_completed = state.get(f"{perspective}_completed_at") is not None
    if not owner_completed:
        response = jsonify(
            {
                "book": _book_payload(book),
                "access_state": "locked",
                "completion": _completion_payload(state),
                "message": "读完以后，这一页会从书里打开。",
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    stops = _shared_stops(book_id)
    _ensure_first_shared_stop_event(book_id, stops)
    if state.get("shared_completed_at"):
        with db.transaction() as conn:
            _insert_event(
                conn,
                book_id=book_id,
                event_type="back_cover_opened",
                actor="both",
            )
    state = _sync_memory_state(book_id)
    reflections = db.fetch_all(
        """
        select id, book_id, owner, rating, review_text, submitted_at,
               created_at, updated_at
        from book_reflections where book_id = %s order by owner
        """,
        (book_id,),
    )
    letters = db.fetch_all(
        """
        select id, book_id, author, recipient, content, letter_date,
               sent_at, created_at, updated_at
        from reading_letters where book_id = %s order by sent_at, id
        """,
        (book_id,),
    )
    events = db.fetch_all(
        """
        select id, event_type, actor, chapter_id, event_data,
               happened_at, created_at
        from reading_memory_events where book_id = %s
        order by happened_at, id
        """,
        (book_id,),
    )
    payload = {
        "book": _book_payload(book),
        "access_state": "open",
        "completion": _completion_payload(state),
        "reflections": _reflection_payload(reflections, state, perspective),
        "shared_stops": {
            "count": len(stops),
            "preview": [_public_stop(item) for item in stops[:3]],
        },
        "letters": [_json_safe(row) for row in letters]
        if state.get("shared_completed_at")
        else [],
        "timeline": [_event_payload(row) for row in events],
        "stamp": _stamp_payload(state),
    }
    payload["memory_state"] = _memory_state(payload["completion"], payload["reflections"], perspective)
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def _write_reflection(book_id: UUID, owner: str):
    payload = request.get_json(silent=True) or {}
    try:
        rating = int(payload.get("rating"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_rating"}), 400
    review_text = _clean_content(payload.get("review_text"), 50_000)
    if rating < 1 or rating > 5:
        return jsonify({"error": "invalid_rating"}), 400
    if not review_text:
        return jsonify({"error": "review_text_required"}), 400

    with db.transaction() as conn:
        if not conn.execute("select id from books where id = %s", (book_id,)).fetchone():
            return jsonify({"error": "book_not_found"}), 404
        _ensure_state(conn, book_id)
        state = conn.execute(
            "select * from book_memory_state where book_id = %s for update",
            (book_id,),
        ).fetchone()
        if state.get("reflections_revealed_at"):
            return jsonify({"error": "reflection_locked_after_reveal"}), 409
        if not state.get(f"{owner}_completed_at"):
            return jsonify({"error": "owner_has_not_completed_book"}), 409
        row = conn.execute(
            """
            insert into book_reflections (
                book_id, owner, rating, review_text, submitted_at
            ) values (%s, %s, %s, %s, now())
            on conflict (book_id, owner) do update set
                rating = excluded.rating,
                review_text = excluded.review_text,
                submitted_at = now(),
                updated_at = now()
            returning id, book_id, owner, rating, review_text,
                      submitted_at, created_at, updated_at
            """,
            (book_id, owner, rating, review_text),
        ).fetchone()
        submitted_count = conn.execute(
            "select count(*) as total from book_reflections where book_id = %s",
            (book_id,),
        ).fetchone()["total"]
        state = _refresh_shared_completion(conn, book_id)
        if submitted_count == 2 and state.get("shared_completed_at"):
            conn.execute(
                """
                update book_memory_state
                set reflections_revealed_at = coalesce(reflections_revealed_at, now())
                where book_id = %s
                """,
                (book_id,),
            )
            state = conn.execute(
                "select * from book_memory_state where book_id = %s",
                (book_id,),
            ).fetchone()
    return jsonify(
        {
            "reflection": _json_safe(row),
            "revealed": state.get("reflections_revealed_at") is not None,
        }
    )


def _write_letter(book_id: UUID, author: str):
    payload = request.get_json(silent=True) or {}
    content = _clean_content(payload.get("content"), 50_000)
    if not content:
        return jsonify({"error": "letter_content_required"}), 400
    recipient = "xiaxia" if author == "user" else "user"
    with db.transaction() as conn:
        if not conn.execute("select id from books where id = %s", (book_id,)).fetchone():
            return jsonify({"error": "book_not_found"}), 404
        _ensure_state(conn, book_id)
        state = _refresh_shared_completion(conn, book_id)
        if not state.get("shared_completed_at"):
            return jsonify({"error": "shared_reading_not_completed"}), 409
        row = conn.execute(
            """
            insert into reading_letters (
                book_id, author, recipient, content
            ) values (%s, %s, %s, %s)
            on conflict (book_id, author) do update set
                content = excluded.content,
                updated_at = now()
            returning id, book_id, author, recipient, content, letter_date,
                      sent_at, created_at, updated_at
            """,
            (book_id, author, recipient, content),
        ).fetchone()
    return jsonify({"letter": _json_safe(row)})


def _book(book_id: UUID) -> dict[str, Any] | None:
    return db.fetch_one(
        """
        select id, title, author, cover_asset_path, chapter_count, created_at
        from books where id = %s
        """,
        (book_id,),
    )


def _book_payload(book: dict[str, Any]) -> dict[str, Any]:
    payload = _json_safe(book)
    payload["cover_url"] = (
        url_for(
            "reading.get_asset",
            book_id=book["id"],
            asset_path=book["cover_asset_path"],
        )
        if book.get("cover_asset_path")
        else None
    )
    return payload


def _ensure_state(conn: Any, book_id: UUID) -> dict[str, Any]:
    inserted = conn.execute(
        """
        insert into book_memory_state (book_id) values (%s)
        on conflict (book_id) do nothing
        returning *
        """,
        (book_id,),
    ).fetchone()
    if inserted:
        return inserted
    return conn.execute(
        "select * from book_memory_state where book_id = %s", (book_id,)
    ).fetchone()


def _sync_memory_state(book_id: UUID) -> dict[str, Any]:
    with db.transaction() as conn:
        _ensure_state(conn, book_id)
        _refresh_ai_completion(conn, book_id)
        return _refresh_shared_completion(conn, book_id)


def _refresh_ai_completion(conn: Any, book_id: UUID) -> None:
    completion = conn.execute(
        """
        select b.chapter_count, count(distinct acc.chapter_id) as completed_count,
               max(acc.completed_at) as latest_completed_at
        from books b
        left join ai_chapter_completions acc on acc.book_id = b.id
        where b.id = %s
        group by b.chapter_count
        """,
        (book_id,),
    ).fetchone()
    if not completion:
        return
    if (
        int(completion["chapter_count"]) > 0
        and int(completion["completed_count"]) == int(completion["chapter_count"])
    ):
        conn.execute(
            """
            update book_memory_state
            set xiaxia_completed_at = coalesce(xiaxia_completed_at, %s)
            where book_id = %s and xiaxia_completed_at is null
            """,
            (completion["latest_completed_at"], book_id),
        )


def _refresh_shared_completion(conn: Any, book_id: UUID) -> dict[str, Any]:
    state = conn.execute(
        "select * from book_memory_state where book_id = %s for update",
        (book_id,),
    ).fetchone()
    if state.get("user_completed_at") and state.get("xiaxia_completed_at"):
        completed_at = max(state["user_completed_at"], state["xiaxia_completed_at"])
        conn.execute(
            """
            update book_memory_state
            set shared_completed_at = coalesce(shared_completed_at, %s)
            where book_id = %s and shared_completed_at is null
            """,
            (completed_at, book_id),
        )
        _insert_event(
            conn,
            book_id=book_id,
            event_type="completed_reading",
            actor="both",
            happened_at=completed_at,
        )
        state = conn.execute(
            "select * from book_memory_state where book_id = %s",
            (book_id,),
        ).fetchone()
    return state


def _insert_event(
    conn: Any,
    *,
    book_id: UUID,
    event_type: str,
    actor: str,
    chapter_id: UUID | None = None,
    source_annotation_id: UUID | None = None,
    source_thought_id: UUID | None = None,
    event_data: dict[str, Any] | None = None,
    happened_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        insert into reading_memory_events (
            book_id, event_type, actor, chapter_id,
            source_annotation_id, source_thought_id,
            dedupe_key, event_data, happened_at
        ) values (%s, %s, %s, %s, %s, %s, 'book', %s, coalesce(%s, now()))
        on conflict (book_id, event_type, dedupe_key) do nothing
        """,
        (
            book_id,
            event_type,
            actor,
            chapter_id,
            source_annotation_id,
            source_thought_id,
            Jsonb(event_data or {}),
            happened_at,
        ),
    )


def _completion_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_completed": state.get("user_completed_at") is not None,
        "xiaxia_completed": state.get("xiaxia_completed_at") is not None,
        "shared_completed": state.get("shared_completed_at") is not None,
        "user_completed_at": _json_safe(state.get("user_completed_at")),
        "xiaxia_completed_at": _json_safe(state.get("xiaxia_completed_at")),
        "shared_completed_at": _json_safe(state.get("shared_completed_at")),
    }


def _reflection_payload(
    rows: list[dict[str, Any]], state: dict[str, Any], perspective: str
) -> dict[str, Any]:
    by_owner = {row["owner"]: row for row in rows}
    revealed = state.get("reflections_revealed_at") is not None
    sides: dict[str, Any] = {}
    for owner in ("user", "xiaxia"):
        row = by_owner.get(owner)
        item: dict[str, Any] = {"owner": owner, "submitted": row is not None}
        if row and (revealed or owner == perspective):
            item.update(
                {
                    "id": str(row["id"]),
                    "rating": row["rating"],
                    "review_text": row["review_text"],
                    "submitted_at": _json_safe(row["submitted_at"]),
                    "updated_at": _json_safe(row["updated_at"]),
                }
            )
        sides[owner] = item
    return {
        "revealed": revealed,
        "revealed_at": _json_safe(state.get("reflections_revealed_at")),
        "user": sides["user"],
        "xiaxia": sides["xiaxia"],
    }


def _stamp_payload(state: dict[str, Any]) -> dict[str, Any]:
    completed_at = state.get("shared_completed_at")
    return {
        "visible": completed_at is not None,
        "text": "一起读过",
        "completed_month": completed_at.strftime("%Y.%m") if completed_at else None,
        "completed_date": completed_at.strftime("%Y.%m.%d") if completed_at else None,
    }


def _memory_state(
    completion: dict[str, Any], reflections: dict[str, Any], perspective: str
) -> str:
    if not completion.get("shared_completed"):
        counterpart = "xiaxia" if perspective == "user" else "user"
        return f"waiting_for_{counterpart}_completion"
    if reflections.get("revealed"):
        return "reflections_revealed"
    if not reflections.get(perspective, {}).get("submitted"):
        return f"waiting_for_{perspective}_reflection"
    counterpart = "xiaxia" if perspective == "user" else "user"
    if not reflections.get(counterpart, {}).get("submitted"):
        return f"waiting_for_{counterpart}_reflection"
    return "waiting_for_reflection_reveal"


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = _json_safe(row)
    payload["label"] = MEMORY_EVENT_LABELS.get(row["event_type"], row["event_type"])
    return payload


def _shared_stops(book_id: UUID) -> list[dict[str, Any]]:
    annotations = db.fetch_all(
        """
        select a.id, a.book_id, a.chapter_id, a.selected_text, a.comment,
               a.start_block_id, a.start_offset, a.end_block_id, a.end_offset,
               a.created_at, a.updated_at,
               c.chapter_index, c.title as chapter_title, c.content_html
        from annotations a
        join chapters c on c.id = a.chapter_id
        where a.book_id = %s
        order by c.chapter_index, a.created_at, a.id
        """,
        (book_id,),
    )
    thoughts = db.fetch_all(
        """
        select xt.id, xt.book_id, xt.chapter_id, xt.scope, xt.mark_type,
               xt.content, xt.selected_text,
               xt.start_block_id, xt.start_offset, xt.end_block_id, xt.end_offset,
               xt.created_at, xt.updated_at
        from xiaxia_thoughts xt
        where xt.book_id = %s and xt.scope in ('range', 'block')
        order by xt.created_at, xt.id
        """,
        (book_id,),
    )
    thoughts_by_chapter: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for thought in thoughts:
        thoughts_by_chapter[thought["chapter_id"]].append(thought)

    stops: dict[str, dict[str, Any]] = {}
    blocks_by_chapter: dict[Any, list[dict[str, Any]]] = {}
    for annotation in annotations:
        chapter_id = annotation["chapter_id"]
        if chapter_id not in blocks_by_chapter:
            blocks_by_chapter[chapter_id] = _chapter_blocks(
                annotation["content_html"]
            )
        blocks = blocks_by_chapter[chapter_id]
        for thought in thoughts_by_chapter.get(chapter_id, []):
            match = _match_anchor_pair(annotation, thought, blocks)
            if not match:
                continue
            block_id = match["block_id"]
            key = f"{chapter_id}:{block_id}"
            if key not in stops:
                block = next(item for item in blocks if item["block_id"] == block_id)
                stop_hash = hashlib.sha256(
                    f"{book_id}:{chapter_id}:{block_id}".encode("utf-8")
                ).hexdigest()[:24]
                stops[key] = {
                    "stop_id": f"stop_{stop_hash}",
                    "book_id": str(book_id),
                    "chapter_id": str(chapter_id),
                    "chapter_index": annotation["chapter_index"],
                    "chapter_title": annotation["chapter_title"],
                    "block_id": block_id,
                    "block_order": block["block_order"],
                    "match_level": match["match_level"],
                    "source_text": block["text"][:3000],
                    "user_annotations": {},
                    "xiaxia_thoughts": {},
                    "_happened_at": max(
                        annotation["created_at"], thought["created_at"]
                    ),
                }
            stop = stops[key]
            if MATCH_PRIORITY[match["match_level"]] > MATCH_PRIORITY[stop["match_level"]]:
                stop["match_level"] = match["match_level"]
            stop["_happened_at"] = min(
                stop["_happened_at"],
                max(annotation["created_at"], thought["created_at"]),
            )
            stop["user_annotations"][str(annotation["id"])] = _trace_payload(
                annotation, "user"
            )
            stop["xiaxia_thoughts"][str(thought["id"])] = _trace_payload(
                thought, "xiaxia"
            )

    result = []
    for stop in stops.values():
        stop["user_annotations"] = list(stop["user_annotations"].values())
        stop["xiaxia_thoughts"] = list(stop["xiaxia_thoughts"].values())
        result.append(stop)
    result.sort(key=lambda item: (item["chapter_index"], item["block_order"]))
    return result


def _match_anchor_pair(
    annotation: dict[str, Any], thought: dict[str, Any], blocks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    order = {block["block_id"]: index for index, block in enumerate(blocks)}
    lengths = {block["block_id"]: len(block["text"]) for block in blocks}
    a_start = order.get(annotation.get("start_block_id"))
    a_end = order.get(annotation.get("end_block_id"))
    x_start = order.get(thought.get("start_block_id"))
    x_end = order.get(thought.get("end_block_id"))
    if None in {a_start, a_end, x_start, x_end}:
        return None
    if a_start > a_end or x_start > x_end:
        return None
    common_start = max(a_start, x_start)
    common_end = min(a_end, x_end)
    if common_start > common_end:
        return None
    common_blocks = blocks[common_start : common_end + 1]
    if thought.get("scope") != "block" and all(
        annotation.get(field) == thought.get(field)
        for field in (
            "start_block_id", "start_offset", "end_block_id", "end_offset"
        )
    ):
        return {
            "match_level": "exact",
            "block_id": annotation["start_block_id"],
        }
    if thought.get("scope") != "block":
        for block in common_blocks:
            block_id = block["block_id"]
            a_interval = _block_interval(annotation, block_id, lengths[block_id])
            x_interval = _block_interval(thought, block_id, lengths[block_id])
            if max(a_interval[0], x_interval[0]) < min(a_interval[1], x_interval[1]):
                return {"match_level": "overlap", "block_id": block_id}
    return {"match_level": "same_block", "block_id": common_blocks[0]["block_id"]}


def _block_interval(record: dict[str, Any], block_id: str, length: int) -> tuple[int, int]:
    start = int(record["start_offset"]) if record["start_block_id"] == block_id else 0
    end = int(record["end_offset"]) if record["end_block_id"] == block_id else length
    return max(0, min(start, length)), max(0, min(end, length))


def _trace_payload(row: dict[str, Any], owner: str) -> dict[str, Any]:
    payload = {
        "id": str(row["id"]),
        "owner": owner,
        "selected_text": row.get("selected_text") or "",
        "anchor": {
            "start_block_id": row.get("start_block_id"),
            "start_offset": row.get("start_offset"),
            "end_block_id": row.get("end_block_id"),
            "end_offset": row.get("end_offset"),
        },
        "created_at": _json_safe(row.get("created_at")),
        "updated_at": _json_safe(row.get("updated_at")),
    }
    if owner == "user":
        payload["comment"] = row.get("comment") or ""
    else:
        payload["thought_content"] = row.get("content") or ""
        payload["mark_type"] = row.get("mark_type") or "thought"
        payload["scope"] = row.get("scope")
    return payload


def _public_stop(stop: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in stop.items() if not key.startswith("_")}


def _ensure_first_shared_stop_event(book_id: UUID, stops: list[dict[str, Any]]) -> None:
    if not stops:
        return
    first = min(stops, key=lambda item: item["_happened_at"])
    annotation_id = UUID(first["user_annotations"][0]["id"])
    thought_id = UUID(first["xiaxia_thoughts"][0]["id"])
    with db.transaction() as conn:
        _insert_event(
            conn,
            book_id=book_id,
            event_type="first_shared_stop",
            actor="both",
            chapter_id=UUID(first["chapter_id"]),
            source_annotation_id=annotation_id,
            source_thought_id=thought_id,
            event_data={
                "chapter_index": first["chapter_index"],
                "chapter_title": first["chapter_title"],
                "block_id": first["block_id"],
                "source_text": first["source_text"][:500],
            },
            happened_at=first["_happened_at"],
        )


def _pagination() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", "1")))
        page_size = max(1, min(50, int(request.args.get("page_size", "20"))))
    except ValueError:
        page, page_size = 1, 20
    return page, page_size


def _clean_content(value: Any, max_length: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:max_length]
