from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

from bs4 import BeautifulSoup

import reading
from app import create_app
from epub_parser import BookParseError, ParsedAsset, ParsedBook, ParsedChapter


BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class ImportConnection:
    def __init__(self, *, fail_book_insert=False):
        self.execute_queries = []
        self.batch_queries = []
        self.fail_book_insert = fail_book_insert

    def execute(self, query, params=()):
        normalized = " ".join(query.lower().split())
        self.execute_queries.append((normalized, params))
        if normalized.startswith("select id, title from books"):
            return Cursor(None)
        if "insert into books" in normalized:
            if self.fail_book_insert:
                raise RuntimeError("forced database failure")
            return Cursor(
                {
                    "id": params[0],
                    "title": params[1],
                    "author": params[2],
                    "format": params[3],
                    "source_filename": params[4],
                    "cover_asset_path": params[9],
                    "chapter_count": params[10],
                    "toc": [],
                    "created_at": datetime(2026, 8, 27, tzinfo=UTC),
                    "updated_at": datetime(2026, 8, 27, tzinfo=UTC),
                }
            )
        return Cursor(None)

    def executemany(self, query, rows):
        self.batch_queries.append((" ".join(query.lower().split()), list(rows)))


def parsed_book(chapter_count=1):
    return ParsedBook(
        title="诗集",
        author="作者",
        format="epub",
        source_filename="poems.epub",
        source_sha256="a" * 64,
        chapters=[
            ParsedChapter(
                index,
                f"第 {index + 1} 章",
                f"Text/{index}.xhtml",
                '<p id="b000001" data-block-id="b000001">正文</p>',
                "正文",
                2,
            )
            for index in range(chapter_count)
        ],
        assets=[ParsedAsset("Images/used.png", "image/png", b"image")],
        epub_version="3.0",
        manifest_item_count=chapter_count + 1,
        spine_item_count=chapter_count,
    )


def client():
    web = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "postgresql://test.invalid/test",
            "PRIVATE_ACCESS_PASSWORD": "private-test-password",
            "ACTION_API_TOKEN": "action-test-token",
            "SECRET_KEY": "test-session-secret",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role",
            "SUPABASE_STORAGE_BUCKET": "private-books",
            "SESSION_COOKIE_SECURE": False,
        }
    ).test_client()
    web.post("/login", data={"password": "private-test-password"})
    return web


def install_pipeline(monkeypatch, connection, parsed, uploaded, deleted):
    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(reading.db, "transaction", transaction)
    monkeypatch.setattr(reading, "parse_uploaded_book", lambda *_args: parsed)
    monkeypatch.setattr(
        reading.object_storage,
        "upload_many",
        lambda objects: uploaded.extend(path for path, _data, _type in objects)
        or [path for path, _data, _type in objects],
    )
    monkeypatch.setattr(
        reading.object_storage,
        "delete_objects",
        lambda paths: deleted.extend(paths),
    )


