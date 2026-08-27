"""Fault-tolerant EPUB2/EPUB3 and TXT parsing with stable block IDs.

The EPUB path parses the ZIP container and OPF directly. Broken NAV/NCX files
therefore cannot abort an otherwise readable book, and fonts, stylesheets, and
unused images are never materialised in memory.
"""

from __future__ import annotations

import gc
import hashlib
import html
import mimetypes
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urldefrag, urlsplit

import bleach
from bs4 import BeautifulSoup, NavigableString, Tag
from charset_normalizer import from_bytes


class BookParseError(ValueError):
    """Raised when an upload cannot be converted into readable chapters."""

    def __init__(self, message: str, code: str = "epub_parse_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class ParsedAsset:
    path: str
    media_type: str
    data: bytes | None = None
    archive_path: str | None = None
    byte_size: int = 0
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.data is not None:
            if not self.byte_size:
                self.byte_size = len(self.data)
            if not self.content_sha256:
                self.content_sha256 = hashlib.sha256(self.data).hexdigest()


@dataclass(slots=True)
class ParsedChapter:
    chapter_index: int
    title: str
    href: str
    content_html: str
    content_text: str
    word_count: int


@dataclass(slots=True)
class ParsedBook:
    title: str
    author: str
    format: str
    source_filename: str
    source_sha256: str
    toc: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[ParsedChapter] = field(default_factory=list)
    assets: list[ParsedAsset] = field(default_factory=list)
    cover_asset_path: str | None = None
    epub_version: str | None = None
    manifest_item_count: int = 0
    spine_item_count: int = 0
    archive_compressed_size: int = 0
    archive_uncompressed_size: int = 0
    archive_item_count: int = 0
    xhtml_item_count: int = 0
    image_item_count: int = 0
    font_item_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _ManifestItem:
    item_id: str
    href: str
    archive_path: str
    media_type: str
    properties: set[str]


ALLOWED_TAGS = {
    "p", "div", "section", "article", "aside", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "code",
    "strong", "b", "em", "i", "u", "s", "sub", "sup", "small", "span",
    "ruby", "rt", "rp", "br", "hr", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "img", "a",
}
ALLOWED_ATTRIBUTES = {
    "a": [
        "href", "title", "data-epub-target-href", "data-epub-target-fragment",
        "data-epub-link-kind",
    ],
    "img": ["alt", "title", "data-asset-path"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
    "*": ["data-epub-fragment", "data-epub-note"],
}
BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "li", "dt", "dd", "th", "td", "caption", "figcaption",
}
UNSAFE_TAGS = {
    "script", "style", "iframe", "object", "embed", "form", "input",
    "button", "textarea", "select", "link", "meta", "svg", "math",
}
DOCUMENT_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}
IMAGE_MEDIA_PREFIX = "image/"
MAX_AUTOMATIC_TITLE_LENGTH = 160
MAX_ARCHIVE_ENTRIES = 20_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ENTRY_BYTES = 128 * 1024 * 1024
MAX_COMPRESSION_RATIO = 300
STREAM_CHUNK_BYTES = 256 * 1024

StageObserver = Callable[[str], None]


def parse_uploaded_book(
    filename: str, data: bytes, deadline: float | None = None
) -> ParsedBook:
    extension = PurePosixPath(filename).suffix.lower()
    if extension == ".epub":
        return _parse_epub(filename, data, deadline=deadline)
    if extension == ".txt":
        return _parse_txt(filename, data)
    raise BookParseError(
        "仅支持 EPUB 和 TXT 文件；上传内容不是受支持的书籍 archive",
        "unsupported_archive",
    )


def parse_uploaded_book_path(
    filename: str,
    source_path: str | Path,
    deadline: float | None = None,
    *,
    source_sha256: str | None = None,
    stage_observer: StageObserver | None = None,
) -> ParsedBook:
    """Parse a spooled upload without retaining the compressed book in RAM."""
    path = Path(source_path)
    extension = PurePosixPath(filename).suffix.lower()
    digest = source_sha256 or _sha256_path(path)
    if extension == ".epub":
        return _parse_epub_source(
            filename,
            path,
            source_sha256=digest,
            archive_compressed_size=path.stat().st_size,
            materialize_assets=False,
            deadline=deadline,
            stage_observer=stage_observer,
        )
    if extension == ".txt":
        parsed = _parse_txt(filename, path.read_bytes())
        parsed.source_sha256 = digest
        parsed.archive_compressed_size = path.stat().st_size
        return parsed
    raise BookParseError(
        "仅支持 EPUB 和 TXT 文件；上传内容不是受支持的书籍 archive",
        "unsupported_archive",
    )


