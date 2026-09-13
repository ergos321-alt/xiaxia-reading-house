import reading
from app import create_app
from pathlib import Path

import yaml


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