def test_five_hundred_chapters_use_two_bulk_database_calls(monkeypatch):
    connection = ImportConnection()
    uploaded = []
    deleted = []
    install_pipeline(monkeypatch, connection, parsed_book(500), uploaded, deleted)
    response = client().post(
        "/api/books",
        data={"file": (BytesIO(b"epub"), "poems.epub")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    batches = {"assets" if "book_assets" in query else "chapters": len(rows) for query, rows in connection.batch_queries}
    assert batches == {"assets": 1, "chapters": 500}
    assert len(uploaded) == 2
    assert deleted == []


def test_database_failure_rolls_back_storage_and_returns_stable_error(monkeypatch):
    connection = ImportConnection(fail_book_insert=True)
    uploaded = []
    deleted = []
    install_pipeline(monkeypatch, connection, parsed_book(), uploaded, deleted)
    response = client().post(
        "/api/books",
        data={"file": (BytesIO(b"epub"), "book.epub")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 500
    assert response.get_json()["error"] == "database_write_failed"
    assert sorted(deleted) == sorted(uploaded)


def test_partial_storage_failure_cleans_only_confirmed_uploads(monkeypatch):
    connection = ImportConnection()
    deleted = []

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(reading.db, "transaction", transaction)
    monkeypatch.setattr(reading, "parse_uploaded_book", lambda *_args: parsed_book())
    monkeypatch.setattr(
        reading.object_storage,
        "upload_many",
        lambda _objects: (_ for _ in ()).throw(
            reading.object_storage.ObjectStorageBatchError(
                "forced storage failure", ["books/partial/source/source.epub"]
            )
        ),
    )
    monkeypatch.setattr(
        reading.object_storage, "delete_objects", lambda paths: deleted.extend(paths)
    )
    response = client().post(
        "/api/books",
        data={"file": (BytesIO(b"epub"), "book.epub")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 502
    assert response.get_json()["error"] == "storage_upload_failed"
    assert deleted == ["books/partial/source/source.epub"]
    assert not any("insert into books" in query for query, _params in connection.execute_queries)


def test_timeout_like_parse_interruption_returns_408_without_persistence(monkeypatch):
    called = {"storage": False, "database": False}
    monkeypatch.setattr(
        reading,
        "parse_uploaded_book",
        lambda *_args: (_ for _ in ()).throw(
            BookParseError("超时", "epub_import_timeout")
        ),
    )
    monkeypatch.setattr(
        reading.object_storage,
        "upload_many",
        lambda _objects: called.__setitem__("storage", True),
    )

    @contextmanager
    def transaction():
        called["database"] = True
        yield ImportConnection()

    monkeypatch.setattr(reading.db, "transaction", transaction)
    response = client().post(
        "/api/books",
        data={"file": (BytesIO(b"epub"), "book.epub")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 408
    assert response.get_json()["error"] == "epub_import_timeout"
    assert called == {"storage": False, "database": False}


def test_deadline_after_storage_upload_cleans_objects_before_database(monkeypatch):
    connection = ImportConnection()
    uploaded = []
    deleted = []
    install_pipeline(monkeypatch, connection, parsed_book(), uploaded, deleted)
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 106.0, 107.0])
    monkeypatch.setattr(reading.time, "perf_counter", lambda: next(clock))
    response = client().post(
        "/api/books",
        data={"file": (BytesIO(b"epub"), "book.epub")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 408
    assert response.get_json()["error"] == "epub_import_timeout"
    assert sorted(deleted) == sorted(uploaded)
    assert not any(
        "insert into books" in query
        for query, _params in connection.execute_queries
    )


def test_internal_links_materialize_without_changing_block_ids():
    main_id = UUID("22222222-2222-4222-8222-222222222222")
    notes_id = UUID("33333333-3333-4333-8333-333333333333")
    main = '''<p id="b000001" data-block-id="b000001" data-epub-fragment="back">
      正文<a href="#" data-epub-target-href="Text/notes.xhtml" data-epub-target-fragment="note 1" data-epub-link-kind="footnote">[1]</a>
      <a href="#" data-epub-target-href="Text/missing.xhtml" data-epub-target-fragment="bad" data-epub-link-kind="footnote">[2]</a>
      <a href="#" data-epub-target-href="Text/main.xhtml" data-epub-target-fragment="back" data-epub-link-kind="chapter">本页</a>
      <a href="#" data-epub-target-href="Text/notes.xhtml" data-epub-target-fragment="" data-epub-link-kind="chapter">注释章</a></p>'''
    notes = '''<aside data-epub-note="true"><p id="b000001" data-block-id="b000001" data-epub-fragment="note 1">注释
      <a href="#" data-epub-target-href="Text/main.xhtml" data-epub-target-fragment="back" data-epub-link-kind="backlink">↩</a></p></aside>'''
    rendered = reading._materialize_reader_html(
        main,
        BOOK_ID,
        [
            {"id": main_id, "href": "Text/main.xhtml", "content_html": main},
            {"id": notes_id, "href": "Text/notes.xhtml", "content_html": notes},
        ],
    )
    soup = BeautifulSoup(rendered, "html.parser")
    links = soup.select("a[data-rh-internal]")
    assert links[0]["data-rh-chapter-id"] == str(notes_id)
    assert links[0]["data-rh-block-id"] == "b000001"
    assert links[0]["data-rh-target-fragment"] == "note 1"
    assert links[1]["data-rh-error-code"] == "footnote_target_missing"
    assert links[2]["data-rh-chapter-id"] == str(main_id)
    assert links[2]["data-rh-block-id"] == "b000001"
    assert links[3]["data-rh-link-kind"] == "chapter"
    assert soup.select_one('[data-block-id="b000001"]')["id"] == "b000001"

    backlink = BeautifulSoup(
        reading._materialize_reader_html(
            notes,
            BOOK_ID,
            [
                {"id": main_id, "href": "Text/main.xhtml", "content_html": main},
                {"id": notes_id, "href": "Text/notes.xhtml", "content_html": notes},
            ],
        ),
        "html.parser",
    ).select_one('a[data-rh-link-kind="backlink"]')
    assert backlink["data-rh-chapter-id"] == str(main_id)


def test_reader_frontend_intercepts_internal_navigation_and_has_footnote_paper():
    root = Path(__file__).resolve().parents[1]
    source = (root / "static/js/reader.js").read_text(encoding="utf-8")
    template = (root / "templates/reader.html").read_text(encoding="utf-8")
    assert 'event.target.closest("a[data-rh-internal]")' in source
    assert "footnote_target_missing" in source
    assert "openFootnote(chapterId, blockId, fragment)" in source
    assert 'id="footnote-dialog"' in template
    assert "location.href" not in source.split("async function followInternalLink", 1)[1].split("async function openFootnote", 1)[0]
