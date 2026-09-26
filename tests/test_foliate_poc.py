import hashlib
import json
from pathlib import Path

import reader_engine_poc
import storage
from app import create_app


ROOT = Path(__file__).resolve().parents[1]
BOOK_ID = "11111111-1111-4111-8111-111111111111"


def make_app(*, enabled=False, ttl=180):
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
            "READER_ENGINE_POC_ENABLED": enabled,
            "READER_ENGINE_POC_SIGNED_URL_TTL": ttl,
        }
    )


def login(client):
    response = client.post(
        "/login", data={"password": "private-test-password"}
    )
    assert response.status_code == 302


def test_poc_feature_flag_is_default_off_and_hides_all_routes():
    app = make_app(enabled=False)
    client = app.test_client()
    assert client.get("/reader-engine-poc").status_code == 404
    assert client.get(f"/reader-engine-poc/{BOOK_ID}").status_code == 404
    assert client.get(
        f"/api/reader-engine-poc/books/{BOOK_ID}/source"
    ).status_code == 404


def test_poc_requires_browser_session_when_enabled():
    client = make_app(enabled=True).test_client()
    page = client.get("/reader-engine-poc")
    source = client.get(f"/api/reader-engine-poc/books/{BOOK_ID}/source")
    assert page.status_code == 302
    assert "/login" in page.headers["Location"]
    assert source.status_code == 401
    assert source.get_json() == {"error": "web_session_required"}


def test_poc_page_has_isolated_shell_and_strict_security_headers():
    client = make_app(enabled=True).test_client()
    login(client)
    response = client.get("/reader-engine-poc")
    html = response.get_data(as_text=True)
    csp = response.headers["Content-Security-Policy"]

    assert response.status_code == 200
    assert "Choose EPUB File" in html
    assert "foliate-poc.js" in html
    assert "reader.js" not in html
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "worker-src 'none'" in csp
    assert "https://test.supabase.co" in csp
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_signed_source_is_authorized_short_lived_and_not_cached(monkeypatch):
    monkeypatch.setattr(
        reader_engine_poc.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "title": "POC Book",
            "format": "epub",
            "source_filename": "book.epub",
            "source_object_path": f"books/{BOOK_ID}/source/source.epub",
        },
    )
    calls = []
    monkeypatch.setattr(
        reader_engine_poc.object_storage,
        "create_signed_download_url",
        lambda path, ttl: calls.append((path, ttl))
        or "https://test.supabase.co/storage/v1/object/sign/private/source.epub?token=short",
    )
    client = make_app(enabled=True, ttl=180).test_client()
    login(client)
    response = client.get(
        f"/api/reader-engine-poc/books/{BOOK_ID}/source"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["book"] == {
        "id": BOOK_ID,
        "title": "POC Book",
        "filename": "book.epub",
    }
    assert payload["expires_in"] == 180
    assert payload["signed_url"].startswith("https://test.supabase.co/")
    assert "service-role" not in json.dumps(payload)
    assert calls == [(f"books/{BOOK_ID}/source/source.epub", 180)]
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_signed_source_rejects_invalid_ttl_without_storage_call(monkeypatch):
    monkeypatch.setattr(
        reader_engine_poc.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "title": "POC Book",
            "format": "epub",
            "source_filename": "book.epub",
            "source_object_path": "private.epub",
        },
    )
    monkeypatch.setattr(
        reader_engine_poc.object_storage,
        "create_signed_download_url",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    client = make_app(enabled=True, ttl=301).test_client()
    login(client)
    response = client.get(
        f"/api/reader-engine-poc/books/{BOOK_ID}/source"
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "poc_configuration_invalid"}


def test_signed_source_rejects_non_epub(monkeypatch):
    monkeypatch.setattr(
        reader_engine_poc.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "title": "Text",
            "format": "txt",
            "source_filename": "book.txt",
            "source_object_path": "private.txt",
        },
    )
    client = make_app(enabled=True).test_client()
    login(client)
    response = client.get(
        f"/api/reader-engine-poc/books/{BOOK_ID}/source"
    )
    assert response.status_code == 422
    assert response.get_json() == {"error": "poc_source_not_epub"}


