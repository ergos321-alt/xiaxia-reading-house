from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import memories
import reading
from app import create_app
from psycopg.errors import UndefinedTable


ROOT = Path(__file__).resolve().parents[1]
BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")
CHAPTER_ID = UUID("22222222-2222-4222-8222-222222222222")
CHAPTER_TWO_ID = UUID("22222222-2222-4222-8222-333333333333")
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
BLOCKS = [
    {"block_id": "b000001", "block_order": 0, "text": "春天从窗边经过。"},
    {"block_id": "b000002", "block_order": 1, "text": "两个人在这里停了一会儿。"},
]


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


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


def web_client():
    client = make_app().test_client()
    assert client.post("/login", data={"password": "private-test-password"}).status_code == 302
    return client


def anchor(start_block, start_offset, end_block, end_offset, **extra):
    result = {
        "start_block_id": start_block,
        "start_offset": start_offset,
        "end_block_id": end_block,
        "end_offset": end_offset,
    }
    result.update(extra)
    return result


def test_anchor_matching_supports_exact_overlap_and_same_block():
    annotation = anchor("b000001", 0, "b000001", 4)
    exact = anchor("b000001", 0, "b000001", 4, scope="range")
    overlap = anchor("b000001", 2, "b000001", 7, scope="range")
    same_block = anchor("b000001", 6, "b000001", 8, scope="range")
    block_scope = anchor("b000001", 0, "b000001", 9, scope="block")
    elsewhere = anchor("b000002", 0, "b000002", 4, scope="range")

    assert memories._match_anchor_pair(annotation, exact, BLOCKS)["match_level"] == "exact"
    assert memories._match_anchor_pair(annotation, overlap, BLOCKS)["match_level"] == "overlap"
    assert memories._match_anchor_pair(annotation, same_block, BLOCKS)["match_level"] == "same_block"
    assert memories._match_anchor_pair(annotation, block_scope, BLOCKS)["match_level"] == "same_block"
    assert memories._match_anchor_pair(annotation, elsewhere, BLOCKS) is None


def test_shared_stops_use_existing_stable_anchors_and_are_deterministic(monkeypatch):
    annotation_id = uuid4()
    thought_id = uuid4()
    content_html = "".join(
        f'<p id="{item["block_id"]}" data-block-id="{item["block_id"]}">{item["text"]}</p>'
        for item in BLOCKS
    )
    annotation = {
        "id": annotation_id,
        "book_id": BOOK_ID,
        "chapter_id": CHAPTER_ID,
        "selected_text": "春天",
        "comment": "我在这里慢下来。",
        "start_block_id": "b000001",
        "start_offset": 0,
        "end_block_id": "b000001",
        "end_offset": 2,
        "created_at": NOW,
        "updated_at": NOW,
        "chapter_index": 0,
        "chapter_title": "第一章",
        "content_html": content_html,
    }
    thought = {
        "id": thought_id,
        "book_id": BOOK_ID,
        "chapter_id": CHAPTER_ID,
        "scope": "range",
        "mark_type": "thought",
        "content": "我也在这里停过。",
        "selected_text": "春天",
        "start_block_id": "b000001",
        "start_offset": 0,
        "end_block_id": "b000001",
        "end_offset": 2,
        "created_at": NOW,
        "updated_at": NOW,
    }

    def fetch_all(query, _params=()):
        if "from annotations" in query:
            return [annotation]
        if "from xiaxia_thoughts" in query:
            return [thought]
        raise AssertionError(query)

    monkeypatch.setattr(memories.db, "fetch_all", fetch_all)
    first = memories._shared_stops(BOOK_ID)
    second = memories._shared_stops(BOOK_ID)

    assert len(first) == 1
    assert first[0]["stop_id"] == second[0]["stop_id"]
    assert first[0]["block_id"] == "b000001"
    assert first[0]["match_level"] == "exact"
    assert first[0]["user_annotations"][0]["anchor"]["start_block_id"] == "b000001"
    assert first[0]["xiaxia_thoughts"][0]["anchor"]["start_block_id"] == "b000001"


