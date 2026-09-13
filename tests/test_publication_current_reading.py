from datetime import UTC, datetime, timedelta
from uuid import UUID

import reading
import locator_bridge
from app import create_app


BOOK_A = UUID("11111111-1111-4111-8111-111111111111")
BOOK_B = UUID("22222222-2222-4222-8222-222222222222")
CHAPTER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CHAPTER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NOW = datetime(2026, 9, 13, tzinfo=UTC)


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


def publication_current_row(section_index=1):
    return {
        "id": BOOK_B,
        "title": "诗经",
        "author": "佚名",
        "format": "epub",
        "source_filename": "shijing.epub",
        "cover_asset_path": None,
        "chapter_count": 2,
        "toc": [],
        "publication_ready": True,
        "reader_engine": "foliate",
        "text_index_status": "ready",
        "locator_bridge_status": "ready",
        "locator_bridge_version": 1,
        "created_at": NOW - timedelta(days=2),
        "updated_at": NOW - timedelta(days=2),
        "legacy_chapter_id": CHAPTER_A,
        "legacy_chapter_index": 0,
        "legacy_position": {"block_id": "b000001"},
        "legacy_percentage": 10,
        "legacy_updated_at": NOW - timedelta(days=1),
        "legacy_chapter_title": "章节 A",
        "publication_locator": {
            "engine": "foliate-js",
            "engine_adapter_version": 1,
            "bridge_version": 1,
            "source_sha256": "a" * 64,
            "href": f"OEBPS/Text/{'a' if section_index == 0 else 'b'}.xhtml",
            "section_index": section_index,
            "cfi": f"epubcfi(/6/{2 + section_index * 2}!/4/2/1:0)",
        },
        "publication_progression": 0.65 if section_index else 0.2,
        "publication_progress_updated_at": NOW,
        "publication_current": True,
        "last_chapter_read": None,
        "last_chunk_index": None,
        "last_chunk_id": None,
        "last_block_id": None,
        "chapter_completed": False,
        "last_annotation_seen": None,
        "ai_updated_at": None,
        "pending_count": 0,
    }


def chapter_identity(chapter_id):
    return {
        "id": chapter_id,
        "chapter_index": 0 if chapter_id == CHAPTER_A else 1,
        "title": "章节 A" if chapter_id == CHAPTER_A else "章节 B",
        "href": f"Text/{'a' if chapter_id == CHAPTER_A else 'b'}.xhtml",
    }


def test_state_prefers_saved_foliate_progress_and_does_not_enumerate_books(monkeypatch):
    queries = []

    def fake_fetch_one(query, _params=()):
        queries.append(" ".join(query.split()))
        if "from books b" in query:
            return publication_current_row()
        if "from chapters where id" in query:
            return chapter_identity(CHAPTER_B)
        raise AssertionError(query)

    monkeypatch.setattr(reading.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        reading.db, "fetch_all", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("bookshelf enumeration"))
    )
    monkeypatch.setattr(reading, "_publication_chapter_id", lambda *_args: CHAPTER_B)

    response = make_client().get("/api/reading/state", headers=auth_headers())

    assert response.status_code == 200
    current = response.get_json()["current"]
    assert current["book"]["id"] == str(BOOK_B)
    assert current["user_progress"]["chapter_id"] == str(CHAPTER_B)
    assert current["user_progress"]["chapter_title"] == "章节 B"
    assert current["user_progress"]["position"]["section_index"] == 1
    assert current["user_progress"]["engine"] == "foliate-js"
    assert "limit 1" in queries[0].lower()
    assert "when b.reader_engine = 'foliate' then ppr.updated_at" in queries[0].lower()


def test_publication_chapter_projection_reuses_verified_bridge_identity(monkeypatch):
    locator = publication_current_row()["publication_locator"]
    bridge = {
        "package_path": "OEBPS/content.opf",
        "chapters": [{
            "chapter_id": str(CHAPTER_B),
            "href": "Text/b.xhtml",
            "spine_index": 1,
        }],
    }
    monkeypatch.setattr(
        locator_bridge,
        "_validated_bridge",
        lambda book_id, supplied: ({"id": book_id}, bridge)
        if supplied is locator
        else (_ for _ in ()).throw(AssertionError("unexpected locator")),
    )

    assert reading._publication_chapter_id(BOOK_B, locator) == CHAPTER_B