def _parse_epub(
    filename: str, data: bytes, *, deadline: float | None = None
) -> ParsedBook:
    return _parse_epub_source(
        filename,
        BytesIO(data),
        source_sha256=hashlib.sha256(data).hexdigest(),
        archive_compressed_size=len(data),
        materialize_assets=True,
        deadline=deadline,
    )


def _parse_epub_source(
    filename: str,
    source: str | Path | BytesIO,
    *,
    source_sha256: str,
    archive_compressed_size: int,
    materialize_assets: bool,
    deadline: float | None = None,
    stage_observer: StageObserver | None = None,
) -> ParsedBook:
    if archive_compressed_size <= 0:
        raise BookParseError("EPUB 文件为空", "invalid_epub")
    _check_deadline(deadline)
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BookParseError(
            "文件不是有效的 EPUB archive，可能已损坏或格式不受支持",
            "unsupported_archive",
        ) from exc

    warnings: list[str] = []
    try:
        archive_names, archive_stats = _validate_archive(archive)
        opf_path = _find_opf_path(archive, archive_names, warnings)
        opf = _parse_markup(
            _read_archive_entry(archive, archive_names, opf_path), xml=True
        )
        package = opf.find(
            lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "package"
        ) or opf
        epub_version = str(package.get("version") or "unknown").strip()[:32]
        opf_dir = posixpath.dirname(opf_path)

        manifest = _parse_manifest(package, opf_dir, archive_names, warnings)
        spine_ids, spine_toc_id = _parse_spine(package)
        metadata_title = _metadata_text(package, "title")
        author = _metadata_text(package, "creator") or "未知作者"

        nav_toc: list[dict[str, Any]] = []
        nav_item = next(
            (item for item in manifest.values() if "nav" in item.properties), None
        )
        if nav_item:
            nav_toc = _parse_nav_toc(archive, archive_names, nav_item, warnings)

        ncx_toc: list[dict[str, Any]] = []
        ncx_item = manifest.get(spine_toc_id or "") or next(
            (
                item
                for item in manifest.values()
                if item.media_type == "application/x-dtbncx+xml"
            ),
            None,
        )
        if ncx_item:
            ncx_toc = _parse_ncx_toc(archive, archive_names, ncx_item, warnings)

        toc = nav_toc or ncx_toc
        if nav_toc and ncx_toc:
            warnings.append("both_nav_and_ncx_present")
        title_map = _toc_title_map(toc)
        toc_hrefs = list(_flatten_toc_hrefs(toc))

        document_items = {
            item.href: item
            for item in manifest.values()
            if _is_document_item(item) and "nav" not in item.properties
        }
        ordered_hrefs = _chapter_order(
            toc_hrefs, spine_ids, manifest, document_items, archive_names, opf_dir
        )

        chapters: list[ParsedChapter] = []
        referenced_assets: set[str] = set()
        for href in ordered_hrefs:
            _check_deadline(deadline)
            item = document_items.get(href)
            archive_path = (
                item.archive_path
                if item
                else _package_to_archive_path(opf_dir, href)
            )
            try:
                raw = _read_archive_entry(archive, archive_names, archive_path)
                clean_html, plain_text, chapter_assets = _sanitize_chapter(raw, href)
            except Exception:
                warnings.append(f"chapter_parse_skipped:{href}")
                continue
            has_image = "data-asset-path=" in clean_html
            if not plain_text.strip() and not has_image:
                warnings.append(f"empty_chapter_skipped:{href}")
                continue
            referenced_assets.update(chapter_assets)
            chapter_title = (
                title_map.get(_href_key(href)) or _title_from_html(clean_html)
            )
            if not chapter_title:
                chapter_title = f"第 {len(chapters) + 1} 章"
            chapters.append(
                ParsedChapter(
                    chapter_index=len(chapters),
                    title=chapter_title,
                    href=href,
                    content_html=clean_html,
                    content_text=plain_text,
                    word_count=_count_words(plain_text),
                )
            )
            if len(chapters) % 50 == 0:
                gc.collect()

        if not chapters:
            raise BookParseError(
                "EPUB 中没有找到可阅读正文；目录、spine 与 manifest fallback 均无有效内容",
                "no_readable_content",
            )

        cover_path = _find_cover_path(
            package, manifest, archive, archive_names, warnings, opf_dir
        )
        wanted_assets = set(referenced_assets)
        if cover_path:
            wanted_assets.add(cover_path)
        _observe(stage_observer, "asset_extraction_before")
        assets = _extract_used_assets(
            archive,
            archive_names,
            manifest,
            wanted_assets,
            warnings,
            deadline,
            opf_dir,
            materialize=materialize_assets,
        )
        _observe(stage_observer, "asset_extraction_after")
        available_assets = {asset.path for asset in assets}
        if cover_path not in available_assets:
            cover_path = None
        chapters = _drop_missing_image_references(
            chapters, available_assets, warnings
        )
        chapters = _prune_empty_after_asset_cleanup(chapters, warnings)
        if not chapters:
            raise BookParseError(
                "EPUB 中没有可阅读正文；唯一内容为缺失或损坏的图片",
                "no_readable_content",
            )

        parsed = ParsedBook(
            title=_preferred_book_title(metadata_title, filename),
            author=_clean_metadata(author),
            format="epub",
            source_filename=filename,
            source_sha256=source_sha256,
            toc=toc,
            chapters=chapters,
            assets=assets,
            cover_asset_path=cover_path,
            epub_version=epub_version,
            manifest_item_count=len(manifest),
            spine_item_count=len(spine_ids),
            archive_compressed_size=archive_compressed_size,
            archive_uncompressed_size=archive_stats["uncompressed_size"],
            archive_item_count=archive_stats["item_count"],
            xhtml_item_count=archive_stats["xhtml_count"],
            image_item_count=archive_stats["image_count"],
            font_item_count=archive_stats["font_count"],
            warnings=_dedupe(warnings),
        )
        opf.decompose()
        return parsed
    except BookParseError:
        raise
    except Exception as exc:
        raise BookParseError(
            "无法解析 EPUB；容器、OPF 或正文结构不可恢复",
            "epub_parse_failed",
        ) from exc
    finally:
        archive.close()