def test_double_blind_reflection_payload_never_leaks_counterpart_content():
    rows = [
        {
            "id": uuid4(), "owner": "user", "rating": 4,
            "review_text": "我的封底话", "submitted_at": NOW, "updated_at": NOW,
        },
        {
            "id": uuid4(), "owner": "xiaxia", "rating": 5,
            "review_text": "她的封底话", "submitted_at": NOW, "updated_at": NOW,
        },
    ]
    sealed = memories._reflection_payload(rows, {"reflections_revealed_at": None}, "user")
    assert sealed["user"]["review_text"] == "我的封底话"
    assert sealed["xiaxia"] == {"owner": "xiaxia", "submitted": True}

    revealed = memories._reflection_payload(
        rows, {"reflections_revealed_at": NOW}, "user"
    )
    assert revealed["xiaxia"]["review_text"] == "她的封底话"
    assert revealed["revealed"] is True


def test_ai_completion_requires_every_real_chapter(monkeypatch):
    class MemoryConnection:
        def __init__(self):
            self.completed = set()
            self.state = {
                "book_id": BOOK_ID,
                "user_completed_at": None,
                "xiaxia_completed_at": None,
                "shared_completed_at": None,
                "reflections_revealed_at": None,
            }

        def execute(self, query, params=()):
            normalized = " ".join(query.split())
            if "select id from chapters" in normalized:
                return Cursor({"id": params[0]} if params[0] in {CHAPTER_ID, CHAPTER_TWO_ID} else None)
            if "insert into book_memory_state" in normalized:
                return Cursor(self.state)
            if "insert into ai_chapter_completions" in normalized:
                self.completed.add(params[1])
                return Cursor()
            if "select b.chapter_count" in normalized:
                return Cursor(
                    {
                        "chapter_count": 2,
                        "completed_count": len(self.completed),
                        "latest_completed_at": NOW,
                    }
                )
            if "set xiaxia_completed_at" in normalized:
                self.state["xiaxia_completed_at"] = NOW
                return Cursor()
            if "select * from book_memory_state" in normalized:
                return Cursor(self.state)
            if "update book_memory_state" in normalized or "insert into reading_memory_events" in normalized:
                return Cursor()
            raise AssertionError(normalized)

    connection = MemoryConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(memories.db, "transaction", transaction)
    memories.record_ai_chapter_completion(BOOK_ID, CHAPTER_ID, f"{CHAPTER_ID}:0", "b000001", NOW)
    assert connection.state["xiaxia_completed_at"] is None
    memories.record_ai_chapter_completion(BOOK_ID, CHAPTER_TWO_ID, f"{CHAPTER_TWO_ID}:0", "b000002", NOW)
    assert connection.state["xiaxia_completed_at"] == NOW


def test_completion_endpoint_requires_final_chapter_and_near_full_progress(monkeypatch):
    class CompletionConnection:
        def __init__(self, progress):
            self.progress = progress

        def execute(self, query, _params=()):
            if "from reading_progress" in query:
                return Cursor(self.progress)
            raise AssertionError(query)

    connection = CompletionConnection(
        {
            "chapter_id": CHAPTER_ID,
            "chapter_index": 0,
            "percentage": 88,
            "updated_at": NOW,
            "chapter_count": 2,
        }
    )

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(memories.db, "transaction", transaction)
    response = web_client().post(f"/api/books/{BOOK_ID}/completion")
    assert response.status_code == 409
    assert response.get_json()["error"] == "book_not_at_end"
    assert response.get_json()["message"] == "尚未读到最后一章末尾。"


