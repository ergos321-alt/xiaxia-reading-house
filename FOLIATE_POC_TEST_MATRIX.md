# foliate-js Controlled POC Test Matrix

Date: 2026-08-28  
Decision gate: **PENDING USER VALIDATION — insufficient qualifying corpus/device evidence**

Legend:

- `PASS`: directly observed for the stated scope.
- `FAIL`: directly observed failure.
- `N/E`: not executed; never counted as PASS.
- `N/A`: publication has no applicable feature.
- `PARTIAL`: useful signal that does not satisfy the full acceptance item.

## A. Real publication matrix

| Field | 卡瓦菲斯诗集＋伊萨卡 | 唐诗选注 | 唐诗选（全二册） |
|---|---|---|---|
| file | 卡瓦菲斯诗集…epub | 唐诗选注…epub | 唐诗选（全二册）…epub |
| size | 712,008 B | 4,306,793 B | 927,996 B |
| epub_version | 2.0 | 2.0 | 3.0 |
| spine_items | 327 | 87 | 72 |
| images | 4 | 181 | 123 |
| fonts | 0 | 0 | 0 |
| nav_type | NCX | NCX | NAV |
| source_mode | Local File | Local File | Local File |
| open_result | PASS (engine-only) | PASS (engine-only) | PASS (engine-only) |
| first_render_ms | N/E | N/E | N/E |
| toc | PASS (basic) | PASS (long TOC) | PASS (71 items) |
| next_prev | PASS (basic) | PASS (basic) | PASS (basic) |
| pagination | PASS (basic) | PASS (basic) | PASS (basic) |
| scroll | PASS (mode switch) | PASS (mode switch) | N/E |
| images | N/E | N/E | N/E |
| internal_links | N/E | N/E | N/E |
| footnotes | N/A by noteref scan | N/A by noteref scan | N/E (2,011 refs available) |
| CFI_create | N/E | N/E | N/E |
| CFI_restore | N/E | N/E | N/E |
| resize | N/E | N/E | N/E |
| font_change | N/E | N/E | N/E |
| orientation | N/E | N/E | N/E |
| browser_memory | N/E | N/E | N/E |
| error | NONE for observed scope | NONE for observed scope | NONE for observed scope |
| workaround | NONE | NONE | NONE |
| full-book qualification | **NO** | **NO** | **NO** |

Observed direct-open results came from a real Desktop Chrome session using the
upstream demo while upstream `main` resolved to the pinned commit. They prove
engine compatibility signals, not the Xiaxia adapter/device matrix.

## B. Required corpus slots

| Slot | Required classification | File | Status |
|---:|---|---|---|
| 1 | previous failure / many XHTML | 卡瓦菲斯诗集＋伊萨卡 | PARTIAL |
| 2 | previous failure / image-heavy | 唐诗选注 | PARTIAL |
| 3 | previous failure / EPUB3 / footnotes | 唐诗选（全二册） | PARTIAL |
| 4 | known legacy success | missing | N/E |
| 5 | known legacy success | missing | N/E |
| 6 | blind EPUB 1 | missing | N/E |
| 7 | blind EPUB 2 | missing | N/E |
| 8 | blind EPUB 3 | missing | N/E |
| 9 | blind EPUB 4 | missing | N/E |
| 10 | additional real EPUB | missing | N/E |

Because slots 4–10 are absent, overall compatibility percentage and blind-book
success rate are **not computable**. They must not be represented as 100% from
the three available books.

## C. Automated POC checks

| Check | Expected | Current status |
|---|---|---|
| feature flag default off | all POC routes 404 | PASS |
| explicit page authorization | browser session required | PASS |
| source authorization | Web session only | PASS |
| signed URL TTL | reject <60 or >300 | PASS |
| signed URL response | no-store; no service key | PASS |
| non-EPUB stored source | explicit 422 | PASS |
| local-file isolation | no importer/DB/Storage dependency | PASS by code/unit test |
| vendor pin | exact SHA/date/license/hashes | PASS |
| upstream patch count | zero | PASS |
| production reader replacement | none | PASS |
| OpenAPI change | none | PASS |
| production annotation write | none | PASS by code/unit test |
| CFI create/restore browser | actual browser | N/E |
| link event browser | actual browser | N/E |
| decoration browser | actual browser | N/E |
| hostile EPUB | five script flags remain false | N/E |

POC-specific Flask tests: `11 passed`. Complete project suite: `119 passed`.
Python compilation and JavaScript syntax checks also passed.

