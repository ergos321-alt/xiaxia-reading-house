"""Server-side operation log and single-step undo for reading traces."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

import database as db
from reading import _json_safe


class UndoConflict(RuntimeError):
    """The target changed after the logged operation and cannot be undone safely."""


RECORD_META: dict[str, dict[str, Any]] = {
    "user_annotation": {
        "table": "annotations",
        "columns": (
            "id", "book_id", "chapter_id", "selected_text", "start_block_id",
            "start_offset", "end_block_id", "end_offset", "prefix_text",
            "suffix_text", "comment", "status", "owner", "content_type",
            "created_at", "updated_at",
        ),
    },
    "xiaxia_reply": {
        "table": "annotation_replies",
        "columns": (
            "id", "annotation_id", "response", "owner", "content_type",
            "created_at", "updated_at",
        ),
    },
    "xiaxia_thought": {
        "table": "xiaxia_thoughts",
        "columns": (
            "id", "book_id", "chapter_id", "scope", "mark_type", "content",
            "selected_text", "start_block_id", "start_offset", "end_block_id",
            "end_offset", "prefix_text", "suffix_text", "owner", "content_type",
            "created_at", "updated_at",
        ),
    },
    "user_reply": {
        "table": "thought_user_replies",
        "columns": (
            "id", "thought_id", "response", "owner", "content_type",
            "created_at", "updated_at",
        ),
    },
}


def snapshot(row: dict[str, Any], content_type: str | None = None) -> dict[str, Any]:
    result = _json_safe(dict(row))
    if content_type:
        result["content_type"] = content_type
        result.setdefault("owner", "user" if content_type.startswith("user_") else "xiaxia")
    return result


def record(
    conn: Any,
    *,
    actor: str,
    operation_type: str,
    target_type: str,
    target_id: UUID | str | None,
    book_id: UUID | str | None,
    chapter_id: UUID | str | None,
    previous_records: list[dict[str, Any]] | None = None,
    new_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return conn.execute(
        """
        insert into reading_operation_log (
            actor, operation_type, target_type, target_id, book_id, chapter_id,
            previous_state, new_state
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        returning id, actor, operation_type, target_type, target_id, created_at
        """,
        (
            actor,
            operation_type,
            target_type,
            target_id,
            book_id,
            chapter_id,
            Jsonb({"records": previous_records or []}),
            Jsonb({"records": new_records or []}),
        ),
    ).fetchone()


def undo_last(actor: str) -> dict[str, Any] | None:
    with db.transaction() as conn:
        operation = conn.execute(
            """
            select id, actor, operation_type, target_type, target_id,
                   previous_state, new_state, created_at
            from reading_operation_log
            where actor = %s and undone_at is null
            order by created_at desc, id desc
            limit 1 for update
            """,
            (actor,),
        ).fetchone()
        if not operation:
            return None
        previous = (operation.get("previous_state") or {}).get("records", [])
        new = (operation.get("new_state") or {}).get("records", [])
        operation_type = str(operation["operation_type"])
        if operation_type.startswith("create") or operation_type == "batch_create":
            _undo_create(conn, new)
        elif operation_type.startswith("update"):
            _undo_update(conn, previous, new)
        elif operation_type.startswith("delete") or operation_type == "batch_delete":
            _undo_delete(conn, previous)
        else:
            raise UndoConflict("operation type is not undoable")
        conn.execute(
            "update reading_operation_log set undone_at = now() where id = %s",
            (operation["id"],),
        )
        return _json_safe(operation)


def _undo_create(conn: Any, records: list[dict[str, Any]]) -> None:
    for item in reversed(records):
        current = _fetch_record(conn, item)
        if not current or not _same_version(current, item):
            raise UndoConflict("created record changed or no longer exists")
        if _has_new_dependent(conn, item):
            raise UndoConflict("a later reply depends on the created record")
        meta = _meta(item)
        conn.execute(f"delete from {meta['table']} where id = %s", (item["id"],))
        _after_delete(conn, item)


def _undo_update(
    conn: Any, previous: list[dict[str, Any]], new: list[dict[str, Any]]
) -> None:
    if len(previous) != len(new) or not previous:
        raise UndoConflict("update snapshot is incomplete")
    new_by_key = {(item.get("content_type"), str(item.get("id"))): item for item in new}
    for old in previous:
        current_expected = new_by_key.get((old.get("content_type"), str(old.get("id"))))
        current = _fetch_record(conn, old)
        if not current_expected or not current or not _same_version(current, current_expected):
            raise UndoConflict("updated record changed after the operation")
    for old in previous:
        _restore_update(conn, old)
        _after_restore(conn, old)


def _undo_delete(conn: Any, records: list[dict[str, Any]]) -> None:
    if not records:
        raise UndoConflict("delete snapshot is incomplete")
    for item in records:
        if _fetch_record(conn, item):
            raise UndoConflict("a deleted record ID is already in use")
    for item in records:
        _restore_insert(conn, item)
        _after_restore(conn, item)


def _meta(item: dict[str, Any]) -> dict[str, Any]:
    content_type = item.get("content_type")
    if content_type not in RECORD_META:
        raise UndoConflict("unknown reading trace type")
    return RECORD_META[content_type]


def _fetch_record(conn: Any, item: dict[str, Any]) -> dict[str, Any] | None:
    meta = _meta(item)
    return conn.execute(
        f"select * from {meta['table']} where id = %s", (item["id"],)
    ).fetchone()


def _same_version(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    if expected.get("updated_at") is not None:
        current_value = _json_safe(current.get("updated_at"))
        return current_value == expected.get("updated_at")
    for key in ("content", "response", "comment", "status", "mark_type"):
        if key in expected and current.get(key) != expected.get(key):
            return False
    return True


def _restore_insert(conn: Any, item: dict[str, Any]) -> None:
    meta = _meta(item)
    columns = [column for column in meta["columns"] if column in item]
    placeholders = ", ".join(["%s"] * len(columns))
    conn.execute(
        f"insert into {meta['table']} ({', '.join(columns)}) values ({placeholders})",
        tuple(item[column] for column in columns),
    )


def _restore_update(conn: Any, item: dict[str, Any]) -> None:
    meta = _meta(item)
    columns = [
        column
        for column in meta["columns"]
        if column in item and column not in {"id", "created_at", "updated_at"}
    ]
    assignments = ", ".join(f"{column} = %s" for column in columns)
    conn.execute(
        f"update {meta['table']} set {assignments}, updated_at = now() where id = %s",
        tuple(item[column] for column in columns) + (item["id"],),
    )


def _after_delete(conn: Any, item: dict[str, Any]) -> None:
    if item.get("content_type") == "xiaxia_reply":
        conn.execute(
            "update annotations set status = 'seen', updated_at = now() where id = %s",
            (item.get("annotation_id"),),
        )


def _after_restore(conn: Any, item: dict[str, Any]) -> None:
    if item.get("content_type") == "xiaxia_reply":
        conn.execute(
            "update annotations set status = 'replied', updated_at = now() where id = %s",
            (item.get("annotation_id"),),
        )


def _has_new_dependent(conn: Any, item: dict[str, Any]) -> bool:
    content_type = item.get("content_type")
    if content_type == "user_annotation":
        row = conn.execute(
            "select id from annotation_replies where annotation_id = %s",
            (item.get("id"),),
        ).fetchone()
        return row is not None
    if content_type == "xiaxia_thought":
        row = conn.execute(
            "select id from thought_user_replies where thought_id = %s",
            (item.get("id"),),
        ).fetchone()
        return row is not None
    return False
