from __future__ import annotations

import hashlib
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

import annotations
import locator_bridge as bridge
from app import create_app


BOOK_ID = UUID("11111111-1111-4111-8111-111111111111")
CHAPTER_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_HASH = "a" * 64


def make_epub(path: Path, body: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", """<?xml version='1.0'?>
          <container><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
        archive.writestr("OEBPS/content.opf", """<package version='3.0'><manifest>
          <item id='c1' href='Text/chapter.xhtml' media-type='application/xhtml+xml'/>
          </manifest><spine><itemref idref='c1'/></spine></package>""")
        archive.writestr("OEBPS/Text/chapter.xhtml", f"<html><body>{body}</body></html>")


def chapter(content: str) -> dict:
    return {
        "id": CHAPTER_ID,
        "chapter_index": 0,
        "href": "Text/chapter.xhtml",
        "content_html": content,
    }


def install_payload(monkeypatch, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    monkeypatch.setattr(
        bridge.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "id": BOOK_ID,
            "source_sha256": SOURCE_HASH,
            "locator_bridge_status": "ready",
            "locator_bridge_version": 1,
            "locator_bridge_object_path": "books/bridge.json",
            "locator_bridge_sha256": digest,
        },
    )
    monkeypatch.setattr(bridge.object_storage, "download_bytes", lambda _path: encoded)
    bridge._load_bridge.cache_clear()


def locator(quote: str, *, before: str = "", after: str = "") -> dict:
    return {
        "engine": "foliate-js",
        "engine_adapter_version": 1,
        "bridge_version": 1,
        "source_sha256": SOURCE_HASH,
        "href": "Text/chapter.xhtml",
        "spine_index": 0,
        "cfi": "epubcfi(/6/2!/4/2/1:0)",
        "text": {"highlight": quote, "before": before, "after": after},
    }


def test_canonical_text_v1_unicode_whitespace_ruby_and_footnote():
    markup = "<p>A\u00a0 B\u00ad<ruby>漢<rt>kan</rt></ruby><a> [1]</a></p>"
    assert bridge.canonical_markup_text(markup) == "A B漢 [1]"
    assert bridge.normalize_text_v1("e\u0301  \u200b x") == "é x"


def test_bridge_href_mapping_and_web_to_legacy_round_trip(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    make_epub(epub, "<p>山色有无中</p><p>江流天地外</p>")
    payload = bridge.build_bridge_payload(
        epub,
        book_id=BOOK_ID,
        source_sha256=SOURCE_HASH,
        chapters=[chapter(
            '<p data-block-id="b000001">山色有无中</p>'
            '<p data-block-id="b000002">江流天地外</p>'
        )],
    )
    install_payload(monkeypatch, payload)
    mapped = bridge.map_engine_selection(BOOK_ID, locator("江流天地外", before="山色有无中"))
    assert mapped["chapter_id"] == CHAPTER_ID
    assert mapped["start_block_id"] == "b000002"
    assert mapped["start_offset"] == 0
    assert mapped["end_offset"] == len("江流天地外")
    assert mapped["selected_text"] == "江流天地外"
    assert mapped["engine_locator"]["source_sha256"] == SOURCE_HASH

    seed = bridge.legacy_locator_seed(BOOK_ID, mapped)
    assert seed["href"] == "Text/chapter.xhtml"
    assert seed["text"]["highlight"] == "江流天地外"
    assert payload["bridge_version"] == 1


def test_duplicate_quote_requires_context(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    make_epub(epub, "<p>甲 重复 乙</p><p>丙 重复 丁</p>")
    payload = bridge.build_bridge_payload(
        epub,
        book_id=BOOK_ID,
        source_sha256=SOURCE_HASH,
        chapters=[chapter(
            '<p data-block-id="b000001">甲 重复 乙</p>'
            '<p data-block-id="b000002">丙 重复 丁</p>'
        )],
    )
    install_payload(monkeypatch, payload)
    with pytest.raises(bridge.LocatorBridgeError) as exc:
        bridge.map_engine_selection(BOOK_ID, locator("重复"))
    assert exc.value.code == "locator_mapping_ambiguous"

    mapped = bridge.map_engine_selection(BOOK_ID, locator("重复", before="丙 ", after=" 丁"))
    assert mapped["start_block_id"] == "b000002"


def test_source_and_version_mismatch_are_rejected(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    make_epub(epub, "<p>正文</p>")
    payload = bridge.build_bridge_payload(
        epub, book_id=BOOK_ID, source_sha256=SOURCE_HASH,
        chapters=[chapter('<p data-block-id="b000001">正文</p>')],
    )
    install_payload(monkeypatch, payload)
    bad = locator("正文")
    bad["source_sha256"] = "b" * 64
    with pytest.raises(bridge.LocatorBridgeError) as exc:
        bridge.map_engine_selection(BOOK_ID, bad)
    assert exc.value.code == "locator_source_mismatch"
    bad = locator("正文")
    bad["bridge_version"] = 9
    with pytest.raises(bridge.LocatorBridgeError) as exc:
        bridge.map_engine_selection(BOOK_ID, bad)
    assert exc.value.code == "locator_version_mismatch"


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append((" ".join(query.split()), params))
        if "insert into annotations" in query:
            return Cursor({
                "id": UUID("33333333-3333-4333-8333-333333333333"),
                "book_id": BOOK_ID,
                "chapter_id": CHAPTER_ID,
                "selected_text": "正文",
                "start_block_id": "b000001",
                "start_offset": 0,
                "end_block_id": "b000001",
                "end_offset": 2,
                "engine_locator": params[-3].obj,
                "engine_locator_version": 1,
            })
        if "insert into reading_operation_log" in query:
            return Cursor({"id": UUID("44444444-4444-4444-8444-444444444444")})
        return Cursor()


def web_client():
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_URL": "postgresql://example",
        "PRIVATE_ACCESS_PASSWORD": "private",
        "ACTION_API_TOKEN": "action",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "secret",
        "SUPABASE_STORAGE_BUCKET": "private",
        "DUAL_ANCHOR_ENABLED": True,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session["private_access"] = True
    return client


def test_foliate_annotation_transaction_stores_both_anchors(monkeypatch):
    mapped = {
        "chapter_id": CHAPTER_ID,
        "selected_text": "正文",
        "start_block_id": "b000001",
        "start_offset": 0,
        "end_block_id": "b000001",
        "end_offset": 2,
        "prefix_text": "",
        "suffix_text": "",
        "engine_locator": locator("正文"),
    }
    monkeypatch.setattr(annotations, "map_engine_selection", lambda *_args: mapped)
    monkeypatch.setattr(
        annotations.db, "fetch_one",
        lambda *_args: {"id": CHAPTER_ID, "content_html": '<p data-block-id="b000001">正文</p>'},
    )
    connection = Connection()

    @contextmanager
    def transaction():
        yield connection

    monkeypatch.setattr(annotations.db, "transaction", transaction)
    response = web_client().post(
        "/api/annotations",
        json={"book_id": str(BOOK_ID), "engine_locator": locator("正文"), "comment": "记住"},
    )
    assert response.status_code == 201
    insert = next(item for item in connection.queries if "insert into annotations" in item[0])
    assert "engine_locator" in insert[0]
    assert insert[1][-2] == 1


def test_phase3_migration_is_additive_and_actions_schema_unchanged():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/007_reader_engine_dual_anchor.sql").read_text()
    assert "add column if not exists engine_locator jsonb" in migration
    assert "locator_bridge_object_path" in migration
    for destructive in ("drop table", "truncate", "delete from annotations", "delete from xiaxia_thoughts"):
        assert destructive not in migration.lower()
    assert "engine_locator" in (root / "operations.py").read_text()
    foliate = (root / "static/js/foliate-reader.js").read_text()
    adapter = (root / "static/js/foliate-reader-adapter.js").read_text()
    assert "selectionEpoch" in foliate
    assert "selectionSuppressedUntil" in foliate
    assert "clearBrowserSelection" in foliate
    assert "removeAllRanges" in adapter
    assert "addDecoration" in adapter and "removeDecoration" in adapter
    assert "engine_locator" not in (root / "openapi.yaml").read_text()
