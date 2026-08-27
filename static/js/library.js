(() => {
  "use strict";

  const shelf = document.querySelector("#bookshelf");
  const empty = document.querySelector("#empty-shelf");
  const count = document.querySelector("#book-count");
  const fileInput = document.querySelector("#book-file");
  const uploadStatus = document.querySelector("#upload-status");
  const template = document.querySelector("#book-card-template");
  const editDialog = document.querySelector("#book-edit-dialog");
  const editForm = document.querySelector("#book-edit-form");
  const editId = document.querySelector("#edit-book-id");
  const editTitle = document.querySelector("#edit-book-title");
  const editAuthor = document.querySelector("#edit-book-author");
  const sharedNote = document.querySelector("#shared-reading-note");
  const sharedLink = document.querySelector("#shared-reading-link");
  const sharedBook = document.querySelector("#shared-reading-book");
  const sharedChapter = document.querySelector("#shared-reading-chapter");
  const sharedProgress = document.querySelector("#shared-reading-progress");
  const sharedPercentage = document.querySelector("#shared-reading-percentage");

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const upstreamFailures = {
        502: {
          error: "import_worker_terminated",
          message: "导入进程被服务器终止，通常是可用内存或进程资源不足；本次书籍未完成导入",
        },
        503: {
          error: "import_resource_exhausted",
          message: "导入服务当前资源不足，请稍后重试",
        },
        504: {
          error: "epub_import_timeout",
          message: "导入超过服务器处理时间，本次书籍未完成导入",
        },
      };
      const isBookImport = url === "/api/books" && options.method === "POST";
      const diagnostic = data.error
        ? data
        : ((isBookImport && upstreamFailures[response.status]) || data);
      const error = new Error(diagnostic.message || diagnostic.error || `请求失败 (${response.status})`);
      error.data = diagnostic;
      error.code = diagnostic.error || "book_import_failed";
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
    renderSharedReadingNote(books);
    books.forEach((book, index) => {
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
      } else {
        cover.dataset.coverTone = String((index % 5) + 1);
        fragment.querySelector(".cover-placeholder").textContent = firstBookCharacter(book.title);
      }
      fragment.querySelector(".book-title").textContent = book.title;
      fragment.querySelector(".book-author").textContent = book.author || "未知作者";
      const percentage = clamp(Number(book.progress_percentage || 0), 0, 100);
      const track = fragment.querySelector(".progress-track");
      track.setAttribute("aria-valuenow", percentage.toFixed(1));
      track.querySelector("span").style.width = `${percentage}%`;
      const chapter = book.current_chapter ? ` · ${book.current_chapter}` : "";
      fragment.querySelector(".book-progress").textContent = `${percentage.toFixed(1)}%${chapter}`;
      fragment.querySelector(".edit-book-button").addEventListener("click", () => openBookEditor(book));
      fragment.querySelector(".delete-book-button").addEventListener("click", () => deleteBook(book));
      card.dataset.bookId = book.id;
      shelf.append(fragment);
    });
  }

  function renderSharedReadingNote(books) {
    const recent = books.find((book) => book.progress_chapter_id) || null;
    sharedNote.hidden = !recent;
    if (!recent) return;
    const percentage = clamp(Number(recent.progress_percentage || 0), 0, 100);
    sharedLink.href = `/reader/${recent.id}`;
    sharedBook.textContent = `《${recent.title}》`;
    sharedChapter.textContent = recent.current_chapter || "从上次停下的书页继续";
    sharedProgress.style.width = `${percentage}%`;
    sharedPercentage.textContent = `我们读了 ${percentage.toFixed(1)}%`;
  }

  function firstBookCharacter(title) {
    const normalized = String(title || "书").trim();
    return Array.from(normalized)[0] || "书";
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

  function openBookEditor(book) {
    editId.value = book.id;
    editTitle.value = book.title || "";
    editAuthor.value = book.author || "未知作者";
    editDialog.showModal();
    setTimeout(() => editTitle.focus(), 0);
  }

  async function deleteBook(book) {
    const warning = `确定永久删除《${book.title}》吗？\n\n这会删除章节、双方阅读进度、所有批注、Thought、回复，以及私有 Storage 中的原书、封面和正文图片。此操作不提供书籍级撤销。`;
    if (!window.confirm(warning)) return;
    uploadStatus.classList.remove("error");
    uploadStatus.textContent = `正在删除《${book.title}》及其私有资源…`;
    try {
      await api(`/api/books/${book.id}`, { method: "DELETE" });
      uploadStatus.textContent = `《${book.title}》已从书架与私有存储中删除。`;
      await loadBooks();
    } catch (error) {
      uploadStatus.classList.add("error");
      uploadStatus.textContent = `删除失败：${error.message}`;
    }
  }

  editForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = editTitle.value.trim();
    const author = editAuthor.value.trim();
    if (!title || !author) return;
    const submit = editForm.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api(`/api/books/${editId.value}`, {
        method: "PATCH",
        body: JSON.stringify({ title, author }),
      });
      editDialog.close();
      await loadBooks();
    } catch (error) {
      uploadStatus.classList.add("error");
      uploadStatus.textContent = `书籍信息保存失败：${error.message}`;
    } finally {
      submit.disabled = false;
    }
  });

  for (const button of document.querySelectorAll("[data-close-book-edit]")) {
    button.addEventListener("click", () => editDialog.close());
  }

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