def test_state_tracks_foliate_section_change_from_a_to_b(monkeypatch):
    active = {"section": 0}

    def fake_fetch_one(query, _params=()):
        if "from books b" in query:
            return publication_current_row(active["section"])
        if "from chapters where id" in query:
            return chapter_identity(CHAPTER_A if active["section"] == 0 else CHAPTER_B)
        raise AssertionError(query)

    monkeypatch.setattr(reading.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        reading,
        "_publication_chapter_id",
        lambda *_args: CHAPTER_A if active["section"] == 0 else CHAPTER_B,
    )
    client = make_client()

    first = client.get("/api/reading/state", headers=auth_headers()).get_json()["current"]
    active["section"] = 1
    second = client.get("/api/reading/state", headers=auth_headers()).get_json()["current"]

    assert first["user_progress"]["chapter_id"] == str(CHAPTER_A)
    assert second["user_progress"]["chapter_id"] == str(CHAPTER_B)
    assert second["user_progress"]["section"]["href"] == "OEBPS/Text/b.xhtml"


def test_context_uses_foliate_chapter_and_position_instead_of_legacy_progress(monkeypatch):
    def fake_fetch_one(query, _params=()):
        if "from books b" in query:
            return publication_current_row()
        if "select id, chapter_index, title, href" in query:
            return chapter_identity(CHAPTER_B)
        if "from chapters c join books" in query:
            return {
                **chapter_identity(CHAPTER_B),
                "book_id": BOOK_B,
                "content_html": '<p data-block-id="b000001">国风正文</p>',
                "content_text": "国风正文",
                "word_count": 4,
                "book_title": "诗经",
                "book_author": "佚名",
                "chapter_count": 2,
            }
        if "from reading_progress" in query:
            raise AssertionError("legacy progress must not supply Foliate context")
        raise AssertionError(query)

    monkeypatch.setattr(reading.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(reading.db, "fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(reading, "_publication_chapter_id", lambda *_args: CHAPTER_B)

    response = make_client().get("/api/reading/context", headers=auth_headers())

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["book"]["id"] == str(BOOK_B)
    assert payload["chapter"]["id"] == str(CHAPTER_B)
    assert payload["chapter"]["title"] == "章节 B"
    assert payload["reading_progress"]["engine"] == "foliate-js"
    assert payload["reading_progress"]["position"]["section_index"] == 1
    assert payload["reading_progress"]["percentage"] == 65.0


def test_missing_publication_progress_preserves_legacy_state(monkeypatch):
    row = publication_current_row()
    row.update(
        {
            "reader_engine": "legacy",
            "publication_locator": None,
            "publication_progression": None,
            "publication_progress_updated_at": None,
            "publication_current": False,
        }
    )
    monkeypatch.setattr(reading.db, "fetch_one", lambda *_args, **_kwargs: row.copy())
    monkeypatch.setattr(
        reading,
        "_publication_chapter_id",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected publication mapping")),
    )

    response = make_client().get("/api/reading/state", headers=auth_headers())

    progress = response.get_json()["current"]["user_progress"]
    assert progress == {
        "chapter_id": str(CHAPTER_A),
        "chapter_index": 0,
        "chapter_title": "章节 A",
        "position": {"block_id": "b000001"},
        "percentage": 10.0,
        "updated_at": (NOW - timedelta(days=1)).isoformat(),
    }


def test_reopen_reads_the_same_persisted_publication_state(monkeypatch):
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda query, _params=(): (
            publication_current_row()
            if "from books b" in query
            else chapter_identity(CHAPTER_B)
        ),
    )
    monkeypatch.setattr(reading, "_publication_chapter_id", lambda *_args: CHAPTER_B)

    first = make_client().get("/api/reading/state", headers=auth_headers()).get_json()
    reopened = make_client().get("/api/reading/state", headers=auth_headers()).get_json()

    assert reopened["current"]["book"]["id"] == first["current"]["book"]["id"]
    assert reopened["current"]["user_progress"] == first["current"]["user_progress"]
