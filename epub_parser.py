"""EPUB2/EPUB3 and TXT parsing with stable annotation block IDs."""

from __future__ import annotations

import hashlib
import html
import mimetypes
import posixpath
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urldefrag

import bleach
import ebooklib
from bs4 import BeautifulSoup, NavigableString
from charset_normalizer import from_bytes
from ebooklib import epub


class BookParseError(ValueError):
    """Raised when an upload cannot be converted into readable chapters."""


@dataclass(slots=True)
class ParsedAsset:
    path: str
    media_type: str
    data: bytes


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


ALLOWED_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "code",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "sub",
    "sup",
    "small",
    "span",
    "br",
    "hr",
    "ul",
    "ol",
    "li",
    "dl",
    "dt",
    "dd",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "caption",
    "img",
    "a",
}
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["alt", "title", "data-asset-path"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
}
BLOCK_TAGS = {
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "li",
    "dt",
    "dd",
    "th",
    "td",
    "caption",
}
UNSAFE_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "link",
    "meta",
    "svg",
    "math",
}


def parse_uploaded_book(filename: str, data: bytes) -> ParsedBook:
    extension = PurePosixPath(filename).suffix.lower()
    if extension == ".epub":
        return _parse_epub(filename, data)
    if extension == ".txt":
        return _parse_txt(filename, data)
    raise BookParseError("V1 仅支持 EPUB 和 TXT 文件")


def _parse_epub(filename: str, data: bytes) -> ParsedBook:
    if not data:
        raise BookParseError("文件为空")
    try:
        # EbookLib 0.19 requires a filesystem path. Render's temporary disk is
        # used only during parsing; durable binaries go to private Storage and
        # PostgreSQL keeps only structured data, text, and object metadata.
        with tempfile.NamedTemporaryFile(suffix=".epub") as temporary:
            temporary.write(data)
            temporary.flush()
            book = epub.read_epub(temporary.name, options={"ignore_ncx": False})
    except Exception as exc:
        raise BookParseError(
            "无法解析 EPUB；文件可能损坏、带 DRM 或不是标准 EPUB"
        ) from exc

    title = _metadata_value(book, "title") or PurePosixPath(filename).stem
    author = _metadata_value(book, "creator") or "未知作者"
    toc, toc_titles = _extract_toc(book.toc)
    assets = _extract_assets(book)
    asset_paths = {asset.path for asset in assets}
    cover_path = _find_cover_path(book, asset_paths)

    chapters: list[ParsedChapter] = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if (
            item is None
            or item.get_type() != ebooklib.ITEM_DOCUMENT
            or isinstance(item, epub.EpubNav)
        ):
            continue
        properties = set(getattr(item, "properties", []) or [])
        if "nav" in properties:
            continue
        href = _normalize_asset_path(item.get_name())
        clean_html, plain_text = _sanitize_chapter(
            item.get_content(), href, asset_paths
        )
        if not plain_text.strip():
            continue
        chapter_title = toc_titles.get(_href_key(href)) or _title_from_html(clean_html)
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

    if not chapters:
        raise BookParseError("EPUB 中没有找到可阅读的正文篇章")

    return ParsedBook(
        title=_clean_metadata(title),
        author=_clean_metadata(author),
        format="epub",
        source_filename=filename,
        source_sha256=hashlib.sha256(data).hexdigest(),
        toc=toc,
        chapters=chapters,
        assets=assets,
        cover_asset_path=cover_path,
    )


