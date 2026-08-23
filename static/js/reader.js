(() => {
  "use strict";

  const body = document.body;
  const bookId = body.dataset.bookId;
  const content = document.querySelector("#chapter-content");
  const bookTitle = document.querySelector("#reader-book-title");
  const chapterTitle = document.querySelector("#reader-chapter-title");
  const chapterPosition = document.querySelector("#chapter-position");
  const previousButton = document.querySelector("#previous-chapter");
  const nextButton = document.querySelector("#next-chapter");
  const progressBar = document.querySelector("#reader-progress span");
  const tocButton = document.querySelector("#toc-button");
  const tocDrawer = document.querySelector("#toc-drawer");
  const tocClose = document.querySelector("#toc-close");
  const tocList = document.querySelector("#toc-list");
  const scrim = document.querySelector("#drawer-scrim");
  const selectionMenu = document.querySelector("#selection-menu");
  const noteDialog = document.querySelector("#note-dialog");
  const noteForm = document.querySelector("#note-form");
  const noteText = document.querySelector("#note-text");
  const selectedQuote = document.querySelector("#selected-quote");
  const annotationDialog = document.querySelector("#annotation-dialog");
  const toast = document.querySelector("#reader-toast");

  const state = {
    book: null,
    chapters: [],
    currentIndex: 0,
    pristineHtml: "",
    annotations: [],
    savedSelection: null,
    progress: null,
    loaded: false,
    fontSize: Number(localStorage.getItem("xiaxia-reader-font-size")) || 19,
  };

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `请求失败 (${response.status})`);
    return data;
  }

  async function initialize() {
    setFontSize(state.fontSize);
    try {
      const { book } = await api(`/api/books/${bookId}`);
      state.book = book;
      state.chapters = book.chapters || [];
      state.progress = book.progress;
      bookTitle.textContent = book.title;
      document.title = `${book.title} · 共读小屋`;
      renderToc();
      if (!state.chapters.length) throw new Error("这本书没有可阅读章节");
      const savedIndex = state.chapters.findIndex((chapter) => chapter.id === state.progress?.chapter_id);
      const initialIndex = savedIndex >= 0 ? savedIndex : 0;
      await loadChapter(initialIndex, savedIndex >= 0 ? state.progress?.position : null);
    } catch (error) {
      content.innerHTML = `<p class="form-error">无法打开这本书：${escapeHtml(error.message)}</p>`;
    }
  }

  async function loadChapter(index, restorePosition = null) {
    if (index < 0 || index >= state.chapters.length) return;
    await saveProgress(true);
    state.loaded = false;
    state.currentIndex = index;
    const chapter = state.chapters[index];
    content.innerHTML = '<p class="reader-loading">正在翻页…</p>';
    closeToc();
    window.scrollTo({ top: 0, behavior: "auto" });
    try {
      const [chapterResult, annotationResult] = await Promise.all([
        api(`/api/books/${bookId}/chapters/${chapter.id}`),
        api(`/api/books/${bookId}/chapters/${chapter.id}/annotations`),
      ]);
      state.pristineHtml = chapterResult.chapter.content_html;
      state.annotations = annotationResult.annotations || [];
      chapterTitle.textContent = chapterResult.chapter.title;
      renderChapter();
      updateControls();
      state.loaded = true;
      if (restorePosition) restoreReadingPosition(restorePosition);
      else window.scrollTo({ top: 0, behavior: "auto" });
      updateProgressIndicator();
    } catch (error) {
      content.innerHTML = `<p class="form-error">章节加载失败：${escapeHtml(error.message)}</p>`;
    }
  }

  function renderChapter() {
    content.innerHTML = state.pristineHtml;
    applyAnnotations();
  }

  function renderToc() {
    tocList.replaceChildren();
    state.chapters.forEach((chapter, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = chapter.title || `第 ${index + 1} 章`;
      button.dataset.index = String(index);
      button.addEventListener("click", () => loadChapter(index));
      tocList.append(button);
    });
  }

  function updateControls() {
    previousButton.disabled = state.currentIndex === 0;
    nextButton.disabled = state.currentIndex >= state.chapters.length - 1;
    chapterPosition.textContent = `${state.currentIndex + 1} / ${state.chapters.length}`;
    for (const [index, button] of [...tocList.querySelectorAll("button")].entries()) {
      button.classList.toggle("current", index === state.currentIndex);
    }
  }

  function applyAnnotations() {
    const ordered = [...state.annotations].sort((a, b) => {
      const blockOrder = String(b.start_block_id).localeCompare(String(a.start_block_id));
      return blockOrder || Number(b.start_offset) - Number(a.start_offset);
    });
    for (const annotation of ordered) {
      let location = annotation;
      if (!coordinatesMatch(annotation)) location = findFallbackLocation(annotation);
      if (location) wrapAnnotation(location, annotation.id);
    }
  }

  function coordinatesMatch(annotation) {
    const start = content.querySelector(`#${cssEscape(annotation.start_block_id)}`);
    const end = content.querySelector(`#${cssEscape(annotation.end_block_id)}`);
    if (!start || !end) return false;
    const selected = textAcrossBlocks(
      start,
      Number(annotation.start_offset),
      end,
      Number(annotation.end_offset),
    );
    return normalizeText(selected) === normalizeText(annotation.selected_text);
  }

  function findFallbackLocation(annotation) {
    const needle = String(annotation.selected_text || "");
    if (!needle) return null;
    const blocks = [...content.querySelectorAll("[data-block-id]")];
    let best = null;
    let bestScore = -1;
    for (const block of blocks) {
      const haystack = block.textContent || "";
      let from = 0;
      while (from <= haystack.length) {
        const index = haystack.indexOf(needle, from);
        if (index < 0) break;
        let score = 1;
        if (annotation.prefix_text && haystack.slice(Math.max(0, index - annotation.prefix_text.length), index).endsWith(annotation.prefix_text)) score += 2;
        if (annotation.suffix_text && haystack.slice(index + needle.length).startsWith(annotation.suffix_text)) score += 2;
        if (score > bestScore) {
          bestScore = score;
          best = {
            ...annotation,
            start_block_id: block.dataset.blockId,
            end_block_id: block.dataset.blockId,
            start_offset: index,
            end_offset: index + needle.length,
          };
        }
        from = index + Math.max(needle.length, 1);
      }
    }
    return best;
  }

  function wrapAnnotation(location, annotationId) {
    const blocks = [...content.querySelectorAll("[data-block-id]")];
    const startIndex = blocks.findIndex((block) => block.dataset.blockId === location.start_block_id);
    const endIndex = blocks.findIndex((block) => block.dataset.blockId === location.end_block_id);
    if (startIndex < 0 || endIndex < startIndex) return;
    for (let index = endIndex; index >= startIndex; index -= 1) {
      const block = blocks[index];
      const start = index === startIndex ? Number(location.start_offset) : 0;
      const end = index === endIndex ? Number(location.end_offset) : (block.textContent || "").length;
      wrapTextRange(block, start, end, annotationId);
    }
  }

  function wrapTextRange(block, startOffset, endOffset, annotationId) {
    if (endOffset <= startOffset) return;
    const points = textPoints(block);
    const start = pointAt(points, startOffset);
    const end = pointAt(points, endOffset);
    if (!start || !end) return;
    const range = document.createRange();
    try {
      range.setStart(start.node, start.offset);
      range.setEnd(end.node, end.offset);
      if (range.collapsed) return;
      const mark = document.createElement("mark");
      mark.className = "reader-highlight";
      mark.dataset.annotationId = annotationId;
      mark.append(range.extractContents());
      range.insertNode(mark);
    } catch (_error) {
      // A malformed publisher DOM must not prevent the rest of the chapter.
    }
  }

  function textPoints(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const points = [];
    let total = 0;
    let node;
    while ((node = walker.nextNode())) {
      const start = total;
      total += node.nodeValue.length;
      points.push({ node, start, end: total });
    }
    return points;
  }

  function pointAt(points, absoluteOffset) {
    if (!points.length) return null;
    const max = points[points.length - 1].end;
    const offset = clamp(Number(absoluteOffset), 0, max);
    for (const point of points) {
      if (offset <= point.end) return { node: point.node, offset: offset - point.start };
    }
    const last = points[points.length - 1];
    return { node: last.node, offset: last.node.nodeValue.length };
  }

  function textAcrossBlocks(startBlock, startOffset, endBlock, endOffset) {
    if (startBlock === endBlock) return (startBlock.textContent || "").slice(startOffset, endOffset);
    const blocks = [...content.querySelectorAll("[data-block-id]")];
    const startIndex = blocks.indexOf(startBlock);
    const endIndex = blocks.indexOf(endBlock);
    if (startIndex < 0 || endIndex < startIndex) return "";
    return blocks.slice(startIndex, endIndex + 1).map((block, relativeIndex, selectedBlocks) => {
      const text = block.textContent || "";
      if (relativeIndex === 0) return text.slice(startOffset);
      if (relativeIndex === selectedBlocks.length - 1) return text.slice(0, endOffset);
      return text;
    }).join("\n");
  }

  function captureSelection() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return hideSelectionMenu();
    const range = selection.getRangeAt(0);
    if (!content.contains(range.commonAncestorContainer)) return hideSelectionMenu();
    const startBlock = closestBlock(range.startContainer);
    const endBlock = closestBlock(range.endContainer);
    if (!startBlock || !endBlock) return hideSelectionMenu();
    const selectedText = range.toString();
    if (!selectedText.trim() || selectedText.length > 20000) return hideSelectionMenu();
    const startOffset = offsetWithin(startBlock, range.startContainer, range.startOffset);
    const endOffset = offsetWithin(endBlock, range.endContainer, range.endOffset);
    const startText = startBlock.textContent || "";
    const endText = endBlock.textContent || "";
    state.savedSelection = {
      book_id: bookId,
      chapter_id: state.chapters[state.currentIndex].id,
      selected_text: selectedText,
      start_block_id: startBlock.dataset.blockId,
      start_offset: startOffset,
      end_block_id: endBlock.dataset.blockId,
      end_offset: endOffset,
      prefix_text: startText.slice(Math.max(0, startOffset - 120), startOffset),
      suffix_text: endText.slice(endOffset, endOffset + 120),
    };
    const rect = range.getBoundingClientRect();
    if (!rect.width && !rect.height) return hideSelectionMenu();
    selectionMenu.hidden = false;
    selectionMenu.style.left = `${clamp(rect.left + rect.width / 2, 86, window.innerWidth - 86)}px`;
    selectionMenu.style.top = `${Math.max(76, rect.top)}px`;
  }

  function closestBlock(node) {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element?.closest("[data-block-id]") || null;
  }

  function offsetWithin(block, node, offset) {
    const range = document.createRange();
    range.selectNodeContents(block);
    try {
      range.setEnd(node, offset);
      return range.toString().length;
    } catch (_error) {
      return 0;
    }
  }

  function hideSelectionMenu() {
    selectionMenu.hidden = true;
  }

  async function saveAnnotation(comment = "") {
    if (!state.savedSelection) return;
    hideSelectionMenu();
    try {
      const { annotation } = await api("/api/annotations", {
        method: "POST",
        body: JSON.stringify({ ...state.savedSelection, comment }),
      });
      state.annotations.push(annotation);
      renderChapter();
      window.getSelection()?.removeAllRanges();
      state.savedSelection = null;
      showToast(comment ? "批注已经留在书页旁。" : "划线已经保存。 ");
    } catch (error) {
      showToast(`保存失败：${error.message}`);
    }
  }

  function openNoteDialog() {
    if (!state.savedSelection) return;
    selectedQuote.textContent = state.savedSelection.selected_text;
    noteText.value = "";
    hideSelectionMenu();
    noteDialog.showModal();
    setTimeout(() => noteText.focus(), 0);
  }

  function openAnnotation(annotationId) {
    const annotation = state.annotations.find((item) => item.id === annotationId);
    if (!annotation) return;
    document.querySelector("#annotation-quote").textContent = annotation.selected_text;
    document.querySelector("#annotation-user-note").textContent = annotation.comment || "（只留下了划线）";
    const replySection = document.querySelector("#xiaxia-reply-section");
    replySection.hidden = !annotation.xiaxia_response;
    document.querySelector("#annotation-xiaxia-reply").textContent = annotation.xiaxia_response || "";
    annotationDialog.showModal();
  }

  function restoreReadingPosition(position) {
    requestAnimationFrame(() => {
      const block = position?.block_id ? content.querySelector(`#${cssEscape(position.block_id)}`) : null;
      if (block) {
        const toolbarHeight = 78;
        window.scrollTo({ top: Math.max(0, block.getBoundingClientRect().top + window.scrollY - toolbarHeight), behavior: "auto" });
      } else if (Number.isFinite(Number(position?.scroll_fraction))) {
        const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        window.scrollTo({ top: maxScroll * Number(position.scroll_fraction), behavior: "auto" });
      }
    });
  }

  async function saveProgress(keepalive = false) {
    if (!state.loaded || !state.chapters.length) return;
    const maxScroll = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    const scrollFraction = clamp(window.scrollY / maxScroll, 0, 1);
    const block = firstVisibleBlock();
    const percentage = clamp(((state.currentIndex + scrollFraction) / state.chapters.length) * 100, 0, 100);
    const payload = {
      chapter_id: state.chapters[state.currentIndex].id,
      chapter_index: state.currentIndex,
      position: {
        block_id: block?.dataset.blockId || "b000001",
        char_offset: 0,
        scroll_fraction: scrollFraction,
      },
      percentage,
    };
    try {
      await api(`/api/books/${bookId}/progress`, {
        method: "PUT",
        body: JSON.stringify(payload),
        keepalive,
      });
    } catch (_error) {
      // Autosave retries on the next scroll/visibility event.
    }
  }

  function firstVisibleBlock() {
    const toolbarBottom = 76;
    return [...content.querySelectorAll("[data-block-id]")].find((block) => block.getBoundingClientRect().bottom > toolbarBottom) || null;
  }

  function updateProgressIndicator() {
    if (!state.chapters.length) return;
    const maxScroll = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    const fraction = clamp(window.scrollY / maxScroll, 0, 1);
    const percentage = ((state.currentIndex + fraction) / state.chapters.length) * 100;
    progressBar.style.width = `${clamp(percentage, 0, 100)}%`;
  }

  function setFontSize(size) {
    state.fontSize = clamp(size, 15, 28);
    document.documentElement.style.setProperty("--reader-size", `${state.fontSize}px`);
    localStorage.setItem("xiaxia-reader-font-size", String(state.fontSize));
  }

  function openToc() {
    tocDrawer.classList.add("open");
    tocDrawer.setAttribute("aria-hidden", "false");
    tocButton.setAttribute("aria-expanded", "true");
    scrim.hidden = false;
  }

  function closeToc() {
    tocDrawer.classList.remove("open");
    tocDrawer.setAttribute("aria-hidden", "true");
    tocButton.setAttribute("aria-expanded", "false");
    scrim.hidden = true;
  }

  let toastTimer;
  function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
  }

  function debounce(fn, delay) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function normalizeText(value) { return String(value || "").replace(/\s+/g, " ").trim(); }
  function clamp(value, min, max) { return Math.max(min, Math.min(max, Number.isFinite(Number(value)) ? Number(value) : min)); }
  function cssEscape(value) { return window.CSS?.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&"); }
  function escapeHtml(value) { const span = document.createElement("span"); span.textContent = value; return span.innerHTML; }

  const delayedSelectionCapture = debounce(captureSelection, 220);
  const delayedProgressSave = debounce(() => saveProgress(false), 900);

  content.addEventListener("mouseup", delayedSelectionCapture);
  content.addEventListener("touchend", delayedSelectionCapture, { passive: true });
  document.addEventListener("selectionchange", () => {
    if (window.getSelection()?.isCollapsed) hideSelectionMenu();
  });
  content.addEventListener("click", (event) => {
    const mark = event.target.closest("mark[data-annotation-id]");
    if (mark) openAnnotation(mark.dataset.annotationId);
  });
  document.querySelector("#highlight-selection").addEventListener("click", () => saveAnnotation(""));
  document.querySelector("#note-selection").addEventListener("click", openNoteDialog);
  noteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveAnnotation(noteText.value.trim());
    noteDialog.close();
  });
  for (const button of document.querySelectorAll("[data-close-note]")) {
    button.addEventListener("click", () => noteDialog.close());
  }
  previousButton.addEventListener("click", () => loadChapter(state.currentIndex - 1));
  nextButton.addEventListener("click", () => loadChapter(state.currentIndex + 1));
  document.querySelector("#font-down").addEventListener("click", () => setFontSize(state.fontSize - 1));
  document.querySelector("#font-up").addEventListener("click", () => setFontSize(state.fontSize + 1));
  tocButton.addEventListener("click", openToc);
  tocClose.addEventListener("click", closeToc);
  scrim.addEventListener("click", closeToc);
  window.addEventListener("scroll", () => {
    updateProgressIndicator();
    delayedProgressSave();
    hideSelectionMenu();
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") saveProgress(true);
  });
  window.addEventListener("pagehide", () => saveProgress(true));

  initialize();
})();