def test_storage_signed_url_wrapper_accepts_provider_response(monkeypatch):
    class FakeBucket:
        def create_signed_url(self, path, expires_in):
            assert path == "books/id/source/source.epub"
            assert expires_in == 120
            return {"signedURL": "https://test.supabase.co/signed/source.epub"}

    monkeypatch.setattr(storage, "_bucket", lambda: FakeBucket())
    assert storage.create_signed_download_url(
        "books/id/source/source.epub", 120
    ) == "https://test.supabase.co/signed/source.epub"


def test_storage_signed_url_wrapper_enforces_ttl_before_provider(monkeypatch):
    monkeypatch.setattr(
        storage, "_bucket", lambda: (_ for _ in ()).throw(AssertionError())
    )
    for ttl in (59, 301):
        try:
            storage.create_signed_download_url("source.epub", ttl)
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-range TTL must fail")


def test_poc_does_not_replace_production_reader_or_action_schema():
    client = make_app(enabled=True).test_client()
    login(client)
    response = client.get(f"/reader/{BOOK_ID}")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "reader.js" in html
    assert "foliate-poc.js" not in html
    assert hashlib.sha256((ROOT / "openapi.yaml").read_bytes()).hexdigest() == (
        "c6ecc7023b08899458d0517ca5ee9ec6ef32085f52837a59a92e9801cbb113ca"
    )
    assert "reader-engine-poc" not in (ROOT / "openapi.yaml").read_text()


def test_poc_frontend_is_local_only_and_never_writes_production_state():
    script = (ROOT / "static/js/foliate-poc.js").read_text()
    adapter = (ROOT / "static/js/foliate-poc-adapter.js").read_text()
    template = (ROOT / "templates/reader_engine_poc.html").read_text()
    combined = script + adapter + template

    assert "poc-file-input" in combined
    assert "createSecurePublication(file)" in script
    assert "view.getCFI(index, range)" in script
    assert "view.goTo(saved)" in script
    assert "drawMemoryHighlight" in script
    assert "event.preventDefault()" in adapter
    assert "script, iframe, frame" in adapter
    assert "__FOLIATE_POC__" in script
    for path in (
        "/api/annotations",
        "/api/xiaxia/thoughts",
        "/api/progress",
        "/api/books/${bookId}/completion",
    ):
        assert path not in combined


def test_foliate_vendor_is_fixed_and_unpatched():
    vendor = ROOT / "static/vendor/foliate-js"
    version = json.loads((vendor / "VERSION.json").read_text())
    assert version["commit"] == "78914aef4466eb960965702401634c2cb348e9b1"
    assert version["commit_date"] == "2026-05-01T19:24:25Z"
    assert version["license"] == "MIT"
    assert version["upstream_patches"] == []
    assert (vendor / "LICENSE").is_file()
    assert (vendor / "ZIPJS_LICENSE").is_file()

    expected = {
        "epub.js": "dfe347495039e9b5991478986ec6b7713cf9efbcd8f475a77ef7b967c5176298",
        "epubcfi.js": "e1a688ad5c84bf31f33cef467f92c7a6d060a0f17ec37c1d3c048e9c7b1cf645",
        "fixed-layout.js": "7aee1d15d8e0479b8a6c59a20304b250dafc4c7dc158147a2b2c6bed9e180b4c",
        "overlayer.js": "d3d3396e3083277ccbd4f17c407621159b22a407107e43ce75ca3014578094fb",
        "paginator.js": "51cec81572d5d182e231fe68ce9c3b5489a0976b36172060516336ba893565ea",
        "progress.js": "544f5e90babc0cb31c5caf971a5f06dd6249ec06600cb62f672fa7179811fde1",
        "text-walker.js": "4ad740ab6142b5be428700ef4d270253cef6649cf6e047ae7d2698a7aec65e92",
        "view.js": "e0093af921feb03bb58fb51aeedc321fa108a4158d215f6913ecf7925415c945",
        "vendor/zip.js": "2a0d43157e4a447c6e43351225758795b75a33e8dfec10c6767248df2182949a",
    }
    for relative, digest in expected.items():
        assert hashlib.sha256((vendor / relative).read_bytes()).hexdigest() == digest
