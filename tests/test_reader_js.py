import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "static/js/reader-utils.js"
READER = ROOT / "static/js/reader.js"
LIBRARY = ROOT / "static/js/library.js"
FOLIATE_READER = ROOT / "static/js/foliate-reader.js"
FOLIATE_ADAPTER = ROOT / "static/js/foliate-reader-adapter.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_reader_pagination_mobile_selection_and_sync_helpers():
    script = f"""
const assert = require('assert');
const utils = require({json.dumps(str(UTILS))});
assert.strictEqual(utils.calculatePageCount(700, 700, 32), 1);
assert.strictEqual(utils.calculatePageCount(1432, 700, 32), 2);
assert.strictEqual(utils.calculatePaginationHeight({{
  viewportTop: 0,
  viewportHeight: 900,
  contentTop: 122,
  shellBottom: 900,
  shellPaddingBottom: 48,
  guard: 10,
}}), 720);
assert.strictEqual(utils.calculatePaginationHeight({{
  viewportTop: 24,
  viewportHeight: 1000,
  contentTop: 120,
  shellBottom: 1024,
  shellPaddingBottom: 54,
  guard: 10,
}}), 840);
assert.strictEqual(utils.swipeDirection({{x: 300, y: 100}}, {{x: 180, y: 105}}, false), 'next');
assert.strictEqual(utils.swipeDirection({{x: 180, y: 100}}, {{x: 300, y: 105}}, false), 'previous');
assert.strictEqual(utils.swipeDirection({{x: 300, y: 100}}, {{x: 180, y: 105}}, true), null);
const mobile = utils.selectionMenuPosition({{
  rect: {{left: 20, top: 400, width: 120, bottom: 430}},
  viewport: {{offsetLeft: 0, offsetTop: 200, width: 412, height: 380, bottomInset: 12}},
  mobile: true,
}});
assert.strictEqual(mobile.placement, 'mobile-below');
assert.ok(mobile.left >= 90 && mobile.left <= 206);
assert.ok(mobile.top >= 200 && mobile.top < 580);
const first = utils.syncSignature([{{id: 'a', updated_at: '1'}}], []);
const changed = utils.syncSignature([{{id: 'a', updated_at: '2', xiaxia_response: 'new'}}], []);
assert.notStrictEqual(first, changed);
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_android_selection_strategy_is_wired_without_disabling_native_selection():
    source = READER.read_text(encoding="utf-8")
    style = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
    for event_name in (
        '"selectionchange"',
        '"touchend"',
        '"pointerup"',
        '"contextmenu"',
    ):
        assert event_name in source
    assert "cloneRange()" in source
    assert "visualViewport" in source
    assert "state.savedSelection" in source
    assert "captureSelection();" in source
    assert "function clearSelectionInteraction()" in source
    assert "state.selectionSubmitting = true" in source
    assert "state.selectionUiVisible = false" in source
    assert "state.selectionEpoch += 1" in source
    assert "state.selectionSuppressedUntil = Date.now() + 900" in source
    assert "clearSelectionInteraction();" in source
    assert "window.getSelection()?.removeAllRanges()" in source
    assert 'selectionMenu.style.setProperty("display", "none", "important")' in source
    assert 'selectionMenu.style.removeProperty("top")' in source
    assert ".selection-menu[hidden], .selection-menu.is-hidden { display: none !important; }" in style
    assert 'placement: "mobile-below"' in (ROOT / "static/js/reader-utils.js").read_text(encoding="utf-8")
    assert "user-select: none" not in style
    assert "-webkit-user-select: none" not in style


def test_reader_keeps_exact_anchor_for_both_scroll_and_pagination_jump():
    source = READER.read_text(encoding="utf-8")
    assert "start_block_id" in source
    assert "start_offset" in source
    assert "end_block_id" in source
    assert "end_offset" in source
    assert 'state.mode === "paginated"' in source
    assert "pageForElement(target)" in source
    assert "getBoundingClientRect().top + window.scrollY" in source
    assert "selected_text" in source  # fallback only when exact coordinates fail


def test_reader_ui_refresh_keeps_navigation_and_actions_on_existing_controls():
    source = READER.read_text(encoding="utf-8")
    template = (ROOT / "templates/reader.html").read_text(encoding="utf-8")

    for control_id in (
        "reading-mode",
        "toc-button",
        "font-down",
        "font-up",
        "edit-annotation",
        "delete-annotation",
        "thought-reply-edit",
        "thought-reply-delete",
    ):
        assert f'id="{control_id}"' in template
    assert 'readerMenuButton.addEventListener("click", toggleReaderMenu)' in source
    assert 'traceMenuButton.addEventListener("click", toggleTraceActions)' in source
    assert 'pageStatusTimer = setTimeout(() => hidePageStatus(false), 1100)' in source
    assert 'modeButton.addEventListener("click"' in source
    assert 'tocButton.addEventListener("click"' in source


def test_paginated_reader_reflows_for_webkit_fonts_and_visual_viewport():
    source = READER.read_text(encoding="utf-8")
    style = (ROOT / "static/css/style.css").read_text(encoding="utf-8")

    assert "calculatePaginationHeight" in source
    assert 'body.style.setProperty("--reader-viewport-height"' in source
    assert 'window.addEventListener("orientationchange", delayedResize' in source
    assert "document.fonts?.ready" in source
    assert 'document.fonts?.addEventListener?.("loadingdone", delayedResize)' in source
    assert "overflow-x: auto" in style
    assert "overflow-y: hidden" in style
    assert "scrollbar-width: none" in style


def test_library_distinguishes_killed_worker_from_json_storage_failure():
    source = LIBRARY.read_text(encoding="utf-8")

    assert '502: {' in source
    assert 'error: "import_worker_terminated"' in source
    assert 'error: "import_resource_exhausted"' in source
    assert 'error: "epub_import_timeout"' in source
    assert "const diagnostic = data.error" in source
    assert 'url === "/api/books" && options.method === "POST"' in source


def test_foliate_mobile_selection_uses_stable_snapshot_and_blocks_paginator_only():
    reader = FOLIATE_READER.read_text(encoding="utf-8")
    adapter = FOLIATE_ADAPTER.read_text(encoding="utf-8")

    assert "pendingSelectionSnapshot = structuredClone(savedSelection)" in reader
    assert "saveAnnotation(noteText.value.trim(), pendingSelectionSnapshot)" in reader
    assert "adapter.clearBrowserSelection()" in reader
    assert "noteDialog.open" in reader
    assert "save_post_started" in reader
    assert "save_persisted" in reader
    assert "decoration_deferred" in reader

    assert "selectionchange', selectionChanged, { capture: true }" in adapter
    assert "touchmove', blockPaginatorTouchMove, { capture: true, passive: true }" in adapter
    assert "event.stopImmediatePropagation()" in adapter
    assert "Do not preventDefault()" in adapter
    assert "selection_page_moved" in adapter
    assert "start_section_index" in adapter and "end_section_index" in adapter
    assert "selection.type !== 'Range'" in adapter
    assert "range.startContainer?.ownerDocument" in adapter
    assert "range.endContainer?.ownerDocument" in adapter


def test_foliate_diagnostics_are_opt_in_and_rebuild_only_renderer_locators():
    reader = FOLIATE_READER.read_text(encoding="utf-8")
    style = (ROOT / "static/css/reader-engine.css").read_text(encoding="utf-8")

    assert "selection_debug') === '1'" in reader
    assert "Selection diagnostics" in reader
    assert "复制诊断 JSON" in reader
    assert "rebuild_all: true" in reader
    assert "__readingHouseSelectionDebug" in reader
    assert ".selection-diagnostics" in style


def test_chapter_thought_uses_publication_section_resource_identity():
    reader = FOLIATE_READER.read_text(encoding="utf-8")
    adapter = FOLIATE_ADAPTER.read_text(encoding="utf-8")

    assert "adapter.hrefsIdentifySameSection(" in reader
    assert "item.chapter_href === href" not in reader
    assert "hrefIdentifiesSection(href, index)" in adapter
    assert "getElementsByTagNameNS?.('*', 'item')" in adapter
