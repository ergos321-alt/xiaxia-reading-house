from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

import annotations as annotations_api
import reading
from app import create_app


BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")
CHAPTER_ID = UUID("22222222-2222-4222-8222-222222222222")
ANNOTATION_ID = UUID("33333333-3333-4333-8333-333333333333")
REPLY_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 23, tzinfo=UTC)


def make_client():
    app = create_app(
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
    return app.test_client()


def auth_headers():
    return {"Authorization": "Bearer action-test-token"}


def test_action_context_contains_complete_chapter_progress_and_annotations(monkeypatch):
    def fake_fetch_one(query, _params=()):
        if "from chapters c join books" in query:
            return {
                "id": CHAPTER_ID,
                "book_id": BOOK_ID,
                "chapter_index": 2,
                "title": "第三章",
                "content_html": (
                    '<p id="b000003" data-block-id="b000003">这是完整章节。</p>'
                    '<p id="b000004" data-block-id="b000004">第二段也在这里。</p>'
                ),
                "content_text": "这是完整章节。第二段也在这里。",
                "word_count": 16,
                "book_title": "测试之书",
                "book_author": "测试作者",
                "chapter_count": 8,
            }
        if "from reading_progress" in query:
            return {
                "chapter_id": CHAPTER_ID,
                "chapter_index": 2,
                "position": {"block_id": "b000003", "char_offset": 0},
                "percentage": 31.5,
                "updated_at": NOW,
            }
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(reading.db, "fetch_one", fake_fetch_one)

    def fake_fetch_all(query, _params=()):
        if "from xiaxia_thoughts" in query:
            return []
        if "from annotations" not in query:
            raise AssertionError(f"Unexpected query: {query}")
        return [
            {
                "id": ANNOTATION_ID,
                "selected_text": "第二段",
                "start_block_id": "b000004",
                "start_offset": 0,
                "end_block_id": "b000004",
                "end_offset": 3,
                "prefix_text": "",
                "suffix_text": "也在这里",
                "comment": "我想和你谈谈这里。",
                "status": "pending",
                "created_at": NOW,
                "updated_at": NOW,
                "xiaxia_response": None,
            }
        ]

    monkeypatch.setattr(reading.db, "fetch_all", fake_fetch_all)

    response = make_client().get(
        f"/api/reading/context?chapter_id={CHAPTER_ID}", headers=auth_headers()
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["chapter"]["full_text"] == "这是完整章节。第二段也在这里。"
    assert payload["reading_progress"]["percentage"] == 31.5
    assert payload["annotations"][0]["comment"] == "我想和你谈谈这里。"


class FakeCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class ReplyConnection:
    def __init__(self):
        self.calls = 0

    def execute(self, query, _params=()):
        self.calls += 1
        if "select id, book_id, chapter_id from annotations" in query:
            return FakeCursor(
                {"id": ANNOTATION_ID, "book_id": BOOK_ID, "chapter_id": CHAPTER_ID}
            )
        if "select * from annotation_replies" in query:
            return FakeCursor(None)
        if "insert into annotation_replies" in query:
            return FakeCursor(
                {
                    "id": REPLY_ID,
                    "annotation_id": ANNOTATION_ID,
                    "response": "我读到了，也把这句话留在这里。",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )
        if "insert into reading_operation_log" in query:
            return FakeCursor({"id": REPLY_ID})
        return FakeCursor()


def test_action_reply_is_returned_as_durable_replied_state(monkeypatch):
    connection = ReplyConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(annotations_api.db, "transaction", fake_transaction)
    response = make_client().post(
        f"/api/annotations/{ANNOTATION_ID}/reply",
        headers=auth_headers(),
        json={"response": "我读到了，也把这句话留在这里。"},
    )
    assert response.status_code == 200
    payload = response.get_json()["reply"]
    assert payload["status"] == "replied"
    assert payload["response"] == "我读到了，也把这句话留在这里。"
    assert connection.calls == 6
