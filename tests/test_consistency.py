from pathlib import Path

import yaml


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
        "xiaxia_thoughts",
        "thought_user_replies",
        "xiaxia_thought_candidates",
        "reading_operation_log",
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
    schema_text = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    schema = yaml.safe_load(schema_text)
    sources = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("reading.py", "annotations.py", "management.py")
    )
    required_paths = [
        "/api/reading/state",
        "/api/books",
        "/api/books/{book_id}/chapters",
        "/api/books/{book_id}/annotations",
        "/api/reading/context",
        "/api/annotations/pending",
        "/api/annotations/{annotation_id}/reply",
        "/api/annotations/{annotation_id}/seen",
        "/api/xiaxia/thoughts",
        "/api/xiaxia/thoughts/{thought_id}",
        "/api/xiaxia/thoughts/preview",
        "/api/xiaxia/thoughts/commit",
        "/api/actions/undo",
        "/api/ai/progress",
    ]
    assert set(required_paths) <= set(schema["paths"])
    methods = {"get", "post", "patch", "delete", "put"}
    operation_ids = []
    for path, path_item in schema["paths"].items():
        flask_path = path.replace("{book_id}", "<uuid:book_id>")
        flask_path = flask_path.replace("{chapter_id}", "<uuid:chapter_id>")
        flask_path = flask_path.replace("{annotation_id}", "<uuid:annotation_id>")
        flask_path = flask_path.replace("{thought_id}", "<uuid:thought_id>")
        assert flask_path in sources
        for method, operation in path_item.items():
            if method not in methods:
                continue
            assert "@" in sources and f'.{method}("{flask_path}")' in sources
            operation_ids.append(operation["operationId"])
            assert len(operation.get("description", "")) <= 300
            path_names = [
                part[1:-1] for part in path.split("/") if part.startswith("{")
            ]
            declared = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "path"
            }
            assert declared == set(path_names)
    assert len(operation_ids) == len(set(operation_ids))


def test_action_schema_is_conservative_for_custom_gpt_actions():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert schema["servers"] == [{"url": "https://xiaxia-reading-house.onrender.com"}]
    assert "objecta" not in (ROOT / "openapi.yaml").read_text(encoding="utf-8")

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert "properties" in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


def test_experience_migration_is_non_destructive_and_complete():
    migration = (
        (ROOT / "migrations/003_v1_experience_refactor.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create table if not exists xiaxia_thoughts" in migration
    assert "last_chunk_index" in migration
    assert "last_block_id" in migration
    assert "chapter_completed" in migration
    assert "mark_type" in migration
    for destructive in (
        "drop table",
        "truncate",
        "delete from books",
        "delete from annotations",
    ):
        assert destructive not in migration


def test_v1_1_migration_is_non_destructive_and_complete():
    migration = (ROOT / "migrations/004_v1_1_management.sql").read_text(encoding="utf-8").lower()
    for item in ("thought_user_replies", "xiaxia_thought_candidates", "reading_operation_log", "last_chunk_id", "owner", "content_type"):
        assert item in migration
    for destructive in ("drop table", "truncate", "delete from books", "delete from annotations"):
        assert destructive not in migration


def test_chunk_and_thought_field_names_are_cross_file_consistent():
    files = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "reading.py",
            "annotations.py",
            "schema.sql",
            "openapi.yaml",
            "README.md",
        )
    }
    for field in (
        "chunk_index",
        "last_block_id",
        "chapter_completed",
    ):
        assert all(field in text for text in files.values())
    for field in ("scope", "mark_type", "xiaxia_thoughts"):
        assert field in files["annotations.py"]
        assert field in files["schema.sql"]
        assert field in files["openapi.yaml"]


def test_frontend_references_real_static_files():
    assert (ROOT / "static/css/style.css").is_file()
    assert (ROOT / "static/js/library.js").is_file()
    assert (ROOT / "static/js/reader-utils.js").is_file()
    assert (ROOT / "static/js/annotations-overview.js").is_file()
    assert (ROOT / "static/js/annotations-management.js").is_file()
    assert (ROOT / "static/js/reader.js").is_file()
    assert (ROOT / "templates/library.html").is_file()
    assert (ROOT / "templates/reader.html").is_file()
    assert (ROOT / "templates/annotations_overview.html").is_file()
    assert (ROOT / "templates/annotations_management.html").is_file()
