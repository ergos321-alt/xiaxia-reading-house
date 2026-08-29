# Phase 3 Test Matrix

## Automated and contract matrix

| Area | Case | Result | Evidence |
|---|---|---|---|
| Migration | 007 additive; no destructive trace rewrite | PASS | static migration regression |
| Feature gates | engine rollback and independent dual-anchor gate | PASS | app/template/loader tests |
| Canonical text | NFC, NBSP, whitespace, soft/zero-width, ruby, footnote marker | PASS | `test_phase3_dual_anchor.py` |
| Bridge | canonical href and real OPF spine mapping | PASS | synthetic + real EPUB builds |
| Bridge | unique quote → exact block/offset | PASS | round-trip tests |
| Bridge | duplicate without context | PASS (rejected) | `locator_mapping_ambiguous` |
| Bridge | prefix/suffix disambiguation | PASS | duplicate quote test |
| Versioning | source/bridge/adapter mismatch | PASS (rejected) | contract tests |
| Annotation | selection stores legacy + engine anchors atomically | PASS | API transaction test |
| Annotation | edit preserves locator; delete removes decoration | PASS | backend + adapter contract |
| Thought | Action stays legacy; lazy locator backfill | PASS | API/static contract |
| Thought | locator failure does not fail Thought | PASS | independent lazy path |
| Replies | both directions remain record-ID based | PASS | full regression suite |
| Preview/Commit | business validation unchanged | PASS | full regression suite |
| Undo | locator included in snapshot/restore | PASS | operation regression |
| Shared stops | legacy algorithm and semantics unchanged | PASS | V2 suite |
| Progress | publication and legacy progress stay separate | PASS | Phase 2/3 tests |
| V2 completion | validated foliate or legacy final position | PASS | completion regression |
| Action | OpenAPI unchanged; locator fields excluded | PASS | hash/static regression |
| Android cleanup | epoch, suppression, removeAllRanges, hard UI hide | PASS (contract) | real device pending |
| Security | private source/bridge and sanitized pinned engine | PASS | existing POC/security suite |

Automated result: `146 passed, 0 failed`.

## Real EPUB bridge matrix

| File | Type represented | Build | Mapped chapters | Exact Web→AI samples | AI→Web observation |
|---|---|---:|---:|---:|---:|
| 卡瓦菲斯诗集＋当你起航前往伊萨卡 | 323 small XHTML / poetry / EPUB2 | PASS | 323/323 | 200/200 | 199 unique; duplicate needs context |
| 唐诗选注 | Chinese text / images / EPUB2 | PASS | 85/85 | 200/200 | 199 unique; duplicate resolved by context |
| 唐诗选（全二册） | poetry / notes / EPUB3 | PASS | 71/71 | 200/200 | 200 unique |
| Known-good legacy source-backed book | historical migration | PENDING — file unavailable | — | — | — |
| Blind EPUB | blind corpus | PENDING — file unavailable | — | — | — |

The three available books are unchanged user files. Synthetic fixtures are not counted as real evidence.

## Browser/device shared-reading matrix

| Device | Reading POC | Phase 3 write | Reload decoration | Thought/reply | Completion | Status |
|---|---:|---:|---:|---:|---:|---|
| Desktop Chrome | user-confirmed PASS | Pending deployed run | Pending | Pending | Pending | PENDING USER VALIDATION |
| Android Chrome | user-confirmed PASS | Pending real device | Pending | Pending | Pending | PENDING USER VALIDATION |
| iPad Safari | user-confirmed PASS | Pending real device | Pending | Pending | Pending | PENDING USER VALIDATION |

## Production device checklist

Run on one text-heavy and one footnote/image-heavy book:

1. Enable both engine and dual-anchor flags.
2. Select a unique sentence, tap **只划线**, verify the toolbar disappears, continue reading.
3. Select another sentence, tap **留一句**, save, and verify toolbar/dialog cleanup.
4. Reload; change font size and flow; rotate; verify warm-brown decorations remain exact.
5. Create a Xiaxia range Thought on the same block; reload and verify blue-grey/shared decoration.
6. Create/edit/delete the user reply and verify Xiaxia reply to the annotation.
7. Edit the annotation note without moving its range; delete it and verify overlay removal.
8. Exercise Preview → Commit and Undo, reload, and verify decoration state.
9. Open a shared-stop card and confirm exact CFI navigation.
10. Follow footnote/backlinks and confirm progress/decorations remain stable.
11. Android: repeat with browser selection handles visible; no floating toolbar may remain.
12. iPad: portrait/landscape, address-bar height changes, bottom clipping, selection drift and Thought marker.
13. Reach 99.5%+, complete the book, then verify back-cover, reflections, letters and stamp.

Final PASS requires this checklist plus one known-good legacy source-backed book and one blind EPUB.
