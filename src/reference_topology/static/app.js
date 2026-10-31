"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const VIEW_NAMES = { library: "文献库", graph: "引用图谱", review: "参考文献复核", tasks: "解析任务", settings: "设置与维护" };
  const STATUS = {
    queued: ["等待处理", "blue"], running: ["正在处理", "blue"], succeeded: ["处理完成", "green"],
    partial: ["部分完成", "amber"], failed: ["处理失败", "red"], cancelled: ["已取消", ""],
    interrupted: ["已中断", "amber"], needs_ocr: ["需要 OCR", "amber"], needs_review: ["待复核", "amber"],
    accepted: ["已确认", "green"], confirmed: ["已确认", "green"], rejected: ["已拒绝", ""],
    pending: ["待确认", "amber"], candidate: ["候选匹配", "amber"], unresolved: ["待匹配", "amber"], ambiguous: ["存在歧义", "amber"],
    draft: ["待完善", ""], provisional: ["待核对", "amber"], stub: ["仅书目信息", ""], verified: ["已核对", "green"], identified: ["已识别", "green"], complete: ["已完善", "green"],
    unverified: ["待核对", "amber"], available: ["可用", "green"], missing: ["文件失联", "red"],
  };
  const state = {
    csrf: null, view: "library", papers: [], allPapers: [], total: 0, libraryTotal: 0,
    pageSize: 30, offset: 0, query: "", selectedPaper: null, detail: null,
    references: [], reviewPaper: null, jobs: [], jobsSignature: "", settings: null,
    importMode: "linked", rootId: "", browsePath: "", browseParent: null,
    selectedFiles: new Set(), uploads: [], editContext: null, cy: null, graph: null,
    paperRequest: 0, detailRequest: 0, reviewRequest: 0, graphRequest: 0,
    importBusy: false, rebindFile: null, polling: false, initialized: false,
    mergeSource: null, lastMerge: null, revisionContext: null,
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function button(label, handler, className = "button compact", iconName) {
    const node = element("button", className);
    node.type = "button";
    if (iconName) node.append(icon(iconName));
    node.append(document.createTextNode(label));
    if (handler) node.addEventListener("click", handler);
    return node;
  }

  function emptyState(container, { title, description, symbol = "file", action, small = false, loading = false }) {
    const box = element("div", `empty-state${small ? " small" : ""}${loading ? " loading" : ""}`);
    const mark = element("div", "empty-symbol");
    mark.append(icon(symbol));
    box.append(mark, element(small ? "h3" : "h2", "", title));
    if (description) box.append(element("p", "", description));
    if (action) box.append(button(action.label, action.onClick, "button primary", action.icon));
    container.replaceChildren(box);
  }

  function badge(status, text) {
    const known = STATUS[status] || [status || "待完善", ""];
    return element("span", `badge ${known[1]}`, text || known[0]);
  }

  function titleOf(paper) { return paper?.title || paper?.display_title || "尚未识别标题"; }
  function names(authors) {
    if (!authors) return [];
    if (typeof authors === "string") return [authors];
    return (Array.isArray(authors) ? authors : []).map((author) => {
      if (typeof author === "string") return author;
      return author.display_name || author.name || [author.given_name || author.given, author.family_name || author.family].filter(Boolean).join(" ");
    }).filter(Boolean);
  }
  function authorText(authors) { return names(authors).join("; "); }
  function itemsOf(data) { return Array.isArray(data) ? data : (data?.items || []); }
  function asObject(value) {
    if (typeof value === "object" && value !== null) return value;
    if (typeof value === "string") { try { return JSON.parse(value); } catch (_) { return {}; } }
    return {};
  }
  function identifier(paper, scheme) {
    return paper?.[scheme] || (paper?.identifiers || []).find((item) => item.scheme === scheme)?.value ||
      (paper?.identifiers || []).find((item) => item.scheme === scheme)?.value_normalized || "";
  }
  function sizeText(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return "";
    return value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`;
  }
  function dateText(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function errorMessage(error) { return error?.message || "操作未完成，请重试。"; }
  function showError(id, error) {
    $(id).textContent = errorMessage(error);
    $(id).hidden = false;
  }
  function clearError(id) { $(id).hidden = true; $(id).textContent = ""; }
  function toast(message, isError = false) {
    const node = element("div", `toast${isError ? " error" : ""}`);
    node.append(icon(isError ? "info" : "check"), element("span", "", message));
    const close = button("", () => node.remove(), "", "close");
    close.setAttribute("aria-label", "关闭通知");
    node.append(close);
    $("toasts").append(node);
    window.setTimeout(() => node.remove(), isError ? 10000 : 6000);
  }

  async function api(path, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    delete opts.authRetried;
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (opts.method && !["GET", "HEAD"].includes(opts.method.toUpperCase())) {
      if (!state.csrf) throw new Error("尚未建立本机会话，请刷新页面后重试。");
      headers.set("X-CSRF-Token", state.csrf);
    }
    if (options.json !== undefined) {
      headers.set("Content-Type", "application/json");
      opts.body = JSON.stringify(options.json);
      delete opts.json;
    }
    opts.headers = headers;
    let response;
    try { response = await fetch(path, opts); }
    catch (_) { throw new Error("无法连接本机服务，请检查工作台是否仍在运行。"); }
    let data = {};
    if (response.status !== 204) {
      const contentType = response.headers.get("Content-Type") || "";
      if (contentType.includes("application/json")) data = await response.json();
      else if (response.ok) throw new Error("服务返回了无法识别的结果，请重新加载页面。");
    }
    if (!response.ok) {
      const info = data.error || data.detail || {};
      if (!options.authRetried && path !== "/api/session" && ["session_required", "csrf_required"].includes(info.code)) {
        const session = await api("/api/session", { authRetried: true });
        state.csrf = session.csrf_token;
        return api(path, { ...options, authRetried: true });
      }
      const message = typeof info === "string" ? info : info.message;
      const error = new Error(message || `请求未完成（HTTP ${response.status}）。`);
      error.code = info.code;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function actionWithButton(target, fn, busyLabel = "处理中…") {
    const children = Array.from(target.childNodes).map((node) => node.cloneNode(true));
    target.disabled = true;
    target.textContent = busyLabel;
    try { return await fn(); }
    catch (error) { toast(errorMessage(error), true); }
    finally { target.disabled = false; target.replaceChildren(...children); }
  }

  function makePdfLink(fileId, compact = false) {
    const link = element("a", compact ? "button compact" : "button primary", "");
    link.href = `/api/files/${encodeURIComponent(fileId)}/content`;
    link.target = "_blank";
    link.rel = "noopener";
    link.append(icon("external"), document.createTextNode("打开 PDF"));
    return link;
  }

  async function changeView(view, options = {}) {
    if (!VIEW_NAMES[view]) view = "library";
    state.view = view;
    for (const name of Object.keys(VIEW_NAMES)) $("view-" + name).hidden = name !== view;
    document.querySelectorAll("[data-view]").forEach((node) => {
      const active = node.dataset.view === view;
      node.classList.toggle("active", active);
      if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    });
    $("breadcrumb-name").textContent = VIEW_NAMES[view];
    document.title = `${VIEW_NAMES[view]} · Reference Topology`;
    if (!options.fromHash && location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (!state.initialized) return;
    try {
      if (view === "graph") { await updatePaperSelectors(); await loadGraph(); }
      if (view === "review") { await updatePaperSelectors(); await loadReview(); }
      if (view === "tasks") await loadJobs(true);
      if (view === "settings") await loadSettings();
    } catch (error) { toast(errorMessage(error), true); }
  }

  async function loadPapers({ quiet = false } = {}) {
    const request = ++state.paperRequest;
    if (!quiet && !state.papers.length) emptyState($("paper-list"), { title: "正在读取文献库", description: "从本机数据库加载文献信息。", loading: true });
    const params = new URLSearchParams({ q: state.query, limit: state.pageSize, offset: state.offset });
    try {
      const result = await api(`/api/papers?${params}`);
      if (request !== state.paperRequest) return;
      state.papers = itemsOf(result);
      state.total = Number(result.total ?? state.papers.length);
      if (!state.query) state.libraryTotal = state.total;
      renderPapers();
      clearError("global-error");
    } catch (error) {
      if (request !== state.paperRequest) return;
      showError("global-error", error);
      if (!state.papers.length) emptyState($("paper-list"), { title: "暂时无法读取文献库", description: errorMessage(error), symbol: "info", action: { label: "重新连接", onClick: () => bootstrap() } });
    }
  }

  function renderPapers() {
    $("nav-paper-count").textContent = state.libraryTotal;
    $("stat-papers").textContent = state.libraryTotal;
    $("stat-files").textContent = state.papers.filter((paper) => paper.has_pdf).length;
    $("paper-results").textContent = state.query ? `${state.total} 个结果` : "全部文献";
    $("paper-pagination").hidden = state.total <= state.pageSize;
    $("page-summary").textContent = `${state.offset + 1}–${Math.min(state.offset + state.pageSize, state.total)} / ${state.total}`;
    $("page-prev").disabled = state.offset === 0;
    $("page-next").disabled = state.offset + state.pageSize >= state.total;
    if (!state.papers.length) {
      emptyState($("paper-list"), state.query ? {
        title: "没有找到匹配的文献", description: "试着使用标题中的其他词、作者姓名或 DOI 搜索。", symbol: "search",
        action: { label: "清除搜索", onClick: () => { $("paper-search").value = ""; state.query = ""; state.offset = 0; loadPapers(); } },
      } : {
        title: "从第一篇论文开始", description: "导入本地 PDF，整理书目信息与参考文献，逐步建立你的引用网络。原始文件保持只读。", symbol: "library",
        action: { label: "导入 PDF", icon: "plus", onClick: openImport },
      });
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const paper of state.papers) {
      const row = button("", () => selectPaper(paper.id), `paper-row${paper.id === state.selectedPaper ? " selected" : ""}`);
      row.setAttribute("aria-label", `查看文献：${titleOf(paper)}`);
      row.setAttribute("aria-pressed", paper.id === state.selectedPaper ? "true" : "false");
      const mark = element("span", `document-symbol${paper.has_pdf ? " has-pdf" : ""}`);
      mark.append(icon("file"));
      const main = element("div", "paper-row-main");
      main.append(element("h3", "paper-title", titleOf(paper)));
      const meta = element("div", "paper-meta");
      const authors = names(paper.authors);
      meta.append(element("span", "", authors.length ? `${authors.slice(0, 2).join(", ")}${authors.length > 2 ? " 等" : ""}` : "作者待核对"));
      if (paper.year) meta.append(element("span", "", paper.year));
      if (paper.venue) meta.append(element("span", "", paper.venue));
      main.append(meta);
      const end = element("span", "paper-row-end");
      end.append(paper.has_pdf ? badge("available", "PDF") : badge("draft", "无 PDF"), icon("chevron"));
      row.append(mark, main, end);
      fragment.append(row);
    }
    $("paper-list").replaceChildren(fragment);
  }

  async function selectPaper(id, { navigate = false, quiet = false } = {}) {
    if (navigate) await changeView("library");
    state.selectedPaper = id;
    const request = ++state.detailRequest;
    $("paper-detail").hidden = false;
    $("library-layout").classList.add("has-detail");
    renderPapers();
    if (!quiet) emptyState($("paper-detail"), { title: "正在读取文献", small: true, loading: true });
    try {
      const result = await api(`/api/papers/${encodeURIComponent(id)}`);
      if (request !== state.detailRequest) return;
      state.detail = result.paper || result;
      renderDetail(state.detail);
    } catch (error) {
      if (request !== state.detailRequest) return;
      emptyState($("paper-detail"), { title: "无法读取文献详情", description: errorMessage(error), symbol: "info", small: true });
    }
  }

  function detailField(list, key, value) {
    if (value === null || value === undefined || value === "") return;
    list.append(element("dt", "", key), element("dd", "", value));
  }

  function renderDetail(paper) {
    const header = element("div", "detail-header");
    header.append(element("span", "eyebrow", "PAPER DETAILS"));
    const close = button("", () => { state.selectedPaper = null; state.detail = null; $("paper-detail").hidden = true; $("library-layout").classList.remove("has-detail"); renderPapers(); }, "icon-button", "close");
    close.setAttribute("aria-label", "关闭文献详情");
    header.append(close);
    const body = element("div", "detail-content");
    body.append(element("h2", "detail-title", titleOf(paper)), element("p", "detail-authors", authorText(paper.authors) || "作者信息尚待核对"));
    const badges = element("div", "detail-badges");
    if (paper.year) badges.append(badge("draft", String(paper.year)));
    badges.append(badge(paper.record_status || "draft"));
    const latestRun = (paper.runs || [])[0];
    if (latestRun?.status && ["needs_ocr", "failed", "partial"].includes(latestRun.status)) badges.append(badge(latestRun.status));
    body.append(badges);
    const actions = element("div", "detail-actions");
    if (paper.file_id) actions.append(makePdfLink(paper.file_id));
    actions.append(button("编辑信息", () => openEdit({ kind: "paper", paper }), "button", "edit"));
    actions.append(button("合并重复文献", () => openMerge(paper), "button compact", "graph"));
    body.append(actions);
    const fields = element("dl", "detail-fields");
    detailField(fields, "出版物", paper.venue || "尚未识别");
    detailField(fields, "DOI", identifier(paper, "doi"));
    detailField(fields, "arXiv", identifier(paper, "arxiv"));
    detailField(fields, "参考条目", `${paper.reference_count ?? "—"} 条`);
    detailField(fields, "确认引用", `${paper.outgoing_count ?? "—"} 篇`);
    detailField(fields, "已知施引", `${paper.incoming_count ?? "—"} 篇 · 本地范围`);
    body.append(fields);
    const relationActions = element("div", "detail-actions");
    relationActions.append(button("复核参考文献", async () => { state.reviewPaper = paper.id; await changeView("review"); }, "button compact", "review"));
    relationActions.append(button("查看引用图", async () => { await updatePaperSelectors(); $("graph-center").value = paper.id; await changeView("graph"); }, "button compact", "graph"));
    body.append(relationActions);
    if (paper.abstract) { const section = element("section", "detail-section"); section.append(element("h3", "", "摘要"), element("p", "", paper.abstract)); body.append(section); }
    const filesSection = element("section", "detail-section");
    filesSection.append(element("h3", "", "PDF 附件"));
    if (!(paper.files || []).length) filesSection.append(element("p", "", "该文献目前只有书目信息。导入对应 PDF 后可关联附件。"));
    for (const file of paper.files || []) {
      const fileId = file.file_id || file.id;
      const location = (file.locations || []).find((item) => item.is_current && item.status === "available") || (file.locations || [])[0] || {};
      const card = element("div", "file-item");
      const top = element("div", "file-item-top");
      top.append(element("span", "file-item-title", location.display_name || file.display_name || file.name || "论文 PDF"));
      top.append(element("span", "small-label", sizeText(file.byte_size || file.size)));
      card.append(top);
      const uri = location.canonical_uri || file.canonical_uri;
      if (uri) card.append(element("div", "file-item-uri", uri));
      const fileActions = element("div", "detail-actions");
      fileActions.style.marginBottom = "0";
      if (file.available !== false) fileActions.append(makePdfLink(fileId, true));
      else fileActions.append(badge("missing"));
      fileActions.append(button("重新解析", (event) => actionWithButton(event.currentTarget, async () => {
        await api(`/api/files/${encodeURIComponent(fileId)}/parse`, { method: "POST", json: { force: true, engine: state.settings?.parser?.engine || "python-rules" } });
        toast("已加入解析队列。人工修正与已有证据会保留。");
        await loadJobs();
      }), "button compact", "refresh"));
      fileActions.append(button("重新定位", () => openImport({ rebind: fileId }), "button compact", "folder"));
      card.append(fileActions);
      filesSection.append(card);
    }
    body.append(filesSection);
    const citingSection = element("section", "detail-section");
    citingSection.append(element("h3", "", "本地已知施引文献"));
    const citingList = element("div", "mini-list");
    const loadCiting = button("查看施引列表", (event) => actionWithButton(event.currentTarget, async () => {
      const result = await api(`/api/papers/${encodeURIComponent(paper.id)}/citations`);
      const citing = itemsOf(result);
      citingList.replaceChildren(...citing.map((item) => button(titleOf(item), () => selectPaper(item.id || item.citing_paper_id), "mini-link")));
      if (!citing.length) citingList.append(element("p", "", "当前本地数据中还没有确认的施引文献。"));
    }), "button compact", "arrow");
    citingList.append(loadCiting); citingSection.append(citingList); body.append(citingSection);
    if ((paper.runs || []).length) {
      const section = element("section", "detail-section");
      section.append(element("h3", "", "最近解析"));
      const run = paper.runs[0];
      section.append(element("p", "", `${run.engine || run.parser_name || "本地解析"} · ${STATUS[run.status]?.[0] || run.status} · ${dateText(run.finished_at || run.started_at)}`));
      if (run.warning || run.message) section.append(element("p", "", run.warning || run.message));
      body.append(section);
    }
    $("paper-detail").replaceChildren(header, body);
  }

  async function updatePaperSelectors() {
    const result = await api("/api/papers?limit=1000&offset=0");
    state.allPapers = itemsOf(result);
    state.libraryTotal = Number(result.total ?? state.allPapers.length);
    const selectedIds = new Set([state.selectedPaper, state.reviewPaper, $("graph-center").value].filter(Boolean));
    for (const id of selectedIds) {
      if (!state.allPapers.some((paper) => String(paper.id) === String(id))) {
        try { const paper = await api(`/api/papers/${encodeURIComponent(id)}`); state.allPapers.push(paper.paper || paper); } catch (_) { /* A removed record must not break other selectors. */ }
      }
    }
    $("nav-paper-count").textContent = state.libraryTotal;
    $("stat-papers").textContent = state.libraryTotal;
    fillPaperSelect($("graph-center"), "全部文献", $("graph-center").value);
    fillPaperSelect($("review-paper"), "选择一篇文献", state.reviewPaper || $("review-paper").value || state.selectedPaper || state.allPapers.find((paper) => paper.reference_count)?.id || state.allPapers[0]?.id);
    state.reviewPaper = $("review-paper").value || null;
  }

  function fillPaperSelect(select, placeholder, value) {
    const option = element("option", "", placeholder);
    option.value = "";
    const children = [option];
    for (const paper of state.allPapers) {
      const item = element("option", "", `${titleOf(paper)}${paper.year ? ` (${paper.year})` : ""}`);
      item.value = paper.id;
      children.push(item);
    }
    if (state.libraryTotal > state.allPapers.length) {
      const note = element("option", "", "此处列出最近 1,000 篇；可从文献库搜索并打开其他文献");
      note.disabled = true; children.push(note);
    }
    select.replaceChildren(...children);
    if (value && state.allPapers.some((paper) => String(paper.id) === String(value))) select.value = value;
  }

  async function openImport(options = {}) {
    clearError("import-error");
    state.rebindFile = options.rebind || null;
    $("import-title").textContent = state.rebindFile ? "重新定位论文 PDF" : "导入论文 PDF";
    $("tab-managed").hidden = Boolean(state.rebindFile);
    $("browse-select-all").hidden = Boolean(state.rebindFile);
    $("import-linked").querySelector(".import-description").textContent = state.rebindFile ? "选择原 PDF 的新位置。后端核验内容哈希后恢复关联，文献身份和已有证据保持不变。" : "从允许目录中选择 PDF。原文件保留在原位置，工作台记录其 URI。";
    if (state.rebindFile) setImportMode("linked");
    $("submit-import").replaceChildren(icon(state.rebindFile ? "folder" : "plus"), document.createTextNode(state.rebindFile ? "核验并重新关联" : "导入并解析"));
    $("import-dialog").showModal();
    state.selectedFiles.clear();
    state.uploads = [];
    $("upload-input").value = "";
    renderUploads();
    updateImportCount();
    try {
      const result = await api("/api/roots");
      const roots = itemsOf(result);
      const select = $("browse-root");
      select.replaceChildren();
      if (!roots.length) {
        const option = element("option", "", "尚未设置论文目录"); option.value = ""; select.append(option);
        state.rootId = "";
        emptyState($("file-browser"), { title: "先设置一个论文目录", description: state.rebindFile ? "请先设置新文件所在目录，再选择相同内容的 PDF。" : "你也可以切换到“上传托管副本”直接选择文件。", symbol: "folder", small: true, action: { label: "设置论文目录", onClick: () => { $("import-dialog").close(); changeView("settings"); } } });
      } else {
        for (const root of roots) { const option = element("option", "", root.name || root.path || root.id); option.value = root.id; select.append(option); }
        state.rootId = roots.some((root) => root.id === state.rootId) ? state.rootId : roots[0].id;
        select.value = state.rootId;
        await browseFiles("");
      }
    } catch (error) { showError("import-error", error); }
  }

  function setImportMode(mode) {
    state.importMode = mode;
    for (const name of ["linked", "managed"]) {
      $("import-" + name).hidden = name !== mode;
      $("tab-" + name).classList.toggle("active", name === mode);
      $("tab-" + name).setAttribute("aria-selected", String(name === mode));
      $("tab-" + name).tabIndex = name === mode ? 0 : -1;
    }
    clearError("import-error");
    updateImportCount();
  }

  async function browseFiles(path) {
    if (!state.rootId) return;
    try {
      const result = await api(`/api/files/browse?${new URLSearchParams({ root_id: state.rootId, path })}`);
      state.browsePath = result.path ?? path;
      state.browseParent = result.parent;
      $("browse-path").textContent = `/ ${state.browsePath || "根目录"}`;
      $("browse-up").disabled = state.browseParent === null || state.browseParent === undefined || state.browsePath === "";
      const entries = itemsOf(result);
      if (!entries.length) {
        emptyState($("file-browser"), { title: "这个目录中没有 PDF", description: "选择其他目录，或上传一份 PDF。", symbol: "folder", small: true });
        updateSelectAllButton();
        return;
      }
      const fragment = document.createDocumentFragment();
      for (const entry of entries) {
        if (entry.type === "directory") {
          const row = button("", () => browseFiles(entry.path), "file-row directory");
          row.append(icon("folder"), element("span", "name", entry.name), icon("chevron"));
          fragment.append(row);
        } else if (entry.type === "pdf") {
          const row = element("label", "file-row");
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.dataset.pdfPath = entry.path;
          checkbox.checked = state.selectedFiles.has(entry.path);
          checkbox.addEventListener("change", () => {
            if (checkbox.checked) {
              if (state.rebindFile) { state.selectedFiles.clear(); $("file-browser").querySelectorAll("input[type=checkbox]").forEach((other) => { if (other !== checkbox) other.checked = false; }); }
              state.selectedFiles.add(entry.path);
            } else state.selectedFiles.delete(entry.path);
            updateImportCount();
          });
          row.append(checkbox, icon("file"), element("span", "name", entry.name), element("small", "", sizeText(entry.size)));
          fragment.append(row);
        }
      }
      $("file-browser").replaceChildren(fragment);
      updateSelectAllButton();
      clearError("import-error");
    } catch (error) { showError("import-error", error); }
  }

  function addUploads(files) {
    let ignored = 0;
    for (const file of files) {
      if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") { ignored++; continue; }
      if (!state.uploads.some((other) => other.name === file.name && other.size === file.size && other.lastModified === file.lastModified)) state.uploads.push(file);
    }
    if (ignored) showError("import-error", new Error(`已跳过 ${ignored} 个非 PDF 文件。`));
    renderUploads(); updateImportCount();
  }

  function renderUploads() {
    const children = state.uploads.map((file, index) => {
      const row = element("div", "upload-item");
      const remove = button("", () => { state.uploads.splice(index, 1); renderUploads(); updateImportCount(); }, "icon-button", "close");
      remove.setAttribute("aria-label", `移除 ${file.name}`);
      row.append(element("span", "", file.name), element("small", "", sizeText(file.size)), remove);
      return row;
    });
    $("upload-list").replaceChildren(...children);
  }

  // Select only PDFs shown in the current directory. Keep selections made
  // in other directories, and do not select subdirectories or truncated pages.
  function visiblePdfCheckboxes() {
    return Array.from($("file-browser").querySelectorAll('input[type="checkbox"][data-pdf-path]'));
  }

  function updateSelectAllButton() {
    const control = $("browse-select-all");
    const boxes = visiblePdfCheckboxes();
    const allSelected = boxes.length > 0 && boxes.every((box) => state.selectedFiles.has(box.dataset.pdfPath));
    control.hidden = Boolean(state.rebindFile);
    control.disabled = Boolean(state.rebindFile || state.importBusy || !boxes.length);
    control.textContent = allSelected ? "取消全选" : "全选";
    control.setAttribute("aria-label", allSelected ? "取消当前目录的 PDF 选择" : "全选当前目录的 PDF");
    control.title = "只影响当前目录已显示的 PDF，其他目录的选择保留";
  }

  function toggleSelectAllVisible() {
    if (state.importBusy || state.rebindFile) return;
    const boxes = visiblePdfCheckboxes();
    const shouldSelect = boxes.some((box) => !state.selectedFiles.has(box.dataset.pdfPath));
    for (const box of boxes) {
      box.checked = shouldSelect;
      if (shouldSelect) state.selectedFiles.add(box.dataset.pdfPath);
      else state.selectedFiles.delete(box.dataset.pdfPath);
    }
    updateImportCount();
  }

  function updateImportCount() {
    const count = state.importMode === "linked" ? state.selectedFiles.size : state.uploads.length;
    $("import-selection").textContent = count ? `已选择 ${count} 份 PDF` : "尚未选择文件";
    $("submit-import").disabled = !count || state.importBusy;
    updateSelectAllButton();
  }

  // The API limits linked imports to 100 paths and uploads to 20 files.
  // Submit large selections in bounded batches instead of rejecting "select all".
  async function importSelectedBatches() {
    const linked = state.importMode === "linked";
    const selected = linked ? Array.from(state.selectedFiles) : [...state.uploads];
    const batchSize = linked ? 100 : 20;
    const combined = { items: [], errors: [], summary: { added: 0, duplicates: 0, failed: 0 } };
    for (let start = 0; start < selected.length; start += batchSize) {
      const chunk = selected.slice(start, start + batchSize);
      let batch;
      try {
        if (linked) {
          batch = await api("/api/files/register", {
            method: "POST",
            json: { root_id: state.rootId, paths: chunk, request_key: crypto.randomUUID() },
          });
        } else {
          const form = new FormData();
          for (const file of chunk) form.append("files", file, file.name);
          form.append("request_key", crypto.randomUUID());
          batch = await api("/api/files/upload", { method: "POST", body: form });
        }
      } catch (error) {
        combined.errors.push({ path: `第 ${Math.floor(start / batchSize) + 1} 批`, message: errorMessage(error) });
        break;
      }
      const items = itemsOf(batch);
      combined.items.push(...items);
      combined.errors.push(...(batch.errors || []));
      combined.summary.added += batch.summary?.added ?? items.filter((item) => item.created !== false).length;
      combined.summary.duplicates += batch.summary?.duplicates ?? items.filter((item) => item.created === false).length;
    }
    combined.summary.failed = combined.errors.length;
    return combined;
  }

  async function submitImport() {
    if (state.importBusy) return;
    state.importBusy = true; updateImportCount(); clearError("import-error");
    const submit = $("submit-import");
    submit.textContent = "正在登记…";
    try {
      if (state.rebindFile) {
        await api(`/api/files/${encodeURIComponent(state.rebindFile)}/rebind`, { method: "POST", json: { root_id: state.rootId, path: Array.from(state.selectedFiles)[0] } });
        $("import-dialog").close();
        toast("已核验文件内容并恢复位置关联。");
        await refreshAfterChange();
        return;
      }
      const result = await importSelectedBatches();
      const imported = itemsOf(result);
      const errors = result.errors || [];
      const added = result.summary?.added ?? imported.filter((item) => item.created !== false).length;
      const duplicates = result.summary?.duplicates ?? imported.filter((item) => item.duplicate || item.created === false).length;
      const status = `新增 ${added} 份，跳过重复 ${duplicates} 份，失败 ${errors.length} 份。`;
      if (errors.length) {
        showError("import-error", new Error(`${status}\n${errors.map((error) => typeof error === "string" ? error : `${error.path || error.filename || error.name || "文件"}：${error.message || error.error?.message || "登记失败"}`).join("\n")}`));
      } else {
        $("import-dialog").close();
        toast(`导入完成：${status}${added ? " 已为新增 PDF 安排解析任务。" : ""}`);
      }
      if (imported.length) {
        state.query = ""; state.offset = 0; $("paper-search").value = "";
        await loadPapers(); await updatePaperSelectors(); await loadJobs();
        const firstNew = imported.find((item) => item.created !== false);
        if (firstNew?.paper_id) await selectPaper(firstNew.paper_id, { navigate: !errors.length });
      }
    } catch (error) { showError("import-error", error); }
    finally { state.importBusy = false; submit.replaceChildren(icon(state.rebindFile ? "folder" : "plus"), document.createTextNode(state.rebindFile ? "核验并重新关联" : "导入并解析")); updateImportCount(); }
  }

  function refStatus(reference) {
    return reference.status || reference.decision?.status || reference.current_assertion?.status || "pending";
  }
  function isAccepted(reference) { return ["accepted", "confirmed"].includes(refStatus(reference)); }
  function parsedReference(reference) { return asObject(reference.parsed || reference.parsed_json || reference.metadata); }

  async function loadReview({ quiet = false } = {}) {
    const id = $("review-paper").value || state.reviewPaper;
    state.reviewPaper = id;
    const request = ++state.reviewRequest;
    if (!id) {
      emptyState($("review-list"), { title: "选择一篇论文开始复核", description: "导入 PDF 后，可以逐条核对参考文献原文、匹配候选与引用证据。", symbol: "review", action: !state.allPapers.length ? { label: "导入 PDF", icon: "plus", onClick: openImport } : undefined });
      return;
    }
    if (!quiet) emptyState($("review-list"), { title: "正在加载参考条目", symbol: "review", loading: true });
    try {
      const result = await api(`/api/papers/${encodeURIComponent(id)}/references`);
      if (request !== state.reviewRequest) return;
      state.references = itemsOf(result);
      renderReview();
    } catch (error) {
      if (request !== state.reviewRequest) return;
      emptyState($("review-list"), { title: "无法读取参考条目", description: errorMessage(error), symbol: "info" });
    }
  }

  function renderReview() {
    const filter = $("review-filter").value;
    const references = state.references.filter((reference) => filter === "all" || (filter === "accepted" ? isAccepted(reference) : filter === "rejected" ? refStatus(reference) === "rejected" : !isAccepted(reference) && refStatus(reference) !== "rejected"));
    if (!references.length) {
      emptyState($("review-list"), state.references.length ? { title: "当前筛选下没有条目", description: "可以切换为“所有条目”查看已经确认或拒绝的结果。", symbol: "check", action: { label: "查看所有条目", onClick: () => { $("review-filter").value = "all"; renderReview(); } } } : { title: "尚无可复核的参考条目", description: "等待解析完成，或从文献详情重新解析。未提取到条目并不表示该论文没有参考文献。", symbol: "review", action: { label: "查看来源文献", onClick: () => selectPaper(state.reviewPaper, { navigate: true }) } });
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const reference of references) {
      const parsed = parsedReference(reference);
      const card = element("article", "review-card");
      const heading = element("div", "review-card-header");
      heading.append(element("span", "reference-number", `REFERENCE ${String(reference.ordinal ?? reference.position ?? "").padStart(2, "0")}`), badge(refStatus(reference)));
      card.append(heading, element("div", "review-raw", reference.raw_text || reference.text || "没有保存原始条目文本"));
      const meta = element("div", "review-parsed");
      if (parsed.title) meta.append(element("span", "", `标题：${parsed.title}`));
      if (parsed.year) meta.append(element("span", "", `年份：${parsed.year}`));
      if (parsed.doi) meta.append(element("span", "", `DOI：${parsed.doi}`));
      if (reference.page_start || reference.page) meta.append(element("span", "", `第 ${reference.page_start || reference.page} 页`));
      if (meta.childNodes.length) card.append(meta);
      const candidates = reference.candidates || [];
      if (!isAccepted(reference) && refStatus(reference) !== "rejected") {
        if (candidates.length) {
          const list = element("div", "review-candidates");
          for (const candidate of candidates) {
            const paper = candidate.target || candidate.paper || candidate;
            const targetId = candidate.paper_id || candidate.target_paper_id || candidate.cited_paper_id || paper.id;
            const row = element("div", "candidate-row");
            const content = element("div", "");
            content.append(element("strong", "", titleOf(paper)));
            const reason = candidate.reason || candidate.method || "匹配候选，等待确认";
            content.append(element("small", "", `${paper.year ? `${paper.year} · ` : ""}${typeof reason === "string" ? reason : JSON.stringify(reason)}`));
            row.append(content);
            if (targetId) row.append(button("接受匹配", (event) => actionWithButton(event.currentTarget, () => decideReference(reference, "accept", targetId)), "button compact primary", "check"));
            list.append(row);
          }
          card.append(list);
        }
        const choose = element("div", "match-existing");
        const select = document.createElement("select"); select.setAttribute("aria-label", `为第 ${reference.ordinal} 条参考文献选择已有文献`);
        const placeholder = element("option", "", "匹配到已有文献…"); placeholder.value = ""; select.append(placeholder);
        for (const paper of state.allPapers.filter((item) => item.id !== state.reviewPaper)) { const option = element("option", "", titleOf(paper)); option.value = paper.id; select.append(option); }
        const accept = button("确认", (event) => actionWithButton(event.currentTarget, () => decideReference(reference, "accept", select.value)), "button compact");
        accept.disabled = true;
        select.addEventListener("change", () => { accept.disabled = !select.value; });
        choose.append(select, accept);
        card.append(choose);
      }
      const actions = element("div", "review-actions");
      if (isAccepted(reference)) {
        const targetId = reference.target?.id || reference.target_paper_id || reference.cited_paper_id || reference.current_assertion?.cited_paper_id;
        actions.append(element("span", "hint", "此条目已形成确认引用。"));
        if (targetId) actions.append(button("查看被引文献", () => selectPaper(targetId, { navigate: true }), "button compact", "arrow"));
        actions.append(button("撤回确认", (event) => actionWithButton(event.currentTarget, () => decideReference(reference, "retract")), "button compact"));
      } else if (refStatus(reference) === "rejected") {
        actions.append(element("span", "hint", "已拒绝此项，自动解析不会直接覆盖这次决定。"));
        actions.append(button("重新复核", (event) => actionWithButton(event.currentTarget, () => decideReference(reference, "retract")), "button compact", "refresh"));
      } else {
        actions.append(element("span", "hint", candidates.length ? "确认身份后建立有向引用。" : "尚未找到唯一的已有文献。"));
        actions.append(button("新建文献并确认", () => openEdit({ kind: "reference", reference, paper: parsed }), "button compact", "plus"));
        actions.append(button("拒绝条目", (event) => actionWithButton(event.currentTarget, () => decideReference(reference, "reject")), "button compact danger"));
      }
      card.append(actions);
      fragment.append(card);
    }
    $("review-list").replaceChildren(fragment);
  }

  async function decideReference(reference, action, targetId, metadata, reason) {
    const body = { action };
    if (targetId) body.target_paper_id = targetId;
    if (metadata) body.metadata = metadata;
    if (reason) body.reason = reason;
    await api(`/api/references/${encodeURIComponent(reference.id)}/decisions`, { method: "POST", json: body });
    toast(action === "accept" ? "引用关系已确认，原始证据已保留。" : action === "reject" ? "已保存拒绝决定。" : "已撤回决定，可以重新复核。");
    await refreshAfterChange();
  }

  function openEdit(context) {
    state.editContext = context;
    const paper = context.paper || {};
    $("edit-title").textContent = context.kind === "reference" ? "新建被引文献" : "编辑文献信息";
    $("edit-submit").textContent = context.kind === "reference" ? "新建并确认引用" : "保存修改";
    $("edit-paper-title").value = paper.title || "";
    $("edit-authors").value = authorText(paper.authors);
    $("edit-year").value = paper.year || "";
    $("edit-venue").value = paper.venue || paper.journal || "";
    $("edit-doi").value = identifier(paper, "doi");
    $("edit-reason").value = "";
    $("edit-reason").closest("label").hidden = context.kind !== "reference";
    clearError("edit-error");
    $("edit-dialog").showModal();
    $("edit-paper-title").focus();
  }

  async function submitEdit(event) {
    event.preventDefault();
    const context = state.editContext;
    if (!context) return;
    const yearText = $("edit-year").value.trim();
    const metadata = {
      title: $("edit-paper-title").value.trim(), authors: $("edit-authors").value.split(/[;；]/).map((value) => value.trim()).filter(Boolean),
      year: yearText ? Number(yearText) : null, venue: $("edit-venue").value.trim() || null, doi: $("edit-doi").value.trim() || null,
    };
    if (!metadata.title) { showError("edit-error", new Error("请填写文献标题。")); return; }
    const submit = $("edit-submit"); submit.disabled = true; clearError("edit-error");
    try {
      if (context.kind === "reference") await decideReference(context.reference, "accept", null, metadata, $("edit-reason").value.trim());
      else {
        await api(`/api/papers/${encodeURIComponent(context.paper.id)}`, { method: "PATCH", json: { fields: metadata, expected_version: context.paper.row_version ?? context.paper.version } });
        toast("文献信息已保存，人工修改将受到保护。");
        await refreshAfterChange();
      }
      $("edit-dialog").close();
    } catch (error) { showError("edit-error", error); }
    finally { submit.disabled = false; }
  }

  async function openMerge(paper) {
    state.mergeSource = paper;
    clearError("merge-error");
    $("merge-source-title").textContent = titleOf(paper);
    $("merge-reason").value = "";
    $("merge-preview").textContent = "请选择确属同一篇论文的记录。";
    $("merge-submit").disabled = true;
    $("merge-dialog").showModal();
    try {
      await updatePaperSelectors();
      const select = $("merge-target");
      const placeholder = element("option", "", "选择保留身份的文献"); placeholder.value = "";
      select.replaceChildren(placeholder);
      for (const target of state.allPapers.filter((item) => item.id !== paper.id)) {
        const option = element("option", "", `${titleOf(target)}${target.year ? ` (${target.year})` : ""}`);
        option.value = target.id; select.append(option);
      }
      if (select.options.length === 1) showError("merge-error", new Error("文献库中尚无其他可合并记录。"));
    } catch (error) { showError("merge-error", error); }
  }

  function updateMergeTarget() {
    const target = state.allPapers.find((paper) => paper.id === $("merge-target").value);
    $("merge-submit").disabled = !target;
    $("merge-preview").textContent = target ? [titleOf(target), authorText(target.authors), target.year, identifier(target, "doi")].filter(Boolean).join(" · ") : "请选择确属同一篇论文的记录。";
  }

  async function submitMerge(event) {
    event.preventDefault();
    const source = state.mergeSource;
    const target = state.allPapers.find((paper) => paper.id === $("merge-target").value);
    if (!source || !target || source.id === target.id) return;
    clearError("merge-error"); $("merge-submit").disabled = true;
    try {
      const result = await api("/api/paper-merges", { method: "POST", json: { from_paper_id: source.id, to_paper_id: target.id, reason: $("merge-reason").value.trim() } });
      state.lastMerge = { ...result, sourceTitle: titleOf(source), targetTitle: titleOf(target) };
      state.selectedPaper = target.id;
      if (state.reviewPaper === source.id) state.reviewPaper = target.id;
      if ($("graph-center").value === source.id) $("graph-center").value = target.id;
      $("merge-dialog").close();
      renderMergeFeedback();
      toast("重复文献已合并，来源记录保留为别名。");
      await refreshAfterChange();
    } catch (error) { showError("merge-error", error); }
    finally { updateMergeTarget(); }
  }

  function renderMergeFeedback(error) {
    const box = $("merge-feedback");
    const merge = state.lastMerge;
    box.hidden = !merge;
    if (!merge) { box.replaceChildren(); return; }
    box.replaceChildren(element("span", "", `已将「${merge.sourceTitle}」合并到「${merge.targetTitle}」。`));
    const undo = button("撤销本次合并", async (event) => {
      const target = event.currentTarget; target.disabled = true;
      try {
        await api(`/api/paper-merges/${encodeURIComponent(merge.id)}/revert`, { method: "POST" });
        state.lastMerge = null; state.selectedPaper = merge.from_paper_id;
        renderMergeFeedback(); toast("本次合并已撤销，原文献身份已恢复。");
        await refreshAfterChange();
      } catch (failure) { renderMergeFeedback(failure); }
      finally { target.disabled = false; }
    }, "button compact", "refresh");
    box.append(undo);
    if (error) box.append(element("p", "merge-conflict", `未能撤销：${errorMessage(error)} 请先核对合并后的变更；工作台不会强制覆盖。`));
  }

  async function openRevision() {
    const paperId = $("review-paper").value || state.reviewPaper;
    if (!paperId) { toast("请先选择一篇来源文献。", true); return; }
    state.revisionContext = null;
    clearError("revision-error");
    $("revision-text").value = ""; $("revision-submit").disabled = true;
    $("revision-paper-title").textContent = "正在读取来源快照…";
    const placeholder = element("option", "", "选择一个来源快照"); placeholder.value = "";
    $("revision-run").replaceChildren(placeholder);
    $("revision-count").textContent = "尚未选择来源";
    $("revision-dialog").showModal();
    try {
      const [detailResult, referencesResult] = await Promise.all([api(`/api/papers/${encodeURIComponent(paperId)}`), api(`/api/papers/${encodeURIComponent(paperId)}/references`)]);
      const paper = detailResult.paper || detailResult;
      const runs = (paper.runs || []).filter((run) => Boolean(run.is_active));
      state.revisionContext = { paper, references: itemsOf(referencesResult), runs };
      $("revision-paper-title").textContent = titleOf(paper);
      for (const run of runs) {
        const file = (paper.files || []).find((item) => (item.file_id || item.id) === run.asset_id);
        const name = file?.locations?.find((location) => location.is_current)?.display_name || file?.locations?.[0]?.display_name || `PDF ${String(run.asset_id || "").slice(0, 8)}`;
        const option = element("option", "", `${name} · ${run.parser_name || run.engine || "解析快照"} · ${dateText(run.finished_at || run.started_at)} · ${String(run.id).slice(0, 8)}`);
        option.value = run.id; $("revision-run").append(option);
      }
      if (runs.length === 1) { $("revision-run").value = runs[0].id; loadRevisionRun(); }
      else if (!runs.length) showError("revision-error", new Error("当前文献没有活动解析快照，暂时无法创建基于来源的条目修订。请先完成一次 PDF 解析。"));
      else $("revision-count").textContent = `发现 ${runs.length} 个来源快照，请明确选择其中一个`;
    } catch (error) { showError("revision-error", error); }
  }

  function loadRevisionRun() {
    const context = state.revisionContext;
    const runId = $("revision-run").value;
    if (!context || !context.runs.some((run) => run.id === runId)) { $("revision-text").value = ""; updateRevisionCount(); return; }
    const entries = context.references.filter((reference) => reference.run_id === runId && reference.active !== false).sort((a, b) => a.ordinal - b.ordinal);
    $("revision-text").value = entries.map((reference) => (reference.raw_text || "").trim().replace(/\n[\t ]*\n+/g, "\n")).filter(Boolean).join("\n\n");
    clearError("revision-error");
    updateRevisionCount();
  }

  function revisionBlocks() {
    return $("revision-text").value.replace(/\r\n?/g, "\n").split(/\n[\t ]*\n+/).map((block) => block.trim()).filter(Boolean);
  }

  function updateRevisionCount() {
    const blocks = revisionBlocks();
    const hasSource = Boolean($("revision-run").value);
    $("revision-count").textContent = hasSource ? `将保存 ${blocks.length} 条参考文献（完整列表）` : "尚未选择来源";
    $("revision-submit").disabled = !hasSource || !blocks.length;
  }

  async function submitRevision(event) {
    event.preventDefault();
    const context = state.revisionContext;
    const baseRunId = $("revision-run").value;
    const blocks = revisionBlocks();
    if (!context || !context.runs.some((run) => run.id === baseRunId) || !blocks.length) { showError("revision-error", new Error("请选择一个活动来源快照，并填写非空的完整参考条目列表。")); return; }
    clearError("revision-error"); $("revision-submit").disabled = true;
    try {
      await api(`/api/papers/${encodeURIComponent(context.paper.id)}/reference-revisions`, { method: "POST", json: { base_run_id: baseRunId, references: blocks.map((raw_text, index) => ({ ordinal: index + 1, raw_text, label: String(index + 1) })) } });
      $("revision-dialog").close();
      state.reviewPaper = context.paper.id;
      toast(`已保存 ${blocks.length} 条人工修订，并保留原始解析证据。`);
      await refreshAfterChange();
    } catch (error) { showError("revision-error", error); }
    finally { updateRevisionCount(); }
  }

  async function loadGraph() {
    const request = ++state.graphRequest;
    const params = new URLSearchParams({ depth: $("graph-depth").value, direction: $("graph-direction").value, include_pending: String($("graph-pending").checked) });
    if ($("graph-center").value) params.set("center", $("graph-center").value);
    try {
      const graph = await api(`/api/graph?${params}`);
      if (request !== state.graphRequest) return;
      state.graph = graph;
      renderGraph(graph);
    } catch (error) {
      if (request !== state.graphRequest) return;
      $("graph-empty").hidden = false;
      emptyState($("graph-empty"), { title: "暂时无法加载引用图", description: errorMessage(error), symbol: "info", small: true });
      $("graph-counts").textContent = "加载失败";
    }
  }

  function nodeIsPending(node) { return node.kind === "reference" || node.type === "reference" || node.pending || String(node.id).startsWith("reference:"); }
  function renderGraph(graph) {
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];
    if (state.cy) { state.cy.destroy(); state.cy = null; }
    $("graph-counts").textContent = `${nodes.length} 个节点 · ${edges.length} 条显示关系`;
    $("graph-coverage").textContent = graph.truncated ? "已到显示上限 · 请选择更小范围" : "范围：本地已知引用；待确认项不计入被引用数";
    $("graph-coverage").classList.toggle("truncated", Boolean(graph.truncated));
    renderGraphOverview(graph);
    if (!nodes.length) {
      $("graph-empty").hidden = false;
      emptyState($("graph-empty"), { title: "引用网络将在这里展开", description: "导入论文并确认参考文献后，即可查看有方向、有证据的文献关系。", symbol: "graph", small: true, action: { label: "前往复核", onClick: () => changeView("review") } });
      return;
    }
    if (typeof window.cytoscape !== "function") {
      $("graph-empty").hidden = false;
      emptyState($("graph-empty"), { title: "本地图形组件未能加载", description: "请重新加载页面或检查应用安装中的 Cytoscape 静态资源。右侧仍可浏览真实文献节点。", symbol: "info", small: true });
      return;
    }
    $("graph-empty").hidden = true;
    const elements = [];
    const nodeIds = new Set(nodes.map((node) => String(node.id)));
    for (const node of nodes) {
      const title = node.title || node.label || node.raw_text || "待确认参考条目";
      elements.push({ data: { id: String(node.id), label: title.length > 53 ? `${title.slice(0, 50)}…` : title, raw: node }, classes: `${nodeIsPending(node) ? "pending" : "paper"}${node.has_pdf ? " has-pdf" : ""}${String(node.paper_id || node.id) === $("graph-center").value ? " center" : ""}` });
    }
    edges.forEach((edge, index) => {
      const source = String(edge.source || edge.citing_paper_id), target = String(edge.target || edge.cited_paper_id);
      if (!nodeIds.has(source) || !nodeIds.has(target)) return;
      elements.push({ data: { id: String(edge.id || `edge:${index}`), source, target, raw: edge }, classes: edge.pending || edge.status === "pending" || edge.type === "pending" || edge.confirmed === false ? "pending" : "confirmed" });
    });
    state.cy = window.cytoscape({
      container: $("graph-canvas"), elements, minZoom: 0.25, maxZoom: 3, wheelSensitivity: 0.25,
      style: [
        { selector: "node", style: { "background-color": "#8fb07c", width: 22, height: 22, label: "data(label)", color: "#587044", "font-size": 10, "font-family": '"Segoe UI","Microsoft YaHei",sans-serif', "text-wrap": "wrap", "text-max-width": 145, "text-valign": "bottom", "text-margin-y": 9, "border-width": 2, "border-color": "#fcfdf9", "overlay-opacity": 0 } },
        { selector: "node.has-pdf", style: { "background-color": "#367a58", "border-color": "#c8ddbf", "border-width": 4 } },
        { selector: "node.center", style: { width: 31, height: 31, "background-color": "#1e6648", "border-width": 6, "font-weight": 650 } },
        { selector: "node.pending", style: { "background-color": "#efdfbb", "border-color": "#d6c49c", "border-style": "dashed", shape: "round-rectangle", color: "#9b895e", width: 18, height: 18 } },
        { selector: "edge", style: { width: 1.3, "line-color": "#b1c6a3", "target-arrow-color": "#89a773", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.8, "overlay-opacity": 0 } },
        { selector: "edge.pending", style: { "line-style": "dashed", "line-color": "#d5c69e", "target-arrow-color": "#c9b786" } },
        { selector: ":selected", style: { "border-color": "#deae56", "border-width": 4, "line-color": "#6d9455", "target-arrow-color": "#6d9455" } },
      ],
      layout: { name: "cose", animate: false, fit: true, padding: 62, nodeRepulsion: () => 10000, idealEdgeLength: () => 140, componentSpacing: 120, randomize: true },
    });
    state.cy.on("tap", "node", (event) => showGraphNode(event.target.data("raw")));
    state.cy.on("tap", "edge", (event) => showGraphEdge(event.target.data("raw")));
    state.cy.on("tap", (event) => { if (event.target === state.cy) renderGraphOverview(graph); });
    window.requestAnimationFrame(() => { if (state.cy && state.view === "graph") { state.cy.resize(); state.cy.fit(undefined, 62); } });
  }

  function renderGraphOverview(graph) {
    const box = $("graph-inspector");
    const mark = element("div", "empty-symbol"); mark.append(icon("graph"));
    box.replaceChildren(element("div", "eyebrow", "NETWORK INSPECTOR"), mark, element("h3", "", "阅读文献之间的关系"), element("p", "", "点击节点查看文献；点击箭头查看引用证据。待确认关系使用虚线。"));
    const nodes = graph.nodes || [];
    if (nodes.length) {
      const details = element("details", "technical-details");
      details.append(element("summary", "", `节点列表（${nodes.length}）`));
      const list = element("div", "mini-list");
      for (const node of nodes) list.append(button(node.title || node.label || "待确认条目", () => showGraphNode(node), "mini-link"));
      details.append(list); box.append(details);
    }
    box.append(element("p", "divider-note", "当前图仅反映已导入与已确认的数据，不代表全球完整被引用关系。"));
  }

  function showGraphNode(node) {
    const box = $("graph-inspector");
    box.replaceChildren(element("div", "eyebrow", nodeIsPending(node) ? "REFERENCE CANDIDATE" : "SELECTED PAPER"), element("h3", "detail-title", node.title || node.label || "待确认参考条目"));
    if (node.year) box.append(element("p", "", String(node.year)));
    if (nodeIsPending(node)) {
      if (node.raw_text) box.append(element("p", "", node.raw_text));
      box.append(badge("pending"), element("p", "divider-note", "此条目尚未确认为文献身份，不计入已确认引用统计。"));
      box.append(button("前往复核", async () => {
        const source = node.source_paper_id || state.graph?.edges?.find((edge) => edge.target === node.id)?.source;
        if (source) state.reviewPaper = String(source).replace(/^paper:/, "");
        await changeView("review");
      }, "button compact", "review"));
      return;
    }
    const paperId = node.paper_id || String(node.id).replace(/^paper:/, "");
    const actions = element("div", "detail-actions");
    actions.append(button("文献详情", () => selectPaper(paperId, { navigate: true }), "button compact", "file"));
    if (node.file_id) actions.append(makePdfLink(node.file_id, true));
    box.append(actions);
    box.append(button("以此为中心展开", async () => { $("graph-center").value = paperId; await loadGraph(); }, "button compact", "graph"));
    if (!node.has_pdf) box.append(element("p", "divider-note", "这篇文献还没有可用的本地 PDF。"));
  }

  async function showGraphEdge(edge) {
    const box = $("graph-inspector");
    const source = edge.source || edge.citing_paper_id, target = edge.target || edge.cited_paper_id;
    const pending = edge.pending || edge.status === "pending" || edge.type === "pending" || edge.confirmed === false;
    box.replaceChildren(element("div", "eyebrow", "CITATION EVIDENCE"), element("h3", "", pending ? "待确认参考关系" : "引用关系的来源"));
    if (pending) {
      box.append(element("p", "", "待确认项尚未形成有效引用。请回到来源论文复核其原始条目。"), button("前往复核", () => { state.reviewPaper = String(source).replace(/^paper:/, ""); changeView("review"); }, "button compact", "review"));
      return;
    }
    box.append(element("p", "", "正在读取原始证据…"));
    try {
      const result = await api(`/api/graph/edges?${new URLSearchParams({ source: String(source).replace(/^paper:/, ""), target: String(target).replace(/^paper:/, "") })}`);
      box.replaceChildren(element("div", "eyebrow", "CITATION EVIDENCE"), element("h3", "", `${itemsOf(result).length} 条来源证据`));
      for (const evidence of itemsOf(result)) {
        const card = element("div", "evidence-card");
        const raw = evidence.raw_text || evidence.reference?.raw_text || evidence.entry?.raw_text;
        card.append(element("p", "", raw || evidence.reason || "已确认引用关系"));
        card.append(element("small", "", [evidence.parser_name || evidence.engine || evidence.source_kind, evidence.actor, dateText(evidence.created_at)].filter(Boolean).join(" · ")));
        if (evidence.file_id || evidence.asset_id) card.append(makePdfLink(evidence.file_id || evidence.asset_id, true));
        box.append(card);
      }
      if (!itemsOf(result).length) box.append(element("p", "", "该关系的证据已发生变化，请刷新图谱。"));
    } catch (error) { box.append(element("p", "", errorMessage(error))); }
  }

  async function loadJobs(forceRender = false) {
    const result = await api("/api/jobs");
    const jobs = itemsOf(result);
    const signature = JSON.stringify(jobs.map((job) => [job.id, job.status, job.progress, job.updated_at, job.completed_at]));
    const changed = state.jobsSignature !== signature;
    const wasInitialized = state.jobsSignature !== "";
    const oldStates = new Map(state.jobs.map((job) => [job.id, job.status]));
    const completed = wasInitialized && jobs.some((job) => ["succeeded", "partial", "failed", "interrupted"].includes(job.status) && oldStates.get(job.id) !== job.status);
    state.jobs = jobs; state.jobsSignature = signature;
    const active = jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
    $("nav-job-count").textContent = active; $("nav-job-count").hidden = !active; $("stat-running").textContent = active;
    if (changed || forceRender) renderJobs();
    if (completed) { await refreshAfterChange(); renderJobs(); }
  }

  function renderJobs() {
    if (!state.jobs.length) {
      emptyState($("task-list"), { title: "暂无解析任务", description: "导入 PDF 后，后台任务将在这里显示。你可以查看进度、异常和处理结果。", symbol: "task", action: { label: "导入 PDF", icon: "plus", onClick: openImport } });
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const job of state.jobs) {
      const row = element("article", "task-row");
      const mark = element("div", "task-symbol"); mark.append(icon(job.status === "succeeded" ? "check" : "task"));
      const main = element("div", "task-main");
      const heading = element("div", "task-top");
      const payload = asObject(job.input_data || job.input || job.payload || job.payload_json);
      const result = asObject(job.result || job.result_json);
      const relatedPaper = state.papers.find((paper) => paper.id === job.paper_id) || state.allPapers.find((paper) => paper.id === job.paper_id);
      const jobTitle = job.title || job.filename || job.file_name || result.title || payload.filename || (relatedPaper ? titleOf(relatedPaper) : null) || (job.kind === "backup" || job.type === "backup" ? "工作空间备份" : "PDF 文献解析");
      heading.append(element("h3", "", jobTitle), badge(job.status));
      const phase = job.step || job.phase || job.message || "";
      main.append(heading, element("p", "task-meta", [dateText(job.created_at || job.started_at), phase, job.attempts || job.attempt ? `第 ${job.attempts || job.attempt} 次尝试` : ""].filter(Boolean).join(" · ")));
      if (["queued", "running"].includes(job.status)) {
        const progress = element("div", "progress-track");
        const fill = element("div", "progress-fill");
        let value = Number(job.progress);
        if (job.progress === null || job.progress === undefined || !Number.isFinite(value)) fill.classList.add("indeterminate");
        else { if (value <= 1) value *= 100; fill.style.width = `${Math.min(100, Math.max(0, value))}%`; progress.setAttribute("role", "progressbar"); progress.setAttribute("aria-valuenow", String(Math.round(value))); progress.setAttribute("aria-label", "解析进度"); }
        progress.append(fill); main.append(progress);
      }
      const error = asObject(job.error || job.error_json);
      const errorText = typeof job.error === "string" ? job.error : error.message || job.error_message;
      if (errorText) main.append(element("p", "task-error", errorText));
      if (result.status === "needs_ocr" || result.parse_status === "needs_ocr") main.append(element("p", "task-error", "未检测到有效文字层，需要 OCR 或人工录入；文件仍可打开。"));
      const details = element("details", "technical-details");
      details.append(element("summary", "", "任务详情"), element("p", "", `任务 ID：${job.id}`));
      if (result.message) details.append(element("p", "", result.message));
      if (result.warnings?.length) details.append(element("p", "", result.warnings.join("；")));
      main.append(details);
      const actions = element("div", "task-actions");
      if (["failed", "interrupted", "partial", "cancelled"].includes(job.status)) actions.append(button("重试", (event) => actionWithButton(event.currentTarget, async () => { await api(`/api/jobs/${encodeURIComponent(job.id)}/retry`, { method: "POST", json: {} }); toast("任务已重新加入队列。"); await loadJobs(true); }), "button compact", "refresh"));
      if (["queued", "running"].includes(job.status)) actions.append(button("取消", (event) => actionWithButton(event.currentTarget, async () => { await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", json: {} }); toast("已请求取消任务。"); await loadJobs(true); }), "button compact"));
      const paperId = job.paper_id || result.paper_id || payload.paper_id;
      if (paperId) actions.append(button("查看文献", () => selectPaper(paperId, { navigate: true }), "button compact", "arrow"));
      row.append(mark, main, actions); fragment.append(row);
    }
    $("task-list").replaceChildren(fragment);
  }

  async function refreshAfterChange() {
    await loadPapers({ quiet: true });
    await updatePaperSelectors();
    if (state.selectedPaper && !$("edit-dialog").open) await selectPaper(state.selectedPaper, { quiet: true });
    if (state.view === "review") await loadReview({ quiet: true });
    if (state.view === "graph") await loadGraph();
  }

  async function loadSettings() {
    state.settings = await api("/api/settings");
    $("allowed-roots").value = (state.settings.allowed_roots || []).map((root) => typeof root === "string" ? root : root.path).filter(Boolean).join("\n");
    $("parser-engine").value = state.settings.parser?.engine || state.settings.engine || "python-rules";
    updateParserHint();
    const limits = state.settings.limits || {};
    const parts = [];
    if (limits.max_pdf_size_mb) parts.push(`单文件上限 ${limits.max_pdf_size_mb} MB`);
    if (limits.max_pdf_pages) parts.push(`页数上限 ${limits.max_pdf_pages}`);
    if (state.settings.parser?.timeout_seconds) parts.push(`解析时限 ${state.settings.parser.timeout_seconds} 秒`);
    $("settings-limits").textContent = parts.join(" · ");
  }

  function updateParserHint() {
    const grobidUrl = state.settings?.parser?.grobid_url || state.settings?.parser?.grobid?.base_url;
    $("parser-hint").textContent = $("parser-engine").value === "grobid-local" ? `需另行运行本机 GROBID 服务${grobidUrl ? `（${grobidUrl}）` : ""}。外部元数据补全默认关闭。` : "本地规则解析保留原文，不能确定的结果进入复核。";
  }

  async function saveSettings(event) {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type=submit]");
    await actionWithButton(submit, async () => {
      const roots = $("allowed-roots").value.split("\n").map((value) => value.trim()).filter(Boolean);
      await api("/api/settings", { method: "PUT", json: { allowed_roots: roots, engine: $("parser-engine").value } });
      await loadSettings();
      $("settings-feedback").textContent = "设置已保存";
      toast("目录与解析设置已保存。");
      window.setTimeout(() => { $("settings-feedback").textContent = ""; }, 5000);
    }, "正在保存…");
  }

  async function downloadExport(format, trigger) {
    const run = async () => {
      const response = await fetch(`/api/exports/${encodeURIComponent(format)}`, { credentials: "same-origin" });
      if (!response.ok) {
        let data = {}; try { data = await response.json(); } catch (_) {}
        throw new Error(data.error?.message || "导出未完成，请重试。");
      }
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = href;
      link.download = `reference-topology-${format}.${format === "bibtex" ? "bib" : "json"}`;
      document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 1000);
      toast("导出文件已准备好。");
    };
    if (trigger) await actionWithButton(trigger, run, "准备导出…"); else { try { await run(); } catch (error) { toast(errorMessage(error), true); } }
  }

  async function createBackup(event) {
    await actionWithButton(event.currentTarget, async () => {
      const result = await api("/api/backups", { method: "POST", json: {} });
      const box = $("backup-result");
      box.replaceChildren(element("p", "", "备份已创建。原位 PDF 请另外保留。"));
      if (result.download_url) {
        const url = new URL(result.download_url, location.origin);
        if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) throw new Error("备份已生成，但下载地址不属于当前工作台。");
        const link = element("a", "button compact", "下载备份"); link.href = url.href; link.download = ""; box.append(link);
      }
      if (result.backup_id) box.append(element("p", "technical-details", `备份编号：${result.backup_id}`));
      toast("工作空间备份已创建。");
    }, "正在备份…");
  }

  function bindEvents() {
    document.querySelectorAll("[data-view]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); changeView(link.dataset.view); }));
    window.addEventListener("hashchange", () => changeView(location.hash.slice(1), { fromHash: true }));
    $("open-import").addEventListener("click", openImport);
    document.querySelectorAll("[data-close-dialog]").forEach((node) => node.addEventListener("click", () => { if (node.dataset.closeDialog === "import-dialog" && state.importBusy) return; $(node.dataset.closeDialog).close(); }));
    $("import-dialog").addEventListener("cancel", (event) => { if (state.importBusy) event.preventDefault(); });
    for (const mode of ["linked", "managed"]) $("tab-" + mode).addEventListener("click", () => setImportMode(mode));
    document.querySelector(".modal-tabs").addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); const next = state.importMode === "linked" ? "managed" : "linked"; setImportMode(next); $("tab-" + next).focus(); } });
    $("browse-root").addEventListener("change", () => { state.rootId = $("browse-root").value; state.selectedFiles.clear(); updateImportCount(); browseFiles(""); });
    $("browse-up").addEventListener("click", () => browseFiles(state.browseParent || ""));
    $("browse-select-all").addEventListener("click", toggleSelectAllVisible);
    $("upload-input").addEventListener("change", (event) => addUploads(event.target.files));
    const zone = $("upload-zone");
    for (const type of ["dragenter", "dragover"]) zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.add("drag-over"); });
    for (const type of ["dragleave", "drop"]) zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.remove("drag-over"); });
    zone.addEventListener("drop", (event) => addUploads(event.dataTransfer.files));
    $("submit-import").addEventListener("click", submitImport);
    let searchTimer;
    $("paper-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = window.setTimeout(() => { state.query = $("paper-search").value.trim(); state.offset = 0; loadPapers(); }, 220); });
    $("refresh-library").addEventListener("click", (event) => actionWithButton(event.currentTarget, () => refreshAfterChange()));
    $("page-prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.pageSize); loadPapers(); });
    $("page-next").addEventListener("click", () => { state.offset += state.pageSize; loadPapers(); });
    $("refresh-tasks").addEventListener("click", (event) => actionWithButton(event.currentTarget, () => loadJobs(true)));
    $("review-paper").addEventListener("change", () => { state.reviewPaper = $("review-paper").value; loadReview(); });
    $("review-filter").addEventListener("change", renderReview);
    $("refresh-review").addEventListener("click", () => loadReview());
    $("edit-form").addEventListener("submit", submitEdit);
    $("merge-form").addEventListener("submit", submitMerge);
    $("merge-target").addEventListener("change", updateMergeTarget);
    $("revise-references").addEventListener("click", openRevision);
    $("revision-run").addEventListener("change", loadRevisionRun);
    $("revision-text").addEventListener("input", updateRevisionCount);
    $("revision-form").addEventListener("submit", submitRevision);
    $("refresh-graph").addEventListener("click", () => loadGraph());
    for (const id of ["graph-center", "graph-direction", "graph-depth", "graph-pending"]) $(id).addEventListener("change", () => loadGraph());
    $("graph-zoom-in").addEventListener("click", () => { if (state.cy) state.cy.zoom({ level: state.cy.zoom() * 1.25, renderedPosition: { x: state.cy.width() / 2, y: state.cy.height() / 2 } }); });
    $("graph-zoom-out").addEventListener("click", () => { if (state.cy) state.cy.zoom({ level: state.cy.zoom() / 1.25, renderedPosition: { x: state.cy.width() / 2, y: state.cy.height() / 2 } }); });
    $("graph-fit").addEventListener("click", () => { if (state.cy) state.cy.fit(undefined, 60); });
    $("settings-form").addEventListener("submit", saveSettings);
    $("parser-engine").addEventListener("change", updateParserHint);
    $("create-backup").addEventListener("click", createBackup);
    $("export-graph").addEventListener("click", (event) => downloadExport("graph-json", event.currentTarget));
    document.querySelectorAll("[data-export]").forEach((node) => node.addEventListener("click", (event) => downloadExport(node.dataset.export, event.currentTarget)));
    window.addEventListener("resize", () => { if (state.cy && state.view === "graph") state.cy.resize(); });
  }

  async function bootstrap() {
    try {
      const session = await api("/api/session");
      state.csrf = session.csrf_token;
      $("connection-label").textContent = "已连接 · 数据保存在本机";
      clearError("global-error");
      await Promise.all([loadPapers(), loadSettings(), loadJobs(true)]);
      state.initialized = true;
      await changeView(location.hash.slice(1) || "library", { fromHash: true });
    } catch (error) {
      $("connection-label").textContent = "服务连接异常";
      showError("global-error", error);
      if (!state.papers.length) emptyState($("paper-list"), { title: "正在等待本机服务", description: errorMessage(error), symbol: "info", action: { label: "重新连接", onClick: bootstrap } });
    }
  }

  bindEvents();
  setImportMode("linked");
  bootstrap();
  window.setInterval(async () => {
    if (!state.initialized || state.polling || document.hidden) return;
    state.polling = true;
    try { await loadJobs(); $("connection-label").textContent = "已连接 · 数据保存在本机"; }
    catch (_) { $("connection-label").textContent = "连接暂时中断，正在重试"; }
    finally { state.polling = false; }
  }, 3500);
})();
