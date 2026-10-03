import hashlib
from pathlib import Path
import sys
import time
import types
from itsdangerous import URLSafeTimedSerializer
import pytest

if sys.platform == "win32" and "resource" not in sys.modules:
    # Reading House's optional process-memory metric uses this POSIX module.
    resource_stub = types.ModuleType("resource")
    resource_stub.RUSAGE_SELF = 0
    resource_stub.getrusage = lambda _who: types.SimpleNamespace(ru_maxrss=0)
    sys.modules["resource"] = resource_stub

import reading
from app import create_app

import yaml


def make_app():
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "postgresql://test.invalid/test",
            "PRIVATE_ACCESS_PASSWORD": "private-test-password",
            "ACTION_API_TOKEN": "action-test-token",
            "READING_ASSERTION_SECRET": "test-reading-bridge-secret-material-32-bytes-min",
            "SECRET_KEY": "test-session-secret",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
            "SUPABASE_STORAGE_BUCKET": "xiaxia-reading-house-private",
            "SESSION_COOKIE_SECURE": False,
        }
    )


def test_private_api_rejects_anonymous_request():
    client = make_app().test_client()
    response = client.get("/api/books")
    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_action_bearer_can_read_books(monkeypatch):
    monkeypatch.setattr(reading.db, "fetch_all", lambda *_args, **_kwargs: [])
    client = make_app().test_client()
    response = client.get(
        "/api/books", headers={"Authorization": "Bearer action-test-token"}
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "books": [],
        "pagination": {"has_more": False, "limit": 10, "offset": 0, "returned": 0},
    }


