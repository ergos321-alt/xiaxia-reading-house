from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import annotations as annotations_api
import management
import operations
import reading
from app import create_app


BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_BOOK_ID = UUID("11111111-1111-4111-8111-222222222222")
CHAPTER_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_CHAPTER_ID = UUID("22222222-2222-4222-8222-333333333333")
THOUGHT_ID = UUID("55555555-5555-4555-8555-555555555555")
REPLY_ID = UUID("66666666-6666-4666-8666-666666666666")
ANNOTATION_ID = UUID("33333333-3333-4333-8333-333333333333")
CANDIDATE_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 8, 23, tzinfo=UTC)
CONTENT_HTML = '<p id="b000001" data-block-id="b000001">林知夏读到这里。</p>'


def make_app():
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "postgresql://test.invalid/test",
            "PRIVATE_ACCESS_PASSWORD": "private-test-password",
            "ACTION_API_TOKEN": "action-test-token",
            "SECRET_KEY": "test-session-secret",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
            "SUPABASE_STORAGE_BUCKET": "xiaxia-reading-house-private",
            "SESSION_COOKIE_SECURE": False,
        }
    )


def action_headers():
    return {"Authorization": "Bearer action-test-token"}


def web_client():
    client = make_app().test_client()
    assert client.post("/login", data={"password": "private-test-password"}).status_code == 302
    return client


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def thought_row(**updates):
    row = {
        "id": THOUGHT_ID,
        "book_id": BOOK_ID,
        "chapter_id": CHAPTER_ID,
        "scope": "range",
        "mark_type": "thought",
        "content": "旧想法",
        "selected_text": "林知夏",
        "start_block_id": "b000001",
        "start_offset": 0,
        "end_block_id": "b000001",
        "end_offset": 3,
        "prefix_text": "",
        "suffix_text": "读到这里。",
        "owner": "xiaxia",
        "content_type": "xiaxia_thought",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(updates)
    return row


def reply_row(**updates):
    row = {
        "id": REPLY_ID,
        "annotation_id": ANNOTATION_ID,
        "response": "旧回复",
        "owner": "xiaxia",
        "content_type": "xiaxia_reply",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(updates)
    return row


def user_reply_row(**updates):
    row = {
        "id": REPLY_ID,
        "thought_id": THOUGHT_ID,
        "response": "用户回复",
        "owner": "user",
        "content_type": "user_reply",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(updates)
    return row


def test_action_and_web_write_boundaries_are_separate():
    client = make_app().test_client()
    action_create_user = client.post(
        "/api/annotations", headers=action_headers(), json={}
    )
    assert action_create_user.status_code == 401
    assert action_create_user.get_json()["error"] == "web_session_required"
    web = web_client()
    web_create_thought = web.post(
        "/api/xiaxia/thoughts",
        json={"book_id": str(BOOK_ID), "chapter_id": str(CHAPTER_ID)},
    )
    assert web_create_thought.status_code == 401
    assert web_create_thought.get_json()["error"] == "action_unauthorized"


def test_thought_creation_then_completed_progress_saves_successfully(monkeypatch):
    class CreationConnection:
        def execute(self, query, _params=()):
            if "insert into xiaxia_thoughts" in query:
                return Cursor(thought_row())
            return Cursor({"id": uuid4()})

    @contextmanager
    def transaction():
        yield CreationConnection()

    monkeypatch.setattr(
        annotations_api.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": CHAPTER_ID, "book_id": BOOK_ID, "content_html": CONTENT_HTML
        },
    )
    monkeypatch.setattr(annotations_api.db, "transaction", transaction)
    client = make_app().test_client()
    created = client.post(
        "/api/xiaxia/thoughts",
        headers=action_headers(),
        json={
            "book_id": str(BOOK_ID), "chapter_id": str(CHAPTER_ID),
            "scope": "range", "content": "创建后继续保存进度",
            "start_block_id": "b000001", "start_offset": 0,
            "end_block_id": "b000001", "end_offset": 3,
            "selected_text": "林知夏",
        },
    )
    assert created.status_code == 201

    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda query, _params=(): (
            {"id": CHAPTER_ID, "content_html": CONTENT_HTML}
            if "from chapters" in query else None
        ),
    )
    monkeypatch.setattr(
        reading.db,
        "execute",
        lambda _query, params=(): {
            "book_id": params[0], "last_chapter_read": params[1],
            "last_chunk_index": params[2], "last_chunk_id": params[3],
            "last_block_id": params[4], "chapter_completed": params[5],
            "last_annotation_seen": params[6], "updated_at": NOW,
        },
    )
    saved = client.post(
        "/api/ai/progress",
        headers=action_headers(),
        json={
            "book_id": str(BOOK_ID), "chapter_id": str(CHAPTER_ID),
            "chunk_index": 0, "chunk_id": f"{CHAPTER_ID}:0",
            "last_block_id": "b000001", "chapter_completed": True,
        },
    )
    assert saved.status_code == 200
    assert saved.get_json()["ai_reading_state"]["chapter_completed"] is True


def test_list_xiaxia_thoughts_has_owner_anchor_and_pagination(monkeypatch):
    monkeypatch.setattr(management.db, "fetch_one", lambda *_args, **_kwargs: {"total": 1})
    row = {
        **thought_row(),
        "book_title": "测试书",
        "chapter_title": "第一章",
        "chapter_index": 0,
        "user_reply_id": None,
        "user_response": None,
        "user_reply_owner": None,
        "user_reply_content_type": None,
        "user_reply_created_at": None,
        "user_reply_updated_at": None,
    }
    monkeypatch.setattr(management.db, "fetch_all", lambda *_args, **_kwargs: [row])
    response = make_app().test_client().get(
        f"/api/xiaxia/thoughts?book_id={BOOK_ID}", headers=action_headers()
    )
    assert response.status_code == 200
    payload = response.get_json()
    thought = payload["thoughts"][0]
    assert thought["thought_id"] == str(THOUGHT_ID)
    assert thought["owner"] == "xiaxia"
    assert thought["content_type"] == "xiaxia_thought"
    assert thought["anchor"]["start_block_id"] == "b000001"
    assert payload["pagination"]["total"] == 1


class ThoughtUpdateConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "select * from xiaxia_thoughts" in query:
            return Cursor(thought_row())
        if "update xiaxia_thoughts set" in query:
            return Cursor(thought_row(content="新想法", mark_type="question", updated_at=NOW + timedelta(seconds=1)))
        if "insert into reading_operation_log" in query:
            return Cursor({"id": uuid4()})
        return Cursor()


def test_update_xiaxia_thought_preserves_anchor_and_logs(monkeypatch):
    connection = ThoughtUpdateConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    response = make_app().test_client().patch(
        f"/api/xiaxia/thoughts/{THOUGHT_ID}",
        headers=action_headers(),
        json={"content": "新想法", "mark_type": "question"},
    )
    assert response.status_code == 200
    item = response.get_json()["xiaxia_thought"]
    assert item["content"] == "新想法"
    assert item["start_block_id"] == "b000001"
    log_params = next(params for query, params in connection.queries if "insert into reading_operation_log" in query)
    assert log_params[0:3] == ("xiaxia", "update_thought", "xiaxia_thought")


class ThoughtDeleteConnection:
    def __init__(self, multiple=False):
        self.queries = []
        self.multiple = multiple

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "select * from xiaxia_thoughts" in query:
            rows = [thought_row()]
            return Cursor(rows[0], rows)
        if "select * from thought_user_replies" in query:
            rows = [user_reply_row()]
            return Cursor(rows[0], rows)
        if "insert into reading_operation_log" in query:
            return Cursor({"id": uuid4()})
        return Cursor()


def test_delete_xiaxia_thought_deletes_linked_user_reply_and_logs(monkeypatch):
    connection = ThoughtDeleteConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    response = make_app().test_client().delete(
        f"/api/xiaxia/thoughts/{THOUGHT_ID}", headers=action_headers()
    )
    assert response.status_code == 200
    assert response.get_json()["deleted_user_reply"] is True
    assert any("delete from xiaxia_thoughts" in query for query, _ in connection.queries)


def test_batch_delete_sql_is_limited_to_xiaxia_owned_thoughts(monkeypatch):
    connection = ThoughtDeleteConnection(multiple=True)

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    response = make_app().test_client().post(
        "/api/xiaxia/thoughts/batch-delete",
        headers=action_headers(),
        json={"book_id": str(BOOK_ID), "chapter_id": str(CHAPTER_ID)},
    )
    assert response.status_code == 200
    delete_query = next(query for query, _ in connection.queries if "delete from xiaxia_thoughts" in query)
    assert "owner = 'xiaxia'" in delete_query
    assert "content_type = 'xiaxia_thought'" in delete_query


class PreviewConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "select id, content_html from chapters" in query:
            return Cursor({"id": CHAPTER_ID, "content_html": CONTENT_HTML})
        return Cursor()


def test_preview_validates_exact_anchor_without_creating_thought(monkeypatch):
    connection = PreviewConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    response = make_app().test_client().post(
        "/api/xiaxia/thoughts/preview",
        headers=action_headers(),
        json={"candidates": [{
            "book_id": str(BOOK_ID), "chapter_id": str(CHAPTER_ID),
            "scope": "range", "content": "候选想法",
            "start_block_id": "b000001", "start_offset": 0,
            "end_block_id": "b000001", "end_offset": 3,
            "selected_text": "林知夏",
        }]},
    )
    assert response.status_code == 200
    candidate = response.get_json()["candidates"][0]
    assert candidate["validation_status"] == "valid"
    assert candidate["matched_text"] == "林知夏"
    assert any("insert into xiaxia_thought_candidates" in query for query, _ in connection.queries)
    assert not any("insert into xiaxia_thoughts" in query for query, _ in connection.queries)


def test_preview_rejects_text_that_does_not_match_offsets(monkeypatch):
    connection = PreviewConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    response = make_app().test_client().post(
        "/api/xiaxia/thoughts/preview",
        headers=action_headers(),
        json={"candidates": [{
            "book_id": str(BOOK_ID), "chapter_id": str(CHAPTER_ID),
            "scope": "range", "content": "候选想法",
            "start_block_id": "b000001", "start_offset": 0,
            "end_block_id": "b000001", "end_offset": 3,
            "selected_text": "不匹配",
        }]},
    )
    assert response.status_code == 200
    assert response.get_json()["candidates"][0]["validation_status"] == "invalid"


class CommitConnection:
    def __init__(self):
        self.queries = []
        self.thought = thought_row(content="候选想法")

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "select * from xiaxia_thought_candidates" in query:
            return Cursor({
                "id": CANDIDATE_ID,
                "book_id": BOOK_ID,
                "chapter_id": CHAPTER_ID,
                "request_payload": {
                    "scope": "range", "mark_type": "thought", "content": "候选想法"
                },
                "validated_anchor": {
                    "selected_text": "林知夏", "start_block_id": "b000001",
                    "start_offset": 0, "end_block_id": "b000001", "end_offset": 3,
                    "prefix_text": "", "suffix_text": "读到这里。",
                },
                "validation_status": "valid",
                "committed_thought_id": None,
                "expires_at": NOW + timedelta(days=1),
            })
        if "insert into xiaxia_thoughts" in query:
            return Cursor(self.thought)
        if "insert into reading_operation_log" in query:
            return Cursor({"id": uuid4()})
        return Cursor()


def test_commit_batch_creates_validated_thought_and_logs(monkeypatch):
    connection = CommitConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    monkeypatch.setattr(management, "datetime", type("Clock", (), {"now": staticmethod(lambda _tz: NOW)}))
    response = make_app().test_client().post(
        "/api/xiaxia/thoughts/commit",
        headers=action_headers(),
        json={"candidate_ids": [str(CANDIDATE_ID), "bad-id"]},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["committed_count"] == 1
    assert payload["failed_count"] == 1
    insert_params = next(params for query, params in connection.queries if "insert into xiaxia_thoughts" in query)
    assert insert_params[5:10] == ("林知夏", "b000001", 0, "b000001", 3)


class AnnotationReplyConnection:
    def __init__(self, delete=False):
        self.delete = delete
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "select ar.*, a.book_id" in query:
            return Cursor({**reply_row(), "book_id": BOOK_ID, "chapter_id": CHAPTER_ID})
        if "update annotation_replies" in query:
            return Cursor(reply_row(response="新回复", updated_at=NOW + timedelta(seconds=1)))
        if "update annotations set status = 'seen'" in query:
            return Cursor({"id": ANNOTATION_ID, "status": "seen", "updated_at": NOW})
        if "insert into reading_operation_log" in query:
            return Cursor({"id": uuid4()})
        return Cursor()


def test_xiaxia_reply_update_and_delete_keep_user_annotation(monkeypatch):
    connection = AnnotationReplyConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", transaction)
    client = make_app().test_client()
    updated = client.patch(
        f"/api/annotations/{ANNOTATION_ID}/reply",
        headers=action_headers(), json={"response": "新回复"},
    )
    assert updated.status_code == 200
    deleted = client.delete(
        f"/api/annotations/{ANNOTATION_ID}/reply", headers=action_headers()
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["annotation_status"] == "seen"
    assert any("delete from annotation_replies" in query for query, _ in connection.queries)
    assert not any("delete from annotations " in query for query, _ in connection.queries)


class UserReplyConnection:
    def __init__(self, existing=None):
        self.existing = existing
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "select id, book_id, chapter_id from xiaxia_thoughts" in query:
            return Cursor({"id": THOUGHT_ID, "book_id": BOOK_ID, "chapter_id": CHAPTER_ID})
        if "select * from thought_user_replies" in query:
            return Cursor(self.existing)
        if "insert into thought_user_replies" in query or "update thought_user_replies set response" in query:
            return Cursor(user_reply_row(response=params[1] if "insert" in query else params[0]))
        if "select tur.*, xt.book_id" in query:
            return Cursor({**user_reply_row(), "book_id": BOOK_ID, "chapter_id": CHAPTER_ID})
        if "insert into reading_operation_log" in query:
            return Cursor({"id": uuid4()})
        return Cursor()


def test_user_reply_to_xiaxia_thought_create_update_delete(monkeypatch):
    client = web_client()
    for method, existing in (("post", None), ("patch", user_reply_row())):
        connection = UserReplyConnection(existing)

        @contextmanager
        def transaction():
            yield connection

        monkeypatch.setattr(management.db, "transaction", transaction)
        response = getattr(client, method)(
            f"/api/xiaxia/thoughts/{THOUGHT_ID}/reply",
            json={"response": "我的新回复"},
        )
        assert response.status_code == (201 if method == "post" else 200)
        assert response.get_json()["user_reply"]["owner"] == "user"
    connection = UserReplyConnection()

    @contextmanager
    def delete_transaction():
        yield connection

    monkeypatch.setattr(management.db, "transaction", delete_transaction)
    deleted = client.delete(f"/api/xiaxia/thoughts/{THOUGHT_ID}/reply")
    assert deleted.status_code == 200


class UndoConnection:
    def __init__(self, conflict=False):
        self.conflict = conflict
        self.queries = []
        self.record = thought_row(updated_at=NOW)

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "from reading_operation_log" in query:
            return Cursor({
                "id": CANDIDATE_ID, "actor": "xiaxia",
                "operation_type": "create_thought", "target_type": "xiaxia_thought",
                "target_id": THOUGHT_ID, "previous_state": {"records": []},
                "new_state": {"records": [operations.snapshot(self.record, "xiaxia_thought")]},
                "created_at": NOW,
            })
        if "select * from xiaxia_thoughts" in query:
            current = dict(self.record)
            if self.conflict:
                current["updated_at"] = NOW + timedelta(seconds=1)
            return Cursor(current)
        return Cursor()


def test_undo_latest_create_and_conflict_detection(monkeypatch):
    connection = UndoConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(operations.db, "transaction", transaction)
    result = operations.undo_last("xiaxia")
    assert result["operation_type"] == "create_thought"
    assert any("delete from xiaxia_thoughts" in query for query, _ in connection.queries)
    assert any("set undone_at = now()" in query for query, _ in connection.queries)

    conflict = UndoConnection(conflict=True)

    @contextmanager
    def conflict_transaction():
        yield conflict

    monkeypatch.setattr(operations.db, "transaction", conflict_transaction)
    with pytest.raises(operations.UndoConflict):
        operations.undo_last("xiaxia")
    assert not any("delete from xiaxia_thoughts" in query for query, _ in conflict.queries)


def test_book_delete_cleans_only_selected_book_storage_and_database(monkeypatch):
    calls = {"storage": [], "db": []}
    monkeypatch.setattr(
        reading.db, "fetch_one",
        lambda *_args, **_kwargs: {"id": BOOK_ID, "title": "测试书", "source_object_path": f"books/{BOOK_ID}/source/source.epub"},
    )
    monkeypatch.setattr(
        reading.db, "fetch_all",
        lambda *_args, **_kwargs: [
            {"object_path": f"books/{BOOK_ID}/cover/a.jpg"},
            {"object_path": f"books/{BOOK_ID}/assets/b.png"},
        ],
    )
    monkeypatch.setattr(reading.object_storage, "delete_objects", lambda paths: calls["storage"].extend(paths))

    def execute(query, params=()):
        calls["db"].append((query, params))
        return {"id": BOOK_ID, "title": "测试书"}

    monkeypatch.setattr(reading.db, "execute", execute)
    response = web_client().delete(f"/api/books/{BOOK_ID}")
    assert response.status_code == 200
    assert response.get_json()["storage_objects_deleted"] == 3
    assert all(str(BOOK_ID) in path for path in calls["storage"])
    assert all(str(OTHER_BOOK_ID) not in path for path in calls["storage"])
    assert calls["db"][0][1] == (BOOK_ID,)


def test_book_delete_stops_before_database_when_storage_cleanup_fails(monkeypatch):
    monkeypatch.setattr(
        reading.db, "fetch_one",
        lambda *_args, **_kwargs: {"id": BOOK_ID, "title": "测试书", "source_object_path": f"books/{BOOK_ID}/source/source.epub"},
    )
    monkeypatch.setattr(reading.db, "fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        reading.object_storage, "delete_objects",
        lambda _paths: (_ for _ in ()).throw(reading.object_storage.ObjectStorageError("failed")),
    )
    called = []
    monkeypatch.setattr(reading.db, "execute", lambda *_args, **_kwargs: called.append(True))
    response = web_client().delete(f"/api/books/{BOOK_ID}")
    assert response.status_code == 502
    assert called == []
