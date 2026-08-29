from __future__ import annotations

import hashlib
import json
import os
import subprocess
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


def make_browser_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", """<?xml version='1.0'?>
          <container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>
          <rootfiles><rootfile full-path='OEBPS/content.opf'
          media-type='application/oebps-package+xml'/></rootfiles></container>""")
        archive.writestr("OEBPS/content.opf", """<?xml version='1.0'?>
          <package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='id'>
          <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
          <dc:identifier id='id'>phase3-browser</dc:identifier><dc:title>Locator Test</dc:title>
          <dc:language>zh-CN</dc:language></metadata><manifest>
          <item id='c1' href='Text/chapter.xhtml' media-type='application/xhtml+xml'/>
          <item id='c2' href='Text/other.xhtml' media-type='application/xhtml+xml'/>
          </manifest><spine><itemref idref='c1'/><itemref idref='c2'/></spine></package>""")
        archive.writestr(
            "OEBPS/Text/chapter.xhtml",
            """<html xmlns='http://www.w3.org/1999/xhtml'><head><title>A</title></head><body>
            <p>此时相望不相闻，愿逐月华流照君</p><p>后来选择的那一句话</p></body></html>""",
        )
        archive.writestr(
            "OEBPS/Text/other.xhtml",
            "<html xmlns='http://www.w3.org/1999/xhtml'><body><p>另一章</p></body></html>",
        )


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
        "locator_integrity_version": 1,
        "browser_truth": {
            "href": "Text/chapter.xhtml",
            "section_index": 0,
            "text": quote,
        },
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


def test_browser_truth_and_section_identity_are_mandatory(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    make_epub(epub, "<p>正文</p>")
    payload = bridge.build_bridge_payload(
        epub, book_id=BOOK_ID, source_sha256=SOURCE_HASH,
        chapters=[chapter('<p data-block-id="b000001">正文</p>')],
    )
    install_payload(monkeypatch, payload)
    no_truth = locator("正文")
    no_truth.pop("browser_truth")
    with pytest.raises(bridge.LocatorBridgeError) as exc:
        bridge.map_engine_selection(BOOK_ID, no_truth)
    assert exc.value.code == "locator_mapping_validation_failed"
    wrong_index = locator("正文")
    wrong_index["spine_index"] = 4
    wrong_index["browser_truth"]["section_index"] = 4
    with pytest.raises(bridge.LocatorBridgeError) as exc:
        bridge.map_engine_selection(BOOK_ID, wrong_index)
    assert exc.value.code == "locator_mapping_validation_failed"


def test_xiaxia_thought_offset_slip_is_corrected_only_when_unique_and_nearby():
    content = '<p data-block-id="b000001">甲此时相望不相闻，愿逐月华流照君乙</p>'
    payload = {
        "start_block_id": "b000001",
        "end_block_id": "b000001",
        "start_offset": 0,
        "end_offset": 17,
        "selected_text": "此时相望不相闻，愿逐月华流照君",
        "prefix_text": "甲",
        "suffix_text": "乙",
    }
    anchor = annotations._thought_anchor("range", payload, content)
    assert anchor["start_offset"] == 1
    assert anchor["selected_text"] == payload["selected_text"]

    duplicate = '<p data-block-id="b000001">甲重复乙，甲重复乙</p>'
    with pytest.raises(ValueError):
        annotations._thought_anchor("range", {
            "start_block_id": "b000001", "end_block_id": "b000001",
            "start_offset": 0, "end_offset": 2, "selected_text": "重复",
        }, duplicate)


def test_live_chromium_locator_identity_and_truth(tmp_path):
    epub = tmp_path / "browser.epub"
    make_browser_epub(epub)
    root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        ["node", "-e", "process.stdout.write(require('playwright').chromium.executablePath())"],
        cwd=root, capture_output=True, text=True,
    )
    if probe.returncode != 0 or not Path(probe.stdout).is_file():
        pytest.skip("Playwright Chromium binary is not installed")
    env = {**os.environ, "PHASE3_BROWSER_EPUB": str(epub)}
    result = subprocess.run(
        ["node", "tests/phase3_locator_integrity_browser.mjs"],
        cwd=root, env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    text_a = "此时相望不相闻，愿逐月华流照君"
    text_b = "后来选择的那一句话"
    assert payload["locatorAText"] == payload["verifiedAText"] == text_a
    assert payload["locatorBText"] == payload["verifiedBText"] == text_b
    assert payload["hits"][0] == ["thought:A"]
    assert payload["hits"][1] == ["annotation:B"]
    assert payload["hits"][2] == ["annotation:C", "thought:A"]
    assert payload["keys"] == ["annotation:B", "annotation:C", "thought:A"]
    assert payload["wrongTextError"] == "locator_mapping_validation_failed"
    assert payload["wrongSectionError"] == "locator_mapping_missing"


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


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


def test_revalidate_clears_only_unverified_locator_and_keeps_legacy_anchor(monkeypatch):
    annotation_id = UUID("33333333-3333-4333-8333-333333333333")
    thought_id = UUID("55555555-5555-4555-8555-555555555555")
    stale = locator("正文")
    stale.pop("locator_integrity_version")
    queries = []

    class RevalidationConnection:
        def execute(self, query, params=()):
            compact = " ".join(query.split())
            queries.append((compact, params))
            if "from annotations" in compact:
                return Cursor(rows=[{"id": annotation_id, "selected_text": "正文", "engine_locator": stale}])
            if "from xiaxia_thoughts" in compact:
                return Cursor(rows=[{"id": thought_id, "selected_text": "正文", "engine_locator": locator("正文")}])
            return Cursor()

    @contextmanager
    def transaction():
        yield RevalidationConnection()

    monkeypatch.setattr(
        annotations.db, "fetch_one",
        lambda *_args: {"id": BOOK_ID, "source_sha256": SOURCE_HASH},
    )
    monkeypatch.setattr(annotations.db, "transaction", transaction)
    response = web_client().post(
        f"/api/books/{BOOK_ID}/engine-traces/revalidate", json={},
    )
    assert response.status_code == 200
    assert response.get_json()["cleared"] == 1
    updates = [item for item in queries if item[0].startswith("update annotations")]
    assert len(updates) == 1 and updates[0][1] == (annotation_id,)
    assert "start_block_id" not in updates[0][0]
    assert not [item for item in queries if item[0].startswith("update xiaxia_thoughts")]


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
    assert "getContents?.()[0]" not in adapter
    assert "records[records.length - 1]" not in foliate
    assert "recordKey" in adapter and "trace-record-choices" in foliate
    assert "browser_truth" in adapter and "verifyLocator" in adapter
    assert "engine_locator" not in (root / "openapi.yaml").read_text()