def test_completion_endpoint_marks_shared_only_when_both_are_complete(monkeypatch):
    class CompletionConnection:
        def __init__(self):
            self.state = {
                "book_id": BOOK_ID,
                "user_completed_at": None,
                "xiaxia_completed_at": NOW,
                "shared_completed_at": None,
                "reflections_revealed_at": None,
            }

        def execute(self, query, _params=()):
            normalized = " ".join(query.split())
            if "from reading_progress" in normalized:
                return Cursor(
                    {
                        "chapter_id": CHAPTER_TWO_ID,
                        "chapter_index": 1,
                        "percentage": 100,
                        "updated_at": NOW,
                        "chapter_count": 2,
                    }
                )
            if "insert into book_memory_state" in normalized:
                return Cursor(self.state)
            if "set user_completed_at" in normalized:
                self.state["user_completed_at"] = NOW
                return Cursor()
            if "select * from book_memory_state" in normalized:
                return Cursor(self.state)
            if "set shared_completed_at" in normalized:
                self.state["shared_completed_at"] = NOW
                return Cursor()
            if "insert into reading_memory_events" in normalized:
                return Cursor()
            raise AssertionError(normalized)

    connection = CompletionConnection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(memories.db, "transaction", transaction)
    response = web_client().post(f"/api/books/{BOOK_ID}/completion")
    assert response.status_code == 200
    assert response.get_json()["completion"] == {
        "user_completed": True,
        "xiaxia_completed": True,
        "shared_completed": True,
        "user_completed_at": NOW.isoformat(),
        "xiaxia_completed_at": NOW.isoformat(),
        "shared_completed_at": NOW.isoformat(),
    }


def test_real_request_order_progress_completion_and_back_cover_is_closed(monkeypatch):
    class FlowDatabase:
        def __init__(self):
            self.progress = None
            self.state = {
                "book_id": BOOK_ID,
                "user_completed_at": None,
                "xiaxia_completed_at": NOW,
                "shared_completed_at": None,
                "reflections_revealed_at": None,
            }

        def execute(self, query, params=()):
            normalized = " ".join(query.split())
            if "insert into reading_progress" in normalized:
                self.progress = {
                    "chapter_id": params[1],
                    "chapter_index": params[2],
                    "position": {
                        "block_id": "b000002",
                        "display_mode": "paginated",
                        "page_index": 4,
                        "page_count": 5,
                    },
                    "percentage": float(params[4]),
                    "updated_at": NOW,
                }
                return self.progress
            if "insert into reading_memory_events" in normalized:
                return {"id": uuid4()}
            raise AssertionError(normalized)

        def fetch_one(self, query, _params=()):
            normalized = " ".join(query.split())
            if "select id from chapters" in normalized:
                return {"id": CHAPTER_ID}
            if "from books where id" in normalized:
                return {
                    "id": BOOK_ID,
                    "title": "闭环测试书",
                    "author": "测试作者",
                    "cover_asset_path": None,
                    "chapter_count": 1,
                    "created_at": NOW,
                }
            raise AssertionError(normalized)

        def fetch_all(self, query, _params=()):
            normalized = " ".join(query.split())
            if any(
                table in normalized
                for table in (
                    "from annotations",
                    "from xiaxia_thoughts",
                    "from book_reflections",
                    "from reading_letters",
                    "from reading_memory_events",
                )
            ):
                return []
            raise AssertionError(normalized)

        @contextmanager
        def transaction(self):
            database = self

            class Connection:
                def execute(self, query, params=()):
                    normalized = " ".join(query.split())
                    if "from reading_progress rp" in normalized:
                        return Cursor(
                            {
                                **database.progress,
                                "chapter_count": 1,
                            }
                        )
                    if "insert into book_memory_state" in normalized:
                        return Cursor(database.state)
                    if "set user_completed_at" in normalized:
                        database.state["user_completed_at"] = NOW
                        return Cursor()
                    if "select b.chapter_count" in normalized:
                        return Cursor(
                            {
                                "chapter_count": 1,
                                "completed_count": 1,
                                "latest_completed_at": NOW,
                            }
                        )
                    if "set xiaxia_completed_at" in normalized:
                        database.state["xiaxia_completed_at"] = NOW
                        return Cursor()
                    if "set shared_completed_at" in normalized:
                        database.state["shared_completed_at"] = NOW
                        return Cursor()
                    if "select * from book_memory_state" in normalized:
                        return Cursor(database.state)
                    if "insert into reading_memory_events" in normalized:
                        return Cursor()
                    raise AssertionError(normalized)

            yield Connection()

    database = FlowDatabase()
    monkeypatch.setattr(reading.db, "execute", database.execute)
    monkeypatch.setattr(reading.db, "fetch_one", database.fetch_one)
    monkeypatch.setattr(reading.db, "fetch_all", database.fetch_all)
    monkeypatch.setattr(reading.db, "transaction", database.transaction)

    client = web_client()
    progress = client.put(
        f"/api/books/{BOOK_ID}/progress",
        json={
            "chapter_id": str(CHAPTER_ID),
            "chapter_index": 0,
            "position": {
                "block_id": "b000002",
                "char_offset": 0,
                "scroll_fraction": 1,
                "display_mode": "paginated",
                "page_index": 4,
                "page_count": 5,
            },
            "percentage": 100,
        },
    )
    assert progress.status_code == 200

    completion = client.post(f"/api/books/{BOOK_ID}/completion")
    assert completion.status_code == 200
    assert completion.get_json()["completion"]["shared_completed"] is True

    cover = client.get(f"/api/books/{BOOK_ID}/back-cover")
    assert cover.status_code == 200
    payload = cover.get_json()
    assert payload["access_state"] == "open"
    assert payload["memory_state"] == "waiting_for_user_reflection"
    assert payload["stamp"] == {
        "visible": True,
        "text": "一起读过",
        "completed_month": "2026.08",
        "completed_date": "2026.08.24",
    }


