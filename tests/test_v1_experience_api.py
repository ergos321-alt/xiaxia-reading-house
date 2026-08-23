from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

import annotations as annotations_api
import reading
from app import create_app


BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_BOOK_ID = UUID("11111111-1111-4111-8111-222222222222")
CHAPTER_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_CHAPTER_ID = UUID("22222222-2222-4222-8222-333333333333")
ANNOTATION_ID = UUID("33333333-3333-4333-8333-333333333333")
THOUGHT_ID = UUID("55555555-5555-4555-8555-555555555555")
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


def make_web_client():
    client = make_client()
    response = client.post("/login", data={"password": "private-test-password"})
    assert response.status_code == 302
    return client


def book_row(title="旧书名", author="旧作者"):
    return {
        "id": BOOK_ID,
        "title": title,
        "author": author,
        "format": "epub",
        "source_filename": "book.epub",
        "cover_asset_path": None,
        "chapter_count": 2,
        "toc": [],
        "created_at": NOW,
        "updated_at": NOW,
    }


def annotation_row(*, comment="", response=None):
    return {
        "id": ANNOTATION_ID,
        "book_id": BOOK_ID,
        "chapter_id": CHAPTER_ID,
        "selected_text": "林知夏",
        "start_block_id": "b000001",
        "start_offset": 0,
        "end_block_id": "b000001",
        "end_offset": 3,
        "prefix_text": "",
        "suffix_text": "读到这里。",
        "comment": comment,
        "status": "replied" if response else "pending",
        "created_at": NOW,
        "updated_at": NOW,
        "xiaxia_response": response,
        "reply_created_at": NOW if response else None,
        "reply_updated_at": NOW if response else None,
    }


