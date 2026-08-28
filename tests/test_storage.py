from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
import zipfile

import pytest

import reading
import storage
from app import create_app
from epub_parser import ParsedAsset, ParsedBook, ParsedChapter


class FakeBucket:
    def __init__(self):
        self.uploads = []
        self.removals = []

    def upload(self, *, path, file, file_options):
        self.uploads.append((path, file, file_options))
        return {"path": path}

    def download(self, path):
        return b"private-image-bytes:" + path.encode()

    def remove(self, paths):
        self.removals.append(paths)
        return paths


class FakeStorageClient:
    def __init__(self, public=False):
        self.bucket = FakeBucket()
        self.public = public
        self.storage = self

    def from_(self, _bucket_name):
        return self.bucket

    def get_bucket(self, bucket_name):
        return {"id": bucket_name, "public": self.public}


def configure_fake_storage(monkeypatch, *, public=False):
    fake = FakeStorageClient(public=public)
    monkeypatch.setattr(storage, "_client", fake)
    monkeypatch.setattr(storage, "_supabase_url", "https://test.supabase.co")
    monkeypatch.setattr(storage, "_service_role_key", "server-only-key")
    monkeypatch.setattr(storage, "_bucket_name", "xiaxia-reading-house-private")
    return fake


def test_storage_wrapper_uploads_downloads_and_removes_private_objects(monkeypatch):
    fake = configure_fake_storage(monkeypatch)
    storage.upload_bytes("books/id/cover/a.png", b"png", "image/png")
    result = storage.download_bytes("books/id/cover/a.png")
    storage.delete_objects(["books/id/cover/a.png"])

    path, body, options = fake.bucket.uploads[0]
    assert path == "books/id/cover/a.png"
    assert body == b"png"
    assert options["content-type"] == "image/png"
    assert options["upsert"] == "false"
    assert result == b"private-image-bytes:books/id/cover/a.png"
    assert fake.bucket.removals == [["books/id/cover/a.png"]]
    assert storage.ping() is True


def test_storage_batch_deduplicates_paths_and_reports_partial_success(monkeypatch):
    uploaded = []

    def fake_upload(path, data, media_type):
        if path.endswith("broken.png"):
            raise storage.ObjectStorageError("forced")
        uploaded.append((path, data, media_type))

    monkeypatch.setattr(storage, "upload_bytes", fake_upload)
    objects = [
        ("books/id/source/source.epub", b"epub", "application/epub+zip"),
        ("books/id/source/source.epub", b"duplicate", "application/epub+zip"),
        ("books/id/assets/good.png", b"good", "image/png"),
        ("books/id/assets/broken.png", b"bad", "image/png"),
    ]
    try:
        storage.upload_many(objects, max_workers=2)
    except storage.ObjectStorageBatchError as exc:
        assert sorted(exc.uploaded_paths) == [
            "books/id/assets/good.png",
            "books/id/source/source.epub",
        ]
    else:
        raise AssertionError("the failed object must surface a batch error")
    assert len(uploaded) == 2


def test_archive_upload_streams_selected_entries_and_deduplicates(tmp_path, monkeypatch):
    archive_path = tmp_path / "assets.epub"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("images/a.png", b"A" * 1000)
        archive.writestr("images/b.png", b"B" * 2000)
    uploaded = []

    def fake_upload(object_path, file_path, media_type):
        uploaded.append((object_path, Path(file_path).read_bytes(), media_type))

    monkeypatch.setattr(storage, "upload_file", fake_upload)
    result = storage.upload_archive_entries(
        archive_path,
        [
            ("books/id/assets/a.png", "images/a.png", "image/png"),
            ("books/id/assets/a.png", "images/a.png", "image/png"),
            ("books/id/assets/b.png", "images/b.png", "image/png"),
        ],
        max_workers=2,
    )

    assert result == ["books/id/assets/a.png", "books/id/assets/b.png"]
    assert sorted((path, len(data)) for path, data, _type in uploaded) == [
        ("books/id/assets/a.png", 1000),
        ("books/id/assets/b.png", 2000),
    ]