def _parse_txt(filename: str, data: bytes) -> ParsedBook:
    if not data:
        raise BookParseError("文件为空")
    best = from_bytes(data).best()
    if best is None:
        raise BookParseError("无法识别 TXT 编码")
    text = str(best).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise BookParseError("TXT 中没有正文")

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
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title = match.group(1).strip()
            body = text[match.end() : end].strip()
            sections.append((title, body or title))
    else:
        sections.append(("正文", text))

    chapters: list[ParsedChapter] = []
    toc: list[dict[str, Any]] = []
    for index, (chapter_title, body) in enumerate(sections):
        chapter_html = _txt_to_html(body)
        stable_html, plain_text = _assign_block_ids(chapter_html)
        href = f"txt/chapter-{index + 1}.xhtml"
        chapters.append(
            ParsedChapter(
                chapter_index=index,
                title=chapter_title,
                href=href,
                content_html=stable_html,
                content_text=plain_text,
                word_count=_count_words(plain_text),
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


def _metadata_value(book: epub.EpubBook, field: str) -> str | None:
    values = book.get_metadata("DC", field)
    if not values:
        return None
    value = values[0][0]
    return str(value).strip() if value else None


def _clean_metadata(value: str) -> str:
    return re.sub(
        r"\s+", " ", BeautifulSoup(value, "html.parser").get_text(" ")
    ).strip()


def _extract_toc(raw_toc: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    title_map: dict[str, str] = {}

    def walk(items: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in items or []:
            children: Any = []
            node = item
            if isinstance(item, tuple):
                node = item[0]
                children = item[1]
            label = getattr(node, "title", None) or str(node)
            href = getattr(node, "href", "") or ""
            normalized_href = _normalize_asset_path(urldefrag(href)[0]) if href else ""
            if normalized_href:
                title_map[_href_key(normalized_href)] = _clean_metadata(label)
            result.append(
                {
                    "label": _clean_metadata(label),
                    "href": normalized_href,
                    "children": walk(children),
                }
            )
        return result

    return walk(raw_toc), title_map


def _extract_assets(book: epub.EpubBook) -> list[ParsedAsset]:
    assets: list[ParsedAsset] = []
    for item in book.get_items():
        if item.get_type() not in {ebooklib.ITEM_IMAGE, ebooklib.ITEM_COVER}:
            continue
        path = _normalize_asset_path(item.get_name())
        media_type = (
            item.media_type
            or mimetypes.guess_type(path)[0]
            or "application/octet-stream"
        )
        assets.append(
            ParsedAsset(path=path, media_type=media_type, data=item.get_content())
        )
    return assets


def _find_cover_path(book: epub.EpubBook, asset_paths: set[str]) -> str | None:
    cover_meta = book.get_metadata("OPF", "cover")
    if cover_meta:
        cover_id = cover_meta[0][1].get("content") if len(cover_meta[0]) > 1 else None
        if cover_id:
            item = book.get_item_with_id(cover_id)
            if item:
                candidate = _normalize_asset_path(item.get_name())
                if candidate in asset_paths:
                    return candidate
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        candidate = _normalize_asset_path(item.get_name())
        if candidate in asset_paths:
            return candidate
    for path in asset_paths:
        if "cover" in path.lower():
            return path
    return None


def _sanitize_chapter(
    raw: bytes, chapter_href: str, asset_paths: set[str]
) -> tuple[str, str]:
    soup = BeautifulSoup(raw, "html.parser")
    for tag_name in UNSAFE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    root = soup.body or soup
    for image in root.find_all("img"):
        source = image.get("src", "")
        resolved = _resolve_asset_href(chapter_href, source)
        image.attrs = {
            key: value for key, value in image.attrs.items() if key in {"alt", "title"}
        }
        if resolved in asset_paths:
            image["data-asset-path"] = resolved
        else:
            image.decompose()
    for anchor in root.find_all("a"):
        href = anchor.get("href", "")
        if href.lower().startswith(("javascript:", "data:")):
            anchor.attrs.pop("href", None)
    cleaned = bleach.clean(
        "".join(str(child) for child in root.children),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        strip=True,
        strip_comments=True,
    )
    return _assign_block_ids(cleaned)


def _assign_block_ids(fragment: str) -> tuple[str, str]:
    soup = BeautifulSoup(fragment, "html.parser")
    # Publisher EPUBs often wrap the entire chapter in one or more generic
    # containers. Removing those wrappers keeps IDs paragraph-sized instead
    # of accidentally turning a whole chapter into one annotation block.
    for container in soup.find_all(["div", "section", "article"]):
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
            # The nearest semantic block owns character offsets; nested block
            # containers would make one text position ambiguous.
            continue
        block_number += 1
        block_id = f"b{block_number:06d}"
        tag["id"] = block_id
        tag["data-block-id"] = block_id
        plain = re.sub(r"\s+", " ", tag.get_text("", strip=False)).strip()
        if plain:
            plain_blocks.append(plain)

    # Images or nested structures can leave unowned text nodes. Wrap them so
    # every selectable character belongs to one stable block.
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

    return str(soup), "\n\n".join(plain_blocks)


def _txt_to_html(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    rendered: list[str] = []
    for paragraph in paragraphs:
        value = paragraph.strip()
        if not value:
            continue
        escaped = html.escape(value).replace("\n", "<br>")
        rendered.append(f"<p>{escaped}</p>")
    return "".join(rendered)


def _resolve_asset_href(chapter_href: str, source: str) -> str:
    source = unquote(urldefrag(source or "")[0]).strip()
    if not source or source.startswith(("http://", "https://", "data:")):
        return ""
    return _normalize_asset_path(
        posixpath.join(posixpath.dirname(chapter_href), source)
    )


def _normalize_asset_path(path: str) -> str:
    normalized = posixpath.normpath(unquote(path).replace("\\", "/")).lstrip("/")
    while normalized.startswith("../"):
        normalized = normalized[3:]
    return normalized


def _href_key(href: str) -> str:
    return _normalize_asset_path(urldefrag(href)[0]).lower()


def _title_from_html(fragment: str) -> str | None:
    soup = BeautifulSoup(fragment, "html.parser")
    heading = soup.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    return _clean_metadata(heading.get_text(" ")) if heading else None


def _count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"\b[\w'-]+\b", re.sub(r"[\u3400-\u9fff]", " ", text)))
    return cjk + latin