def thought_row(*, scope="range"):
    return {
        "id": THOUGHT_ID,
        "book_id": BOOK_ID,
        "chapter_id": CHAPTER_ID,
        "scope": scope,
        "mark_type": "question",
        "content": "我想把这个问题留在这里。",
        "selected_text": "林知夏" if scope != "chapter" else "",
        "start_block_id": "b000001" if scope != "chapter" else None,
        "start_offset": 0 if scope != "chapter" else None,
        "end_block_id": "b000001" if scope != "chapter" else None,
        "end_offset": 3 if scope != "chapter" else None,
        "prefix_text": "",
        "suffix_text": "读到这里。" if scope != "chapter" else "",
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_manual_book_title_and_author_edit_is_persisted(monkeypatch):
    captured = {}

    def fake_execute(query, params=()):
        captured["query"] = query
        captured["params"] = params
        return book_row("手动书名", "手动作者")

    monkeypatch.setattr(reading.db, "execute", fake_execute)
    response = make_web_client().patch(
        f"/api/books/{BOOK_ID}",
        json={"title": "手动书名", "author": "手动作者"},
    )
    assert response.status_code == 200
    assert response.get_json()["book"]["title"] == "手动书名"
    assert response.get_json()["book"]["author"] == "手动作者"
    assert "update books set title = %s, author = %s" in captured["query"]
    assert captured["params"] == ["手动书名", "手动作者", BOOK_ID]


def test_chapter_directory_returns_stable_ids_in_reading_order(monkeypatch):
    captured = {}
    rows = [
        {
            "chapter_id": CHAPTER_ID,
            "chapter_index": 0,
            "title": "第一章",
            "word_count": 1200,
        },
        {
            "chapter_id": OTHER_CHAPTER_ID,
            "chapter_index": 1,
            "title": "第二章",
            "word_count": 900,
        },
    ]

    def fake_fetch_all(query, _params=()):
        captured["query"] = query
        return rows

    monkeypatch.setattr(reading.db, "fetch_all", fake_fetch_all)
    response = make_client().get(
        f"/api/books/{BOOK_ID}/chapters", headers=auth_headers()
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["chapter_id"] for item in payload["chapters"]] == [
        str(CHAPTER_ID),
        str(OTHER_CHAPTER_ID),
    ]
    assert [item["chapter_index"] for item in payload["chapters"]] == [0, 1]
    assert "order by chapter_index" in captured["query"].lower()


def test_annotation_edit_keeps_anchor_and_updates_comment(monkeypatch):
    original = annotation_row(comment="补写后的想法")
    captured = {}

    class Connection:
        def execute(self, query, params=()):
            captured.setdefault("queries", []).append(query)
            captured.setdefault("params", []).append(params)
            if "select * from annotations" in query:
                return DeleteCursor({**original, "owner": "user", "content_type": "user_annotation"})
            if "select " in query and "from annotations a" in query:
                return DeleteCursor(original)
            if "insert into reading_operation_log" in query:
                return DeleteCursor({"id": THOUGHT_ID})
            return DeleteCursor()

    @contextmanager
    def fake_transaction():
        yield Connection()

    monkeypatch.setattr(annotations_api.db, "transaction", fake_transaction)
    response = make_web_client().patch(
        f"/api/annotations/{ANNOTATION_ID}",
        json={"comment": "补写后的想法"},
    )
    assert response.status_code == 200
    item = response.get_json()["annotation"]
    assert item["comment"] == "补写后的想法"
    assert item["start_block_id"] == "b000001"
    assert item["start_offset"] == 0
    update_query = next(query for query in captured["queries"] if "update annotations set comment" in query)
    assert "set comment = %s" in update_query
    assert "start_block_id =" not in update_query
    assert "updated_at = now()" in update_query


class DeleteCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class DeleteConnection:
    def __init__(self, had_reply):
        self.had_reply = had_reply
        self.queries = []

    def execute(self, query, _params=()):
        self.queries.append(query)
        if "select * from annotations" in query:
            return DeleteCursor({
                **annotation_row(), "owner": "user", "content_type": "user_annotation"
            })
        if "select * from annotation_replies" in query:
            return DeleteCursor(
                {
                    "id": THOUGHT_ID, "annotation_id": ANNOTATION_ID,
                    "response": "回复", "owner": "xiaxia",
                    "content_type": "xiaxia_reply", "created_at": NOW,
                    "updated_at": NOW,
                }
                if self.had_reply else None
            )
        if "insert into reading_operation_log" in query:
            return DeleteCursor({"id": THOUGHT_ID})
        return DeleteCursor()


@pytest.mark.parametrize("had_reply", [False, True])
def test_annotation_delete_atomically_handles_reply(monkeypatch, had_reply):
    connection = DeleteConnection(had_reply)

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(annotations_api.db, "transaction", fake_transaction)
    response = make_web_client().delete(f"/api/annotations/{ANNOTATION_ID}")
    assert response.status_code == 200
    assert response.get_json()["deleted_reply"] is had_reply
    assert any("delete from annotations" in query for query in connection.queries)


def test_annotation_overview_filter_and_source_jump_fields(monkeypatch):
    row = {
        **annotation_row(comment=""),
        "chapter_title": "第一章",
        "chapter_index": 0,
    }
    captured = {}

    def fake_fetch_all(query, _params=()):
        captured.setdefault("queries", []).append(query)
        return [row]

    monkeypatch.setattr(annotations_api.db, "fetch_all", fake_fetch_all)
    response = make_client().get(
        f"/api/books/{BOOK_ID}/annotations?filter=highlights",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["filter"] == "highlights"
    assert payload["xiaxia_thoughts"] == []
    item = payload["annotations"][0]
    for field in (
        "chapter_id",
        "chapter_index",
        "chapter_title",
        "start_block_id",
        "start_offset",
        "end_block_id",
        "end_offset",
        "selected_text",
    ):
        assert field in item
    assert "a.comment = ''" in captured["queries"][0]


def test_chapter_annotation_sync_returns_new_reply_and_independent_thought(monkeypatch):
    def fake_fetch_all(query, _params=()):
        if "from annotations" in query:
            return [annotation_row(response="我刚刚写回的内容。")]
        if "from xiaxia_thoughts" in query:
            return [thought_row()]
        raise AssertionError(query)

    monkeypatch.setattr(annotations_api.db, "fetch_all", fake_fetch_all)
    response = make_client().get(
        f"/api/books/{BOOK_ID}/chapters/{CHAPTER_ID}/annotations",
        headers=auth_headers(),
    )
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["annotations"][0]["xiaxia_response"] == "我刚刚写回的内容。"
    assert payload["xiaxia_thoughts"][0]["mark_type"] == "question"
    assert payload["sync_token"]


def test_ai_creates_precise_independent_range_thought_from_read_block(monkeypatch):
    content_html = '<p id="b000001" data-block-id="b000001">林知夏读到这里。</p>'
    captured = {}
    monkeypatch.setattr(
        annotations_api.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": CHAPTER_ID,
            "book_id": BOOK_ID,
            "content_html": content_html,
        },
    )

    class ThoughtConnection:
        def execute(self, query, params=()):
            if "insert into xiaxia_thoughts" in query:
                captured["query"] = query
                captured["params"] = params
                return DeleteCursor({**thought_row(), "owner": "xiaxia", "content_type": "xiaxia_thought"})
            return DeleteCursor({"id": THOUGHT_ID})

    @contextmanager
    def thought_transaction():
        yield ThoughtConnection()

    monkeypatch.setattr(annotations_api.db, "transaction", thought_transaction)
    response = make_client().post(
        "/api/xiaxia/thoughts",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(CHAPTER_ID),
            "scope": "range",
            "mark_type": "question",
            "content": "我想把这个问题留在这里。",
            "start_block_id": "b000001",
            "start_offset": 0,
            "end_block_id": "b000001",
            "end_offset": 3,
            "selected_text": "林知夏",
        },
    )
    assert response.status_code == 201
    assert captured["params"][5:10] == (
        "林知夏",
        "b000001",
        0,
        "b000001",
        3,
    )
    assert "insert into xiaxia_thoughts" in captured["query"]


