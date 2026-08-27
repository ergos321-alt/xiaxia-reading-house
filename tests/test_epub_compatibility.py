from __future__ import annotations

import random
import time
import tracemalloc
import zipfile
from io import BytesIO

import pytest

from epub_parser import BookParseError, parse_uploaded_book


def make_epub(
    *,
    version="3.0",
    chapters=None,
    spine=None,
    nav=None,
    ncx=None,
    images=None,
    image_manifest=None,
    title="兼容性测试",
    include_title=True,
    extra_manifest="",
    extra_metadata="",
    guide="",
    container_path="OEBPS/content.opf",
):
    chapters = chapters or {"Text/one.xhtml": "<h1>第一章</h1><p>正文。</p>"}
    spine = list(chapters) if spine is None else spine
    images = images or {}
    image_manifest = image_manifest or {
        path: ("image/svg+xml" if path.endswith(".svg") else "image/png")
        for path in images
    }
    ids = {path: f"doc{index}" for index, path in enumerate(chapters)}
    manifest = [
        f'<item id="{ids[path]}" href="{path}" media-type="application/xhtml+xml"/>'
        for path in chapters
    ]
    manifest.extend(
        f'<item id="img{index}" href="{path}" media-type="{media}"/>'
        for index, (path, media) in enumerate(image_manifest.items())
    )
    spine_toc = ""
    if nav is not None:
        manifest.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
    if ncx is not None:
        manifest.append(
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        )
        spine_toc = ' toc="ncx"'
    metadata = (
        (f"<dc:title>{title}</dc:title>" if include_title else "")
        + "<dc:creator>测试作者</dc:creator>"
        + extra_metadata
    )
    spine_xml = "".join(
        f'<itemref idref="{ids[path]}"/>' for path in spine if path in ids
    )
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
    <package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="{version}">
      <metadata>{metadata}</metadata>
      <manifest>{''.join(manifest)}{extra_manifest}</manifest>
      <spine{spine_toc}>{spine_xml}</spine>
      {guide}
    </package>'''
    container = f'''<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="{container_path}" media-type="application/oebps-package+xml"/></rootfiles>
    </container>'''
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        for path, body in chapters.items():
            archive.writestr(f"OEBPS/{path.replace('%20', ' ')}", body)
        for path, payload in images.items():
            archive.writestr(f"OEBPS/{path}", payload)
        if nav is not None:
            archive.writestr("OEBPS/nav.xhtml", nav)
        if ncx is not None:
            archive.writestr("OEBPS/toc.ncx", ncx)
    return output.getvalue()


def nav_for(items):
    links = "".join(f'<li><a href="{href}">{title}</a></li>' for href, title in items)
    return f'<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>{links}</ol></nav></body></html>'


def ncx_for(items):
    points = "".join(
        f'<navPoint id="n{index}"><navLabel><text>{title}</text></navLabel><content src="{href}"/></navPoint>'
        for index, (href, title) in enumerate(items)
    )
    return f'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>{points}</navMap></ncx>'


def test_epub2_ncx_and_epub3_nav_are_both_supported():
    epub2 = make_epub(
        version="2.0",
        ncx=ncx_for([("Text/one.xhtml#start", "NCX 标题")]),
    )
    epub3 = make_epub(
        nav=nav_for([("Text/one.xhtml#start", "NAV 标题")]),
    )
    assert parse_uploaded_book("epub2.epub", epub2).chapters[0].title == "NCX 标题"
    assert parse_uploaded_book("epub3.epub", epub3).chapters[0].title == "NAV 标题"


def test_nav_wins_when_nav_and_ncx_order_differ():
    chapters = {
        "Text/a.xhtml": "<p>A</p>",
        "Text/b.xhtml": "<p>B</p>",
    }
    parsed = parse_uploaded_book(
        "mixed.epub",
        make_epub(
            chapters=chapters,
            nav=nav_for([("Text/b.xhtml", "B"), ("Text/a.xhtml", "A")]),
            ncx=ncx_for([("Text/a.xhtml", "A"), ("Text/b.xhtml", "B")]),
        ),
    )
    assert [chapter.href for chapter in parsed.chapters] == ["Text/b.xhtml", "Text/a.xhtml"]
    assert "both_nav_and_ncx_present" in parsed.warnings


def test_broken_toc_falls_back_to_spine_and_duplicate_spine_is_deduped():
    parsed = parse_uploaded_book(
        "broken-toc.epub",
        make_epub(
            spine=["Text/one.xhtml", "Text/one.xhtml"],
            nav=nav_for([("missing.xhtml", "坏目录")]),
        ),
    )
    assert len(parsed.chapters) == 1
    assert parsed.chapters[0].href == "Text/one.xhtml"


def test_manifest_documents_are_final_fallback_when_spine_is_missing():
    parsed = parse_uploaded_book("no-spine.epub", make_epub(spine=[]))
    assert [chapter.href for chapter in parsed.chapters] == ["Text/one.xhtml"]


def test_broken_container_falls_back_to_discovered_opf():
    payload = make_epub(container_path="OEBPS/missing.opf")
    parsed = parse_uploaded_book("container-fallback.epub", payload)
    assert len(parsed.chapters) == 1
    assert "broken_container_fallback" in parsed.warnings


def test_url_encoded_case_variant_paths_fragments_and_metadata_fallback():
    chapters = {
        "Text/Chapter%201.xhtml": '<section id="开 头"><p>正文</p></section>',
    }
    parsed = parse_uploaded_book(
        "文件名书名.epub",
        make_epub(
            chapters=chapters,
            spine=["Text/Chapter%201.xhtml"],
            include_title=False,
            nav=nav_for([("text/chapter%201.xhtml#%E5%BC%80%20%E5%A4%B4", "编码章节")]),
        ),
    )
    assert parsed.title == "文件名书名"
    assert parsed.chapters[0].title == "编码章节"
    assert 'data-epub-fragment="开 头"' in parsed.chapters[0].content_html


def test_ruby_poem_table_footnote_and_malformed_xhtml_degrade_to_readable_blocks():
    body = '''\ufeff<html><body><section><p class="stanza">春眠<br>不觉晓</p>
    <p><ruby>漢<rt>かん</rt></ruby></p><blockquote>引用</blockquote>
    <table><tr><td>表格</td></tr></table><p><b>没有闭合
    <a epub:type="noteref" href="#fn1">[1]</a>
    <aside epub:type="footnote" id="fn1"><p>脚注正文<a href="#back" class="backlink">↩</a></p></aside>
    </section></body></html>'''
    parsed = parse_uploaded_book("recover.epub", make_epub(chapters={"Text/one.xhtml": body}))
    html_value = parsed.chapters[0].content_html
    assert "春眠" in parsed.chapters[0].content_text
    assert "<ruby>" in html_value and "<rt>" in html_value
    assert "data-epub-link-kind=\"footnote\"" in html_value
    assert "data-epub-fragment=\"fn1\"" in html_value
    assert 'data-block-id="b000001"' in html_value


def test_internal_same_file_cross_file_backlink_and_missing_target_are_preserved():
    chapters = {
        "Text/main.xhtml": '''<p id="back">正文<a epub:type="noteref" href="notes.xhtml#n1">[1]</a>
        <a href="#local">本页</a><a href="missing.xhtml#x">缺失</a></p><p id="local">本页目标</p>''',
        "Text/notes.xhtml": '<aside id="n1" epub:type="footnote"><p>注释<a href="main.xhtml#back" class="backlink">↩</a></p></aside>',
    }
    parsed = parse_uploaded_book("links.epub", make_epub(chapters=chapters))
    main = parsed.chapters[0].content_html
    notes = parsed.chapters[1].content_html
    assert 'data-epub-target-href="Text/notes.xhtml"' in main
    assert 'data-epub-target-href="Text/main.xhtml"' in main
    assert 'data-epub-target-href="Text/missing.xhtml"' in main
    assert 'data-epub-link-kind="backlink"' in notes


def test_missing_cover_broken_image_css_and_fonts_do_not_block_text():
    parsed = parse_uploaded_book(
        "assets.epub",
        make_epub(
            chapters={"Text/one.xhtml": '<p>正文</p><img src="../Images/missing.png">'},
            extra_manifest='''<item id="css" href="style.css" media-type="text/css"/>
            <item id="font" href="font.woff" media-type="font/woff"/>
            <item id="cover" href="Images/missing.png" media-type="image/png" properties="cover-image"/>''',
        ),
    )
    assert parsed.cover_asset_path is None
    assert parsed.assets == []
    assert "正文" in parsed.chapters[0].content_text
    assert "<img" not in parsed.chapters[0].content_html


def test_only_referenced_images_are_loaded_and_image_only_chapter_is_allowed():
    rng = random.Random(7)
    large = rng.randbytes(1_000_000)
    parsed = parse_uploaded_book(
        "images.epub",
        make_epub(
            chapters={"Text/one.xhtml": '<img src="../Images/used.png" alt="图">'},
            images={"Images/used.png": large, "Images/unused.png": large},
        ),
    )
    assert [asset.path for asset in parsed.assets] == ["Images/used.png"]
    assert len(parsed.assets[0].data) == len(large)
    assert len(parsed.chapters) == 1


def test_image_only_book_with_missing_image_is_not_saved_as_empty_book():
    with pytest.raises(BookParseError) as raised:
        parse_uploaded_book(
            "missing-image-only.epub",
            make_epub(
                chapters={
                    "Text/one.xhtml": '<img src="../Images/missing.png" alt="缺图">'
                }
            ),
        )
    assert raised.value.code == "no_readable_content"


def test_empty_chapters_are_skipped_and_no_readable_content_has_stable_code():
    parsed = parse_uploaded_book(
        "empty.epub",
        make_epub(chapters={"Text/empty.xhtml": "<p> </p>", "Text/good.xhtml": "<p>正文</p>"}),
    )
    assert len(parsed.chapters) == 1
    with pytest.raises(BookParseError) as raised:
        parse_uploaded_book("none.epub", make_epub(chapters={"Text/empty.xhtml": "<p> </p>"}))
    assert raised.value.code == "no_readable_content"


@pytest.mark.parametrize("chapter_count", [100, 300, 500])
def test_many_small_chapters_scale_linearly(chapter_count):
    chapters = {
        f"Text/c{index:04d}.xhtml": f"<p>第 {index} 首诗。</p>"
        for index in range(chapter_count)
    }
    payload = make_epub(chapters=chapters)
    started = time.perf_counter()
    parsed = parse_uploaded_book(f"poems-{chapter_count}.epub", payload)
    duration = time.perf_counter() - started
    assert len(parsed.chapters) == chapter_count
    assert duration < 5


def test_five_hundred_chapter_peak_memory_stays_bounded():
    chapters = {
        f"Text/c{index:04d}.xhtml": f"<p>第 {index} 首诗。</p>"
        for index in range(500)
    }
    payload = make_epub(chapters=chapters)
    tracemalloc.start()
    parsed = parse_uploaded_book("poems-500-memory.epub", payload)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(parsed.chapters) == 500
    assert peak < 96 * 1024 * 1024


def test_invalid_archive_and_missing_opf_have_diagnostic_codes():
    with pytest.raises(BookParseError) as invalid:
        parse_uploaded_book("bad.epub", b"not a zip")
    assert invalid.value.code == "unsupported_archive"
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    with pytest.raises(BookParseError) as no_opf:
        parse_uploaded_book("bad.epub", output.getvalue())
    assert no_opf.value.code == "invalid_epub"


def test_txt_regression_keeps_stable_block_ids():
    parsed = parse_uploaded_book("诗集.txt", "第一章 春\n\n春眠不觉晓".encode())
    assert parsed.format == "txt"
    assert parsed.chapters[0].content_html.count('data-block-id="b000001"') == 1
