"""Versioned mapping between publisher EPUB text and Reading House anchors.

The bridge is deliberately book-level and lives in private Storage.  Database
rows only keep its lifecycle metadata; a selection never reparses the EPUB.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag
from uuid import UUID

from bs4 import BeautifulSoup, NavigableString, Tag
import database as db
import storage as object_storage


logger = logging.getLogger(__name__)
BRIDGE_VERSION = 1
ENGINE_ADAPTER_VERSION = 1
CANONICAL_TEXT_VERSION = 1
ENGINE_NAME = "foliate-js"
_DROP_CHARS = {"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"}


@dataclass(slots=True)
class LocatorBridgeError(RuntimeError):
    code: str
    message: str
    status: int = 409

    def __str__(self) -> str:
        return self.message


def normalize_text_v1(value: str) -> str:
    """Canonical text v1: NFC, no soft/zero-width chars, collapsed whitespace.

    Punctuation is intentionally unchanged. HTML entities are decoded by the
    parser before this function runs. Ruby readings are removed at DOM level;
    ruby base text remains part of the stream.
    """
    value = unicodedata.normalize("NFC", str(value or ""))
    value = "".join(" " if char == "\u00a0" else char for char in value if char not in _DROP_CHARS)
    return re.sub(r"\s+", " ", value).strip()


def canonical_markup_text(markup: bytes | str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    block_tags = {
        "address", "article", "aside", "blockquote", "caption", "dd", "div",
        "dl", "dt", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
        "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }
    skipped = {"script", "style", "template", "rt", "rp"}
    pieces: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, NavigableString):
            pieces.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = str(node.name or "").lower()
        if name in skipped:
            return
        if name == "br":
            pieces.append(" ")
            return
        for child in node.children:
            walk(child)
        if name in block_tags:
            pieces.append(" ")

    walk(soup.body or soup)
    text = normalize_text_v1("".join(pieces))
    soup.decompose()
    return text


def canonical_href(value: str) -> str:
    raw = unquote(urldefrag(str(value or ""))[0]).replace("\\", "/").strip()
    normalized = posixpath.normpath(raw).lstrip("/")
    return "" if normalized in {"", "."} else normalized


def _chapter_blocks(content_html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(content_html or "", "html.parser")
    blocks = []
    for block_order, element in enumerate(soup.select("[data-block-id]")):
        block_id = str(element.get("data-block-id") or "")
        text = element.get_text("", strip=False)
        if block_id and text.strip():
            blocks.append({"block_id": block_id, "block_order": block_order, "text": text})
    if not blocks:
        blocks.append({"block_id": "b000001", "block_order": 0, "text": soup.get_text("", strip=False)})
    soup.decompose()
    return blocks


def _text_across_blocks(blocks: list[dict[str, Any]], start_block_id: str, start_offset: int, end_block_id: str, end_offset: int) -> str:
    indexes = {item["block_id"]: index for index, item in enumerate(blocks)}
    if start_block_id not in indexes or end_block_id not in indexes:
        return ""
    start_index, end_index = indexes[start_block_id], indexes[end_block_id]
    if start_index > end_index:
        return ""
    start_text, end_text = blocks[start_index]["text"], blocks[end_index]["text"]
    if not (0 <= start_offset <= len(start_text) and 0 <= end_offset <= len(end_text)):
        return ""
    if start_index == end_index:
        return start_text[start_offset:end_offset] if end_offset >= start_offset else ""
    return "\n".join([start_text[start_offset:], *[item["text"] for item in blocks[start_index + 1:end_index]], end_text[:end_offset]])


def build_bridge_payload(
    source_path: str | Path,
    *,
    book_id: UUID | str,
    source_sha256: str,
    chapters: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic bridge without retaining publisher DOM objects."""
    original_sections = _read_original_sections(source_path, chapters)
    mapped: list[dict[str, Any]] = []
    for chapter in chapters:
        href = canonical_href(chapter.get("href") or "")
        original = original_sections.get(href.lower())
        if original is None:
            continue
        blocks = _chapter_blocks(str(chapter.get("content_html") or ""))
        block_payload: list[dict[str, Any]] = []
        cursor = 0
        for block in blocks:
            canonical = normalize_text_v1(block["text"])
            if not canonical:
                continue
            if block_payload:
                cursor += 1
            start = cursor
            cursor += len(canonical)
            block_payload.append(
                {
                    "block_id": block["block_id"],
                    "text": block["text"],
                    "canonical_start": start,
                    "canonical_end": cursor,
                    "text_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                }
            )
        mapped.append(
            {
                "spine_index": int(original.get("spine_index", chapter.get("chapter_index") or 0)),
                "href": href,
                "chapter_id": str(chapter["id"]),
                "original_text": original["text"],
                "normalized_text": " ".join(normalize_text_v1(b["text"]) for b in blocks if normalize_text_v1(b["text"])),
                "blocks": block_payload,
            }
        )
    if not mapped:
        raise LocatorBridgeError(
            "locator_mapping_missing",
            "原始 EPUB 与夏夏整理后的章节无法建立对应关系",
            422,
        )
    return {
        "bridge_version": BRIDGE_VERSION,
        "canonical_text_version": CANONICAL_TEXT_VERSION,
        "engine": ENGINE_NAME,
        "engine_adapter_version": ENGINE_ADAPTER_VERSION,
        "book_id": str(book_id),
        "source_sha256": source_sha256,
        "chapters": mapped,
    }


def persist_locator_bridge(
    book_id: UUID,
    *,
    source_path: str | Path | None = None,
    promote_legacy: bool = False,
) -> dict[str, Any]:
    """Build and atomically publish a bridge; publication data is never rolled back."""
    started = time.perf_counter()
    book = db.fetch_one(
        """
        select id, source_sha256, source_object_path, publication_ready,
               text_index_status, locator_bridge_object_path,
               locator_bridge_sha256
        from books where id = %s
        """,
        (book_id,),
    )
    if not book:
        raise LocatorBridgeError("book_not_found", "没有找到这本书", 404)
    if not book.get("publication_ready") or not book.get("source_object_path"):
        raise LocatorBridgeError("locator_bridge_not_ready", "这本书没有原始 EPUB", 409)
    if book.get("text_index_status") != "ready":
        raise LocatorBridgeError("locator_bridge_not_ready", "夏夏尚未完成正文整理", 409)

    db.execute(
        """
        update books set locator_bridge_status = 'building',
            locator_bridge_failure_code = null,
            locator_bridge_updated_at = now()
        where id = %s returning id
        """,
        (book_id,),
    )
    local_temp: tempfile.TemporaryDirectory[str] | None = None
    uploaded_path: str | None = None
    try:
        if source_path is None:
            local_temp = tempfile.TemporaryDirectory(prefix="rh-locator-bridge-")
            source_path = Path(local_temp.name) / "source.epub"
            object_storage.download_to_file(book["source_object_path"], source_path)
        chapters = db.fetch_all(
            """
            select id, chapter_index, href, content_html
            from chapters where book_id = %s order by chapter_index
            """,
            (book_id,),
        )
        payload = build_bridge_payload(
            source_path,
            book_id=book_id,
            source_sha256=book["source_sha256"],
            chapters=chapters,
        )
        historical = db.fetch_all(
            """
            select id, chapter_id, selected_text, start_block_id, start_offset,
                   end_block_id, end_offset, prefix_text, suffix_text
            from annotations where book_id = %s
            union all
            select id, chapter_id, selected_text, start_block_id, start_offset,
                   end_block_id, end_offset, prefix_text, suffix_text
            from xiaxia_thoughts
            where book_id = %s and scope in ('range', 'block')
            """,
            (book_id, book_id),
        )
        coverage = _historical_coverage(payload, historical)
        payload["historical_anchor_validation"] = coverage
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        object_path = f"books/{book_id}/locator-bridge/v{BRIDGE_VERSION}-{digest[:24]}.json"
        if not (
            book.get("locator_bridge_object_path") == object_path
            and book.get("locator_bridge_sha256") == digest
        ):
            object_storage.upload_bytes(object_path, encoded, "application/json")
            uploaded_path = object_path
        previous = book.get("locator_bridge_object_path")
        row = db.execute(
            """
            update books set locator_bridge_status = 'ready',
                locator_bridge_version = %s,
                locator_bridge_object_path = %s,
                locator_bridge_sha256 = %s,
                locator_bridge_failure_code = null,
                reader_engine = case when %s and %s then 'foliate' else reader_engine end,
                locator_bridge_updated_at = now()
            where id = %s
            returning locator_bridge_status, locator_bridge_version,
                      locator_bridge_object_path, locator_bridge_sha256,
                      locator_bridge_updated_at, reader_engine
            """,
            (BRIDGE_VERSION, object_path, digest, promote_legacy, coverage["all_mappable"], book_id),
        )
        _load_bridge.cache_clear()
        if previous and previous != object_path:
            try:
                object_storage.delete_objects([previous])
            except object_storage.ObjectStorageError:
                logger.warning("locator bridge old object cleanup failed book_id=%s", book_id)
        _log_mapping(book_id, None, None, book["source_sha256"], "", "bridge_ready", started)
        return dict(row or {})
    except LocatorBridgeError as exc:
        _mark_failed(book_id, exc.code)
        _log_mapping(book_id, None, None, book["source_sha256"], "", "failed", started, exc.code)
        raise
    except Exception as exc:
        if uploaded_path:
            try:
                object_storage.delete_objects([uploaded_path])
            except object_storage.ObjectStorageError:
                pass
        _mark_failed(book_id, "locator_backfill_failed")
        _log_mapping(book_id, None, None, book["source_sha256"], "", "failed", started, "locator_backfill_failed")
        raise LocatorBridgeError(
            "locator_backfill_failed", "书页定位索引暂时无法建立", 500
        ) from exc
    finally:
        if local_temp is not None:
            local_temp.cleanup()


def map_engine_selection(book_id: UUID, locator: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    book, bridge = _validated_bridge(book_id, locator)
    href = canonical_href(locator.get("href") or "")
    chapter = _bridge_chapter(bridge, href, locator.get("spine_index", locator.get("section_index")))
    text = locator.get("text") if isinstance(locator.get("text"), dict) else {}
    quote = normalize_text_v1(text.get("highlight") or locator.get("selected_text") or "")
    before = normalize_text_v1(text.get("before") or "")
    after = normalize_text_v1(text.get("after") or "")
    if not quote:
        raise LocatorBridgeError("locator_mapping_missing", "所选文字为空", 422)
    match = _unique_match(chapter["normalized_text"], quote, before, after)
    if match is None:
        code = "locator_mapping_ambiguous" if _count_occurrences(chapter["normalized_text"], quote) > 1 else "locator_mapping_missing"
        _log_mapping(book_id, None, "user_annotation", book["source_sha256"], href, "failed", started, code)
        raise LocatorBridgeError(code, "这处文字暂时无法留下共读痕迹", 422)
    start, end = match
    anchor = _normalized_span_to_legacy(chapter, start, end)
    selected = _text_across_blocks(
        [{"block_id": item["block_id"], "text": item["text"]} for item in chapter["blocks"]],
        anchor["start_block_id"], anchor["start_offset"],
        anchor["end_block_id"], anchor["end_offset"],
    )
    if normalize_text_v1(selected) != quote:
        raise LocatorBridgeError(
            "locator_mapping_validation_failed",
            "所选文字与夏夏整理后的书页不一致",
            422,
        )
    anchor.update(
        {
            "book_id": book_id,
            "chapter_id": UUID(chapter["chapter_id"]),
            "selected_text": selected,
            "prefix_text": anchor.pop("prefix"),
            "suffix_text": anchor.pop("suffix"),
            "engine_locator": _sanitize_locator(locator, book["source_sha256"], href, quote, before, after),
        }
    )
    _log_mapping(book_id, None, "user_annotation", book["source_sha256"], href, "mapped", started)
    return anchor


def legacy_locator_seed(book_id: UUID, record: dict[str, Any]) -> dict[str, Any]:
    book, bridge = _validated_bridge(book_id, None)
    return _legacy_seed_from_payload(bridge, record, book["source_sha256"])


def _legacy_seed_from_payload(bridge: dict[str, Any], record: dict[str, Any], source_sha256: str) -> dict[str, Any]:
    chapter = next((item for item in bridge["chapters"] if item["chapter_id"] == str(record["chapter_id"])), None)
    if not chapter or not record.get("start_block_id"):
        raise LocatorBridgeError("locator_mapping_missing", "这条痕迹没有可映射的范围", 422)
    blocks = chapter["blocks"]
    start_item = next((item for item in blocks if item["block_id"] == record["start_block_id"]), None)
    end_item = next((item for item in blocks if item["block_id"] == record["end_block_id"]), None)
    if not start_item or not end_item:
        raise LocatorBridgeError("locator_mapping_missing", "旧书页锚点不在当前索引中", 422)
    start = start_item["canonical_start"] + _canonical_prefix_length(start_item["text"], int(record["start_offset"]))
    end = end_item["canonical_start"] + _canonical_prefix_length(end_item["text"], int(record["end_offset"]))
    quote = normalize_text_v1(record.get("selected_text") or "")
    actual = chapter["normalized_text"][start:end]
    if actual != quote:
        match = _unique_match(
            chapter["normalized_text"], quote,
            normalize_text_v1(record.get("prefix_text") or ""),
            normalize_text_v1(record.get("suffix_text") or ""),
        )
        if match is None:
            raise LocatorBridgeError("locator_mapping_ambiguous", "旧书页锚点无法唯一映射", 422)
        start, end = match
    original_match = _unique_match(
        chapter["original_text"], quote,
        normalize_text_v1(record.get("prefix_text") or ""),
        normalize_text_v1(record.get("suffix_text") or ""),
    )
    if original_match is None:
        raise LocatorBridgeError("locator_mapping_ambiguous", "原始 EPUB 中无法唯一定位这处文字", 422)
    original_start, original_end = original_match
    return {
        "engine": ENGINE_NAME,
        "engine_adapter_version": ENGINE_ADAPTER_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "source_sha256": source_sha256,
        "href": chapter["href"],
        "spine_index": chapter["spine_index"],
        "original_start": original_start,
        "original_end": original_end,
        "text": {
            "highlight": quote,
            "before": chapter["original_text"][max(0, original_start - 120):original_start],
            "after": chapter["original_text"][original_end:original_end + 120],
        },
    }


def _historical_coverage(bridge: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    mapped = 0
    failures: dict[str, int] = {}
    for record in records:
        try:
            _legacy_seed_from_payload(bridge, record, bridge["source_sha256"])
            mapped += 1
        except LocatorBridgeError as exc:
            failures[exc.code] = failures.get(exc.code, 0) + 1
    total = len(records)
    return {
        "total": total,
        "mappable": mapped,
        "failed": total - mapped,
        "ratio": 1.0 if not total else round(mapped / total, 6),
        "all_mappable": mapped == total,
        "failure_codes": failures,
    }


def validate_backfill_locator(book_id: UUID, record: dict[str, Any], locator: dict[str, Any]) -> dict[str, Any]:
    mapped = map_engine_selection(book_id, locator)
    keys = ("chapter_id", "start_block_id", "start_offset", "end_block_id", "end_offset")
    for key in keys:
        if str(mapped[key]) != str(record.get(key)):
            raise LocatorBridgeError(
                "locator_mapping_validation_failed",
                "浏览器定位与原有书页锚点不一致",
                422,
            )
    return mapped["engine_locator"]


def bridge_state(book_id: UUID) -> dict[str, Any] | None:
    return db.fetch_one(
        """
        select locator_bridge_status, locator_bridge_version,
               locator_bridge_failure_code, locator_bridge_updated_at
        from books where id = %s
        """,
        (book_id,),
    )


def _validated_bridge(book_id: UUID, locator: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    book = db.fetch_one(
        """
        select id, source_sha256, locator_bridge_status, locator_bridge_version,
               locator_bridge_object_path, locator_bridge_sha256
        from books where id = %s
        """,
        (book_id,),
    )
    if not book or book.get("locator_bridge_status") != "ready" or not book.get("locator_bridge_object_path"):
        raise LocatorBridgeError("locator_bridge_not_ready", "这本书的共读书页仍在准备", 409)
    if int(book.get("locator_bridge_version") or 0) != BRIDGE_VERSION:
        raise LocatorBridgeError("locator_version_mismatch", "书页定位版本需要重建", 409)
    if locator is not None:
        if locator.get("engine") != ENGINE_NAME:
            raise LocatorBridgeError("locator_version_mismatch", "阅读引擎定位版本不匹配", 409)
        if int(locator.get("engine_adapter_version") or 0) != ENGINE_ADAPTER_VERSION:
            raise LocatorBridgeError("locator_version_mismatch", "阅读引擎适配版本不匹配", 409)
        if int(locator.get("bridge_version") or 0) != BRIDGE_VERSION:
            raise LocatorBridgeError("locator_version_mismatch", "书页定位版本不匹配", 409)
        if locator.get("source_sha256") != book["source_sha256"]:
            raise LocatorBridgeError("locator_source_mismatch", "定位不属于当前 EPUB 版本", 409)
        cfi = str(locator.get("cfi") or "")
        if not cfi.startswith("epubcfi(") or len(cfi) > 8192:
            raise LocatorBridgeError("locator_invalid_cfi", "浏览器书页定位无效", 422)
    bridge = _load_bridge(book["locator_bridge_object_path"], book["locator_bridge_sha256"])
    if bridge.get("source_sha256") != book["source_sha256"]:
        raise LocatorBridgeError("locator_source_mismatch", "书页索引不属于当前 EPUB 版本", 409)
    return book, bridge


@lru_cache(maxsize=2)
def _load_bridge(object_path: str, expected_sha256: str) -> dict[str, Any]:
    encoded = object_storage.download_bytes(object_path)
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise LocatorBridgeError("locator_version_mismatch", "书页定位索引校验失败", 409)
    return json.loads(encoded)


def _read_original_sections(source_path: str | Path, chapters: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    with zipfile.ZipFile(source_path) as archive:
        names = {canonical_href(name).lower(): name for name in archive.namelist()}
        opf_path = _opf_path(archive, names)
        opf_dir = posixpath.dirname(opf_path)
        spine_by_href: dict[str, int] = {}
        opf_name = names.get(opf_path.lower())
        if opf_name:
            package = BeautifulSoup(archive.read(opf_name), "xml")
            manifest: dict[str, str] = {}
            for item in package.find_all(lambda tag: isinstance(tag, Tag) and str(tag.name).split(":")[-1] == "item"):
                item_id = str(item.get("id") or "")
                href = canonical_href(str(item.get("href") or ""))
                if item_id and href:
                    manifest[item_id] = href
            spine = package.find(lambda tag: isinstance(tag, Tag) and str(tag.name).split(":")[-1] == "spine")
            if spine:
                for index, itemref in enumerate(spine.find_all(lambda tag: isinstance(tag, Tag) and str(tag.name).split(":")[-1] == "itemref")):
                    href = manifest.get(str(itemref.get("idref") or ""))
                    if href:
                        spine_by_href[href.lower()] = index
            package.decompose()
        result: dict[str, dict[str, Any]] = {}
        for chapter in chapters:
            href = canonical_href(chapter.get("href") or "")
            candidates = [href, canonical_href(posixpath.join(opf_dir, href))]
            archive_name = next((names.get(item.lower()) for item in candidates if item.lower() in names), None)
            if not archive_name:
                continue
            result[href.lower()] = {
                "text": canonical_markup_text(archive.read(archive_name)),
                "spine_index": spine_by_href.get(href.lower(), int(chapter.get("chapter_index") or 0)),
            }
        return result


def _opf_path(archive: zipfile.ZipFile, names: dict[str, str]) -> str:
    container_name = names.get("meta-inf/container.xml")
    if container_name:
        soup = BeautifulSoup(archive.read(container_name), "xml")
        rootfile = soup.find(lambda tag: getattr(tag, "name", "").split(":")[-1] == "rootfile")
        value = canonical_href(rootfile.get("full-path") if rootfile else "")
        soup.decompose()
        if value and value.lower() in names:
            return value
    opfs = sorted(key for key in names if key.endswith(".opf"))
    if not opfs:
        raise LocatorBridgeError("locator_mapping_missing", "EPUB package path 不可用", 422)
    return opfs[0]


def _bridge_chapter(bridge: dict[str, Any], href: str, spine_index: Any) -> dict[str, Any]:
    by_href = [item for item in bridge["chapters"] if canonical_href(item["href"]).lower() == href.lower()]
    if len(by_href) == 1:
        return by_href[0]
    try:
        index = int(spine_index)
    except (TypeError, ValueError):
        index = -1
    by_index = [item for item in bridge["chapters"] if int(item["spine_index"]) == index]
    if len(by_index) == 1:
        return by_index[0]
    raise LocatorBridgeError("locator_mapping_missing", "无法对应到夏夏整理后的章节", 422)


def _unique_match(haystack: str, needle: str, before: str, after: str) -> tuple[int, int] | None:
    positions = []
    offset = 0
    while needle and (found := haystack.find(needle, offset)) >= 0:
        positions.append(found)
        offset = found + max(1, len(needle))
    if len(positions) == 1:
        return positions[0], positions[0] + len(needle)
    if not positions:
        return None
    scored: list[tuple[int, int]] = []
    for position in positions:
        score = 0
        if before:
            window = normalize_text_v1(haystack[max(0, position - len(before) - 32):position])
            score += _common_suffix(window, before)
        if after:
            window = normalize_text_v1(haystack[position + len(needle):position + len(needle) + len(after) + 32])
            score += _common_prefix(window, after)
        scored.append((score, position))
    scored.sort(reverse=True)
    if scored[0][0] <= 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1], scored[0][1] + len(needle)


def _count_occurrences(haystack: str, needle: str) -> int:
    return len(re.findall(re.escape(needle), haystack)) if needle else 0


def _normalized_span_to_legacy(chapter: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    blocks = chapter["blocks"]
    start_item = next((item for item in blocks if item["canonical_start"] <= start < item["canonical_end"]), None)
    end_position = max(start, end - 1)
    end_item = next((item for item in blocks if item["canonical_start"] <= end_position < item["canonical_end"]), None)
    if not start_item or not end_item:
        raise LocatorBridgeError("locator_mapping_validation_failed", "选择跨越了不可映射的书页边界", 422)
    raw_start = _raw_offset_for_canonical(start_item["text"], start - start_item["canonical_start"])
    raw_end = _raw_offset_for_canonical(end_item["text"], end - end_item["canonical_start"])
    return {
        "start_block_id": start_item["block_id"],
        "start_offset": raw_start,
        "end_block_id": end_item["block_id"],
        "end_offset": raw_end,
        "prefix": start_item["text"][max(0, raw_start - 120):raw_start],
        "suffix": end_item["text"][raw_end:raw_end + 120],
    }


def _canonical_prefix_length(raw: str, offset: int) -> int:
    target = max(0, min(len(raw), offset))
    starts, end = _canonical_boundaries(raw)
    return sum(1 for value in starts if value < target) if target < end else len(starts)


def _raw_offset_for_canonical(raw: str, target: int) -> int:
    if target <= 0:
        return 0
    starts, end = _canonical_boundaries(raw)
    return starts[target] if target < len(starts) else end


def _canonical_boundaries(raw: str) -> tuple[list[int], int]:
    starts: list[int] = []
    pending_space: int | None = None
    emitted = False
    for offset, char in enumerate(unicodedata.normalize("NFC", raw)):
        if char in _DROP_CHARS:
            continue
        if char == "\u00a0" or char.isspace():
            if emitted and pending_space is None:
                pending_space = offset
            continue
        if pending_space is not None:
            starts.append(pending_space)
            pending_space = None
        starts.append(offset)
        emitted = True
    return starts, len(raw)


def _sanitize_locator(locator: dict[str, Any], source_sha256: str, href: str, quote: str, before: str, after: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "engine": ENGINE_NAME,
        "engine_adapter_version": ENGINE_ADAPTER_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "source_sha256": source_sha256,
        "href": href,
        "cfi": str(locator.get("cfi") or "")[:8192],
        "text": {"highlight": quote[:20_000], "before": before[-500:], "after": after[:500]},
    }
    try:
        result["spine_index"] = max(0, int(locator.get("spine_index", locator.get("section_index"))))
    except (TypeError, ValueError):
        pass
    try:
        result["progression"] = max(0.0, min(1.0, float(locator.get("progression", 0))))
    except (TypeError, ValueError):
        result["progression"] = 0.0
    return result


def _mark_failed(book_id: UUID, code: str) -> None:
    try:
        db.execute(
            """
            update books set locator_bridge_status = 'failed',
                locator_bridge_failure_code = %s,
                locator_bridge_updated_at = now()
            where id = %s returning id
            """,
            (code, book_id),
        )
    except Exception:
        logger.exception("locator bridge failure state could not be saved book_id=%s", book_id)


def _common_prefix(left: str, right: str) -> int:
    return next((i for i, (a, b) in enumerate(zip(left, right)) if a != b), min(len(left), len(right)))


def _common_suffix(left: str, right: str) -> int:
    return _common_prefix(left[::-1], right[::-1])


def _log_mapping(book_id: UUID, record_id: Any, record_type: Any, source_sha256: str, href: str, status: str, started: float, error_code: str | None = None) -> None:
    logger.info(
        "locator_bridge %s",
        json.dumps(
            {
                "book_id": str(book_id),
                "record_id": str(record_id) if record_id else None,
                "record_type": record_type,
                "source_sha256": source_sha256,
                "bridge_version": BRIDGE_VERSION,
                "href": href,
                "mapping_status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "error_code": error_code,
            },
            ensure_ascii=False,
        ),
    )
