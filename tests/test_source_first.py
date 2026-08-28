from __future__ import annotations

import zipfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID

import psycopg
from werkzeug.datastructures import FileStorage

import database
import reading
import source_first
from app import create_app
from epub_parser import BookParseError, inspect_epub_publication_path


BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


def make_epub(*, title="测试书", author="作者", cover=False) -> bytes:
    cover_item = (
        '<item id="cover" href="Images/cover.png" media-type="image/png" properties="cover-image"/>'
        if cover else ""
    )
    opf = f'''<package xmlns="http://www.idpf.org/2007/opf"
      xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
      <metadata><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>
      <manifest>
        <item id="chapter" href="Text/chapter.xhtml" media-type="application/xhtml+xml"/>
        {cover_item}
      </manifest>
      <spine><itemref idref="chapter"/></spine>
    </package>'''
    container = '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="EPUB/content.opf"
      media-type="application/oebps-package+xml"/></rootfiles></container>'''
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/content.opf", opf)
        archive.writestr(
            "EPUB/Text/chapter.xhtml",
            '<html><body><h1>第一章</h1><p>可供夏夏读取的正文。</p></body></html>',
        )
        if cover:
            archive.writestr("EPUB/Images/cover.png", b"\x89PNG\r\n\x1a\ncover")
    return output.getvalue()


class PublicationConnection:
    def __init__(self, fail=False):
        self.fail = fail
        self.book = None
        self.assets = []

    def execute(self, query, params=()):
        normalized = " ".join(query.lower().split())
        if "insert into books" in normalized:
            if self.fail:
                raise RuntimeError("forced book insert failure")
            self.book = {
                "id": params[0],
                "title": params[1],
                "author": params[2],
                "format": "epub",
                "source_filename": params[3],
                "cover_asset_path": params[7],
                "chapter_count": 0,
                "toc": [],
                "publication_ready": True,
                "reader_engine": "foliate",
                "text_index_status": "pending",
                "text_index_failure_code": None,
            }
            return Cursor(self.book)
        if "insert into book_assets" in normalized:
            self.assets.append(params)
        return Cursor(None)


def install_publication_store(monkeypatch, connection, uploaded, deleted):
    monkeypatch.setattr(source_first.db, "fetch_one", lambda *_args, **_kwargs: None)

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(source_first.db, "transaction", transaction)
    monkeypatch.setattr(
        source_first.object_storage,
        "upload_file",
        lambda path, _file, _type: uploaded.append(path),
    )
    monkeypatch.setattr(
        source_first.object_storage,
        "delete_objects",
        lambda paths: deleted.extend(paths),
    )


def test_minimal_publication_validation_reads_package_without_normalizing(tmp_path):
    source = tmp_path / "book.epub"
    source.write_bytes(make_epub())
    result = inspect_epub_publication_path("book.epub", source)
    assert result.title == "测试书"
    assert result.author == "作者"
    assert result.spine_item_count == 1
    assert result.readable_section_count == 1
    assert not hasattr(result, "chapters")


def test_minimal_metadata_falls_back_without_rejecting_publication(tmp_path):
    source = tmp_path / "没有元数据.epub"
    source.write_bytes(make_epub(title="", author=""))
    result = inspect_epub_publication_path(source.name, source)
    assert result.title == "没有元数据"
    assert result.author == "未知作者"
    assert result.readable_section_count == 1


def test_source_first_import_persists_only_source_and_pending_book(monkeypatch):
    connection = PublicationConnection()
    uploaded, deleted = [], []
    install_publication_store(monkeypatch, connection, uploaded, deleted)
    upload = FileStorage(stream=BytesIO(make_epub()), filename="book.epub")
    book = source_first.create_epub_publication(upload, "book.epub")
    assert book["publication_ready"] is True
    assert book["reader_engine"] == "foliate"
    assert book["text_index_status"] == "pending"
    assert book["chapter_count"] == 0
    assert len(uploaded) == 1
    assert uploaded[0].endswith("/source/source.epub")
    assert deleted == []


def test_epub_upload_uses_source_first_only_when_production_flag_is_on(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reading,
        "create_epub_publication",
        lambda _upload, filename: calls.append(filename)
        or {
            "id": BOOK_ID,
            "title": "测试书",
            "author": "作者",
            "format": "epub",
            "source_filename": filename,
            "cover_asset_path": None,
            "chapter_count": 0,
            "toc": [],
            "publication_ready": True,
            "reader_engine": "foliate",
            "text_index_status": "pending",
            "text_index_failure_code": None,
        },
    )
    client = make_client(READER_ENGINE_ENABLED=True)
    response = client.post(
        "/api/books",
        data={"file": (BytesIO(b"epub source"), "book.epub")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    assert response.get_json()["book"]["publication_ready"] is True
    assert calls == ["book.epub"]


def test_duplicate_source_returns_existing_book_without_upload(monkeypatch):
    monkeypatch.setattr(
        source_first.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "title": "已在书架",
            "text_index_status": "failed",
        },
    )
    monkeypatch.setattr(
        source_first.object_storage,
        "upload_file",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not upload")),
    )
    upload = FileStorage(stream=BytesIO(make_epub()), filename="book.epub")
    try:
        source_first.create_epub_publication(upload, "book.epub")
        raise AssertionError("duplicate source accepted")
    except source_first.SourceFirstFailure as exc:
        assert exc.code == "book_already_exists"
        assert exc.data["book_id"] == str(BOOK_ID)
        assert exc.data["retryable"] is True


def test_malformed_epub_is_rejected_before_storage_or_database(monkeypatch):
    called = {"storage": False, "database": False}
    monkeypatch.setattr(
        source_first.object_storage,
        "upload_file",
        lambda *_args: called.__setitem__("storage", True),
    )

    @contextmanager
    def transaction():
        called["database"] = True
        yield PublicationConnection()

    monkeypatch.setattr(source_first.db, "transaction", transaction)
    upload = FileStorage(stream=BytesIO(b"not a zip"), filename="bad.epub")
    try:
        source_first.create_epub_publication(upload, "bad.epub")
        raise AssertionError("invalid EPUB accepted")
    except source_first.SourceFirstFailure as exc:
        assert exc.code == "unsupported_archive"
    assert called == {"storage": False, "database": False}


def test_source_storage_failure_does_not_create_book(monkeypatch):
    connection = PublicationConnection()
    deleted = []
    monkeypatch.setattr(source_first.db, "fetch_one", lambda *_args: None)
    monkeypatch.setattr(
        source_first.object_storage,
        "upload_file",
        lambda *_args: (_ for _ in ()).throw(
            source_first.object_storage.ObjectStorageError("forced")
        ),
    )
    monkeypatch.setattr(
        source_first.object_storage,
        "delete_objects",
        lambda paths: deleted.extend(paths),
    )

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(source_first.db, "transaction", transaction)
    upload = FileStorage(stream=BytesIO(make_epub()), filename="book.epub")
    try:
        source_first.create_epub_publication(upload, "book.epub")
        raise AssertionError("storage failure accepted")
    except source_first.SourceFirstFailure as exc:
        assert exc.code == "storage_upload_failed"
    assert connection.book is None
    assert len(deleted) == 1


def test_database_create_failure_cleans_source(monkeypatch):
    connection = PublicationConnection(fail=True)
    uploaded, deleted = [], []
    install_publication_store(monkeypatch, connection, uploaded, deleted)
    upload = FileStorage(stream=BytesIO(make_epub()), filename="book.epub")
    try:
        source_first.create_epub_publication(upload, "book.epub")
        raise AssertionError("database failure accepted")
    except source_first.SourceFirstFailure as exc:
        assert exc.code == "database_write_failed"
    assert deleted == uploaded


def test_cover_upload_failure_degrades_to_placeholder(monkeypatch):
    connection = PublicationConnection()
    deleted = []
    monkeypatch.setattr(source_first.db, "fetch_one", lambda *_args: None)

    def upload(path, _file, _type):
        if "/cover/" in path:
            raise source_first.object_storage.ObjectStorageError("bad cover")

    monkeypatch.setattr(source_first.object_storage, "upload_file", upload)
    monkeypatch.setattr(
        source_first.object_storage, "delete_objects", lambda paths: deleted.extend(paths)
    )

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(source_first.db, "transaction", transaction)
    upload_file = FileStorage(stream=BytesIO(make_epub(cover=True)), filename="book.epub")
    book = source_first.create_epub_publication(upload_file, "book.epub")
    assert book["publication_ready"] is True
    assert book["cover_asset_path"] is None
    assert any("/cover/" in path for path in deleted)


class TextIndexState:
    def __init__(self, payload, status="pending", anchored=0):
        self.payload = payload
        self.book = {
            "id": BOOK_ID,
            "title": "测试书",
            "format": "epub",
            "source_filename": "book.epub",
            "source_sha256": "a" * 64,
            "source_object_path": "books/source.epub",
            "publication_ready": True,
            "reader_engine": "foliate",
            "text_index_status": status,
            "text_index_failure_code": None,
            "chapter_count": 0,
        }
        self.chapters = []
        self.anchored = anchored
        self.source_deleted = False

    def fetch_one(self, query, _params=()):
        if "from books" in " ".join(query.lower().split()):
            return dict(self.book)
        return None

    def execute(self, query, params=()):
        normalized = " ".join(query.lower().split())
        if "set text_index_status = 'processing'" in normalized:
            if self.book["text_index_status"] == "processing":
                return None
            self.book["text_index_status"] = "processing"
            return {"id": BOOK_ID}
        if "set text_index_status = 'failed'" in normalized:
            self.book["text_index_status"] = "failed"
            self.book["text_index_failure_code"] = params[0]
            return dict(self.book)
        return None

    @contextmanager
    def transaction(self):
        yield TextIndexConnection(self)


class TextIndexConnection:
    def __init__(self, state):
        self.state = state

    def execute(self, query, params=()):
        normalized = " ".join(query.lower().split())
        if "from books" in normalized and "for update" in normalized:
            return Cursor(dict(self.state.book))
        if "anchored_count" in normalized:
            return Cursor({"anchored_count": self.state.anchored})
        if "select count(*) as total from chapters" in normalized:
            return Cursor({"total": len(self.state.chapters)})
        if normalized.startswith("delete from chapters"):
            self.state.chapters.clear()
        if "set chapter_count" in normalized:
            self.state.book["chapter_count"] = params[0]
            self.state.book["text_index_status"] = "ready"
            self.state.book["text_index_failure_code"] = None
            return Cursor(dict(self.state.book))
        return Cursor(None)

    def cursor(self):
        return TextIndexCursor(self.state)


class TextIndexCursor:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def executemany(self, _query, rows):
        self.state.chapters.extend(list(rows))


def install_text_index_state(monkeypatch, state):
    monkeypatch.setattr(source_first.db, "fetch_one", state.fetch_one)
    monkeypatch.setattr(source_first.db, "execute", state.execute)
    monkeypatch.setattr(source_first.db, "transaction", state.transaction)

    def download(_path, target):
        Path(target).write_bytes(state.payload)
        return len(state.payload)

    monkeypatch.setattr(source_first.object_storage, "download_to_file", download)


def test_text_index_success_is_independent_and_atomic(monkeypatch):
    state = TextIndexState(make_epub())
    install_text_index_state(monkeypatch, state)
    assert not hasattr(TextIndexConnection, "executemany")
    result = source_first.build_text_index(BOOK_ID)
    assert result["text_index_status"] == "ready"
    assert result["publication_ready"] is True
    assert len(state.chapters) == 1
    assert state.book["chapter_count"] == 1


def test_psycopg3_batch_contract_uses_cursor_not_connection():
    assert not hasattr(psycopg.Connection, "executemany")
    assert hasattr(psycopg.Cursor, "executemany")

    class ContractCursor:
        def __init__(self):
            self.rows = None

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def executemany(self, query, rows):
            self.query = query
            self.rows = list(rows)

    class ContractConnection:
        def __init__(self):
            self.batch_cursor = ContractCursor()

        def cursor(self):
            return self.batch_cursor

    connection = ContractConnection()
    database.execute_many(
        connection,
        "insert into chapters (id) values (%s)",
        [("chapter-1",), ("chapter-2",)],
    )
    assert connection.batch_cursor.rows == [("chapter-1",), ("chapter-2",)]
    assert "insert into chapters" in connection.batch_cursor.query


def test_text_index_failure_keeps_publication_and_source(monkeypatch):
    state = TextIndexState(b"broken archive")
    install_text_index_state(monkeypatch, state)
    result = source_first.build_text_index(BOOK_ID)
    assert result["text_index_status"] == "failed"
    assert result["text_index_failure_code"] == "text_index_parse_failed"
    assert result["publication_ready"] is True
    assert state.source_deleted is False


def test_text_index_database_failure_rolls_back_index_only(monkeypatch):
    state = TextIndexState(make_epub())
    install_text_index_state(monkeypatch, state)

    class FailedConnection(TextIndexConnection):
        def cursor(self):
            return FailedCursor(self.state)

    class FailedCursor(TextIndexCursor):
        def executemany(self, _query, _rows):
            raise RuntimeError("forced index insert failure")

    @contextmanager
    def failed_transaction():
        yield FailedConnection(state)

    monkeypatch.setattr(source_first.db, "transaction", failed_transaction)
    result = source_first.build_text_index(BOOK_ID)
    assert result["text_index_status"] == "failed"
    assert result["text_index_failure_code"] == "text_index_internal_error"
    assert result["publication_ready"] is True
    assert state.chapters == []
    assert state.source_deleted is False


def test_text_index_timeout_has_stable_taxonomy(monkeypatch):
    state = TextIndexState(make_epub())
    install_text_index_state(monkeypatch, state)
    monkeypatch.setattr(
        source_first,
        "parse_uploaded_book_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BookParseError("timeout", "epub_import_timeout")
        ),
    )
    result = source_first.build_text_index(BOOK_ID)
    assert result["text_index_failure_code"] == "text_index_timeout"
    assert state.book["publication_ready"] is True


def test_text_index_retry_is_idempotent_without_duplicate_chapters(monkeypatch):
    state = TextIndexState(make_epub(), status="failed")
    install_text_index_state(monkeypatch, state)
    first = source_first.build_text_index(BOOK_ID)
    second = source_first.build_text_index(BOOK_ID)
    assert first["text_index_status"] == "ready"
    assert second["text_index_status"] == "ready"
    assert len(state.chapters) == 1


def test_retry_refuses_to_delete_existing_anchor_data(monkeypatch):
    state = TextIndexState(make_epub(), status="failed", anchored=1)
    state.chapters.append(("existing",))
    install_text_index_state(monkeypatch, state)
    result = source_first.build_text_index(BOOK_ID)
    assert result["text_index_failure_code"] == "text_index_mapping_failed"
    assert state.chapters == [("existing",)]


def make_client(**config):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "postgresql://test.invalid/test",
            "PRIVATE_ACCESS_PASSWORD": "private-test-password",
            "ACTION_API_TOKEN": "action-test-token",
            "SECRET_KEY": "test-session-secret",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
            "SUPABASE_STORAGE_BUCKET": "private-books",
            "SESSION_COOKIE_SECURE": False,
            **config,
        }
    )
    client = app.test_client()
    client.post("/login", data={"password": "private-test-password"})
    return client


def test_source_access_is_authorized_short_lived_and_secret_free(monkeypatch):
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda *_args: {
            "id": BOOK_ID,
            "title": "测试书",
            "format": "epub",
            "source_filename": "book.epub",
            "source_object_path": "books/source.epub",
            "publication_ready": True,
        },
    )
    monkeypatch.setattr(
        reading.object_storage,
        "create_signed_download_url",
        lambda *_args: "https://test.supabase.co/signed/book.epub?token=short",
    )
    response = make_client(READER_SOURCE_SIGNED_URL_TTL=180).get(
        f"/api/books/{BOOK_ID}/source-access"
    )
    assert response.status_code == 200
    assert response.get_json()["expires_in"] == 180
    assert "service-role-secret" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"].startswith("no-store")


def test_ai_chapter_api_reports_pending_text_index_instead_of_not_found(monkeypatch):
    monkeypatch.setattr(reading.db, "fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "publication_ready": True,
            "text_index_status": "pending",
            "text_index_failure_code": None,
        },
    )
    response = make_client().get(f"/api/books/{BOOK_ID}/chapters")
    assert response.status_code == 409
    assert response.get_json() == {
        "error": "text_index_not_ready",
        "publication_ready": True,
        "retryable": True,
        "status": "pending",
    }


def test_ai_context_reports_failed_text_index_with_retryability(monkeypatch):
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "publication_ready": True,
            "text_index_status": "failed",
            "text_index_failure_code": "text_index_parse_failed",
        },
    )
    response = make_client().get(f"/api/reading/context?book_id={BOOK_ID}")
    assert response.status_code == 409
    assert response.get_json() == {
        "error": "text_index_failed",
        "failure_code": "text_index_parse_failed",
        "publication_ready": True,
        "retryable": True,
        "status": "failed",
    }


def test_phase2_migration_and_reader_gate_are_additive():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/006_reader_engine_source_first.sql").read_text()
    loader = (root / "static/js/reader-engine-loader.js").read_text()
    foliate = (root / "static/js/foliate-reader.js").read_text()
    assert "add column if not exists publication_ready" in migration
    assert "reader_engine = 'legacy'" in migration
    assert "b.source_object_path is not null" in migration
    assert "publication_reading_progress" in migration
    assert "delete from books" not in migration.lower()
    assert "query.get('engine') !== 'legacy'" in loader
    assert "/annotations" not in foliate
    assert "/thoughts" not in foliate
    assert "publication-progress" in foliate


def test_production_reader_flag_keeps_same_shell_and_legacy_escape_hatch():
    disabled = make_client(READER_ENGINE_ENABLED=False).get(f"/reader/{BOOK_ID}")
    enabled = make_client(READER_ENGINE_ENABLED=True).get(f"/reader/{BOOK_ID}")
    assert 'data-reader-engine-enabled="false"' in disabled.get_data(as_text=True)
    assert 'data-reader-engine-enabled="true"' in enabled.get_data(as_text=True)
    for response in (disabled, enabled):
        html = response.get_data(as_text=True)
        assert "reader-engine-loader.js" in html
        assert "foliate-poc.js" not in html
        assert "chapter-content" in html
    loader = (Path(__file__).resolve().parents[1] / "static/js/reader-engine-loader.js").read_text()
    reader = (Path(__file__).resolve().parents[1] / "static/js/foliate-reader.js").read_text()
    assert "query.get('engine') !== 'legacy'" in loader
    assert "fallback.searchParams.set('engine', 'legacy')" in reader
