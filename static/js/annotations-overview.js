(() => {
  "use strict";

  const bookId = document.body.dataset.bookId;
  const title = document.querySelector("#overview-book-title");
  const list = document.querySelector("#annotation-overview-list");
  const empty = document.querySelector("#overview-empty");
  const count = document.querySelector("#overview-count");
  const filters = document.querySelector("#annotation-filters");
  const template = document.querySelector("#annotation-overview-template");

  async function api(url) {
    const response = await fetch(url, { credentials: "same-origin" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `请求失败 (${response.status})`);
    return data;
  }

  async function initialize() {
    try {
      const { book } = await api(`/api/books/${bookId}`);
      title.textContent = `《${book.title}》的划线与批注`;
      await loadOverview("all");
    } catch (error) {
      list.innerHTML = `<p class="form-error">无法读取批注：${escapeHtml(error.message)}</p>`;
    }
  }

  async function loadOverview(filter) {
    list.setAttribute("aria-busy", "true");
    try {
      const data = await api(`/api/books/${bookId}/annotations?filter=${encodeURIComponent(filter)}`);
      render(data.annotations || [], data.xiaxia_thoughts || []);
      for (const button of filters.querySelectorAll("button")) {
        button.classList.toggle("active", button.dataset.filter === filter);
      }
    } catch (error) {
      list.innerHTML = `<p class="form-error">读取失败：${escapeHtml(error.message)}</p>`;
    } finally {
      list.removeAttribute("aria-busy");
    }
  }

  function render(annotations, thoughts) {
    const items = [
      ...annotations.map((item) => ({ ...item, kind: "user" })),
      ...thoughts.map((item) => ({ ...item, kind: "xiaxia" })),
    ].sort((a, b) => Number(a.chapter_index) - Number(b.chapter_index) || String(a.created_at).localeCompare(String(b.created_at)));
    list.replaceChildren();
    count.textContent = `${items.length} 条`;
    empty.hidden = items.length > 0;
    for (const item of items) {
      const fragment = template.content.cloneNode(true);
      fragment.querySelector(".overview-card").classList.add(item.kind === "user" ? "trace-user" : "trace-xiaxia");
      fragment.querySelector(".overview-kind").textContent = item.kind === "user" ? "我的划线 / 批注" : "林知夏的独立想法";
      fragment.querySelector(".overview-chapter").textContent = `第 ${Number(item.chapter_index) + 1} 章 · ${item.chapter_title}`;
      const quote = fragment.querySelector(".overview-quote");
      quote.textContent = item.selected_text || (item.scope === "chapter" ? "（章节级想法）" : "");
      quote.hidden = !quote.textContent;
      const user = fragment.querySelector(".overview-user");
      user.textContent = item.kind === "user" && item.comment ? `我：${item.comment}` : "";
      user.hidden = !user.textContent;
      const reply = fragment.querySelector(".overview-reply");
      reply.textContent = item.xiaxia_response ? `林知夏的回复：${item.xiaxia_response}` : "";
      reply.hidden = !reply.textContent;
      const thought = fragment.querySelector(".overview-thought");
      thought.textContent = item.kind === "xiaxia" ? `林知夏：${item.content}` : "";
      thought.hidden = !thought.textContent;
      fragment.querySelector(".overview-time").textContent = `创建 ${formatTime(item.created_at)} · 更新 ${formatTime(item.updated_at)}`;
      fragment.querySelector(".overview-jump").addEventListener("click", () => {
        const params = new URLSearchParams({ chapter_id: item.chapter_id });
        params.set(item.kind === "user" ? "annotation_id" : "thought_id", item.id);
        location.href = `/reader/${bookId}?${params.toString()}`;
      });
      list.append(fragment);
    }
  }

  filters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (button) loadOverview(button.dataset.filter);
  });

  function formatTime(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
  }

  function escapeHtml(value) {
    const span = document.createElement("span");
    span.textContent = value;
    return span.innerHTML;
  }

  initialize();
})();