def test_ai_chapter_thought_has_no_fake_text_anchor(monkeypatch):
    monkeypatch.setattr(
        annotations_api.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": CHAPTER_ID,
            "book_id": BOOK_ID,
            "content_html": '<p data-block-id="b000001">正文</p>',
        },
    )
    captured = {}

    class ChapterThoughtConnection:
        def execute(self, query, params=()):
            if "insert into xiaxia_thoughts" in query:
                captured["params"] = params
                return DeleteCursor({**thought_row(scope="chapter"), "owner": "xiaxia", "content_type": "xiaxia_thought"})
            return DeleteCursor({"id": THOUGHT_ID})

    @contextmanager
    def chapter_thought_transaction():
        yield ChapterThoughtConnection()

    monkeypatch.setattr(annotations_api.db, "transaction", chapter_thought_transaction)
    response = make_client().post(
        "/api/xiaxia/thoughts",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(CHAPTER_ID),
            "scope": "chapter",
            "content": "这是本章级想法。",
        },
    )
    assert response.status_code == 201
    assert captured["params"][5:10] == ("", None, None, None, None)


def make_long_chapter_html(block_count=11, block_size=1100):
    return "".join(
        f'<p id="b{index:06d}" data-block-id="b{index:06d}">段{index}-'
        f"{'字' * block_size}</p>"
        for index in range(1, block_count + 1)
    )


