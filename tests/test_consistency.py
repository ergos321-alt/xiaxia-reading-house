from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_required_database_tables_are_declared():
    sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
    required = {
        "books",
        "book_assets",
        "chapters",
        "reading_progress",
        "annotations",
        "annotation_replies",
        "ai_reading_state",
    }
    for table in required:
        assert f"create table if not exists {table}" in sql


def test_binary_assets_are_private_storage_metadata_only():
    sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
    reading = (ROOT / "reading.py").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    browser_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "static").rglob("*")
        if path.is_file()
    )

    assert "data bytea" not in sql.lower()
    assert "object_path text not null unique" in sql
    assert "source_object_path text not null unique" in sql
    assert "xiaxia-reading-house-private" in sql
    assert "public = false" in sql
    assert "select object_path, media_type, byte_size" in reading
    assert "supabase==" in requirements
    assert "SUPABASE_SERVICE_ROLE_KEY" not in browser_sources
    for variable in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_STORAGE_BUCKET",
    ):
        assert f"{variable}=" in environment


def test_action_schema_matches_implemented_action_routes():
    schema = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    sources = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("reading.py", "annotations.py")
    )
    paths = [
        "/api/reading/state",
        "/api/books",
        "/api/books/{book_id}/chapters/{chapter_id}",
        "/api/reading/context",
        "/api/annotations/pending",
        "/api/annotations/{annotation_id}/reply",
        "/api/annotations/{annotation_id}/seen",
        "/api/ai/progress",
    ]
    for path in paths:
        assert f"  {path}:" in schema
        flask_path = path.replace("{book_id}", "<uuid:book_id>")
        flask_path = flask_path.replace("{chapter_id}", "<uuid:chapter_id>")
        flask_path = flask_path.replace("{annotation_id}", "<uuid:annotation_id>")
        assert flask_path in sources


def test_frontend_references_real_static_files():
    assert (ROOT / "static/css/style.css").is_file()
    assert (ROOT / "static/js/library.js").is_file()
    assert (ROOT / "static/js/reader.js").is_file()
    assert (ROOT / "templates/library.html").is_file()
    assert (ROOT / "templates/reader.html").is_file()
