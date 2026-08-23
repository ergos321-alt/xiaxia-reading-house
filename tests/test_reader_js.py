import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "static/js/reader-utils.js"
READER = ROOT / "static/js/reader.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_reader_pagination_mobile_selection_and_sync_helpers():
    script = f"""
const assert = require('assert');
const utils = require({json.dumps(str(UTILS))});
assert.strictEqual(utils.calculatePageCount(700, 700, 32), 1);
assert.strictEqual(utils.calculatePageCount(1432, 700, 32), 2);
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