def install_context_data(monkeypatch, *, content_html, annotations=None):
    annotations = annotations or []

    def fake_fetch_one(query, _params=()):
        if "from chapters c join books" in query:
            return {
                "id": OTHER_CHAPTER_ID,
                "book_id": BOOK_ID,
                "chapter_index": 1,
                "title": "任意选择的第二章",
                "content_html": content_html,
                "content_text": "\n\n".join(
                    block["text"] for block in reading._chapter_blocks(content_html)
                ),
                "word_count": len(content_html),
                "book_title": "测试之书",
                "book_author": "测试作者",
                "chapter_count": 2,
            }
        if "from reading_progress" in query:
            return {
                "chapter_id": CHAPTER_ID,
                "chapter_index": 0,
                "position": {"block_id": "b000001", "char_offset": 0},
                "percentage": 12.5,
                "updated_at": NOW,
            }
        raise AssertionError(query)

    def fake_fetch_all(query, _params=()):
        if "from annotations" in query:
            return annotations
        if "from xiaxia_thoughts" in query:
            return []
        raise AssertionError(query)

    monkeypatch.setattr(reading.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(reading.db, "fetch_all", fake_fetch_all)


def test_custom_gpt_can_choose_arbitrary_chapter_and_read_chunk_zero(monkeypatch):
    long_html = make_long_chapter_html()
    install_context_data(monkeypatch, content_html=long_html)
    response = make_client().get(
        f"/api/reading/context?chapter_id={OTHER_CHAPTER_ID}&chunk_index=0",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    chapter = response.get_json()["chapter"]
    assert chapter["id"] == str(OTHER_CHAPTER_ID)
    assert chapter["chunk_index"] == 0
    assert chapter["chunk_id"] == f"{OTHER_CHAPTER_ID}:0"
    assert chapter["chunk_count"] > 1
    assert chapter["has_next"] is True
    assert chapter["blocks"][0]["block_id"] == "b000001"
    assert chapter["blocks"][0]["block_order"] == 0


def test_chunk_blocks_use_same_ids_and_offsets_as_annotation_anchor(monkeypatch):
    long_html = make_long_chapter_html()
    focused = {
        **annotation_row(comment="这一段"),
        "chapter_id": OTHER_CHAPTER_ID,
        "start_block_id": "b000008",
        "start_offset": 3,
        "end_block_id": "b000008",
        "end_offset": 8,
        "selected_text": "字字字字字",
    }
    install_context_data(monkeypatch, content_html=long_html, annotations=[focused])
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda query, params=(): (
            {
                **focused,
                "xiaxia_response": None,
                "reply_created_at": None,
            }
            if "from annotations" in query
            else {
                "id": OTHER_CHAPTER_ID,
                "book_id": BOOK_ID,
                "chapter_index": 1,
                "title": "任意选择的第二章",
                "content_html": long_html,
                "content_text": "正文",
                "word_count": 12000,
                "book_title": "测试之书",
                "book_author": "测试作者",
                "chapter_count": 2,
            }
            if "from chapters c join books" in query
            else {
                "chapter_id": CHAPTER_ID,
                "chapter_index": 0,
                "position": {},
                "percentage": 0,
                "updated_at": NOW,
            }
        ),
    )
    response = make_client().get(
        f"/api/reading/context?annotation_id={ANNOTATION_ID}",
        headers=auth_headers(),
    )
    payload = response.get_json()
    chunk = payload["chapter"]
    assert payload["focus_annotation_chunk_index"] == chunk["chunk_index"]
    matching = [block for block in chunk["blocks"] if block["block_id"] == "b000008"]
    assert matching
    assert matching[0]["start_offset"] <= 3 < matching[0]["end_offset"]


def test_short_chapter_is_not_split_unnecessarily(monkeypatch):
    short_html = (
        '<p id="b000001" data-block-id="b000001">短章节第一段。</p>'
        '<p id="b000002" data-block-id="b000002">短章节第二段。</p>'
    )
    install_context_data(monkeypatch, content_html=short_html)
    response = make_client().get(
        f"/api/reading/context?chapter_id={OTHER_CHAPTER_ID}",
        headers=auth_headers(),
    )
    chapter = response.get_json()["chapter"]
    assert chapter["chunk_count"] == 1
    assert chapter["has_next"] is False
    assert chapter["is_complete_chapter"] is True
    assert "full_text" in chapter


def test_original_reading_progress_persists_paginated_display_state(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        reading.db, "fetch_one", lambda *_args, **_kwargs: {"id": CHAPTER_ID}
    )

    def fake_execute(_query, params=()):
        captured["params"] = params
        return {
            "chapter_id": CHAPTER_ID,
            "chapter_index": 0,
            "position": params[3].obj,
            "percentage": 25,
            "updated_at": NOW,
        }

    monkeypatch.setattr(reading.db, "execute", fake_execute)
    response = make_web_client().put(
        f"/api/books/{BOOK_ID}/progress",
        json={
            "chapter_id": str(CHAPTER_ID),
            "chapter_index": 0,
            "position": {
                "block_id": "b000007",
                "char_offset": 9,
                "scroll_fraction": 0.25,
                "display_mode": "paginated",
                "page_index": 3,
                "page_count": 12,
            },
            "percentage": 25,
        },
    )
    assert response.status_code == 200
    position = response.get_json()["progress"]["position"]
    assert position["block_id"] == "b000007"
    assert position["display_mode"] == "paginated"
    assert position["page_index"] == 3


def test_ai_chunk_checkpoint_persists_and_completed_requires_final_chunk(monkeypatch):
    long_html = make_long_chapter_html()
    chunks = reading._chunk_chapter_blocks(reading._chapter_blocks(long_html))
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda query, _params=(): (
            {"id": OTHER_CHAPTER_ID, "content_html": long_html}
            if "from chapters" in query
            else None
        ),
    )
    captured = {}

    def fake_execute(_query, params=()):
        captured["params"] = params
        return {
            "book_id": BOOK_ID,
            "last_chapter_read": OTHER_CHAPTER_ID,
            "last_chunk_index": params[2],
            "last_chunk_id": params[3],
            "last_block_id": params[4],
            "chapter_completed": params[5],
            "last_annotation_seen": None,
            "updated_at": NOW,
        }

    monkeypatch.setattr(reading.db, "execute", fake_execute)
    nonfinal = make_client().post(
        "/api/ai/progress",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(OTHER_CHAPTER_ID),
            "chunk_index": 0,
            "last_block_id": "b000001",
            "chapter_completed": True,
        },
    )
    assert nonfinal.status_code == 400
    assert nonfinal.get_json()["error"] == "chapter_not_fully_read"

    middle = make_client().post(
        "/api/ai/progress",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(OTHER_CHAPTER_ID),
            "chunk_index": 0,
            "chunk_id": f"{OTHER_CHAPTER_ID}:0",
            "last_block_id": chunks[0]["blocks"][-1]["block_id"],
            "chapter_completed": False,
        },
    )
    assert middle.status_code == 200
    assert middle.get_json()["ai_reading_state"]["chapter_completed"] is False

    wrong_block = make_client().post(
        "/api/ai/progress",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(OTHER_CHAPTER_ID),
            "chunk_index": 0,
            "last_block_id": "b999999",
            "chapter_completed": False,
        },
    )
    assert wrong_block.status_code == 400
    assert wrong_block.get_json()["error"] == "last_block_not_in_chunk"

    wrong_chunk_id = make_client().post(
        "/api/ai/progress",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(OTHER_CHAPTER_ID),
            "chunk_index": 0,
            "chunk_id": f"{CHAPTER_ID}:0",
        },
    )
    assert wrong_chunk_id.status_code == 400
    assert wrong_chunk_id.get_json()["error"] == "chunk_chapter_mismatch"

    final_index = len(chunks) - 1
    final_block = chunks[-1]["blocks"][-1]["block_id"]
    completed = make_client().post(
        "/api/ai/progress",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(OTHER_CHAPTER_ID),
            "chunk_index": final_index,
            "last_block_id": final_block,
            "chapter_completed": True,
        },
    )
    assert completed.status_code == 200
    state = completed.get_json()["ai_reading_state"]
    assert state["last_chunk_index"] == final_index
    assert state["last_block_id"] == final_block
    assert state["chapter_completed"] is True
    assert captured["params"][2:6] == (
        final_index, f"{OTHER_CHAPTER_ID}:{final_index}", final_block, True
    )

    normalized = make_client().post(
        "/api/ai/progress",
        headers=auth_headers(),
        json={
            "book_id": str(BOOK_ID),
            "chapter_id": str(OTHER_CHAPTER_ID),
            "chunk_index": final_index,
            "last_block_id": str(CHAPTER_ID),
            "chapter_completed": True,
        },
    )
    assert normalized.status_code == 200
    assert normalized.get_json()["last_block_normalized"] is True
    assert normalized.get_json()["ai_reading_state"]["last_block_id"] == final_block


def test_ai_chunk_checkpoint_is_recoverable_from_reading_state(monkeypatch):
    row = {
        **book_row(),
        "chapter_id": CHAPTER_ID,
        "chapter_index": 0,
        "position": {"block_id": "b000001"},
        "percentage": 25,
        "progress_updated_at": NOW,
        "current_chapter": "第一章",
        "last_chapter_read": OTHER_CHAPTER_ID,
        "last_chunk_index": 2,
        "last_chunk_id": f"{OTHER_CHAPTER_ID}:2",
        "last_block_id": "b000011",
        "chapter_completed": False,
        "last_annotation_seen": ANNOTATION_ID,
        "ai_updated_at": NOW,
        "pending_count": 1,
    }
    monkeypatch.setattr(reading.db, "fetch_one", lambda *_args, **_kwargs: row)
    response = make_client().get("/api/reading/state", headers=auth_headers())
    progress = response.get_json()["current"]["xiaxia_progress"]
    assert progress["last_chapter_read"] == str(OTHER_CHAPTER_ID)
    assert progress["last_chunk_index"] == 2
    assert progress["last_block_id"] == "b000011"
    assert progress["chapter_completed"] is False
