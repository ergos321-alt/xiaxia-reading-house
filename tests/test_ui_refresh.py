from pathlib import Path
from uuid import UUID

from app import create_app


ROOT = Path(__file__).resolve().parents[1]
BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")


def make_web_client():
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
    assert client.post("/login", data={"password": "private-test-password"}).status_code == 302
    return client


def test_library_has_shared_reading_note_and_soft_upload_entry():
    template = (ROOT / "templates/library.html").read_text(encoding="utf-8")
    source = (ROOT / "static/js/library.js").read_text(encoding="utf-8")

    for element_id in (
        "shared-reading-note",
        "shared-reading-book",
        "shared-reading-chapter",
        "shared-reading-progress",
    ):
        assert f'id="{element_id}"' in template
    assert "最近一起读到这里" in template
    assert "把一本书带回家" in template
    assert "progress_chapter_id" in source
    assert "firstBookCharacter" in source
    assert 'api("/api/books")' in source


def test_two_ink_system_and_collapsed_xiaxia_traces_are_present():
    style = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
    reader = (ROOT / "static/js/reader.js").read_text(encoding="utf-8")
    thought_renderer = reader.split("function renderChapterThoughts()", 1)[1].split(
        "async function refreshAnnotationMarks", 1
    )[0]

    for token in ("--user-ink", "--user-wash", "--xiaxia-ink", "--xiaxia-wash"):
        assert token in style
    assert 'content: "🐾"' in style
    assert 'class="thought-paw"' in reader
    assert "林知夏在这一章停留过" in thought_renderer
    assert "thought.content" not in thought_renderer
    assert 'mark.dataset.thoughtId = recordId' in reader
    assert 'mark.setAttribute("aria-label", "打开林知夏留在这里的想法")' in reader


def test_mobile_selection_and_reader_anchors_remain_unchanged():
    style = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
    reader = (ROOT / "static/js/reader.js").read_text(encoding="utf-8")

    for field in ("start_block_id", "start_offset", "end_block_id", "end_offset"):
        assert field in reader
    for event_name in ('"selectionchange"', '"touchend"', '"pointerup"', '"contextmenu"'):
        assert event_name in reader
    assert "visualViewport" in reader
    assert "state.savedSelection" in reader
    assert "user-select: none" not in style
    assert "-webkit-user-select: none" not in style
    assert ".selection-menu button { min-height: 48px" in style


def test_ui_refresh_does_not_add_framework_or_change_action_files():
    templates_and_static = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in (ROOT / "templates", ROOT / "static")
        for path in folder.rglob("*")
        if path.is_file()
    )
    assert "react" not in templates_and_static.lower()
    assert "vue" not in templates_and_static.lower()
    assert "tailwind" not in templates_and_static.lower()
    assert "bootstrap" not in templates_and_static.lower()


def test_refreshed_pages_and_static_assets_render_for_private_web_session():
    client = make_web_client()
    pages = {
        "/library": b'shared-reading-note',
        f"/reader/{BOOK_ID}": b'reader-utility-tools',
        f"/reader/{BOOK_ID}/annotations": b'annotation-overview-list',
        "/annotations/manage": b'management-filters',
        "/static/css/style.css": b'--xiaxia-ink',
        "/static/js/library.js": b'renderSharedReadingNote',
        "/static/js/reader.js": "林知夏在这一章停留过".encode(),
    }
    for path, marker in pages.items():
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.data
