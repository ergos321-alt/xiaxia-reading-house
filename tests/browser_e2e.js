"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { chromium, webkit } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const BOOK_ID = "11111111-1111-4111-8111-111111111111";
const CHAPTER_ID = "22222222-2222-4222-8222-222222222222";
const ORIGIN = "http://reading-house.test";

function renderedTemplate(filename) {
  let source = fs.readFileSync(path.join(ROOT, "templates", filename), "utf8");
  source = source.replace(
    /\{\{ url_for\('static', filename='([^']+)'\) \}\}/g,
    "/static/$1",
  );
  source = source.replace(
    /\{\{ url_for\('reader_page', book_id=book_id\) \}\}/g,
    `/reader/${BOOK_ID}`,
  );
  source = source.replace(
    /\{\{ url_for\('annotations_overview_page', book_id=book_id\) \}\}/g,
    `/reader/${BOOK_ID}/annotations`,
  );
  source = source.replace(
    /\{\{ url_for\('memories\.after_reading_page', book_id=book_id\) \}\}/g,
    `/reader/${BOOK_ID}/after-reading`,
  );
  source = source.replace(/\{\{ url_for\('library_page'\) \}\}/g, "/library");
  source = source.replace(/\{\{ book_id \}\}/g, BOOK_ID);
  assert.ok(!source.includes("{{"), `Unrendered template expression in ${filename}`);
  return source;
}

function chapterHtml(paragraphCount) {
  const sentence = "两个人沿着同一页慢慢读下去，纸页边缘留下了两种不同的笔迹。";
  return Array.from({ length: paragraphCount }, (_, index) => (
    `<p id="b${String(index + 1).padStart(6, "0")}" data-block-id="b${String(index + 1).padStart(6, "0")}">` +
    `${sentence.repeat(index % 3 === 0 ? 4 : 2)}</p>`
  )).join("");
}

async function installReadingHouseRoutes(context, { paragraphs = 4 } = {}) {
  const requestLog = [];
  const annotations = [];
  const readerHtml = renderedTemplate("reader.html");
  const afterReadingHtml = renderedTemplate("after_reading.html");

  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    requestLog.push(`${request.method()} ${pathname}`);

    if (pathname.startsWith("/static/")) {
      const localPath = path.join(ROOT, pathname.slice(1));
      const extension = path.extname(localPath);
      const contentType = extension === ".css" ? "text/css" : "text/javascript";
      await route.fulfill({ status: 200, contentType, body: fs.readFileSync(localPath) });
      return;
    }
    if (pathname === `/reader/${BOOK_ID}`) {
      await route.fulfill({ status: 200, contentType: "text/html", body: readerHtml });
      return;
    }
    if (pathname === `/reader/${BOOK_ID}/after-reading`) {
      await route.fulfill({ status: 200, contentType: "text/html", body: afterReadingHtml });
      return;
    }
    if (request.method() === "GET" && pathname === `/api/books/${BOOK_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          book: {
            id: BOOK_ID,
            title: "共同读完的一本书",
            author: "测试作者",
            chapters: [{ id: CHAPTER_ID, chapter_index: 0, title: "最后一章" }],
            progress: null,
          },
        }),
      });
      return;
    }
    if (request.method() === "GET" && pathname === `/api/books/${BOOK_ID}/chapters/${CHAPTER_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          chapter: {
            id: CHAPTER_ID,
            chapter_index: 0,
            title: "最后一章",
            content_html: chapterHtml(paragraphs),
          },
        }),
      });
      return;
    }
    if (request.method() === "GET" && pathname === `/api/books/${BOOK_ID}/chapters/${CHAPTER_ID}/annotations`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ annotations, xiaxia_thoughts: [] }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/annotations") {
      const submitted = request.postDataJSON();
      const annotation = {
        id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(annotations.length + 1).padStart(12, "0")}`,
        ...submitted,
        owner: "user",
        content_type: "user_annotation",
        status: "pending",
        created_at: "2026-08-24T12:00:00+00:00",
        updated_at: "2026-08-24T12:00:00+00:00",
      };
      annotations.push(annotation);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ annotation }),
      });
      return;
    }
    if (request.method() === "PUT" && pathname === `/api/books/${BOOK_ID}/progress`) {
      const progress = request.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ progress: { ...progress, updated_at: "2026-08-24T12:00:00+00:00" } }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === `/api/books/${BOOK_ID}/completion`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          completion: {
            user_completed: true,
            xiaxia_completed: true,
            shared_completed: true,
          },
        }),
      });
      return;
    }
    if (request.method() === "GET" && pathname === `/api/books/${BOOK_ID}/back-cover`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          book: { id: BOOK_ID, title: "共同读完的一本书", author: "测试作者" },
          access_state: "open",
          memory_state: "waiting_for_user_reflection",
          completion: {
            user_completed: true,
            xiaxia_completed: true,
            shared_completed: true,
            user_completed_at: "2026-08-23T12:00:00+00:00",
            xiaxia_completed_at: "2026-08-24T12:00:00+00:00",
            shared_completed_at: "2026-08-24T12:00:00+00:00",
          },
          reflections: {
            revealed: false,
            user: { owner: "user", submitted: false },
            xiaxia: { owner: "xiaxia", submitted: false },
          },
          shared_stops: { count: 0, preview: [] },
          letters: [],
          timeline: [],
          stamp: {
            visible: true,
            text: "一起读过",
            completed_month: "2026.08",
            completed_date: "2026.08.24",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ error: "browser_fixture_not_found", pathname }),
    });
  });
  return { requestLog, annotations };
}

async function selectParagraphText(page, paragraphIndex) {
  await page.evaluate((index) => {
    const paragraph = document.querySelectorAll("#chapter-content [data-block-id]")[index];
    const textNode = paragraph.firstChild;
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, Math.min(8, textNode.nodeValue.length));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
    paragraph.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true,
      pointerType: "touch",
    }));
  }, paragraphIndex);
  await page.locator("#selection-menu").waitFor({ state: "visible" });
}

async function assertSelectionFinished(page) {
  await page.waitForTimeout(850);
  assert.strictEqual(await page.locator("#selection-menu").isHidden(), true);
  assert.strictEqual(await page.evaluate(() => window.getSelection().isCollapsed), true);
  const placement = await page.locator("#selection-menu").getAttribute("data-placement");
  assert.strictEqual(placement, null);
}

async function runFlowScenario() {
  const browser = await chromium.launch({
    headless: true,
    executablePath: chromium.executablePath(),
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 412, height: 915 },
      isMobile: true,
      hasTouch: true,
    });
    const fixture = await installReadingHouseRoutes(context, { paragraphs: 4 });
    const page = await context.newPage();
    await page.addInitScript(() => localStorage.setItem("xiaxia-reader-mode", "scroll"));
    await page.goto(`${ORIGIN}/reader/${BOOK_ID}`);
    await page.locator("#chapter-content [data-block-id]").first().waitFor();

    await selectParagraphText(page, 0);
    await page.locator("#note-selection").click();
    await page.locator("#note-text").fill("我想把这一句留在书页旁。");
    await page.locator("#save-note").click();
    await assertSelectionFinished(page);

    await selectParagraphText(page, 1);
    await page.locator("#highlight-selection").click();
    await assertSelectionFinished(page);
    assert.strictEqual(fixture.annotations.length, 2);
    assert.strictEqual(fixture.annotations[0].start_block_id, "b000001");
    assert.strictEqual(fixture.annotations[1].start_block_id, "b000002");

    await page.locator("#finish-book-entry").waitFor({ state: "visible" });
    await page.locator("#finish-book-button").click();
    await page.waitForURL(`**/reader/${BOOK_ID}/after-reading`);
    await page.locator("#memory-content").waitFor({ state: "visible" });
    await page.locator("#reading-stamp").waitFor({ state: "visible" });
    assert.strictEqual(await page.locator("#reading-stamp span").textContent(), "一起读过");
    assert.strictEqual(await page.locator("#reading-stamp b").textContent(), "🐶 × 🐱");
    assert.strictEqual(await page.locator("#stamp-month").textContent(), "2026.08.24");

    const progressIndex = fixture.requestLog.findIndex((item) => item === `PUT /api/books/${BOOK_ID}/progress`);
    const completionIndex = fixture.requestLog.findIndex((item) => item === `POST /api/books/${BOOK_ID}/completion`);
    const coverIndex = fixture.requestLog.findIndex((item) => item === `GET /api/books/${BOOK_ID}/back-cover`);
    assert.ok(progressIndex >= 0 && completionIndex > progressIndex && coverIndex > completionIndex);
    await context.close();
  } finally {
    await browser.close();
  }
}

async function paginationMeasurements(page) {
  return page.evaluate(() => {
    const content = document.querySelector("#chapter-content");
    const contentRect = content.getBoundingClientRect();
    const lineRects = [];
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (!walker.currentNode.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(walker.currentNode);
      lineRects.push(...range.getClientRects());
    }
    return {
      clientHeight: content.clientHeight,
      scrollHeight: content.scrollHeight,
      clientWidth: content.clientWidth,
      scrollWidth: content.scrollWidth,
      lineCount: lineRects.length,
      clippedLines: lineRects.filter((rect) => (
        rect.top < contentRect.top - 1 || rect.bottom > contentRect.bottom + 1
      )).length,
      paginationHeight: Number.parseFloat(getComputedStyle(content).height),
    };
  });
}

async function checkPagination(browserType, contextOptions, resizedViewport) {
  const browser = await browserType.launch({
    headless: true,
    executablePath: browserType.executablePath(),
  });
  try {
    const context = await browser.newContext(contextOptions);
    await installReadingHouseRoutes(context, { paragraphs: 110 });
    const page = await context.newPage();
    await page.addInitScript(() => localStorage.setItem("xiaxia-reader-mode", "paginated"));
    await page.goto(`${ORIGIN}/reader/${BOOK_ID}`);
    await page.waitForFunction(() => (
      document.body.classList.contains("reader-paginated") &&
      document.querySelector("#chapter-content")?.scrollWidth >
        document.querySelector("#chapter-content")?.clientWidth
    ));
    await page.evaluate(() => document.fonts?.ready);
    await page.waitForTimeout(350);
    const initial = await paginationMeasurements(page);
    assert.ok(initial.paginationHeight > 100);
    assert.ok(initial.scrollWidth > initial.clientWidth);
    assert.ok(initial.lineCount > 100);
    assert.strictEqual(initial.clippedLines, 0);
    assert.ok(initial.scrollHeight <= initial.clientHeight + 1);

    await page.setViewportSize(resizedViewport);
    await page.evaluate(() => window.dispatchEvent(new Event("orientationchange")));
    await page.waitForTimeout(500);
    const resized = await paginationMeasurements(page);
    assert.ok(resized.paginationHeight > 100);
    assert.ok(resized.scrollWidth > resized.clientWidth);
    assert.strictEqual(resized.clippedLines, 0);
    assert.ok(resized.scrollHeight <= resized.clientHeight + 1);
    await context.close();
  } finally {
    await browser.close();
  }
}

async function runPaginationScenario() {
  await checkPagination(
    webkit,
    { viewport: { width: 1024, height: 768 }, isMobile: true, hasTouch: true },
    { width: 768, height: 1024 },
  );
  await checkPagination(
    chromium,
    { viewport: { width: 1365, height: 768 } },
    { width: 1100, height: 820 },
  );
}

async function main() {
  const scenario = process.argv[2];
  if (scenario === "flow") await runFlowScenario();
  else if (scenario === "pagination") await runPaginationScenario();
  else throw new Error(`Unknown browser scenario: ${scenario}`);
  process.stdout.write(`${scenario}: ok\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
