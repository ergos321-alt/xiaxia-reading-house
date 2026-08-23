from pathlib import Path

from ebooklib import epub

from epub_parser import BookParseError, parse_uploaded_book


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
)


def build_epub(tmp_path: Path) -> bytes:
    book = epub.EpubBook()
    book.set_identifier("xiaxia-test-book")
    book.set_title("测试之书")
    book.set_language("zh-CN")
    book.add_author("测试作者")
    book.set_cover("images/cover.png", PNG_BYTES)

    inside = epub.EpubItem(
        uid="inside-image",
        file_name="images/inside.png",
        media_type="image/png",
        content=PNG_BYTES,
    )
    chapter = epub.EpubHtml(
        title="第一章 相遇", file_name="chapters/one.xhtml", lang="zh-CN"
    )
    chapter.content = """
        <h1>第一章 相遇</h1>
        <p>这是第一段<strong>正文</strong>。</p>
        <script>alert('no')</script>
        <p>这是第二段。</p>
        <img src="../images/inside.png" alt="插图">
    """
    book.add_item(inside)
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (epub.Link("chapters/one.xhtml", "第一章 相遇", "chapter-one"),)
    book.spine = ["nav", chapter]
    target = tmp_path / "fixture.epub"
    epub.write_epub(target, book)
    return target.read_bytes()


def build_cover_variant_epub(
    tmp_path: Path, *, cover_mode: str, title: str = "测试之书"
) -> bytes:
    book = epub.EpubBook()
    book.set_identifier(f"xiaxia-{cover_mode}")
    book.set_title(title)
    book.set_language("zh-CN")
    book.add_author("测试作者")
    cover = epub.EpubItem(
        uid=f"{cover_mode}-cover",
        file_name=f"images/{cover_mode}.png",
        media_type="image/png",
        content=PNG_BYTES,
    )
    if cover_mode == "epub3":
        cover.properties = ["cover-image"]
    else:
        book.add_metadata(
            "OPF",
            "cover",
            "",
            {"name": "cover", "content": cover.id},
        )
    chapter = epub.EpubHtml(title="第一章", file_name="text/one.xhtml", lang="zh-CN")
    chapter.content = "<h1>第一章</h1><p>用于测试封面的正文。</p>"
    book.add_item(cover)
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (epub.Link("text/one.xhtml", "第一章", "one"),)
    book.spine = ["nav", chapter]
    target = tmp_path / f"{cover_mode}.epub"
    epub.write_epub(target, book)
    return target.read_bytes()


def test_epub_metadata_spine_images_and_stable_blocks(tmp_path):
    parsed = parse_uploaded_book("测试.epub", build_epub(tmp_path))

    assert parsed.title == "测试之书"
    assert parsed.author == "测试作者"
    assert parsed.format == "epub"
    assert parsed.cover_asset_path == "images/cover.png"
    assert len(parsed.chapters) == 1
    assert parsed.chapters[0].title == "第一章 相遇"
    assert 'data-block-id="b000001"' in parsed.chapters[0].content_html
    assert 'data-asset-path="images/inside.png"' in parsed.chapters[0].content_html
    assert "script" not in parsed.chapters[0].content_html
    assert "这是第一段正文" in parsed.chapters[0].content_text
    assert {asset.path for asset in parsed.assets} >= {
        "images/cover.png",
        "images/inside.png",
    }


def test_epub3_manifest_cover_image_is_detected(tmp_path):
    parsed = parse_uploaded_book(
        "EPUB3封面测试.epub",
        build_cover_variant_epub(tmp_path, cover_mode="epub3"),
    )
    assert parsed.cover_asset_path == "images/epub3.png"


def test_epub2_opf_cover_metadata_is_detected(tmp_path):
    parsed = parse_uploaded_book(
        "EPUB2封面测试.epub",
        build_cover_variant_epub(tmp_path, cover_mode="epub2"),
    )
    assert parsed.cover_asset_path == "images/epub2.png"


def test_abnormally_long_metadata_title_falls_back_to_filename(tmp_path):
    promotional_title = "这是宣传文案" * 50
    parsed = parse_uploaded_book(
        "真正的书名.epub",
        build_cover_variant_epub(tmp_path, cover_mode="epub3", title=promotional_title),
    )
    assert parsed.title == "真正的书名"


def test_txt_chapter_split_and_encoding():
    raw = (
        "前面的话\n\n第一章 开始\n第一段。\n\n第二段。\n\n第二章 继续\n下一章。".encode(
            "utf-8"
        )
    )
    parsed = parse_uploaded_book("随笔.txt", raw)

    assert parsed.format == "txt"
    assert [chapter.title for chapter in parsed.chapters] == [
        "前言",
        "第一章 开始",
        "第二章 继续",
    ]
    assert all("data-block-id" in chapter.content_html for chapter in parsed.chapters)


def test_rejects_formats_outside_v1():
    try:
        parse_uploaded_book("book.pdf", b"not-a-pdf")
    except BookParseError as exc:
        assert "EPUB" in str(exc) and "TXT" in str(exc)
    else:
        raise AssertionError("PDF must remain outside the V1 boundary")