def test_archive_upload_stops_at_deadline_before_network(tmp_path, monkeypatch):
    archive_path = tmp_path / "assets.epub"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("images/a.png", b"A")
    network_called = False

    def fake_upload(*_args):
        nonlocal network_called
        network_called = True

    monkeypatch.setattr(storage, "upload_file", fake_upload)
    with pytest.raises(storage.ObjectStorageDeadlineError) as raised:
        storage.upload_archive_entries(
            archive_path,
            [("books/id/assets/a.png", "images/a.png", "image/png")],
            deadline=0,
        )

    assert raised.value.uploaded_paths == []
    assert network_called is False


def test_storage_health_rejects_public_bucket(monkeypatch):
    configure_fake_storage(monkeypatch, public=True)
    assert storage.ping() is False


def test_browser_asset_route_proxies_private_storage(monkeypatch):
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
    monkeypatch.setattr(
        reading.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "object_path": "books/id/assets/a.png",
            "media_type": "image/png",
            "byte_size": 7,
        },
    )
    monkeypatch.setattr(
        reading.object_storage, "download_bytes", lambda _path: b"PNGDATA"
    )

    response = app.test_client().get(
        "/api/books/11111111-1111-4111-8111-111111111111/assets/images/a.png",
        headers={"Authorization": "Bearer action-test-token"},
    )
    assert response.status_code == 200
    assert response.data == b"PNGDATA"
    assert response.mimetype == "image/png"
    assert "Location" not in response.headers
    assert b"service-role" not in response.data


class UploadCursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class UploadConnection:
    def __init__(self):
        self.asset_insert = None

    def execute(self, query, params=()):
        if "select id, title from books" in query:
            return UploadCursor(None)
        if "insert into books" in query:
            return UploadCursor(
                {
                    "id": params[0],
                    "title": params[1],
                    "author": params[2],
                    "format": params[3],
                    "source_filename": params[4],
                    "cover_asset_path": "images/cover.png",
                    "chapter_count": 1,
                    "toc": [],
                    "created_at": datetime(2026, 8, 23, tzinfo=UTC),
                    "updated_at": datetime(2026, 8, 23, tzinfo=UTC),
                }
            )
        if "insert into book_assets" in query:
            self.asset_insert = (query, params)
            return UploadCursor()
        if "insert into chapters" in query:
            return UploadCursor(
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "chapter_index": 0,
                }
            )
        return UploadCursor()


def test_upload_persists_binaries_in_storage_and_only_metadata_in_postgres(monkeypatch):
    parsed = ParsedBook(
        title="测试之书",
        author="测试作者",
        format="txt",
        source_filename="book.txt",
        source_sha256="a" * 64,
        chapters=[
            ParsedChapter(
                chapter_index=0,
                title="第一章",
                href="chapter.xhtml",
                content_html='<p data-block-id="b000001">正文</p>',
                content_text="正文",
                word_count=2,
            )
        ],
        assets=[
            ParsedAsset(
                path="images/cover.png",
                media_type="image/png",
                data=b"COVER",
                archive_path="images/cover.png",
            )
        ],
        cover_asset_path="images/cover.png",
    )
    connection = UploadConnection()
    uploads = []

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(
        reading, "parse_uploaded_book_path", lambda *_args, **_kwargs: parsed
    )
    monkeypatch.setattr(reading.db, "transaction", fake_transaction)
    monkeypatch.setattr(
        reading.object_storage,
        "upload_file",
        lambda path, file_path, media_type: uploads.append(
            (path, Path(file_path).read_bytes(), media_type)
        ),
    )
    monkeypatch.setattr(
        reading.object_storage,
        "upload_archive_entries",
        lambda _archive, objects, **_kwargs: uploads.extend(
            (path, b"COVER", media_type)
            for path, _entry, media_type in objects
        )
        or [path for path, _entry, _media_type in objects],
    )

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
    client = app.test_client()
    client.post("/login", data={"password": "private-test-password"})
    response = client.post(
        "/api/books",
        data={"file": (BytesIO(b"RAW-TXT"), "book.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    assert [item[1] for item in uploads] == [b"RAW-TXT"]
    query, params = connection.asset_insert
    assert "object_path" in query
    assert " data" not in query.lower()
    assert b"COVER" not in params
    assert params[3:] == ("image/png", 5)
