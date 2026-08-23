(() => {
  "use strict";
  const bookSelect = document.querySelector("#manage-book");
  const chapterSelect = document.querySelector("#manage-chapter");
  const ownerSelect = document.querySelector("#manage-owner");
  const typeSelect = document.querySelector("#manage-type");
  const list = document.querySelector("#management-list");
  const empty = document.querySelector("#management-empty");
  const count = document.querySelector("#management-count");
  const deleteButton = document.querySelector("#delete-selected-traces");
  const undoButton = document.querySelector("#undo-user-action");
  let traces = [];

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `请求失败 (${response.status})`);
    return data;
  }

  async function initialize() {
    const data = await api("/api/books");
    for (const book of data.books || []) {
      const option = document.createElement("option");
      option.value = book.id;
      option.textContent = book.title;
      bookSelect.append(option);
    }
    await loadTraces();
  }

  async function loadChapters() {
    chapterSelect.innerHTML = '<option value="">全部章节</option>';
    if (!bookSelect.value) return;
    const data = await api(`/api/books/${bookSelect.value}/chapters`);
    for (const chapter of data.chapters || []) {
      const option = document.createElement("option");
      option.value = chapter.chapter_id;
      option.textContent = `${Number(chapter.chapter_index) + 1}. ${chapter.title}`;
      chapterSelect.append(option);
    }
  }

  async function loadTraces() {
    const query = new URLSearchParams({ owner: ownerSelect.value, content_type: typeSelect.value, page_size: "100" });
    if (bookSelect.value) query.set("book_id", bookSelect.value);
    if (chapterSelect.value) query.set("chapter_id", chapterSelect.value);
    try {
      const data = await api(`/api/management/traces?${query}`);
      traces = data.traces || [];
      render();
    } catch (error) {
      list.innerHTML = `<p class="form-error">读取失败：${escapeHtml(error.message)}</p>`;
    }
  }

  function render() {
    list.replaceChildren();
    count.textContent = `${traces.length} 条`;
    empty.hidden = traces.length > 0;
    for (const trace of traces) {
      const card = document.createElement("article");
      card.className = `overview-card management-card ${trace.owner === "xiaxia" ? "trace-xiaxia" : "trace-user"}`;
      const canDelete = trace.owner === "user" && ["user_annotation", "user_reply"].includes(trace.content_type);
      const input = canDelete ? `<input class="trace-select" type="checkbox" data-id="${trace.id}" data-type="${trace.content_type}" aria-label="选择这条记录">` : "";
      const quote = trace.selected_text ? `<blockquote class="overview-quote">${escapeHtml(trace.selected_text)}</blockquote>` : "";
      const content = trace.comment || trace.content || trace.response || "（纯划线）";
      const jump = sourceUrl(trace);
      card.innerHTML = `<div class="management-card-heading">${input}<span class="overview-kind">${label(trace.content_type)} · ${trace.owner}</span></div><span class="overview-chapter">${escapeHtml(trace.book_title || "")} · ${escapeHtml(trace.chapter_title || "")}</span>${quote}<p>${escapeHtml(content)}</p><span class="overview-time">${formatTime(trace.created_at)} / 更新 ${formatTime(trace.updated_at)}</span>${jump ? `<a class="continue-link" href="${jump}">回到原文 →</a>` : ""}`;
      list.append(card);
    }
    updateDeleteButton();
  }

  function sourceUrl(trace) {
    if (!trace.book_id || !trace.chapter_id) return "";
    const query = new URLSearchParams({ chapter_id: trace.chapter_id });
    if (trace.content_type === "user_annotation") query.set("annotation_id", trace.id);
    else if (trace.content_type === "xiaxia_reply") query.set("annotation_id", trace.annotation_id);
    else if (trace.content_type === "xiaxia_thought") query.set("thought_id", trace.id);
    else if (trace.content_type === "user_reply") query.set("thought_id", trace.thought_id);
    return `/reader/${trace.book_id}?${query}`;
  }

  function selectedItems() {
    return [...document.querySelectorAll(".trace-select:checked")].map((input) => ({ id: input.dataset.id, content_type: input.dataset.type }));
  }
  function updateDeleteButton() { deleteButton.disabled = selectedItems().length === 0; }

  deleteButton.addEventListener("click", async () => {
    const items = selectedItems();
    if (!items.length || !window.confirm(`确定删除所选 ${items.length} 条属于你的阅读痕迹吗？已有 Xiaxia Reply 的用户批注也会连同回复删除。`)) return;
    try {
      await api("/api/management/traces/batch-delete", { method: "POST", body: JSON.stringify({ items, confirm_xiaxia_replies: true }) });
      await loadTraces();
    } catch (error) { window.alert(`删除失败：${error.message}`); }
  });
  undoButton.addEventListener("click", async () => {
    try { await api("/api/management/undo", { method: "POST" }); await loadTraces(); }
    catch (error) { window.alert(`撤销失败：${error.message}`); }
  });
  list.addEventListener("change", updateDeleteButton);
  bookSelect.addEventListener("change", async () => { await loadChapters(); await loadTraces(); });
  chapterSelect.addEventListener("change", loadTraces);
  ownerSelect.addEventListener("change", loadTraces);
  typeSelect.addEventListener("change", loadTraces);

  function label(type) { return ({ user_annotation: "我的划线 / 批注", xiaxia_thought: "林知夏的想法", xiaxia_reply: "林知夏的回复", user_reply: "我的回复" })[type] || type; }
  function formatTime(value) { return value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—"; }
  function escapeHtml(value) { const span = document.createElement("span"); span.textContent = String(value || ""); return span.innerHTML; }
  initialize().catch((error) => { list.innerHTML = `<p class="form-error">初始化失败：${escapeHtml(error.message)}</p>`; });
})();
