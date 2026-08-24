(() => {
  "use strict";

  const utils = window.XiaxiaReaderUtils;
  const body = document.body;
  const bookId = body.dataset.bookId;
  const content = document.querySelector("#chapter-content");
  const readerShell = document.querySelector(".reader-shell");
  const bookTitle = document.querySelector("#reader-book-title");
  const chapterTitle = document.querySelector("#reader-chapter-title");
  const chapterPosition = document.querySelector("#chapter-position");
  const previousButton = document.querySelector("#previous-chapter");
  const nextButton = document.querySelector("#next-chapter");
  const pagePrevious = document.querySelector("#page-previous");
  const pageNext = document.querySelector("#page-next");
  const pageStatus = document.querySelector("#page-status");
  const finishBookEntry = document.querySelector("#finish-book-entry");
  const finishBookButton = document.querySelector("#finish-book-button");
  const progressBar = document.querySelector("#reader-progress span");
  const readerMenuButton = document.querySelector("#reader-menu-button");
  const readerMenu = document.querySelector("#reader-menu");
  const tocButton = document.querySelector("#toc-button");
  const tocDrawer = document.querySelector("#toc-drawer");
  const tocClose = document.querySelector("#toc-close");
  const tocList = document.querySelector("#toc-list");
  const scrim = document.querySelector("#drawer-scrim");
  const modeButton = document.querySelector("#reading-mode");
  const chapterThoughts = document.querySelector("#chapter-thoughts");
  const selectionMenu = document.querySelector("#selection-menu");
  const noteDialog = document.querySelector("#note-dialog");
  const noteForm = document.querySelector("#note-form");
  const noteTitle = document.querySelector("#note-dialog-title");
  const noteText = document.querySelector("#note-text");
  const selectedQuote = document.querySelector("#selected-quote");
  const annotationDialog = document.querySelector("#annotation-dialog");
  const editAnnotationButton = document.querySelector("#edit-annotation");
  const deleteAnnotationButton = document.querySelector("#delete-annotation");
  const thoughtReplySection = document.querySelector("#thought-user-reply-section");
  const thoughtReplyText = document.querySelector("#thought-user-reply");
  const thoughtReplyButton = document.querySelector("#thought-reply-edit");
  const thoughtReplyDelete = document.querySelector("#thought-reply-delete");
  const traceMenuButton = document.querySelector("#trace-menu-button");
  const traceActionsMenu = document.querySelector("#trace-actions-menu");
  const thoughtActions = document.querySelector("#thought-actions");
  const annotationActions = document.querySelector("#annotation-actions");
  const toast = document.querySelector("#reader-toast");
  const query = new URLSearchParams(location.search);

  const state = {
    book: null,
    chapters: [],
    currentIndex: 0,
    pristineHtml: "",
    annotations: [],
    thoughts: [],
    syncSignature: "",
    savedSelection: null,
    editingAnnotationId: null,
    openRecord: null,
    progress: null,
    loaded: false,
    fontSize: Number(localStorage.getItem("xiaxia-reader-font-size")) || 19,
    mode: localStorage.getItem("xiaxia-reader-mode") === "paginated" ? "paginated" : "scroll",
    pageIndex: 0,
    pageCount: 1,
    menuInteracting: false,
    selectionSubmitting: false,
    selectionTimers: [],
    touchStart: null,
    target: {
      chapterId: query.get("chapter_id"),
      annotationId: query.get("annotation_id"),
      thoughtId: query.get("thought_id"),
    },
  };

  let pageStatusTimer = null;
  let pageStatusHideTimer = null;

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `请求失败 (${response.status})`);
    return data;
  }

  function openReaderMenu() {
    readerMenu.hidden = false;
    readerMenuButton.setAttribute("aria-expanded", "true");
  }

  function closeReaderMenu() {
    readerMenu.hidden = true;
    readerMenuButton.setAttribute("aria-expanded", "false");
  }

  function toggleReaderMenu() {
    if (readerMenu.hidden) openReaderMenu();
    else closeReaderMenu();
  }

  function openTraceActions() {
    traceActionsMenu.hidden = false;
    traceMenuButton.setAttribute("aria-expanded", "true");
  }

  function closeTraceActions() {
    traceActionsMenu.hidden = true;
    traceMenuButton.setAttribute("aria-expanded", "false");
  }

  function toggleTraceActions() {
    if (traceActionsMenu.hidden) openTraceActions();
    else closeTraceActions();
  }

  async function initialize() {
    setFontSize(state.fontSize);
    applyModeUi();
    try {
      const { book } = await api(`/api/books/${bookId}`);
      state.book = book;
      state.chapters = book.chapters || [];
      state.progress = book.progress;
      if (!localStorage.getItem("xiaxia-reader-mode") && book.progress?.position?.display_mode) {
        state.mode = book.progress.position.display_mode === "paginated" ? "paginated" : "scroll";
        applyModeUi();
      }
      bookTitle.textContent = book.title;
      document.title = `${book.title} · 共读小屋`;
      renderToc();
      if (!state.chapters.length) throw new Error("这本书没有可阅读章节");
      const targetIndex = state.target.chapterId
        ? state.chapters.findIndex((chapter) => chapter.id === state.target.chapterId)
        : -1;
      const savedIndex = state.chapters.findIndex((chapter) => chapter.id === state.progress?.chapter_id);
      const initialIndex = targetIndex >= 0 ? targetIndex : (savedIndex >= 0 ? savedIndex : 0);
      const restore = targetIndex < 0 && savedIndex >= 0 ? state.progress?.position : null;
      await loadChapter(initialIndex, restore);
      window.setInterval(syncChapterAnnotations, 12000);
    } catch (error) {
      content.innerHTML = `<p class="form-error">无法打开这本书：${escapeHtml(error.message)}</p>`;
    }
  }

  async function loadChapter(index, restorePosition = null) {
    if (index < 0 || index >= state.chapters.length) return;
    await saveProgress(true);
    state.loaded = false;
    state.currentIndex = index;
    state.pageIndex = 0;
    const chapter = state.chapters[index];
    content.innerHTML = '<p class="reader-loading">正在翻页…</p>';
    chapterThoughts.hidden = true;
    closeToc();
    if (state.mode === "scroll") window.scrollTo({ top: 0, behavior: "auto" });
    try {
      const [chapterResult, annotationResult] = await Promise.all([
        api(`/api/books/${bookId}/chapters/${chapter.id}`),
        api(`/api/books/${bookId}/chapters/${chapter.id}/annotations`),
      ]);
      state.pristineHtml = chapterResult.chapter.content_html;
      state.annotations = annotationResult.annotations || [];
      state.thoughts = annotationResult.xiaxia_thoughts || [];
      state.syncSignature = utils.syncSignature(state.annotations, state.thoughts);
      chapterTitle.textContent = chapterResult.chapter.title;
      renderChapter();
      updateControls();
      state.loaded = true;
      await nextFrame(2);
      await recalculatePages();
      if (hasTargetForChapter(chapter.id)) {
        jumpToRequestedTarget();
      } else if (restorePosition) {
        restoreReadingPosition(restorePosition);
      } else if (state.mode === "scroll") {
        window.scrollTo({ top: 0, behavior: "auto" });
      } else {
        goToPage(0, "auto");
      }
      updateProgressIndicator();
    } catch (error) {
      content.innerHTML = `<p class="form-error">章节加载失败：${escapeHtml(error.message)}</p>`;
    }
  }

  function renderChapter() {
    content.innerHTML = state.pristineHtml;
    applyAnnotations();
    applyXiaxiaThoughts();
    renderChapterThoughts();
    for (const image of content.querySelectorAll("img")) {
      if (!image.complete) image.addEventListener("load", () => recalculatePages(captureViewportAnchor()), { once: true });
    }
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
    updatePageControls();
    updateFinishEntry();
  }

  function applyAnnotations() {
    const ordered = [...state.annotations].sort(reverseAnchorOrder);
    for (const annotation of ordered) {
      const location = coordinatesMatch(annotation) ? annotation : findFallbackLocation(annotation);
      if (location) wrapAnnotation(location, annotation.id, "user");
    }
  }

  function applyXiaxiaThoughts() {
    const ordered = state.thoughts.filter((item) => item.scope !== "chapter").sort(reverseAnchorOrder);
    for (const thought of ordered) {
      const location = coordinatesMatch(thought) ? thought : findFallbackLocation(thought);
      if (location) wrapAnnotation(location, thought.id, "xiaxia");
    }
  }

  function reverseAnchorOrder(a, b) {
    const blockOrder = String(b.start_block_id).localeCompare(String(a.start_block_id));
    return blockOrder || Number(b.start_offset) - Number(a.start_offset);
  }

  function coordinatesMatch(record) {
    const start = content.querySelector(`#${cssEscape(record.start_block_id)}`);
    const end = content.querySelector(`#${cssEscape(record.end_block_id)}`);
    if (!start || !end) return false;
    const selected = textAcrossBlocks(start, Number(record.start_offset), end, Number(record.end_offset));
    return normalizeText(selected) === normalizeText(record.selected_text);
  }

  function findFallbackLocation(record) {
    const needle = String(record.selected_text || "");
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
        if (record.prefix_text && haystack.slice(Math.max(0, index - record.prefix_text.length), index).endsWith(record.prefix_text)) score += 2;
        if (record.suffix_text && haystack.slice(index + needle.length).startsWith(record.suffix_text)) score += 2;
        if (score > bestScore) {
          bestScore = score;
          best = { ...record, start_block_id: block.dataset.blockId, end_block_id: block.dataset.blockId, start_offset: index, end_offset: index + needle.length };
        }
        from = index + Math.max(needle.length, 1);
      }
    }
    return best;
  }

  function wrapAnnotation(location, recordId, kind) {
    const blocks = [...content.querySelectorAll("[data-block-id]")];
    const startIndex = blocks.findIndex((block) => block.dataset.blockId === location.start_block_id);
    const endIndex = blocks.findIndex((block) => block.dataset.blockId === location.end_block_id);
    if (startIndex < 0 || endIndex < startIndex) return;
    for (let index = endIndex; index >= startIndex; index -= 1) {
      const block = blocks[index];
      const start = index === startIndex ? Number(location.start_offset) : 0;
      const end = index === endIndex ? Number(location.end_offset) : (block.textContent || "").length;
      wrapTextRange(block, start, end, recordId, kind);
    }
  }

  function wrapTextRange(block, startOffset, endOffset, recordId, kind) {
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
      if (kind === "xiaxia") {
        mark.className = "xiaxia-thought-highlight";
        mark.dataset.thoughtId = recordId;
        mark.setAttribute("role", "button");
        mark.setAttribute("tabindex", "0");
        mark.setAttribute("aria-label", "打开林知夏留在这里的想法");
        mark.setAttribute("title", "林知夏来过这里");
      } else {
        mark.className = "reader-highlight";
        mark.dataset.annotationId = recordId;
        mark.setAttribute("role", "button");
        mark.setAttribute("tabindex", "0");
        mark.setAttribute("aria-label", "打开我的划线或批注");
      }
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
    const offset = utils.clamp(Number(absoluteOffset), 0, max);
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

  function scheduleSelectionCapture(delays = [120, 320, 620]) {
    for (const timer of state.selectionTimers) clearTimeout(timer);
    state.selectionTimers = [];
    if (state.selectionSubmitting) return;
    state.selectionTimers = delays.map((delay) => setTimeout(() => {
      if (!state.selectionSubmitting) captureSelection();
    }, delay));
  }

  function captureSelection() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return;
    const range = selection.getRangeAt(0).cloneRange();
    if (!content.contains(range.commonAncestorContainer)) return;
    const startBlock = closestBlock(range.startContainer);
    const endBlock = closestBlock(range.endContainer);
    if (!startBlock || !endBlock) return;
    const selectedText = range.toString();
    if (!selectedText.trim() || selectedText.length > 20000) return;
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
    const rects = [...range.getClientRects()].filter((rect) => rect.width || rect.height);
    if (!rects.length) return;
    positionSelectionMenu(rects[rects.length - 1]);
  }

  function positionSelectionMenu(rect) {
    const visual = window.visualViewport;
    const viewport = {
      offsetLeft: visual?.offsetLeft || 0,
      offsetTop: visual?.offsetTop || 0,
      width: visual?.width || window.innerWidth,
      height: visual?.height || window.innerHeight,
      bottomInset: 12,
    };
    const mobile = matchMedia("(pointer: coarse)").matches || navigator.maxTouchPoints > 0;
    const position = utils.selectionMenuPosition({ rect, viewport, mobile });
    selectionMenu.hidden = false;
    selectionMenu.dataset.placement = position.placement;
    selectionMenu.style.left = `${position.left}px`;
    selectionMenu.style.top = position.top == null ? "auto" : `${position.top}px`;
    selectionMenu.style.bottom = position.bottom == null ? "auto" : `${position.bottom}px`;
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

  function hideSelectionMenu(clearSaved = false) {
    selectionMenu.hidden = true;
    if (clearSaved) state.savedSelection = null;
  }

  function clearSelectionInteraction() {
    for (const timer of state.selectionTimers) clearTimeout(timer);
    state.selectionTimers = [];
    window.getSelection()?.removeAllRanges();
    hideSelectionMenu(true);
    selectionMenu.removeAttribute("data-placement");
    selectionMenu.style.removeProperty("left");
    selectionMenu.style.removeProperty("top");
    selectionMenu.style.removeProperty("bottom");
    state.menuInteracting = false;
    state.selectionSubmitting = false;
  }

  async function saveAnnotation(comment = "") {
    if (!state.savedSelection) return;
    const selectionSnapshot = { ...state.savedSelection };
    state.selectionSubmitting = true;
    hideSelectionMenu();
    try {
      const { annotation } = await api("/api/annotations", {
        method: "POST",
        body: JSON.stringify({ ...selectionSnapshot, comment }),
      });
      state.annotations.push(annotation);
      clearSelectionInteraction();
      await refreshAnnotationMarks();
      showToast(comment ? "批注已经留在书页旁。" : "划线已经保存。");
    } catch (error) {
      state.selectionSubmitting = false;
      showToast(`保存失败：${error.message}`);
    }
  }

  function openNewNoteDialog() {
    if (!state.savedSelection) return;
    state.editingAnnotationId = null;
    noteTitle.textContent = "写在书页旁";
    selectedQuote.textContent = state.savedSelection.selected_text;
    noteText.value = "";
    hideSelectionMenu();
    noteDialog.showModal();
    setTimeout(() => noteText.focus(), 0);
  }

  function openEditAnnotation(annotationId) {
    const annotation = state.annotations.find((item) => item.id === annotationId);
    if (!annotation) return;
    state.editingAnnotationId = annotation.id;
    noteTitle.textContent = annotation.comment ? "编辑我的想法" : "给这条划线添加想法";
    selectedQuote.textContent = annotation.selected_text;
    noteText.value = annotation.comment || "";
    if (annotationDialog.open) annotationDialog.close();
    noteDialog.showModal();
    setTimeout(() => noteText.focus(), 0);
  }

  async function saveEditedAnnotation(comment) {
    const annotationId = state.editingAnnotationId;
    if (!annotationId) return;
    try {
      const { annotation } = await api(`/api/annotations/${annotationId}`, {
        method: "PATCH",
        body: JSON.stringify({ comment }),
      });
      const index = state.annotations.findIndex((item) => item.id === annotationId);
      if (index >= 0) state.annotations[index] = annotation;
      state.syncSignature = utils.syncSignature(state.annotations, state.thoughts);
      showToast(comment ? "批注已经更新。" : "已保留为纯划线。");
    } catch (error) {
      showToast(`更新失败：${error.message}`);
    }
  }

  function openAnnotation(annotationId) {
    const annotation = state.annotations.find((item) => item.id === annotationId);
    if (!annotation) return;
    state.openRecord = { kind: "user", id: annotationId };
    document.querySelector("#annotation-kind").textContent = "我的划线 / 批注";
    document.querySelector("#annotation-quote").textContent = annotation.selected_text;
    document.querySelector("#user-note-section").hidden = false;
    document.querySelector("#annotation-user-note").textContent = annotation.comment || "（只留下了划线）";
    const replySection = document.querySelector("#xiaxia-reply-section");
    replySection.hidden = !annotation.xiaxia_response;
    document.querySelector("#annotation-xiaxia-reply").textContent = annotation.xiaxia_response || "";
    document.querySelector("#xiaxia-thought-section").hidden = true;
    thoughtReplySection.hidden = true;
    thoughtActions.hidden = true;
    annotationActions.hidden = false;
    editAnnotationButton.textContent = annotation.comment ? "编辑" : "添加想法";
    closeTraceActions();
    if (!annotationDialog.open) annotationDialog.showModal();
  }

  function openThought(thoughtId) {
    const thought = state.thoughts.find((item) => item.id === thoughtId);
    if (!thought) return;
    state.openRecord = { kind: "xiaxia", id: thoughtId };
    document.querySelector("#annotation-kind").textContent = "林知夏的独立想法";
    document.querySelector("#annotation-quote").textContent = thought.selected_text || "（本章）";
    document.querySelector("#user-note-section").hidden = true;
    document.querySelector("#xiaxia-reply-section").hidden = true;
    document.querySelector("#xiaxia-thought-section").hidden = false;
    document.querySelector("#annotation-xiaxia-thought").textContent = thought.content;
    thoughtReplySection.hidden = false;
    thoughtReplyText.textContent = thought.user_response || "（我还没有回复）";
    thoughtReplyButton.textContent = thought.user_response ? "编辑我的回复" : "回复林知夏";
    thoughtReplyDelete.hidden = !thought.user_response;
    thoughtActions.hidden = false;
    annotationActions.hidden = true;
    closeTraceActions();
    if (!annotationDialog.open) annotationDialog.showModal();
  }

  async function editThoughtReply() {
    if (state.openRecord?.kind !== "xiaxia") return;
    const thought = state.thoughts.find((item) => item.id === state.openRecord.id);
    if (!thought) return;
    const response = window.prompt("回复林知夏的这条想法", thought.user_response || "");
    if (response === null || !response.trim()) return;
    try {
      const method = thought.user_response ? "PATCH" : "POST";
      const data = await api(`/api/xiaxia/thoughts/${thought.id}/reply`, {
        method,
        body: JSON.stringify({ response: response.trim() }),
      });
      const reply = data.user_reply;
      thought.user_reply_id = reply.id;
      thought.user_response = reply.response;
      thought.user_reply_created_at = reply.created_at;
      thought.user_reply_updated_at = reply.updated_at;
      state.syncSignature = utils.syncSignature(state.annotations, state.thoughts);
      openThought(thought.id);
      showToast("回复已经留在这条想法旁。");
    } catch (error) {
      showToast(`回复失败：${error.message}`);
    }
  }

  async function deleteThoughtReply() {
    if (state.openRecord?.kind !== "xiaxia") return;
    const thought = state.thoughts.find((item) => item.id === state.openRecord.id);
    if (!thought?.user_response || !window.confirm("确定删除我对这条想法的回复吗？")) return;
    try {
      await api(`/api/xiaxia/thoughts/${thought.id}/reply`, { method: "DELETE" });
      thought.user_reply_id = null;
      thought.user_response = null;
      thought.user_reply_created_at = null;
      thought.user_reply_updated_at = null;
      state.syncSignature = utils.syncSignature(state.annotations, state.thoughts);
      openThought(thought.id);
      showToast("我的回复已删除。");
    } catch (error) {
      showToast(`删除失败：${error.message}`);
    }
  }

  async function deleteCurrentAnnotation() {
    if (state.openRecord?.kind !== "user") return;
    const annotation = state.annotations.find((item) => item.id === state.openRecord.id);
    if (!annotation) return;
    const message = annotation.xiaxia_response
      ? "这条批注已有林知夏的回复。删除会同时永久删除该回复，确定继续吗？"
      : "确定删除这条划线 / 批注吗？";
    if (!window.confirm(message)) return;
    try {
      await api(`/api/annotations/${annotation.id}`, { method: "DELETE" });
      state.annotations = state.annotations.filter((item) => item.id !== annotation.id);
      state.openRecord = null;
      annotationDialog.close();
      await refreshAnnotationMarks();
      showToast("划线与批注已删除。");
    } catch (error) {
      showToast(`删除失败：${error.message}`);
    }
  }

  function renderChapterThoughts() {
    const chapterLevel = state.thoughts.filter((item) => item.scope === "chapter");
    chapterThoughts.replaceChildren();
    chapterThoughts.hidden = chapterLevel.length === 0;
    for (const thought of chapterLevel) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chapter-thought";
      button.setAttribute("aria-label", "打开林知夏留在本章的想法");
      button.innerHTML = `<span class="thought-paw" aria-hidden="true">🐾</span><span><strong>林知夏在这一章停留过</strong><small>点开页边留下的话</small></span>`;
      button.addEventListener("click", () => openThought(thought.id));
      chapterThoughts.append(button);
    }
  }

  async function refreshAnnotationMarks() {
    const snapshot = captureViewportAnchor();
    unwrapMarks();
    applyAnnotations();
    applyXiaxiaThoughts();
    renderChapterThoughts();
    state.syncSignature = utils.syncSignature(state.annotations, state.thoughts);
    await nextFrame(1);
    await recalculatePages(snapshot);
    restoreViewportAnchor(snapshot);
  }

  function unwrapMarks() {
    for (const mark of [...content.querySelectorAll("mark.reader-highlight, mark.xiaxia-thought-highlight")].reverse()) {
      mark.replaceWith(...mark.childNodes);
    }
    content.normalize();
  }

  function applyModeUi() {
    const paginated = state.mode === "paginated";
    body.classList.toggle("reader-paginated", paginated);
    if (paginated) window.scrollTo({ top: 0, behavior: "auto" });
    modeButton.textContent = paginated ? "切换为滚动阅读" : "切换为分页阅读";
    modeButton.setAttribute("aria-label", paginated ? "切换到滚动阅读" : "切换到分页阅读");
    hidePageStatus(true);
    updatePageControls();
  }

  async function toggleReadingMode() {
    if (!state.loaded) return;
    const snapshot = captureViewportAnchor();
    await saveProgress(true);
    state.mode = state.mode === "paginated" ? "scroll" : "paginated";
    localStorage.setItem("xiaxia-reader-mode", state.mode);
    applyModeUi();
    await nextFrame(2);
    await recalculatePages(snapshot);
    restoreViewportAnchor(snapshot);
    updateProgressIndicator();
  }

  async function recalculatePages(anchor = null) {
    if (state.mode !== "paginated" || !state.loaded) {
      body.style.removeProperty("--reader-viewport-height");
      content.style.removeProperty("--reader-column-width");
      content.style.removeProperty("--pagination-height");
      state.pageCount = 1;
      state.pageIndex = 0;
      content.scrollLeft = 0;
      updatePageControls();
      return;
    }
    const visual = window.visualViewport;
    const viewportHeight = Math.min(
      Number(window.innerHeight) || Number(visual?.height) || 1,
      Number(visual?.height) || Number(window.innerHeight) || 1,
    );
    const viewportTop = visual?.offsetTop || 0;
    body.style.setProperty("--reader-viewport-height", `${viewportHeight}px`);
    await nextFrame(1);
    const shellRect = readerShell.getBoundingClientRect();
    const contentRect = content.getBoundingClientRect();
    const shellStyle = getComputedStyle(readerShell);
    const availableHeight = utils.calculatePaginationHeight({
      viewportTop,
      viewportHeight,
      contentTop: contentRect.top,
      shellBottom: shellRect.bottom,
      shellPaddingBottom: Number.parseFloat(shellStyle.paddingBottom) || 0,
      guard: 10,
    });
    content.style.setProperty("--pagination-height", `${availableHeight}px`);
    content.style.setProperty("--reader-column-width", `${Math.floor(contentRect.width)}px`);
    await nextFrame(2);
    const gap = pageGap();
    state.pageCount = utils.calculatePageCount(content.scrollWidth, content.clientWidth, gap);
    if (anchor?.blockId) jumpToBlock(anchor.blockId, false);
    else goToPage(state.pageIndex, "auto");
    updatePageControls();
  }

  function pageGap() {
    return Number.parseFloat(getComputedStyle(content).columnGap) || 32;
  }

  function goToPage(index, behavior = "smooth", announce = false) {
    if (state.mode !== "paginated") return;
    state.pageIndex = utils.clamp(index, 0, state.pageCount - 1);
    const left = state.pageIndex * (content.clientWidth + pageGap());
    if (behavior === "auto") content.scrollLeft = left;
    else content.scrollTo({ left, top: 0, behavior });
    updatePageControls();
    if (announce) showPageStatus();
    updateProgressIndicator();
  }

  async function turnPage(direction) {
    if (state.mode !== "paginated" || !state.loaded) return;
    if (direction === "next" && state.pageIndex < state.pageCount - 1) {
      goToPage(state.pageIndex + 1, "smooth", true);
    } else if (direction === "previous" && state.pageIndex > 0) {
      goToPage(state.pageIndex - 1, "smooth", true);
    } else if (direction === "next" && state.currentIndex < state.chapters.length - 1) {
      await loadChapter(state.currentIndex + 1);
      goToPage(0, "auto", true);
    } else if (direction === "previous" && state.currentIndex > 0) {
      await loadChapter(state.currentIndex - 1);
      goToPage(state.pageCount - 1, "auto", true);
    }
    delayedProgressSave();
  }

  function updatePageControls() {
    const paginated = state.mode === "paginated";
    pagePrevious.hidden = !paginated;
    pageNext.hidden = !paginated;
    if (!paginated) {
      hidePageStatus(true);
      updateFinishEntry();
      return;
    }
    pagePrevious.disabled = state.pageIndex === 0 && state.currentIndex === 0;
    pageNext.disabled = state.pageIndex >= state.pageCount - 1 && state.currentIndex >= state.chapters.length - 1;
    pageStatus.textContent = `${state.pageIndex + 1} / ${state.pageCount}`;
    updateFinishEntry();
  }

  function updateFinishEntry() {
    if (!finishBookEntry || !state.chapters.length) return;
    const finalChapter = state.currentIndex === state.chapters.length - 1;
    const finalPage = state.mode !== "paginated" || state.pageIndex >= state.pageCount - 1;
    finishBookEntry.hidden = !(state.loaded && finalChapter && finalPage);
  }

  function showPageStatus() {
    if (state.mode !== "paginated") return;
    clearTimeout(pageStatusTimer);
    clearTimeout(pageStatusHideTimer);
    pageStatus.hidden = false;
    requestAnimationFrame(() => pageStatus.classList.add("visible"));
    pageStatusTimer = setTimeout(() => hidePageStatus(false), 1100);
  }

  function hidePageStatus(immediate = false) {
    clearTimeout(pageStatusTimer);
    clearTimeout(pageStatusHideTimer);
    pageStatus.classList.remove("visible");
    if (immediate) {
      pageStatus.hidden = true;
      return;
    }
    pageStatusHideTimer = setTimeout(() => { pageStatus.hidden = true; }, 260);
  }

  function pageForElement(element) {
    const rect = [...element.getClientRects()].find((item) => item.width || item.height);
    if (!rect) return 0;
    const containerRect = content.getBoundingClientRect();
    const absoluteLeft = rect.left - containerRect.left + content.scrollLeft;
    return utils.clamp(Math.floor((absoluteLeft + 1) / (content.clientWidth + pageGap())), 0, state.pageCount - 1);
  }

  function captureViewportAnchor() {
    const block = firstVisibleBlock();
    return {
      blockId: block?.dataset.blockId || "b000001",
      scrollY: window.scrollY,
      pageIndex: state.pageIndex,
      mode: state.mode,
    };
  }

  function restoreViewportAnchor(snapshot) {
    if (!snapshot) return;
    if (state.mode === "paginated") jumpToBlock(snapshot.blockId, false);
    else jumpToBlock(snapshot.blockId, false, snapshot.scrollY);
  }

  function jumpToBlock(blockId, flash = false, fallbackScrollY = 0) {
    const block = blockId ? content.querySelector(`#${cssEscape(blockId)}`) : null;
    if (!block) {
      if (state.mode === "paginated") goToPage(0, "auto");
      else window.scrollTo({ top: fallbackScrollY, behavior: "auto" });
      return;
    }
    if (state.mode === "paginated") goToPage(pageForElement(block), "auto");
    else window.scrollTo({ top: Math.max(0, block.getBoundingClientRect().top + window.scrollY - 78), behavior: "auto" });
    if (flash) flashTargets([block]);
  }

  function restoreReadingPosition(position) {
    requestAnimationFrame(() => {
      const blockId = position?.block_id;
      if (blockId && content.querySelector(`#${cssEscape(blockId)}`)) {
        jumpToBlock(blockId, false);
      } else if (state.mode === "paginated") {
        goToPage(Number(position?.page_index) || 0, "auto");
      } else if (Number.isFinite(Number(position?.scroll_fraction))) {
        const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        window.scrollTo({ top: maxScroll * Number(position.scroll_fraction), behavior: "auto" });
      }
    });
  }

  async function saveProgress(keepalive = false, forceComplete = false) {
    if (!state.loaded || !state.chapters.length) return;
    const fraction = forceComplete ? 1 : state.mode === "paginated"
      ? (state.pageCount > 1 ? state.pageIndex / (state.pageCount - 1) : 0)
      : utils.clamp(window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight), 0, 1);
    const blocks = [...content.querySelectorAll("[data-block-id]")];
    const block = forceComplete ? blocks[blocks.length - 1] : firstVisibleBlock();
    const percentage = utils.clamp(((state.currentIndex + fraction) / state.chapters.length) * 100, 0, 100);
    const payload = {
      chapter_id: state.chapters[state.currentIndex].id,
      chapter_index: state.currentIndex,
      position: {
        block_id: block?.dataset.blockId || "b000001",
        char_offset: 0,
        scroll_fraction: fraction,
        display_mode: state.mode,
        page_index: state.pageIndex,
        page_count: state.pageCount,
      },
      percentage,
    };
    try {
      await api(`/api/books/${bookId}/progress`, { method: "PUT", body: JSON.stringify(payload), keepalive });
    } catch (error) {
      if (forceComplete) throw error;
      // Autosave retries on the next scroll, page turn, or visibility event.
    }
  }

  async function openAfterReading() {
    if (!finishBookButton || finishBookButton.disabled) return;
    finishBookButton.disabled = true;
    finishBookButton.textContent = "正在合上这本书…";
    try {
      await saveProgress(true, true);
      await api(`/api/books/${bookId}/completion`, { method: "POST" });
      location.assign(`/reader/${bookId}/after-reading`);
    } catch (error) {
      finishBookButton.disabled = false;
      finishBookButton.textContent = "合上正文，翻到读完以后";
      showToast(`暂时还不能翻到封底：${error.message}`);
    }
  }

  function firstVisibleBlock() {
    const blocks = [...content.querySelectorAll("[data-block-id]")];
    if (state.mode !== "paginated") {
      return blocks.find((block) => block.getBoundingClientRect().bottom > 76) || null;
    }
    const visible = content.getBoundingClientRect();
    return blocks.find((block) => [...block.getClientRects()].some((rect) => rect.right > visible.left + 1 && rect.left < visible.right - 1 && rect.bottom > visible.top && rect.top < visible.bottom)) || null;
  }

  function updateProgressIndicator() {
    if (!state.chapters.length) return;
    const fraction = state.mode === "paginated"
      ? (state.pageCount > 1 ? state.pageIndex / (state.pageCount - 1) : 0)
      : utils.clamp(window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight), 0, 1);
    const percentage = ((state.currentIndex + fraction) / state.chapters.length) * 100;
    progressBar.style.width = `${utils.clamp(percentage, 0, 100)}%`;
  }

  function setFontSize(size) {
    state.fontSize = utils.clamp(size, 15, 28);
    document.documentElement.style.setProperty("--reader-size", `${state.fontSize}px`);
    localStorage.setItem("xiaxia-reader-font-size", String(state.fontSize));
  }

  async function changeFontSize(delta) {
    const anchor = captureViewportAnchor();
    setFontSize(state.fontSize + delta);
    await recalculatePages(anchor);
    restoreViewportAnchor(anchor);
  }

  async function syncChapterAnnotations() {
    if (!state.loaded || selectionIsActive() || noteDialog.open || state.menuInteracting) return;
    const chapter = state.chapters[state.currentIndex];
    try {
      const data = await api(`/api/books/${bookId}/chapters/${chapter.id}/annotations`);
      const annotations = data.annotations || [];
      const thoughts = data.xiaxia_thoughts || [];
      const signature = utils.syncSignature(annotations, thoughts);
      if (signature === state.syncSignature) return;
      const oldAnchorSignature = anchorSignature(state.annotations, state.thoughts);
      const oldExternalCount = externalContentCount(state.annotations, state.thoughts);
      state.annotations = annotations;
      state.thoughts = thoughts;
      state.syncSignature = signature;
      if (oldAnchorSignature !== anchorSignature(annotations, thoughts)) {
        await refreshAnnotationMarks();
      } else {
        renderChapterThoughts();
      }
      refreshOpenDialog();
      if (externalContentCount(annotations, thoughts) > oldExternalCount) showToast("书页旁有林知夏留下的新内容。");
    } catch (_error) {
      // A later poll retries without disturbing reading.
    }
  }

  function anchorSignature(annotations, thoughts) {
    const anchors = [...annotations, ...thoughts.filter((item) => item.scope !== "chapter")]
      .map((item) => [item.id, item.start_block_id, item.start_offset, item.end_block_id, item.end_offset]);
    return JSON.stringify(anchors);
  }

  function externalContentCount(annotations, thoughts) {
    return annotations.filter((item) => item.xiaxia_response).length + thoughts.length;
  }

  function refreshOpenDialog() {
    if (!annotationDialog.open || !state.openRecord) return;
    if (state.openRecord.kind === "user") openAnnotation(state.openRecord.id);
    else openThought(state.openRecord.id);
  }

  function selectionIsActive() {
    const selection = window.getSelection();
    return Boolean(selection && !selection.isCollapsed && selection.rangeCount && content.contains(selection.getRangeAt(0).commonAncestorContainer));
  }

  function hasTargetForChapter(chapterId) {
    return state.target.chapterId === chapterId && (state.target.annotationId || state.target.thoughtId);
  }

  function jumpToRequestedTarget() {
    let elements = [];
    if (state.target.annotationId) elements = [...content.querySelectorAll(`mark[data-annotation-id="${cssEscape(state.target.annotationId)}"]`)];
    if (state.target.thoughtId) elements = [...content.querySelectorAll(`mark[data-thought-id="${cssEscape(state.target.thoughtId)}"]`)];
    if (elements.length) {
      const target = elements[0];
      if (state.mode === "paginated") goToPage(pageForElement(target), "auto");
      else window.scrollTo({ top: Math.max(0, target.getBoundingClientRect().top + window.scrollY - 96), behavior: "auto" });
      flashTargets(elements);
    } else if (state.target.thoughtId) {
      const thought = state.thoughts.find((item) => item.id === state.target.thoughtId);
      const button = [...chapterThoughts.querySelectorAll("button")][state.thoughts.filter((item) => item.scope === "chapter").indexOf(thought)];
      if (button) flashTargets([button]);
    }
    state.target = { chapterId: null, annotationId: null, thoughtId: null };
  }

  function flashTargets(elements) {
    for (const element of elements) element.classList.add("target-flash");
    setTimeout(() => elements.forEach((element) => element.classList.remove("target-flash")), 2400);
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
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
  }

  function debounce(fn, delay) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function nextFrame(count = 1) {
    return new Promise((resolve) => {
      const step = (remaining) => requestAnimationFrame(() => remaining > 1 ? step(remaining - 1) : resolve());
      step(count);
    });
  }

  function normalizeText(value) { return String(value || "").replace(/\s+/g, " ").trim(); }
  function cssEscape(value) { return window.CSS?.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&"); }
  function escapeHtml(value) { const span = document.createElement("span"); span.textContent = value; return span.innerHTML; }

  const delayedProgressSave = debounce(() => saveProgress(false), 900);
  const delayedResize = debounce(async () => {
    const anchor = captureViewportAnchor();
    await recalculatePages(anchor);
    restoreViewportAnchor(anchor);
  }, 180);

  content.addEventListener("mouseup", () => scheduleSelectionCapture([40, 180]));
  content.addEventListener("pointerup", (event) => {
    if (event.pointerType === "touch" || event.pointerType === "pen") scheduleSelectionCapture();
  });
  content.addEventListener("touchend", () => scheduleSelectionCapture(), { passive: true });
  content.addEventListener("contextmenu", () => scheduleSelectionCapture([100, 350, 700]));
  document.addEventListener("selectionchange", () => {
    if (selectionIsActive()) {
      scheduleSelectionCapture([180, 420]);
    } else if (!state.menuInteracting) {
      setTimeout(() => { if (!selectionIsActive() && !state.menuInteracting) hideSelectionMenu(); }, 260);
    }
  });
  selectionMenu.addEventListener("pointerdown", (event) => {
    captureSelection();
    state.menuInteracting = true;
    event.preventDefault();
    setTimeout(() => { state.menuInteracting = false; }, 500);
  });
  window.visualViewport?.addEventListener("resize", () => {
    if (!selectionMenu.hidden) scheduleSelectionCapture([80]);
  });
  window.visualViewport?.addEventListener("scroll", () => {
    if (!selectionMenu.hidden) scheduleSelectionCapture([80]);
  });

  content.addEventListener("click", (event) => {
    const mark = event.target.closest(
      "mark[data-annotation-id], mark[data-thought-id]",
    );
    if (mark?.dataset.annotationId) openAnnotation(mark.dataset.annotationId);
    else if (mark?.dataset.thoughtId) openThought(mark.dataset.thoughtId);
  });
  content.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const mark = event.target.closest("mark[data-annotation-id], mark[data-thought-id]");
    if (!mark) return;
    event.preventDefault();
    if (mark.dataset.annotationId) openAnnotation(mark.dataset.annotationId);
    else if (mark.dataset.thoughtId) openThought(mark.dataset.thoughtId);
  });
  document.querySelector("#highlight-selection").addEventListener("click", () => saveAnnotation(""));
  document.querySelector("#note-selection").addEventListener("click", openNewNoteDialog);
  noteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.editingAnnotationId) await saveEditedAnnotation(noteText.value.trim());
    else await saveAnnotation(noteText.value.trim());
    noteDialog.close();
  });
  noteDialog.addEventListener("close", () => { state.editingAnnotationId = null; });
  annotationDialog.addEventListener("close", () => {
    state.openRecord = null;
    closeTraceActions();
  });
  for (const button of document.querySelectorAll("[data-close-note]")) button.addEventListener("click", () => noteDialog.close());
  editAnnotationButton.addEventListener("click", () => state.openRecord?.kind === "user" && openEditAnnotation(state.openRecord.id));
  deleteAnnotationButton.addEventListener("click", deleteCurrentAnnotation);
  thoughtReplyButton.addEventListener("click", editThoughtReply);
  thoughtReplyDelete.addEventListener("click", deleteThoughtReply);
  previousButton.addEventListener("click", () => loadChapter(state.currentIndex - 1));
  nextButton.addEventListener("click", () => loadChapter(state.currentIndex + 1));
  pagePrevious.addEventListener("click", () => turnPage("previous"));
  pageNext.addEventListener("click", () => turnPage("next"));
  finishBookButton?.addEventListener("click", openAfterReading);
  readerMenuButton.addEventListener("click", toggleReaderMenu);
  traceMenuButton.addEventListener("click", toggleTraceActions);
  modeButton.addEventListener("click", () => {
    closeReaderMenu();
    toggleReadingMode();
  });
  document.querySelector("#font-down").addEventListener("click", () => changeFontSize(-1));
  document.querySelector("#font-up").addEventListener("click", () => changeFontSize(1));
  tocButton.addEventListener("click", () => {
    closeReaderMenu();
    openToc();
  });
  tocClose.addEventListener("click", closeToc);
  scrim.addEventListener("click", closeToc);
  document.addEventListener("pointerdown", (event) => {
    if (!readerMenu.hidden && !readerMenu.contains(event.target) && !readerMenuButton.contains(event.target)) closeReaderMenu();
    if (!traceActionsMenu.hidden && !traceActionsMenu.contains(event.target) && !traceMenuButton.contains(event.target)) closeTraceActions();
  });

  content.addEventListener("touchstart", (event) => {
    const touch = event.changedTouches[0];
    state.touchStart = touch ? { x: touch.clientX, y: touch.clientY } : null;
  }, { passive: true });
  content.addEventListener("touchend", (event) => {
    if (state.mode !== "paginated") return;
    const touch = event.changedTouches[0];
    const direction = utils.swipeDirection(state.touchStart, touch ? { x: touch.clientX, y: touch.clientY } : null, selectionIsActive());
    state.touchStart = null;
    if (direction) turnPage(direction);
  }, { passive: true });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !readerMenu.hidden) {
      event.preventDefault();
      closeReaderMenu();
      readerMenuButton.focus();
      return;
    }
    if (event.key === "Escape" && !traceActionsMenu.hidden) {
      event.preventDefault();
      closeTraceActions();
      traceMenuButton.focus();
      return;
    }
    if (state.mode !== "paginated" || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.target.closest("input, textarea, dialog") || selectionIsActive()) return;
    if (event.key === "ArrowRight") { event.preventDefault(); turnPage("next"); }
    if (event.key === "ArrowLeft") { event.preventDefault(); turnPage("previous"); }
  });

  window.addEventListener("scroll", () => {
    if (state.mode !== "scroll") return;
    updateProgressIndicator();
    delayedProgressSave();
    if (!selectionIsActive()) hideSelectionMenu();
  }, { passive: true });
  window.addEventListener("resize", delayedResize, { passive: true });
  window.addEventListener("orientationchange", delayedResize, { passive: true });
  window.visualViewport?.addEventListener("resize", delayedResize, { passive: true });
  if (document.fonts?.ready) document.fonts.ready.then(delayedResize);
  document.fonts?.addEventListener?.("loadingdone", delayedResize);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") saveProgress(true);
  });
  window.addEventListener("pagehide", () => saveProgress(true));

  initialize();
})();