def _validate_archive(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, str], dict[str, int]]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
        raise BookParseError("EPUB archive 条目数量异常", "unsupported_archive")
    total = 0
    item_count = 0
    xhtml_count = 0
    image_count = 0
    font_count = 0
    names: dict[str, str] = {}
    for info in infos:
        normalized = _normalize_archive_path(info.filename)
        if not normalized or info.is_dir():
            continue
        item_count += 1
        suffix = PurePosixPath(normalized).suffix.lower()
        xhtml_count += suffix in {".xhtml", ".html", ".htm"}
        image_count += suffix in {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"
        }
        font_count += suffix in {".ttf", ".otf", ".woff", ".woff2", ".eot"}
        if info.file_size > MAX_ENTRY_BYTES:
            raise BookParseError(
                "EPUB 中存在异常大的单个资源", "unsupported_archive"
            )
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise BookParseError(
                "EPUB 解压后体积异常，已拒绝处理", "unsupported_archive"
            )
        if (
            info.compress_size
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise BookParseError(
                "EPUB 压缩比例异常，已拒绝处理", "unsupported_archive"
            )
        names.setdefault(normalized.lower(), info.filename)
    return names, {
        "uncompressed_size": total,
        "item_count": item_count,
        "xhtml_count": xhtml_count,
        "image_count": image_count,
        "font_count": font_count,
    }


def _find_opf_path(
    archive: zipfile.ZipFile, names: dict[str, str], warnings: list[str]
) -> str:
    container_name = names.get("meta-inf/container.xml")
    if container_name:
        try:
            container = _parse_markup(archive.read(container_name), xml=True)
            rootfile = container.find(
                lambda tag: isinstance(tag, Tag)
                and _local_name(tag.name) == "rootfile"
                and tag.get("full-path")
            )
            if rootfile:
                candidate = _normalize_archive_path(rootfile.get("full-path"))
                if candidate.lower() in names:
                    return candidate
            warnings.append("broken_container_fallback")
        except Exception:
            warnings.append("broken_container_fallback")
    else:
        warnings.append("missing_container_fallback")
    candidates = sorted(
        (
            _normalize_archive_path(name)
            for name in names.values()
            if name.lower().endswith(".opf")
        ),
        key=lambda value: (value.count("/"), len(value), value.lower()),
    )
    if not candidates:
        raise BookParseError("EPUB 缺少可用的 content.opf", "invalid_epub")
    return candidates[0]


def _parse_manifest(
    package: Tag, opf_dir: str, names: dict[str, str], warnings: list[str]
) -> dict[str, _ManifestItem]:
    result: dict[str, _ManifestItem] = {}
    for tag in package.find_all(
        lambda node: isinstance(node, Tag) and _local_name(node.name) == "item"
    ):
        item_id = str(tag.get("id") or "").strip()
        raw_href = str(tag.get("href") or "").strip()
        if not item_id or not raw_href:
            continue
        archive_path = _resolve_archive_href(opf_dir, raw_href)
        canonical = _archive_to_package_path(opf_dir, archive_path)
        media_type = str(
            tag.get("media-type")
            or mimetypes.guess_type(canonical)[0]
            or "application/octet-stream"
        ).lower()
        properties = {
            value.lower() for value in str(tag.get("properties") or "").split()
        }
        if archive_path.lower() not in names:
            warnings.append(f"missing_manifest_resource:{canonical}")
        result[item_id] = _ManifestItem(
            item_id, canonical, archive_path, media_type, properties
        )
    return result


def _parse_spine(package: Tag) -> tuple[list[str], str | None]:
    spine = package.find(
        lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "spine"
    )
    if not spine:
        return [], None
    ids: list[str] = []
    for itemref in spine.find_all(
        lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "itemref"
    ):
        item_id = str(itemref.get("idref") or "").strip()
        if item_id and item_id not in ids:
            ids.append(item_id)
    return ids, str(spine.get("toc") or "").strip() or None


def _parse_nav_toc(
    archive: zipfile.ZipFile,
    names: dict[str, str],
    item: _ManifestItem,
    warnings: list[str],
) -> list[dict[str, Any]]:
    try:
        soup = _parse_markup(
            _read_archive_entry(archive, names, item.archive_path), xml=False
        )
        navs = soup.find_all("nav")
        nav = next((node for node in navs if _is_toc_nav(node)), None) or (
            navs[0] if navs else None
        )
        if not nav:
            warnings.append("nav_without_toc")
            return []
        root_list = nav.find(["ol", "ul"])
        result = _parse_html_toc_list(root_list, item.href) if root_list else []
        soup.decompose()
        return result
    except Exception:
        warnings.append("broken_nav_fallback")
        return []


def _parse_html_toc_list(
    node: Tag | None, base_href: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if node is None:
        return result
    for li in node.find_all("li", recursive=False):
        anchor = li.find("a", href=True)
        nested = li.find(["ol", "ul"], recursive=False)
        if not anchor:
            continue
        raw_href, fragment = urldefrag(str(anchor.get("href") or ""))
        href = _resolve_internal_href(base_href, raw_href)
        result.append(
            {
                "label": _clean_metadata(anchor.get_text(" "))
                or "未命名章节",
                "href": href,
                "fragment": unquote(fragment),
                "children": _parse_html_toc_list(nested, base_href),
            }
        )
    return result


def _parse_ncx_toc(
    archive: zipfile.ZipFile,
    names: dict[str, str],
    item: _ManifestItem,
    warnings: list[str],
) -> list[dict[str, Any]]:
    try:
        soup = _parse_markup(
            _read_archive_entry(archive, names, item.archive_path), xml=True
        )
        nav_map = soup.find(
            lambda tag: isinstance(tag, Tag)
            and _local_name(tag.name) == "navmap"
        )
        result = _parse_ncx_points(nav_map, item.href) if nav_map else []
        soup.decompose()
        return result
    except Exception:
        warnings.append("broken_ncx_fallback")
        return []


def _parse_ncx_points(
    node: Tag | None, base_href: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if node is None:
        return result
    points = node.find_all(
        lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "navpoint",
        recursive=False,
    )
    for point in points:
        content = point.find(
            lambda tag: isinstance(tag, Tag)
            and _local_name(tag.name) == "content"
        )
        label_node = point.find(
            lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "text"
        )
        raw = str(content.get("src") or "") if content else ""
        raw_href, fragment = urldefrag(raw)
        href = _resolve_internal_href(base_href, raw_href)
        if href:
            result.append(
                {
                    "label": _clean_metadata(
                        label_node.get_text(" ") if label_node else ""
                    )
                    or "未命名章节",
                    "href": href,
                    "fragment": unquote(fragment),
                    "children": _parse_ncx_points(point, base_href),
                }
            )
    return result


def _chapter_order(
    toc_hrefs: list[str],
    spine_ids: list[str],
    manifest: dict[str, _ManifestItem],
    documents: dict[str, _ManifestItem],
    archive_names: dict[str, str],
    opf_dir: str,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(href: str) -> None:
        normalized = _normalize_asset_path(href)
        key = _href_key(normalized)
        if not normalized or key in seen:
            return
        item = documents.get(normalized)
        archive_path = (
            item.archive_path
            if item
            else _package_to_archive_path(opf_dir, normalized)
        )
        if item or (
            archive_path.lower() in archive_names
            and PurePosixPath(normalized).suffix.lower()
            in {".xhtml", ".html", ".htm"}
        ):
            seen.add(key)
            ordered.append(normalized)

    for href in toc_hrefs:
        add(href)
    for item_id in spine_ids:
        item = manifest.get(item_id)
        if item and _is_document_item(item) and "nav" not in item.properties:
            add(item.href)
    # Manifest is the final fallback, not an unconditional appendix.  Many
    # valid EPUBs contain cover/title XHTML outside the reading spine.
    if not ordered:
        for item in manifest.values():
            if _is_document_item(item) and "nav" not in item.properties:
                add(item.href)
    return ordered


def _sanitize_chapter(
    raw: bytes, chapter_href: str
) -> tuple[str, str, set[str]]:
    soup = _parse_markup(raw, xml=False)
    for tag_name in UNSAFE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    root = soup.body or soup
    referenced_assets: set[str] = set()

    for target in root.find_all(True):
        original_id = str(
            target.get("id") or target.get("name") or ""
        ).strip()
        if original_id:
            target["data-epub-fragment"] = unquote(original_id)
        target.attrs.pop("id", None)
        target.attrs.pop("name", None)
        type_values = " ".join(
            str(target.get(name) or "")
            for name in ("epub:type", "type", "role", "class")
        ).lower()
        if any(
            token in type_values
            for token in (
                "footnote",
                "endnote",
                "doc-footnote",
                "doc-endnote",
            )
        ):
            target["data-epub-note"] = "true"

    for image in root.find_all("img"):
        source = str(image.get("src") or "")
        resolved = _resolve_internal_href(chapter_href, source)
        image.attrs = {
            key: value
            for key, value in image.attrs.items()
            if key in {"alt", "title"}
        }
        if resolved:
            image["data-asset-path"] = resolved
            referenced_assets.add(resolved)
        else:
            image.decompose()

    for anchor in root.find_all("a"):
        raw_href = str(anchor.get("href") or "").strip()
        link_kind = _link_kind(anchor, raw_href)
        anchor.attrs = {
            key: value
            for key, value in anchor.attrs.items()
            if key in {"title", "data-epub-fragment"}
        }
        if not raw_href:
            continue
        split = urlsplit(raw_href)
        if split.scheme.lower() in {"http", "https", "mailto"}:
            anchor["href"] = raw_href
            continue
        if split.scheme or raw_href.lower().startswith(("javascript:", "data:")):
            continue
        target_path, fragment = urldefrag(raw_href)
        resolved = _resolve_internal_href(chapter_href, target_path)
        anchor["href"] = "#"
        anchor["data-epub-target-href"] = resolved or chapter_href
        anchor["data-epub-target-fragment"] = unquote(fragment)
        anchor["data-epub-link-kind"] = link_kind

    rendered = "".join(str(child) for child in root.children)
    soup.decompose()
    cleaned = bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        strip=True,
        strip_comments=True,
    )
    stable_html, plain_text = _assign_block_ids(cleaned)
    return stable_html, plain_text, referenced_assets


def _assign_block_ids(fragment: str) -> tuple[str, str]:
    soup = BeautifulSoup(fragment, "html.parser")
    for container in soup.find_all(["div", "section", "article"]):
        fragment_name = container.get("data-epub-fragment")
        note = container.get("data-epub-note")
        if fragment_name or note:
            marker = soup.new_tag("span")
            if fragment_name:
                marker["data-epub-fragment"] = fragment_name
            if note:
                marker["data-epub-note"] = note
            container.insert(0, marker)
        container.unwrap()
    if not soup.find(BLOCK_TAGS):
        wrapper = soup.new_tag("p")
        for child in list(soup.contents):
            wrapper.append(child.extract())
        soup.append(wrapper)

    block_number = 0
    plain_blocks: list[str] = []
    for tag in soup.find_all(BLOCK_TAGS):
        if tag.find_parent(BLOCK_TAGS):
            continue
        block_number += 1
        block_id = f"b{block_number:06d}"
        tag["id"] = block_id
        tag["data-block-id"] = block_id
        plain = re.sub(r"\s+", " ", tag.get_text("", strip=False)).strip()
        if plain:
            plain_blocks.append(plain)

    for child in list(soup.contents):
        if isinstance(child, NavigableString) and child.strip():
            block_number += 1
            wrapper = soup.new_tag("p")
            block_id = f"b{block_number:06d}"
            wrapper["id"] = block_id
            wrapper["data-block-id"] = block_id
            wrapper.string = str(child)
            child.replace_with(wrapper)
            plain_blocks.append(
                re.sub(r"\s+", " ", wrapper.get_text("", strip=False)).strip()
            )
    stable_html = str(soup)
    plain_text = "\n\n".join(plain_blocks)
    soup.decompose()
    return stable_html, plain_text


def _find_cover_path(
    package: Tag,
    manifest: dict[str, _ManifestItem],
    archive: zipfile.ZipFile,
    names: dict[str, str],
    warnings: list[str],
    opf_dir: str,
) -> str | None:
    for item in manifest.values():
        if (
            "cover-image" in item.properties
            and item.media_type.startswith(IMAGE_MEDIA_PREFIX)
        ):
            return item.href
    for meta in package.find_all(
        lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "meta"
    ):
        if str(meta.get("name") or "").lower() == "cover":
            item = manifest.get(str(meta.get("content") or ""))
            if item and item.media_type.startswith(IMAGE_MEDIA_PREFIX):
                return item.href
    guide = package.find(
        lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == "guide"
    )
    if guide:
        reference = guide.find(
            lambda tag: isinstance(tag, Tag)
            and _local_name(tag.name) == "reference"
            and "cover" in str(tag.get("type") or "").lower()
        )
        if reference and reference.get("href"):
            cover_doc = _archive_to_package_path(
                opf_dir,
                _resolve_archive_href(opf_dir, str(reference.get("href"))),
            )
            item = next(
                (
                    value
                    for value in manifest.values()
                    if _href_key(value.href) == _href_key(cover_doc)
                ),
                None,
            )
            if item:
                try:
                    soup = _parse_markup(
                        _read_archive_entry(archive, names, item.archive_path),
                        xml=False,
                    )
                    image = soup.find("img", src=True)
                    if image:
                        cover = _resolve_internal_href(
                            item.href, str(image.get("src"))
                        )
                        soup.decompose()
                        return cover
                    soup.decompose()
                except Exception:
                    warnings.append("broken_guide_cover")
    named = [
        item.href
        for item in manifest.values()
        if item.media_type.startswith(IMAGE_MEDIA_PREFIX)
        and "cover"
        in re.sub(
            r"[^a-z0-9]",
            "",
            f"{item.item_id} {PurePosixPath(item.href).stem}".lower(),
        )
    ]
    return sorted(named)[0] if named else None


def _extract_used_assets(
    archive: zipfile.ZipFile,
    names: dict[str, str],
    manifest: dict[str, _ManifestItem],
    wanted: set[str],
    warnings: list[str],
    deadline: float | None,
    opf_dir: str,
    *,
    materialize: bool,
) -> list[ParsedAsset]:
    by_href = {_href_key(item.href): item for item in manifest.values()}
    assets: list[ParsedAsset] = []
    for path in sorted(wanted, key=str.lower):
        _check_deadline(deadline)
        item = by_href.get(_href_key(path))
        archive_path = (
            item.archive_path
            if item
            else _package_to_archive_path(opf_dir, path)
        )
        media_type = (
            item.media_type
            if item
            else (mimetypes.guess_type(path)[0] or "application/octet-stream")
        )
        if not media_type.startswith(IMAGE_MEDIA_PREFIX):
            warnings.append(f"unsupported_asset_skipped:{path}")
            continue
        actual_path = names.get(_normalize_archive_path(archive_path).lower())
        if not actual_path:
            warnings.append(f"missing_image_skipped:{path}")
            continue
        try:
            digest = hashlib.sha256()
            total = 0
            payload_parts: list[bytes] | None = [] if materialize else None
            with archive.open(actual_path) as stream:
                while chunk := stream.read(STREAM_CHUNK_BYTES):
                    _check_deadline(deadline)
                    digest.update(chunk)
                    total += len(chunk)
                    if payload_parts is not None:
                        payload_parts.append(chunk)
        except Exception:
            warnings.append(f"missing_image_skipped:{path}")
            continue
        if not total:
            warnings.append(f"broken_image_skipped:{path}")
            continue
        payload = b"".join(payload_parts) if payload_parts is not None else None
        assets.append(
            ParsedAsset(
                path=_normalize_asset_path(path),
                media_type=media_type,
                data=payload,
                archive_path=actual_path,
                byte_size=total,
                content_sha256=digest.hexdigest(),
            )
        )
    return assets


def _drop_missing_image_references(
    chapters: list[ParsedChapter], available: set[str], warnings: list[str]
) -> list[ParsedChapter]:
    for chapter in chapters:
        soup = BeautifulSoup(chapter.content_html, "html.parser")
        changed = False
        for image in soup.select("img[data-asset-path]"):
            if image.get("data-asset-path") not in available:
                warnings.append(
                    f"missing_image_removed:{image.get('data-asset-path')}"
                )
                image.decompose()
                changed = True
        if changed:
            chapter.content_html = str(soup)
        soup.decompose()
    return chapters


def _prune_empty_after_asset_cleanup(
    chapters: list[ParsedChapter], warnings: list[str]
) -> list[ParsedChapter]:
    retained: list[ParsedChapter] = []
    for chapter in chapters:
        soup = BeautifulSoup(chapter.content_html, "html.parser")
        has_image = soup.find("img") is not None
        soup.decompose()
        if chapter.content_text.strip() or has_image:
            chapter.chapter_index = len(retained)
            retained.append(chapter)
        else:
            warnings.append(f"empty_chapter_after_asset_cleanup:{chapter.href}")
    return retained


def _parse_txt(filename: str, data: bytes) -> ParsedBook:
    if not data:
        raise BookParseError("文件为空", "no_readable_content")
    best = from_bytes(data).best()
    if best is None:
        raise BookParseError("无法识别 TXT 编码", "book_parse_failed")
    text = str(best).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise BookParseError("TXT 中没有正文", "no_readable_content")
    heading_re = re.compile(
        r"(?im)^\s*((?:第[0-9一二三四五六七八九十百千万零〇两]+[章节卷回部].*)|(?:chapter\s+\d+.*))\s*$"
    )
    matches = list(heading_re.finditer(text))
    sections: list[tuple[str, str]] = []
    if matches:
        preface = text[: matches[0].start()].strip()
        if preface:
            sections.append(("前言", preface))
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text)
            )
            title = match.group(1).strip()
            body = text[match.end() : end].strip()
            sections.append((title, body or title))
    else:
        sections.append(("正文", text))
    chapters: list[ParsedChapter] = []
    toc: list[dict[str, Any]] = []
    for index, (chapter_title, body) in enumerate(sections):
        stable_html, plain_text = _assign_block_ids(_txt_to_html(body))
        href = f"txt/chapter-{index + 1}.xhtml"
        chapters.append(
            ParsedChapter(
                index,
                chapter_title,
                href,
                stable_html,
                plain_text,
                _count_words(plain_text),
            )
        )
        toc.append({"label": chapter_title, "href": href, "children": []})
    return ParsedBook(
        title=PurePosixPath(filename).stem,
        author="未知作者",
        format="txt",
        source_filename=filename,
        source_sha256=hashlib.sha256(data).hexdigest(),
        toc=toc,
        chapters=chapters,
    )


def _metadata_text(package: Tag, local_name: str) -> str | None:
    node = package.find(
        lambda tag: isinstance(tag, Tag) and _local_name(tag.name) == local_name
    )
    return node.get_text(" ", strip=True) if node else None


def _clean_metadata(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    cleaned = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    soup.decompose()
    return cleaned


def _preferred_book_title(metadata_title: str | None, filename: str) -> str:
    cleaned = _clean_metadata(metadata_title or "")
    if cleaned and len(cleaned) <= MAX_AUTOMATIC_TITLE_LENGTH:
        return cleaned
    fallback = _clean_metadata(PurePosixPath(filename).stem)
    return (fallback or "未命名书籍")[:MAX_AUTOMATIC_TITLE_LENGTH]


def _parse_markup(raw: bytes | str, *, xml: bool) -> BeautifulSoup:
    return BeautifulSoup(raw, "xml" if xml else "html.parser")


def _read_archive_entry(
    archive: zipfile.ZipFile, names: dict[str, str], path: str
) -> bytes:
    normalized = _normalize_archive_path(path)
    actual = names.get(normalized.lower())
    if not actual:
        raise KeyError(path)
    return archive.read(actual)


def _normalize_archive_path(path: str) -> str:
    normalized = posixpath.normpath(
        unquote(str(path or "")).replace("\\", "/")
    ).lstrip("/")
    if (
        normalized in {"", "."}
        or normalized == ".."
        or normalized.startswith("../")
    ):
        return ""
    return normalized


def _normalize_asset_path(path: str) -> str:
    normalized = posixpath.normpath(
        unquote(str(path or "")).replace("\\", "/")
    ).lstrip("/")
    while normalized.startswith("../"):
        normalized = normalized[3:]
    return "" if normalized in {"", "."} else normalized


def _resolve_archive_href(base_dir: str, href: str) -> str:
    raw = urldefrag(unquote(str(href or "")))[0]
    return _normalize_archive_path(posixpath.join(base_dir, raw))


def _resolve_internal_href(base_href: str, href: str) -> str:
    raw = urldefrag(unquote(str(href or "")))[0].strip()
    if not raw:
        return _normalize_asset_path(base_href)
    if urlsplit(raw).scheme or raw.startswith("//"):
        return ""
    return _normalize_asset_path(
        posixpath.join(posixpath.dirname(base_href), raw)
    )


def _archive_to_package_path(opf_dir: str, archive_path: str) -> str:
    relative = posixpath.relpath(archive_path, opf_dir or ".")
    return _normalize_asset_path(
        relative if not relative.startswith("../") else archive_path
    )


def _package_to_archive_path(opf_dir: str, package_path: str) -> str:
    return _normalize_archive_path(posixpath.join(opf_dir, package_path))


def _href_key(href: str) -> str:
    return _normalize_asset_path(urldefrag(href)[0]).lower()


def _is_document_item(item: _ManifestItem) -> bool:
    return item.media_type in DOCUMENT_MEDIA_TYPES or PurePosixPath(
        item.href
    ).suffix.lower() in {".xhtml", ".html", ".htm"}


def _is_toc_nav(nav: Tag) -> bool:
    values = " ".join(
        str(nav.get(name) or "") for name in ("epub:type", "type", "role")
    ).lower()
    return "toc" in values or "doc-toc" in values


def _local_name(name: str | None) -> str:
    return str(name or "").split(":")[-1].lower()


def _flatten_toc_hrefs(toc: list[dict[str, Any]]) -> Iterable[str]:
    for node in toc:
        if node.get("href"):
            yield str(node["href"])
        yield from _flatten_toc_hrefs(node.get("children") or [])


def _toc_title_map(toc: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in toc:
        href = str(node.get("href") or "")
        label = _clean_metadata(str(node.get("label") or ""))
        if href and label:
            result.setdefault(_href_key(href), label)
        for key, value in _toc_title_map(node.get("children") or []).items():
            result.setdefault(key, value)
    return result


def _link_kind(anchor: Tag, raw_href: str) -> str:
    values = " ".join(
        [
            str(anchor.get(name) or "")
            for name in ("epub:type", "type", "role", "class")
        ]
        + [raw_href, anchor.get_text(" ", strip=True)]
    ).lower()
    if any(
        token in values
        for token in ("backlink", "backref", "doc-backlink", "↩", "返回")
    ):
        return "backlink"
    if any(
        token in values
        for token in ("noteref", "footnote", "endnote", "doc-noteref")
    ):
        return "footnote"
    if re.search(r"(?:^|[#/_-])(?:fn|footnote|note)[-_]?\d*", values):
        return "footnote"
    return "chapter"


def _title_from_html(fragment: str) -> str | None:
    soup = BeautifulSoup(fragment, "html.parser")
    heading = soup.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    title = _clean_metadata(heading.get_text(" ")) if heading else None
    soup.decompose()
    return title


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(STREAM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _observe(observer: StageObserver | None, stage: str) -> None:
    if observer is not None:
        observer(stage)


def _txt_to_html(text: str) -> str:
    rendered: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        value = paragraph.strip()
        if value:
            rendered.append(
                f"<p>{html.escape(value).replace(chr(10), '<br>')}</p>"
            )
    return "".join(rendered)


def _count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(
        re.findall(r"\b[\w'-]+\b", re.sub(r"[\u3400-\u9fff]", " ", text))
    )
    return cjk + latin


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() >= deadline:
        raise BookParseError(
            "EPUB 导入超过安全处理时间，请检查书籍结构",
            "epub_import_timeout",
        )


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
