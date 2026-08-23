(() => {
  "use strict";

  const shelf = document.querySelector("#bookshelf");
  const empty = document.querySelector("#empty-shelf");
  const count = document.querySelector("#book-count");
  const fileInput = document.querySelector("#book-file");
  const uploadStatus = document.querySelector("#upload-status");
  const template = document.querySelector("#book-card-template");

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: "same-origin", ...options });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || data.error || `请求失败 (${response.status})`);
      error.data = data;
      throw error;
    }
    return data;
  }

  async function loadBooks() {
    shelf.setAttribute("aria-busy", "true");
    try {
      const data = await api("/api/books");
      renderBooks(data.books || []);
    } catch (error) {
      shelf.innerHTML = `<p class="form-error">无法读取书架：${escapeHtml(error.message)}</p>`;
    } finally {
      shelf.removeAttribute("aria-busy");
    }
  }

  function renderBooks(books) {
    shelf.replaceChildren();
    count.textContent = books.length ? `${books.length} 本` : "";
    empty.hidden = books.length !== 0;
    for (const book of books) {
      const fragment = template.content.cloneNode(true);
      const card = fragment.querySelector(".book-card");
      const href = `/reader/${book.id}`;
      for (const link of fragment.querySelectorAll("a")) link.href = href;

      const cover = fragment.querySelector(".book-cover");
      const image = fragment.querySelector("img");
      if (book.cover_url) {
        image.src = book.cover_url;
        image.alt = `${book.title}封面`;
        image.addEventListener("load", () => cover.classList.add("has-image"), { once: true });
      }
      fragment.querySelector(".book-title").textContent = book.title;
      fragment.querySelector(".book-author").textContent = book.author || "未知作者";
      const percentage = clamp(Number(book.progress_percentage || 0), 0, 100);
      const track = fragment.querySelector(".progress-track");
      track.setAttribute("aria-valuenow", percentage.toFixed(1));
      track.querySelector("span").style.width = `${percentage}%`;
      const chapter = book.current_chapter ? ` · ${book.current_chapter}` : "";
      fragment.querySelector(".book-progress").textContent = `${percentage.toFixed(1)}%${chapter}`;
      card.dataset.bookId = book.id;
      shelf.append(fragment);
    }
  }

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    uploadStatus.classList.remove("error");
    uploadStatus.textContent = `正在解析《${file.name}》…`;
    fileInput.disabled = true;
    const body = new FormData();
    body.append("file", file);
    try {
      const result = await api("/api/books", { method: "POST", body });
      uploadStatus.textContent = `《${result.book.title}》已经放上书架。`;
      await loadBooks();
    } catch (error) {
      uploadStatus.classList.add("error");
      if (error.data?.error === "book_already_exists") {
        uploadStatus.textContent = `《${error.data.title}》已经在书架上。`;
      } else {
        uploadStatus.textContent = `导入失败：${error.message}`;
      }
    } finally {
      fileInput.value = "";
      fileInput.disabled = false;
    }
  });

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, Number.isFinite(value) ? value : min));
  }

  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = value;
    return node.innerHTML;
  }

  loadBooks();
})();

