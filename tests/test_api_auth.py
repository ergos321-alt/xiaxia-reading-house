import reading
from app import create_app


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
    assert response.get_json() == {"books": []}


def test_browser_password_creates_private_session(monkeypatch):
    monkeypatch.setattr(reading.db, "fetch_all", lambda *_args, **_kwargs: [])
    client = make_app().test_client()
    login = client.post(
        "/login",
        data={"password": "private-test-password"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    response = client.get("/api/books")
    assert response.status_code == 200


def test_wrong_action_token_is_rejected():
    client = make_app().test_client()
    response = client.get(
        "/api/books", headers={"Authorization": "Bearer not-the-token"}
    )
    assert response.status_code == 401
