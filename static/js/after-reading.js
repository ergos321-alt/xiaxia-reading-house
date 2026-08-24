(() => {
  "use strict";

  const body = document.body;
  const bookId = body.dataset.bookId;
  const loading = document.querySelector("#memory-loading");
  const locked = document.querySelector("#memory-locked");
  const content = document.querySelector("#memory-content");
  const toast = document.querySelector("#memory-toast");
  const userReflectionForm = document.querySelector("#user-reflection-form");
  const userRating = document.querySelector("#user-rating");
  const userReview = document.querySelector("#user-review");
  const userReflectionRead = document.querySelector("#user-reflection-read");
  const xiaxiaReflectionSealed = document.querySelector("#xiaxia-reflection-sealed");
  const xiaxiaReflectionRead = document.querySelector("#xiaxia-reflection-read");
  const stopsList = document.querySelector("#shared-stops-list");
  const stopsEmpty = document.querySelector("#shared-stops-empty");
  const loadMoreStops = document.querySelector("#load-more-stops");
  const userLetterForm = document.querySelector("#user-letter-form");
  const userLetter = document.querySelector("#user-letter");
  const state = { data: null, stopPage: 1, stopPageCount: 1 };

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || errorMessage(data.error) || `请求失败 (${response.status})`);
    return data;
  }

  function errorMessage(code) {
    const messages = {
      after_reading_locked: "读完以后，这一页才会打开。",
      reflection_locked_after_reveal: "两张纸条已经一起打开，不能再改写了。",
      owner_has_not_completed_book: "这一方还没有读完整本书。",
      shared_reading_not_completed: "两个人都读完以后，信纸才会展开。",
    };
    return messages[code] || code || "暂时无法完成";
  }

  async function initialize() {
    try {
      const data = await api(`/api/books/${bookId}/back-cover`);
      loading.hidden = true;
      if (data.access_state === "locked") {
        renderLocked(data);
        return;
      }
      state.data = data;
      renderOpen(data);
    } catch (error) {
      loading.textContent = `封底暂时无法打开：${error.message}`;
    }
  }

  function renderLocked(data) {
    locked.hidden = false;
    document.querySelector("#locked-book-title").textContent = data.book?.title || "这本书";
    document.querySelector("#locked-message").textContent = data.message || "读完以后，这一页会从书里打开。";
  }

  function renderOpen(data) {
    content.hidden = false;
    document.querySelector("#memory-book-title").textContent = data.book.title;
    document.querySelector("#memory-book-author").textContent = data.book.author || "";
    document.title = `${data.book.title} · 读完以后`;
    renderCompletion(data.completion, data.stamp);
    renderReflections(data.reflections);
    renderStops(data.shared_stops.preview || [], true);
    state.stopPageCount = Math.max(1, Math.ceil((data.shared_stops.count || 0) / 20));
    loadMoreStops.hidden = state.stopPageCount <= 1;
    stopsEmpty.hidden = (data.shared_stops.count || 0) !== 0;
    renderLetters(data.letters || [], data.completion.shared_completed);
    renderTimeline(data.timeline || []);
  }

  function renderCompletion(completion, stamp) {
    const note = document.querySelector("#completion-note");
    note.textContent = completion.shared_completed
      ? "两个人都走到了这本书的最后一页。"
      : "我已经读完；这张封底还在等另一种墨水走到这里。";
    const stampNode = document.querySelector("#reading-stamp");
    stampNode.hidden = !stamp?.visible;
    document.querySelector("#stamp-month").textContent = stamp?.completed_month || "";
    document.querySelector("#letters-section").hidden = !completion.shared_completed;
  }

  function renderReflections(reflections) {
    const mine = reflections.user;
    const hers = reflections.xiaxia;
    if (mine.submitted) {
      userRating.value = String(mine.rating || "");
      userReview.value = mine.review_text || "";
    }
    if (reflections.revealed) {
      document.querySelector("#reflection-rule").textContent = "两张纸条都已写好，现在可以一起展开。";
      userReflectionForm.hidden = true;
      userReflectionRead.hidden = false;
      fillReflection(userReflectionRead, mine);
      xiaxiaReflectionSealed.hidden = true;
      xiaxiaReflectionRead.hidden = false;
      fillReflection(xiaxiaReflectionRead, hers);
    } else {
      userReflectionForm.hidden = false;
      userReflectionRead.hidden = true;
      xiaxiaReflectionRead.hidden = true;
      xiaxiaReflectionSealed.hidden = false;
      xiaxiaReflectionSealed.querySelector("p").textContent = hers.submitted
        ? "她已经写好，等你的纸条合上以后一起打开。"
        : "她的纸条还没有写好。";
      document.querySelector("#save-user-reflection").textContent = mine.submitted
        ? "重新合上我的纸条"
        : "把这张纸条合上";
    }
  }

  function fillReflection(node, reflection) {
    node.replaceChildren();
    const rating = document.createElement("p");
    rating.className = "reflection-rating";
    rating.textContent = `${reflection.rating} / 5`;
    const text = document.createElement("p");
    text.textContent = reflection.review_text || "";
    const time = document.createElement("time");
    time.textContent = formatDate(reflection.submitted_at);
    node.append(rating, text, time);
  }

  function renderStops(items, replace = false) {
    if (replace) stopsList.replaceChildren();
    for (const stop of items) stopsList.append(sharedStopCard(stop));
  }

  function sharedStopCard(stop) {
    const article = document.createElement("article");
    article.className = "shared-stop-card";
    const heading = document.createElement("header");
    const chapter = document.createElement("p");
    chapter.textContent = `第 ${Number(stop.chapter_index) + 1} 章 · ${stop.chapter_title || "未命名章节"}`;
    const badge = document.createElement("span");
    badge.textContent = ({ exact: "同一处", overlap: "彼此交叠", same_block: "同一段落" })[stop.match_level] || "共同停留";
    heading.append(chapter, badge);
    const quote = document.createElement("blockquote");
    quote.textContent = stop.source_text || "";
    const traces = document.createElement("div");
    traces.className = "shared-traces";
    traces.append(traceNote("🐶 我", stop.user_annotations, "comment", "user"));
    traces.append(traceNote("🐱 林知夏", stop.xiaxia_thoughts, "thought_content", "xiaxia"));
    const jump = document.createElement("a");
    const annotationId = stop.user_annotations?.[0]?.id;
    jump.href = `/reader/${bookId}?chapter_id=${encodeURIComponent(stop.chapter_id)}&annotation_id=${encodeURIComponent(annotationId || "")}`;
    jump.textContent = "回到这一页";
    article.append(heading, quote, traces, jump);
    return article;
  }

  function traceNote(label, rows, contentField, ink) {
    const section = document.createElement("section");
    section.className = `shared-trace shared-trace-${ink}`;
    const owner = document.createElement("h3");
    owner.textContent = label;
    section.append(owner);
    for (const row of rows || []) {
      const quote = document.createElement("p");
      quote.className = "trace-selection";
      quote.textContent = row.selected_text || "在这一段停过";
      const note = document.createElement("p");
      note.textContent = row[contentField] || (ink === "user" ? "只留下了一道划线。" : "只留下了页边记号。") ;
      section.append(quote, note);
    }
    return section;
  }

  function renderLetters(letters, sharedCompleted) {
    if (!sharedCompleted) return;
    const mine = letters.find((item) => item.author === "user");
    const hers = letters.find((item) => item.author === "xiaxia");
    userLetter.value = mine?.content || "";
    document.querySelector("#user-letter-date").textContent = mine ? formatDate(mine.letter_date) : "";
    document.querySelector("#xiaxia-letter").textContent = hers?.content || "信纸还是空的。";
    document.querySelector("#xiaxia-letter-date").textContent = hers ? formatDate(hers.letter_date) : "";
  }

  function renderTimeline(events) {
    const timeline = document.querySelector("#memory-timeline");
    timeline.replaceChildren();
    for (const event of events) {
      const item = document.createElement("li");
      const date = document.createElement("time");
      date.textContent = formatDate(event.happened_at);
      const label = document.createElement("p");
      label.textContent = event.label;
      item.append(date, label);
      timeline.append(item);
    }
  }

  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric" }).format(date);
  }

  let toastTimer;
  function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
  }

  userReflectionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api(`/api/books/${bookId}/reflection`, {
        method: "PUT",
        body: JSON.stringify({ rating: Number(userRating.value), review_text: userReview.value.trim() }),
      });
      showToast("纸条已经合好。");
      const data = await api(`/api/books/${bookId}/back-cover`);
      state.data = data;
      renderReflections(data.reflections);
      renderCompletion(data.completion, data.stamp);
    } catch (error) {
      showToast(error.message);
    }
  });

  userLetterForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api(`/api/books/${bookId}/letter`, {
        method: "PUT",
        body: JSON.stringify({ content: userLetter.value.trim() }),
      });
      document.querySelector("#user-letter-date").textContent = formatDate(result.letter.letter_date);
      showToast("信已经留在封底。");
    } catch (error) {
      showToast(error.message);
    }
  });

  loadMoreStops.addEventListener("click", async () => {
    if (state.stopPage >= state.stopPageCount) return;
    loadMoreStops.disabled = true;
    try {
      const nextPage = state.stopPage + 1;
      const data = await api(`/api/books/${bookId}/shared-stops?page=${nextPage}&page_size=20`);
      renderStops(data.shared_stops || []);
      state.stopPage = nextPage;
      state.stopPageCount = data.pagination.page_count;
      loadMoreStops.hidden = state.stopPage >= state.stopPageCount;
    } catch (error) {
      showToast(error.message);
    } finally {
      loadMoreStops.disabled = false;
    }
  });

  initialize();
})();