def test_unapplied_v2_migration_is_actionable_and_does_not_break_v1_progress(monkeypatch):
    def missing_table(*_args, **_kwargs):
        raise UndefinedTable("relation reading_memory_events does not exist")

    monkeypatch.setattr(memories.db, "fetch_one", missing_table)
    response = web_client().get(f"/api/books/{BOOK_ID}/back-cover")
    assert response.status_code == 503
    assert response.get_json() == {
        "error": "v2_migration_required",
        "message": "读完以后所需的数据表尚未就绪，请先执行 005_v2_reading_memories.sql。",
        "migration": "migrations/005_v2_reading_memories.sql",
    }

    monkeypatch.setattr(memories.db, "execute", missing_table)
    with make_app().app_context():
        memories.record_user_progress_milestone(BOOK_ID, CHAPTER_ID, 100, NOW)

        @contextmanager
        def missing_v2_transaction():
            raise UndefinedTable("relation ai_chapter_completions does not exist")
            yield

        monkeypatch.setattr(memories.db, "transaction", missing_v2_transaction)
        memories.record_ai_chapter_completion(
            BOOK_ID, CHAPTER_ID, f"{CHAPTER_ID}:0", "b000002", NOW
        )


def test_progress_http_response_survives_missing_v2_timeline_table(monkeypatch):
    monkeypatch.setattr(reading.db, "fetch_one", lambda *_args, **_kwargs: {"id": CHAPTER_ID})

    def execute(query, _params=()):
        if "insert into reading_progress" in query:
            return {
                "chapter_id": CHAPTER_ID,
                "chapter_index": 0,
                "position": {"block_id": "b000002", "display_mode": "paginated"},
                "percentage": 100.0,
                "updated_at": NOW,
            }
        if "insert into reading_memory_events" in query:
            raise UndefinedTable("relation reading_memory_events does not exist")
        raise AssertionError(query)

    monkeypatch.setattr(reading.db, "execute", execute)
    response = web_client().put(
        f"/api/books/{BOOK_ID}/progress",
        json={
            "chapter_id": str(CHAPTER_ID),
            "chapter_index": 0,
            "position": {
                "block_id": "b000002",
                "char_offset": 0,
                "scroll_fraction": 1,
                "display_mode": "paginated",
                "page_index": 4,
                "page_count": 5,
            },
            "percentage": 100,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["progress"]["percentage"] == 100.0


def test_stamp_and_back_cover_business_states_are_explicit():
    stamp = memories._stamp_payload({"shared_completed_at": NOW})
    assert stamp == {
        "visible": True,
        "text": "一起读过",
        "completed_month": "2026.08",
        "completed_date": "2026.08.24",
    }
    completion = {"shared_completed": False}
    reflections = {
        "revealed": False,
        "user": {"submitted": False},
        "xiaxia": {"submitted": False},
    }
    assert memories._memory_state(completion, reflections, "user") == "waiting_for_xiaxia_completion"
    completion["shared_completed"] = True
    assert memories._memory_state(completion, reflections, "user") == "waiting_for_user_reflection"
    reflections["user"]["submitted"] = True
    assert memories._memory_state(completion, reflections, "user") == "waiting_for_xiaxia_reflection"
    reflections["xiaxia"]["submitted"] = True
    reflections["revealed"] = True
    assert memories._memory_state(completion, reflections, "user") == "reflections_revealed"


def test_v2_web_and_xiaxia_identity_boundaries_are_separate():
    client = make_app().test_client()
    assert client.get(f"/reader/{BOOK_ID}/after-reading").status_code == 302
    assert client.put(f"/api/books/{BOOK_ID}/reflection", headers={"Authorization": "Bearer action-test-token"}, json={}).status_code == 401
    assert client.put(f"/api/xiaxia/books/{BOOK_ID}/reflection", json={}).status_code == 401

    web = web_client()
    page = web.get(f"/reader/{BOOK_ID}/after-reading")
    assert page.status_code == 200
    assert "读完以后" in page.get_data(as_text=True)
    assert web.put(f"/api/xiaxia/books/{BOOK_ID}/reflection", json={}).status_code == 401


def test_v2_migration_is_additive_cascading_and_action_schema_is_frozen():
    migration = (ROOT / "migrations/005_v2_reading_memories.sql").read_text(encoding="utf-8").lower()
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8").lower()
    openapi = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    tables = (
        "book_memory_state",
        "ai_chapter_completions",
        "book_reflections",
        "reading_letters",
        "reading_memory_events",
    )
    for table in tables:
        assert f"create table if not exists {table}" in migration
        assert f"create table if not exists {table}" in schema
    assert migration.count("references books(id) on delete cascade") >= 5
    for destructive in ("drop table", "truncate", "delete from books", "delete from annotations"):
        assert destructive not in migration
    assert "/back-cover" not in openapi
    assert "/reflection" not in openapi
    assert "/shared-stops" not in openapi


def test_v2_reader_and_back_cover_ui_keep_existing_anchor_model():
    reader_template = (ROOT / "templates/reader.html").read_text(encoding="utf-8")
    reader_js = (ROOT / "static/js/reader.js").read_text(encoding="utf-8")
    memory_template = (ROOT / "templates/after_reading.html").read_text(encoding="utf-8")
    memory_js = (ROOT / "static/js/after-reading.js").read_text(encoding="utf-8")

    assert 'id="after-reading-link"' in reader_template
    assert 'id="finish-book-entry"' in reader_template
    assert "saveProgress(true, true)" in reader_js
    for field in ("chapter_id", "annotation_id"):
        assert field in memory_js
    for section in ("reading-stamp", "shared-stops-list", "user-reflection-form", "user-letter-form", "memory-timeline"):
        assert f'id="{section}"' in memory_template
    assert "stamp?.completed_date || stamp?.completed_month" in memory_js
    assert "waiting_for_xiaxia_completion" in memory_js