def test_action_book_search_is_lightweight_and_bounded(monkeypatch):
    captured = {}

    def fake_fetch_all(query, params=()):
        captured.update(query=query, params=params)
        return [{"id": "book-shijing", "title": "诗经", "author": "佚名", "format": "epub"}]

    monkeypatch.setattr(reading.db, "fetch_all", fake_fetch_all)
    response = make_app().test_client().get(
        "/api/books?query=诗经&limit=10",
        headers={"Authorization": "Bearer action-test-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["books"] == [
        {"id": "book-shijing", "title": "诗经", "author": "佚名", "format": "epub"}
    ]
    assert "b.title ilike %s" in captured["query"]
    assert "coalesce(b.author, '') ilike %s" in captured["query"]
    assert captured["params"] == ("%诗经%", "%诗经%", 11, 0)


def test_action_book_browsing_uses_limit_and_offset(monkeypatch):
    calls = []

    def fake_fetch_all(_query, params=()):
        calls.append(params)
        offset = params[-1]
        return [
            {"id": f"book-{offset + index}", "title": f"书 {offset + index}", "author": "", "format": "epub"}
            for index in range(6)
        ]

    monkeypatch.setattr(reading.db, "fetch_all", fake_fetch_all)
    client = make_app().test_client()
    headers = {"Authorization": "Bearer action-test-token"}

    first = client.get("/api/books?limit=5&offset=0", headers=headers).get_json()
    second = client.get("/api/books?limit=5&offset=5", headers=headers).get_json()

    assert calls == [(6, 0), (6, 5)]
    assert [book["id"] for book in first["books"]] == [f"book-{index}" for index in range(5)]
    assert [book["id"] for book in second["books"]] == [f"book-{index}" for index in range(5, 10)]
    assert first["pagination"] == {"has_more": True, "limit": 5, "offset": 0, "returned": 5}
    assert second["pagination"] == {"has_more": True, "limit": 5, "offset": 5, "returned": 5}


def test_action_book_limit_rejects_unbounded_or_invalid_values(monkeypatch):
    monkeypatch.setattr(
        reading.db,
        "fetch_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("database should not be queried")),
    )
    client = make_app().test_client()
    headers = {"Authorization": "Bearer action-test-token"}

    for query in ("limit=51", "limit=0", "limit=invalid", "offset=-1"):
        response = client.get(f"/api/books?{query}", headers=headers)
        assert response.status_code == 400
        assert response.get_json() == {"error": "invalid_books_pagination"}


def test_browser_password_creates_private_session(monkeypatch):
    captured = {}

    def fake_fetch_all(query, params=()):
        captured.update(query=query, params=params)
        return [{
            "id": "book-web",
            "title": "网页书架",
            "author": "作者",
            "format": "epub",
            "source_filename": "web.epub",
            "cover_asset_path": None,
            "chapter_count": 1,
            "toc": [{"label": "第一章", "href": "Text/one.xhtml"}],
        }]

    monkeypatch.setattr(reading.db, "fetch_all", fake_fetch_all)
    client = make_app().test_client()
    login = client.post(
        "/login",
        data={"password": "private-test-password"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    response = client.get("/api/books")
    assert response.status_code == 200
    assert response.get_json()["books"][0]["toc"] == [
        {"label": "第一章", "href": "Text/one.xhtml"}
    ]
    assert "select b.id, b.title, b.author, b.format" not in captured["query"]
    assert captured["params"] == ()


def test_existing_web_login_library_and_reader_routes_remain_available():
    client = make_app().test_client()
    assert client.get("/login").status_code == 200
    assert client.post("/login", data={"password": "private-test-password"}).status_code == 302
    library = client.get("/library")
    reader = client.get("/reader/11111111-1111-4111-8111-111111111111")
    assert library.status_code == 200
    assert reader.status_code == 200
    assert "library.js" in library.get_data(as_text=True)
    assert "reader.js" in reader.get_data(as_text=True)


def test_openapi_routes_current_book_to_state_and_books_to_bounded_search():
    root = Path(__file__).resolve().parents[1]
    document = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    state = document["paths"]["/api/reading/state"]["get"]
    books = document["paths"]["/api/books"]["get"]
    parameters = {item["name"]: item for item in books["parameters"]}
    instructions = (root / "README.md").read_text(encoding="utf-8")

    assert state["operationId"] == "getReadingState"
    assert "current.book.id" in state["description"]
    assert "不要先枚举整个书架" in state["description"]
    assert books["operationId"] == "listBooks"
    assert "当前书应先调用 getReadingState" in books["description"]
    assert set(parameters) == {"query", "limit", "offset"}
    assert parameters["query"]["required"] is False
    assert parameters["limit"]["schema"] == {
        "type": "integer", "minimum": 1, "maximum": 50, "default": 10,
    }
    assert parameters["offset"]["schema"] == {
        "type": "integer", "minimum": 0, "default": 0,
    }
    assert "不得先调用 `listBooks`" in instructions


def test_wrong_action_token_is_rejected():
    client = make_app().test_client()
    response = client.get(
        "/api/books", headers={"Authorization": "Bearer not-the-token"}
    )
    assert response.status_code == 401


def _reading_assertion(**overrides):
    now = int(time.time())
    claims = {
        "aud": "xiaxia-reading-house",
        "role": "human",
        "sub": "human-owner",
        "iat": now,
        "exp": now + 60,
        "jti": "test-one-request-id",
        **overrides,
    }
    return URLSafeTimedSerializer(
        "test-reading-bridge-secret-material-32-bytes-min",
        salt="xiaxia-reading-human-v1",
        signer_kwargs={"digest_method": hashlib.sha256},
    ).dumps(claims)


def test_core_assertion_bootstraps_human_session_for_read_only_books(monkeypatch):
    monkeypatch.setattr(reading.db, "fetch_all", lambda *_args, **_kwargs: [])
    app = make_app()
    app.config["SESSION_COOKIE_SECURE"] = True
    client = app.test_client()
    response = client.post(
        "/api/app/bootstrap",
        json={"assertion": _reading_assertion()},
    )
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "Secure" in response.headers["Set-Cookie"]
    assert client.get("/api/books", base_url="https://localhost").status_code == 200
    assert client.get(
        "/api/xiaxia/thoughts?book_id=11111111-1111-4111-8111-111111111111",
        base_url="https://localhost",
    ).status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-service"},
        {"role": "xiaxia"},
        {"exp": int(time.time()) - 1, "iat": int(time.time()) - 62},
    ],
)
def test_bootstrap_rejects_wrong_audience_role_and_expired_assertions(claims):
    client = make_app().test_client()
    response = client.post(
        "/api/app/bootstrap",
        json={"assertion": _reading_assertion(**claims)},
    )
    assert response.status_code == 401


def test_bootstrap_rejects_malformed_or_bad_signature():
    client = make_app().test_client()
    assert client.post("/api/app/bootstrap", json={"assertion": "not-a-token"}).status_code == 401
    forged = URLSafeTimedSerializer(
        "different-secret-material-that-is-at-least-32-bytes",
        salt="xiaxia-reading-human-v1",
        signer_kwargs={"digest_method": hashlib.sha256},
    ).dumps({"aud": "xiaxia-reading-house", "role": "human"})
    assert client.post("/api/app/bootstrap", json={"assertion": forged}).status_code == 401


def test_bootstrap_unavailable_without_separate_bridge_secret():
    app = make_app()
    app.config["READING_ASSERTION_SECRET"] = ""
    response = app.test_client().post(
        "/api/app/bootstrap", json={"assertion": "not-used"}
    )
    assert response.status_code == 503