## D. Private Storage matrix

| Test | Expected | Status |
|---|---|---|
| signed URL creation | authenticated, 180 s default | PASS (mocked provider) |
| TTL floor/ceiling | 60–300 seconds | PASS |
| CORS | browser can read signed response | N/E on live Supabase |
| Range request | `206` + `Content-Range` | N/E on live Supabase |
| Accept-Ranges | header recorded | N/E on live Supabase |
| Content-Length | header recorded | N/E on live Supabase |
| engine remote random access | no full download | FAIL for pinned vendor design |
| Blob fallback | full fetch → File → engine | IMPLEMENTED; live signed path N/E |
| service role secrecy | absent from HTML/JS/JSON | PASS |

## E. Manual Desktop Chrome checklist

For each corpus file:

1. Open `/reader-engine-poc` and choose the original EPUB.
2. Confirm title, first page and TOC; export initial result.
3. Use Prev/Next for at least ten page turns and cross two chapter boundaries.
4. Switch to Scroll, read through a chapter transition, then switch back.
5. Change font size twice and resize from wide desktop to tablet width.
6. Select text; confirm debug contains quote, href, section index and CFI.
7. Add Memory Highlight, refresh/reopen the same file, then Restore CFI.
8. Exercise same-file and cross-file internal links where present.
9. For noteref books, open same-file/cross-file note and use Link Back.
10. Visit ten sections including an image-heavy section; sample memory before,
    after load, after ten sections and after Close.
11. Export the JSON result. `workaround` must remain `NONE`.

## F. Android Chrome checklist

1. Repeat local File open in portrait and landscape.
2. Test pagination swipe/tap for ten pages and a chapter boundary.
3. Switch Scroll/Pagination twice; rotate once in each mode.
4. Change font size and confirm location remains near the same text.
5. Select text and verify quote/href/CFI debug.
6. Add Memory Highlight and Restore CFI after reopening the same source.
7. Exercise one internal chapter link and one footnote/backlink when available.
8. Confirm images load after scroll and no section DOM accumulates visibly.
9. Close publication and export the result.

Do not mark Android PASS without real-device execution.

## G. iPad Safari checklist

For at least one text-heavy, image-heavy and footnote-heavy real EPUB:

1. Initial load in portrait; confirm first page has no bottom clipping.
2. Page ten times and cross a chapter boundary.
3. Rotate to landscape; confirm the current location remains stable.
4. Collapse/expand Safari address/tool bars and confirm viewport/iframe height.
5. Enter split view and change width; confirm reflow and no missing final line.
6. Increase/decrease font size; confirm reflow without blank-page explosion.
7. Switch to Scroll and cross a chapter boundary; switch back to Pagination.
8. Open an image-heavy section and wait for images; confirm layout settles.
9. Select text; record quote/href/CFI and add Memory Highlight.
10. Refresh/reopen, Restore CFI, then rotate again and restore once more.
11. Exercise footnote → note target → Link Back.
12. Read ten sections, Close publication and export result JSON.

Observe and record:

- bottom-line clipping;
- dynamic toolbar viewport height;
- iframe sizing;
- selection drift;
- CFI restore drift;
- linear memory growth.

Do not mark iPad PASS until a real iPad result is attached.

## H. Security checklist

Generate the fixture:

```bash
python scripts/create_foliate_security_fixture.py /tmp/foliate-security.epub
```

Open it through the Xiaxia POC and run the browser automation. Required:

- external manifest script: not executed;
- inline script: not executed;
- inline onload handler: not executed;
- JavaScript URL: not executed;
- iframe `srcdoc` script: not executed;
- external link: blocked and logged;
- service role key: absent from network/HTML/JS;
- publication cannot access Reading House application state.

## I. Commands

```bash
python -m pytest -q
python -m py_compile app.py reader_engine_poc.py storage.py
node --check static/js/foliate-poc-adapter.js
node --check static/js/foliate-poc.js
node --check tests/foliate_poc_browser.mjs
python scripts/inspect_foliate_corpus.py /path/to/corpus/*.epub
```

Optional real browser automation after deploying/enabling the POC:

```bash
FOLIATE_POC_E2E_BASE_URL=https://your-service.example \
FOLIATE_POC_E2E_PASSWORD='<test private password>' \
FOLIATE_POC_E2E_FILES='["/absolute/book1.epub","/absolute/book2.epub"]' \
node tests/foliate_poc_browser.mjs > foliate-results.json
```

The password must remain local to the operator's environment and must never be
committed or pasted into a report.
