/* Keepstack single-page client. Vanilla JS, no build step. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const SORT_VALUES = ["newest", "oldest", "name", "largest"];
const SORT_LABELS = {
  newest: "Newest first",
  oldest: "Oldest first",
  name: "Name A-Z",
  largest: "Largest first",
};
const PAGE_SIZES = [10, 20, 50];
const PAGE_SIZE_KEY = "keepstack_page_size";
const EXPORT_PREFS_KEY = "keepstack_export_optional_columns";

const REQUIRED_EXPORT_COLUMNS = [
  ["uuid", "UUID"],
  ["title", "Title"],
  ["description", "Description"],
  ["filename", "Filename"],
  ["media_type", "Media type"],
  ["tags", "Tags"],
  ["created_at", "Created at"],
];
const OPTIONAL_EXPORT_COLUMNS = [
  ["alt_text", "Alt text"],
  ["mime_type", "MIME type"],
  ["size", "Size"],
  ["width", "Width"],
  ["height", "Height"],
  ["updated_at", "Updated at"],
  ["dc_creator", "Creator"],
  ["dc_subject", "Subject"],
  ["dc_publisher", "Publisher"],
  ["dc_date", "Date"],
  ["license", "License"],
  ["rights_statement", "Rights statement"],
  ["credit", "Credit"],
  ["usage_expiry", "Usage expiry"],
  ["retention_until", "Retention until"],
];
const OPTIONAL_COLUMN_KEYS = new Set(OPTIONAL_EXPORT_COLUMNS.map(([key]) => key));

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"]/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

function normalizeTag(raw) {
  return (raw ?? "").toString().trim().toLowerCase();
}

function uniqueTags(values) {
  const seen = new Set();
  const out = [];
  for (const raw of values || []) {
    const tag = normalizeTag(raw);
    if (tag && !seen.has(tag)) {
      seen.add(tag);
      out.push(tag);
    }
  }
  return out;
}

function parsePageSize(raw) {
  const value = Number(raw);
  return PAGE_SIZES.includes(value) ? value : 10;
}

function loadExportPreferences() {
  try {
    const parsed = JSON.parse(localStorage.getItem(EXPORT_PREFS_KEY) || "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter(key => OPTIONAL_COLUMN_KEYS.has(key)) : []);
  } catch {
    return new Set();
  }
}

const initialParams = new URLSearchParams(window.location.search);
const initialCollectionRaw = initialParams.get("collection_id") || "";
const initialCollection = /^\d+$/.test(initialCollectionRaw) && Number(initialCollectionRaw) > 0
  ? initialCollectionRaw
  : null;
const initialSort = SORT_VALUES.includes(initialParams.get("sort"))
  ? initialParams.get("sort")
  : "newest";
const initialPageSize = parsePageSize(localStorage.getItem(PAGE_SIZE_KEY) || "10");

const state = {
  token: localStorage.getItem("keepstack_token"),
  user: null,
  q: initialParams.get("q") || "",
  media: (initialParams.get("media_type") || "").trim() || null,
  collection: initialCollection,
  includeTags: uniqueTags(initialParams.getAll("tags")).sort(),
  excludeTags: uniqueTags(initialParams.getAll("exclude_tags")).sort(),
  sort: initialSort,
  pageSize: initialPageSize,
  pageIndex: 0,
  cursorStack: [null],
  nextCursor: null,
  prevCursor: null,
  total: 0,
  loading: false,
  libraryOK: false,
  collections: [],
  collectionsLoaded: false,
  tagCandidates: [],
  tagTotal: 0,
  exportOptional: loadExportPreferences(),
  exportLastFocus: null,
};

let listGeneration = 0;
let listAbort = null;
let pendingListTimer = null;
let tagGeneration = 0;
let tagAbort = null;
let tagInputTimer = null;
let toastTimer = null;

const fmtBytes = (b) => {
  if (!b) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(b) / Math.log(1024));
  return (b / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + units[i];
};

function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = detail && typeof detail === "object" ? detail.code : null;
  }
}

async function responseError(res) {
  let detail = null;
  let message = res.statusText || `Request failed (${res.status})`;
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      const payload = await res.json();
      detail = payload && Object.prototype.hasOwnProperty.call(payload, "detail")
        ? payload.detail
        : payload;
      if (typeof detail === "string") {
        message = detail;
      } else if (Array.isArray(detail)) {
        message = "Invalid request parameters";
      } else if (detail && typeof detail === "object" && detail.message) {
        message = detail.message;
      }
    } catch {
      /* keep the HTTP status text */
    }
  } else {
    try {
      const text = await res.text();
      if (text) message = text.slice(0, 500);
    } catch {
      /* ignore unread body */
    }
  }
  return new ApiError(message, { status: res.status, detail });
}

function authHeaders() {
  return state.token ? { Authorization: "Bearer " + state.token } : {};
}

async function api(path, opts = {}) {
  const { json, headers: extraHeaders, ...rest } = opts;
  const headers = new Headers(extraHeaders || {});
  if (state.token) headers.set("Authorization", "Bearer " + state.token);
  let body = rest.body;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const method = rest.method || (json !== undefined ? "POST" : "GET");
  const res = await fetch(path, { ...rest, method, headers, body });
  if (res.status === 401) {
    logout();
    throw new ApiError("Your session expired. Sign in again.", { status: 401 });
  }
  if (!res.ok) throw await responseError(res);
  return (res.headers.get("content-type") || "").includes("json") ? res.json() : res;
}

function syncModalState() {
  const open = !$("#drawer").classList.contains("hidden")
    || !$("#upload-modal").classList.contains("hidden")
    || !$("#export-modal").classList.contains("hidden")
    || !$("#user-modal").classList.contains("hidden")
    || !$("#delete-user-modal").classList.contains("hidden");
  document.body.classList.toggle("modal-open", open);
}

function logout() {
  state.token = null;
  state.user = null;
  localStorage.removeItem("keepstack_token");
  invalidateListRequest();
  if (tagAbort) tagAbort.abort();
  closeExportModal();
  closeTagResults();
  $("#drawer").classList.add("hidden");
  $("#upload-modal").classList.add("hidden");
  $("#user-modal").classList.add("hidden");
  $("#delete-user-modal").classList.add("hidden");
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#user-label").textContent = "";
  syncModalState();
}

function hasActiveFilters() {
  return Boolean(
    state.q.trim()
    || state.media
    || state.collection
    || state.includeTags.length
    || state.excludeTags.length
  );
}

function syncUrl() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.media) params.set("media_type", state.media);
  if (state.collection) params.set("collection_id", state.collection);
  state.includeTags.forEach(tag => params.append("tags", tag));
  state.excludeTags.forEach(tag => params.append("exclude_tags", tag));
  params.set("sort", state.sort);
  const url = new URL(window.location.href);
  url.search = params.toString();
  window.history.replaceState(null, "", url);
}

function collectionName(id) {
  const item = state.collections.find(collection => String(collection.id) === String(id));
  return item ? item.name : `Collection #${id}`;
}

function renderActiveFilters() {
  const chips = [];
  if (state.q.trim()) {
    chips.push(`<span class="filter-chip keyword">
      <span class="filter-label">Keyword</span><span class="filter-value">${esc(state.q.trim())}</span>
      <button type="button" class="chip-btn" data-action="remove-keyword" aria-label="Remove keyword filter">&times;</button>
    </span>`);
  }
  if (state.media) {
    chips.push(`<span class="filter-chip keyword">
      <span class="filter-label">Media</span><span class="filter-value">${esc(state.media)}</span>
      <button type="button" class="chip-btn" data-action="remove-media" aria-label="Remove media type filter">&times;</button>
    </span>`);
  }
  if (state.collection) {
    chips.push(`<span class="filter-chip keyword">
      <span class="filter-label">Collection</span><span class="filter-value">${esc(collectionName(state.collection))}</span>
      <button type="button" class="chip-btn" data-action="remove-collection" aria-label="Remove collection filter">&times;</button>
    </span>`);
  }
  state.includeTags.forEach(tag => {
    chips.push(`<span class="filter-chip tag-chip include" data-tag="${esc(tag)}" data-mode="include">
      <span class="filter-label">Include</span><span class="filter-value">${esc(tag)}</span>
      <button type="button" class="chip-btn" data-action="toggle-tag" data-mode="include" data-tag="${esc(tag)}"
              aria-label="Move tag ${esc(tag)} to excluded">&#8644;</button>
      <button type="button" class="chip-btn" data-action="remove-tag" data-mode="include" data-tag="${esc(tag)}"
              aria-label="Remove included tag ${esc(tag)}">&times;</button>
    </span>`);
  });
  state.excludeTags.forEach(tag => {
    chips.push(`<span class="filter-chip tag-chip exclude" data-tag="${esc(tag)}" data-mode="exclude">
      <span class="filter-label">Exclude</span><span class="filter-value">${esc(tag)}</span>
      <button type="button" class="chip-btn" data-action="toggle-tag" data-mode="exclude" data-tag="${esc(tag)}"
              aria-label="Move tag ${esc(tag)} to included">&#8644;</button>
      <button type="button" class="chip-btn" data-action="remove-tag" data-mode="exclude" data-tag="${esc(tag)}"
              aria-label="Remove excluded tag ${esc(tag)}">&times;</button>
    </span>`);
  });
  $("#active-filters").innerHTML = chips.join("");
  $("#active-filters").classList.toggle("hidden", chips.length === 0);
  $("#btn-clear-filters").disabled = chips.length === 0;
}

function setFilterControls() {
  $("#search").value = state.q;
  $("#sort").value = state.sort;
  $("#page-size").value = String(state.pageSize);
  $("#filter-media").value = state.media || "";
  $("#filter-collection").value = state.collection || "";
}

function renderMediaOptions(mediaTypes) {
  const select = $("#filter-media");
  const values = [];
  const seen = new Set();
  for (const item of mediaTypes || []) {
    const value = item && item.value;
    if (value && !seen.has(value)) {
      seen.add(value);
      values.push(value);
    }
  }
  if (state.media && !seen.has(state.media)) values.push(state.media);
  select.innerHTML = '<option value="">All types</option>' +
    values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
  select.value = state.media || "";
}

function renderCollectionOptions() {
  const select = $("#filter-collection");
  const current = state.collection;
  const items = [...state.collections].sort((a, b) => a.name.localeCompare(b.name));
  select.innerHTML = '<option value="">All collections</option>' +
    items.map(collection => `<option value="${collection.id}">${esc(collection.name)}</option>`).join("");
  if (current && !items.some(collection => String(collection.id) === String(current))) {
    const option = document.createElement("option");
    option.value = current;
    option.textContent = `Collection #${current}`;
    select.appendChild(option);
  }
  select.value = current || "";
}

function invalidateListRequest() {
  listGeneration += 1;
  if (listAbort) {
    listAbort.abort();
    listAbort = null;
  }
  if (pendingListTimer) {
    clearTimeout(pendingListTimer);
    pendingListTimer = null;
  }
}

function resetPaginationState() {
  state.pageIndex = 0;
  state.cursorStack = [null];
  state.nextCursor = null;
  state.prevCursor = null;
}

function setLibraryLoading(loading) {
  state.loading = loading;
  $("#library-loading").classList.toggle("hidden", !loading);
  $("#btn-export").disabled = loading || !state.libraryOK;
  if (loading) {
    $("#library-error").classList.add("hidden");
    $("#empty").classList.add("hidden");
    $("#pagination").classList.add("hidden");
    $("#result-summary").textContent = "Loading...";
    $("#grid").innerHTML = "";
  }
}

function showLibraryError(message) {
  state.libraryOK = false;
  $("#library-loading").classList.add("hidden");
  $("#library-error-text").textContent = message || "Could not load assets.";
  $("#library-error").classList.remove("hidden");
  $("#grid").innerHTML = "";
  $("#empty").classList.add("hidden");
  $("#pagination").classList.add("hidden");
  $("#result-summary").textContent = "Load failed";
  $("#btn-export").disabled = true;
}

function hasMoreFilters() {
  return hasActiveFilters();
}

function buildListParams(cursor) {
  const params = new URLSearchParams();
  params.set("limit", String(state.pageSize));
  params.set("sort", state.sort);
  if (state.q) params.set("q", state.q);
  if (state.media) params.set("media_type", state.media);
  if (state.collection) params.set("collection_id", state.collection);
  state.includeTags.forEach(tag => params.append("tags", tag));
  state.excludeTags.forEach(tag => params.append("exclude_tags", tag));
  if (cursor) params.set("cursor", cursor);
  return params;
}

function updateCursorState(data, navigation, requestedCursor) {
  const pagination = data.pagination || { page: 1 };
  const page = Number(pagination.page) || 1;
  const index = Math.max(0, page - 1);
  state.pageIndex = index;
  state.nextCursor = pagination.next_cursor || null;
  state.prevCursor = pagination.prev_cursor || null;

  if (navigation === "initial") {
    state.cursorStack = [requestedCursor || null];
    state.pageIndex = 0;
  } else if (navigation === "next") {
    state.cursorStack = state.cursorStack.slice(0, index);
    state.cursorStack[index] = requestedCursor;
  } else if (navigation === "prev") {
    state.cursorStack = state.cursorStack.slice(0, index + 1);
  } else {
    state.cursorStack = state.cursorStack.slice(0, index + 1);
    if (state.cursorStack[index] === undefined) {
      state.cursorStack[index] = requestedCursor || null;
    }
  }
}

function renderResults(data) {
  state.total = Number(data.total) || 0;
  state.libraryOK = true;
  $("#btn-export").disabled = false;
  renderMediaOptions(data.facets && data.facets.media_types);
  $("#library-loading").classList.add("hidden");
  $("#library-error").classList.add("hidden");

  const filtered = hasMoreFilters();
  $("#result-title").textContent = filtered ? "Filtered library" : "Library";
  $("#result-summary").textContent = `${state.total} asset${state.total === 1 ? "" : "s"}`;

  const items = data.items || [];
  $("#grid").innerHTML = items.map(asset => `
    <div class="card" data-uuid="${esc(asset.uuid)}">
      <img class="thumb" loading="lazy" src="/api/assets/${esc(asset.uuid)}/thumb" alt="${esc(asset.alt_text || asset.title || asset.filename)}">
      <div class="meta">
        <span class="title" title="${esc(asset.title || asset.filename)}">${esc(asset.title || asset.filename)}</span>
        <span class="sub"><span class="badge">${esc(asset.media_type)}</span><span>${fmtBytes(asset.size)}</span></span>
      </div>
    </div>`).join("");

  const empty = $("#empty");
  if (items.length === 0) {
    empty.textContent = filtered
      ? "No matching assets. Adjust or clear the filters."
      : "No assets yet. Upload some to get started.";
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
  }

  const pagination = data.pagination || { page: 1 };
  const page = Number(pagination.page) || 1;
  const paginationEl = $("#pagination");
  if (state.total > 0) {
    paginationEl.classList.remove("hidden");
    $("#pagination-summary").textContent = `${state.total} matching asset${state.total === 1 ? "" : "s"}`;
    $("#page-label").textContent = `Page ${page}`;
    $("#prev-page").disabled = !state.prevCursor;
    $("#next-page").disabled = !state.nextCursor;
  } else {
    paginationEl.classList.add("hidden");
  }
}

async function startListRequest({ cursor = null, navigation = "initial", allowCursorReset = true } = {}) {
  const generation = ++listGeneration;
  if (pendingListTimer) {
    clearTimeout(pendingListTimer);
    pendingListTimer = null;
  }
  if (listAbort) listAbort.abort();
  const controller = new AbortController();
  listAbort = controller;
  setLibraryLoading(true);

  try {
    const params = buildListParams(cursor);
    const data = await api("/api/assets?" + params.toString(), { signal: controller.signal });
    if (generation !== listGeneration) return;
    updateCursorState(data, navigation, cursor);
    renderResults(data);
  } catch (err) {
    if (err.name === "AbortError" || generation !== listGeneration) return;
    if (err instanceof ApiError && err.code === "invalid_cursor" && allowCursorReset) {
      toast("Pagination state expired. Showing the first page.", "err");
      resetPaginationState();
      startListRequest({ cursor: null, navigation: "initial", allowCursorReset: false });
      return;
    }
    showLibraryError(err.message || "Could not load assets.");
  } finally {
    if (generation === listGeneration) {
      listAbort = null;
      setLibraryLoading(false);
    }
  }
}

function queueFilterReload({ delay = 0 } = {}) {
  invalidateListRequest();
  resetPaginationState();
  syncUrl();
  renderActiveFilters();
  setLibraryLoading(true);
  if (delay > 0) {
    pendingListTimer = setTimeout(() => {
      pendingListTimer = null;
      startListRequest({ cursor: null, navigation: "initial" });
    }, delay);
  } else {
    startListRequest({ cursor: null, navigation: "initial" });
  }
}

function reloadCurrentPage() {
  invalidateListRequest();
  const cursor = state.pageIndex === 0
    ? null
    : (state.cursorStack[state.pageIndex] || state.prevCursor || null);
  startListRequest({ cursor, navigation: "refresh" });
}

function previousPage() {
  if (!state.prevCursor) return;
  invalidateListRequest();
  const cursor = state.prevCursor;
  startListRequest({ cursor, navigation: "prev" });
}

function nextPage() {
  if (!state.nextCursor) return;
  invalidateListRequest();
  const cursor = state.nextCursor;
  startListRequest({ cursor, navigation: "next" });
}

function clearFilters() {
  state.q = "";
  state.media = null;
  state.collection = null;
  state.includeTags = [];
  state.excludeTags = [];
  $("#search").value = "";
  $("#filter-media").value = "";
  $("#filter-collection").value = "";
  closeTagResults();
  queueFilterReload();
}

function addTag(raw, mode = "include") {
  const tag = normalizeTag(raw);
  if (!tag) return;
  if (mode === "include") {
    state.excludeTags = state.excludeTags.filter(value => value !== tag);
    if (!state.includeTags.includes(tag)) state.includeTags.push(tag);
    state.includeTags.sort();
  } else {
    state.includeTags = state.includeTags.filter(value => value !== tag);
    if (!state.excludeTags.includes(tag)) state.excludeTags.push(tag);
    state.excludeTags.sort();
  }
  renderActiveFilters();
  renderTagCandidates();
  queueFilterReload();
}

function removeTag(tag, mode) {
  if (mode === "include") state.includeTags = state.includeTags.filter(value => value !== tag);
  else state.excludeTags = state.excludeTags.filter(value => value !== tag);
  renderActiveFilters();
  renderTagCandidates();
  queueFilterReload();
}

function toggleTagMode(tag, mode) {
  if (mode === "include") addTag(tag, "exclude");
  else addTag(tag, "include");
}

function openTagResults() {
  $("#tag-results").classList.remove("hidden");
  $("#tag-search").setAttribute("aria-expanded", "true");
}

function closeTagResults() {
  $("#tag-results").classList.add("hidden");
  $("#tag-search").setAttribute("aria-expanded", "false");
}

function renderTagCandidates() {
  const el = $("#tag-results");
  if (!state.tagCandidates.length) {
    el.innerHTML = '<p class="tag-empty muted">No matching tags.</p>';
    return;
  }
  const rows = state.tagCandidates.map(item => {
    const tag = item.name;
    const included = state.includeTags.includes(tag);
    const excluded = state.excludeTags.includes(tag);
    const stateLabel = included ? "Included" : (excluded ? "Excluded" : "");
    return `<button type="button" role="option" aria-selected="${included || excluded}"
      class="tag-result ${included ? "included" : ""} ${excluded ? "excluded" : ""}"
      data-tag="${esc(tag)}">
      <span>${esc(tag)}</span><span class="tag-result-state">${stateLabel}</span>
    </button>`;
  });
  if (state.tagCandidates.length < state.tagTotal) {
    rows.push('<button type="button" class="tag-more">Load more</button>');
  }
  el.innerHTML = rows.join("");
}

async function loadTagCandidates(append = false) {
  const generation = ++tagGeneration;
  if (tagAbort) tagAbort.abort();
  const controller = new AbortController();
  tagAbort = controller;
  const q = $("#tag-search").value.trim();
  const offset = append ? state.tagCandidates.length : 0;
  try {
    const data = await api(
      `/api/tags?limit=20&offset=${offset}&q=${encodeURIComponent(q)}`,
      { signal: controller.signal }
    );
    if (generation !== tagGeneration) return;
    const incoming = data.tags || [];
    state.tagCandidates = append ? state.tagCandidates.concat(incoming) : incoming;
    state.tagTotal = Number(data.total) || state.tagCandidates.length;
    renderTagCandidates();
  } catch (err) {
    if (err.name === "AbortError" || generation !== tagGeneration) return;
    $("#tag-results").innerHTML = `<p class="tag-empty muted">${esc(err.message || "Could not load tags.")}</p>`;
  }
}

async function fetchCollections(force = false) {
  if (!force && state.collectionsLoaded) return state.collections;
  const data = await api("/api/collections");
  state.collections = data.collections || [];
  state.collectionsLoaded = true;
  renderCollectionOptions();
  renderActiveFilters();
  return state.collections;
}

function extractFilename(header, fallback) {
  if (!header) return fallback;
  const match = /filename\*=(?:UTF-8'')?([^;]+)|filename="([^"]+)"|filename=([^;]+)/i.exec(header);
  const raw = match && (match[1] || match[2] || match[3]);
  if (!raw) return fallback;
  const trimmed = raw.trim().replace(/^"|"$/g, "");
  try {
    return decodeURIComponent(trimmed);
  } catch {
    return trimmed;
  }
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "download";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function downloadProtected(path, fallbackName) {
  const res = await fetch(path, { headers: authHeaders() });
  if (res.status === 401) {
    logout();
    throw new ApiError("Your session expired. Sign in again.", { status: 401 });
  }
  if (!res.ok) throw await responseError(res);
  const blob = await res.blob();
  const filename = extractFilename(res.headers.get("content-disposition"), fallbackName);
  triggerBlobDownload(blob, filename);
}

// ---------- authentication ----------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").classList.add("hidden");
  try {
    const result = await api("/api/auth/login", {
      json: { username: $("#login-user").value, password: $("#login-pass").value },
    });
    state.token = result.token;
    localStorage.setItem("keepstack_token", result.token);
    state.user = result.user;
    enterApp();
  } catch {
    $("#login-error").textContent = "Invalid credentials";
    $("#login-error").classList.remove("hidden");
  }
});
$("#btn-logout").addEventListener("click", logout);

function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-label").textContent = `${state.user.username} (${state.user.role})`;
  const isAdmin = state.user.role === "admin";
  $$(".admin-only").forEach(el => el.classList.toggle("hidden", !isAdmin));
  setFilterControls();
  renderActiveFilters();
  syncUrl();
  activateView("library");
  fetchCollections(true).catch(err => toast(err.message || "Could not load collections.", "err"));
  queueFilterReload();
}

// ---------- navigation ----------
function activateView(view) {
  $$(".nav-item").forEach(button => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $$(".view").forEach(node => node.classList.add("hidden"));
  const target = $("#view-" + view);
  if (target) target.classList.remove("hidden");
}

$$(".nav-item").forEach(button => button.addEventListener("click", () => {
  const view = button.dataset.view;
  activateView(view);
  if (view === "library") {
    reloadCurrentPage();
  } else if (view === "collections") {
    loadCollections().catch(err => toast(err.message || "Could not load collections.", "err"));
  } else if (view === "dashboard") {
    loadDashboard().catch(err => toast(err.message || "Could not load dashboard.", "err"));
  } else if (view === "users") {
    loadUsers().catch(err => toast(err.message || "Could not load users.", "err"));
  } else if (view === "audit") {
    loadAudit().catch(err => toast(err.message || "Could not load audit log.", "err"));
  }
}));

// ---------- library filters ----------
$("#search").addEventListener("input", (e) => {
  state.q = e.target.value;
  activateView("library");
  syncUrl();
  renderActiveFilters();
  queueFilterReload({ delay: 250 });
});

$("#sort").addEventListener("change", (e) => {
  state.sort = SORT_VALUES.includes(e.target.value) ? e.target.value : "newest";
  queueFilterReload();
});

$("#page-size").addEventListener("change", (e) => {
  state.pageSize = parsePageSize(e.target.value);
  localStorage.setItem(PAGE_SIZE_KEY, String(state.pageSize));
  queueFilterReload();
});

$("#filter-media").addEventListener("change", (e) => {
  state.media = e.target.value || null;
  queueFilterReload();
});

$("#filter-collection").addEventListener("change", (e) => {
  state.collection = e.target.value || null;
  queueFilterReload();
});

$("#btn-clear-filters").addEventListener("click", clearFilters);
$("#btn-retry").addEventListener("click", reloadCurrentPage);
$("#prev-page").addEventListener("click", previousPage);
$("#next-page").addEventListener("click", nextPage);

$("#active-filters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "remove-keyword") {
    state.q = "";
    $("#search").value = "";
    queueFilterReload();
  } else if (action === "remove-media") {
    state.media = null;
    $("#filter-media").value = "";
    queueFilterReload();
  } else if (action === "remove-collection") {
    state.collection = null;
    $("#filter-collection").value = "";
    queueFilterReload();
  } else if (action === "toggle-tag") {
    toggleTagMode(button.dataset.tag, button.dataset.mode);
  } else if (action === "remove-tag") {
    removeTag(button.dataset.tag, button.dataset.mode);
  }
});

$("#grid").addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (card && card.dataset.uuid) openAsset(card.dataset.uuid);
});

$("#tag-search").addEventListener("focus", () => {
  openTagResults();
  loadTagCandidates();
});
$("#tag-search").addEventListener("input", () => {
  openTagResults();
  clearTimeout(tagInputTimer);
  tagInputTimer = setTimeout(loadTagCandidates, 150);
});
$("#tag-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeTagResults();
  }
});
$("#tag-results").addEventListener("click", (event) => {
  if (event.target.closest(".tag-more")) {
    loadTagCandidates(true);
    return;
  }
  const button = event.target.closest(".tag-result");
  if (!button) return;
  const tag = button.dataset.tag;
  if (state.includeTags.includes(tag)) {
    removeTag(tag, "include");
  } else {
    addTag(tag, "include");
  }
  $("#tag-search").focus();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".tag-picker")) closeTagResults();
});

// ---------- asset detail ----------
const drawer = $("#drawer");
$("#drawer-close").addEventListener("click", closeDrawer);
$(".drawer-backdrop").addEventListener("click", closeDrawer);

function closeDrawer() {
  drawer.classList.add("hidden");
  syncModalState();
}

async function openAsset(uuid) {
  drawer.classList.remove("hidden");
  syncModalState();
  $("#drawer-body").innerHTML = '<p class="muted">Loading...</p>';
  try {
    const [detail, meta, similar] = await Promise.all([
      api("/api/assets/" + encodeURIComponent(uuid)),
      api("/api/assets/" + encodeURIComponent(uuid) + "/metadata"),
      api("/api/assets/" + encodeURIComponent(uuid) + "/similar"),
    ]);
    const asset = detail.asset;
    const canEdit = ["admin", "editor", "contributor"].includes(state.user.role);
    const preview = asset.media_type === "image"
      ? `<img class="detail-preview" src="/api/assets/${encodeURIComponent(uuid)}/preview" alt="${esc(asset.alt_text || asset.title || asset.filename)}">`
      : `<img class="detail-preview" src="/api/assets/${encodeURIComponent(uuid)}/thumb" alt="${esc(asset.title || asset.filename)}">`;
    const dc = asset.dublin_core || {};
    $("#drawer-body").innerHTML = `
      ${preview}
      <h2 class="detail-title">${esc(asset.title || asset.filename)}</h2>
      <p class="muted">${esc(asset.media_type)} &middot; ${esc((asset.ext || "").toUpperCase())} &middot; ${fmtBytes(asset.size)} ${asset.width ? `&middot; ${asset.width}&times;${asset.height}` : ""}</p>
      <div class="btn-row">
        <button class="btn" id="d-download" type="button">Download</button>
        ${canEdit ? '<button class="btn" id="d-share" type="button">Create share link</button>' : ""}
        ${["admin", "editor"].includes(state.user.role) ? '<button class="btn btn-danger" id="d-delete" type="button">Delete</button>' : ""}
      </div>

      ${canEdit ? `<div class="detail-section"><h4>Edit metadata</h4>
        <div class="edit-grid">
          <label>Title<input id="e-title" value="${esc(asset.title)}"></label>
          <label>Description<textarea id="e-description" rows="2">${esc(asset.description)}</textarea></label>
          <label>Alt text (accessibility)<input id="e-alt_text" value="${esc(asset.alt_text)}"></label>
          <label>Creator<input id="e-dc_creator" value="${esc(asset.dc_creator)}"></label>
          <label>Rights / license<input id="e-rights_statement" value="${esc(asset.rights_statement)}"></label>
          <label>Retention until<input id="e-retention_until" type="date" value="${esc((asset.retention_until || "").slice(0, 10))}"></label>
        </div>
        <div class="btn-row"><button class="btn btn-primary" id="d-save" type="button">Save changes</button></div></div>` : ""}

      <div class="detail-section"><h4>Tags</h4>
        <div class="tag-row" id="tag-row">
          ${detail.tags.map(tag => `<span class="tag ${tag.source === "ai" ? "ai" : ""}">${esc(tag.name)}${canEdit ? `<span class="rm" data-tag="${esc(tag.name)}">&times;</span>` : ""}</span>`).join("")}
          ${canEdit ? `<form class="tag-add-form" id="tag-add-form">
            <input class="tag-add" id="tag-add" placeholder="+ add tag" aria-label="New tag name">
            <button class="btn tag-add-submit" id="tag-add-submit" type="submit">Add</button>
          </form>` : ""}
        </div></div>

      ${similar.items.length ? `<div class="detail-section"><h4>Similar assets</h4>
        <div class="similar-strip">${similar.items.slice(0, 8).map(item => `<img src="/api/assets/${esc(item.uuid)}/thumb" data-uuid="${esc(item.uuid)}" title="${esc(item.title || item.filename)}" alt="${esc(item.title || item.filename)}">`).join("")}</div></div>` : ""}

      <div class="detail-section"><h4>Dublin Core</h4>
        ${Object.entries(dc).filter(([, value]) => value).map(([key, value]) => `<div class="field-row"><span class="k">${esc(key)}</span><span class="v">${esc(value)}</span></div>`).join("")}
        <div class="btn-row"><a class="btn btn-ghost" href="/oai?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai::${encodeURIComponent(asset.uuid)}" target="_blank" rel="noopener">View OAI-PMH XML</a></div>
      </div>

      ${Object.keys(meta.exif || {}).length || Object.keys(meta.iptc || {}).length ? `<div class="detail-section"><h4>Embedded metadata (EXIF / IPTC)</h4>
        <div class="kv-scroll">${[...Object.entries(meta.iptc || {}), ...Object.entries(meta.exif || {})].slice(0, 40).map(([key, value]) => `<div class="field-row"><span class="k">${esc(key)}</span><span class="v">${esc(typeof value === "object" ? JSON.stringify(value) : value)}</span></div>`).join("")}</div></div>` : ""}

      ${detail.versions.length ? `<div class="detail-section"><h4>Versions</h4>${detail.versions.map(version => `<div class="field-row"><span class="k">v${version.version_no}</span><span class="v">${esc(version.note || "")} &middot; ${fmtBytes(version.size)} &middot; ${esc((version.created_at || "").slice(0, 10))}</span></div>`).join("")}</div>` : ""}
    `;

    $$("#drawer-body .similar-strip img").forEach(img => img.addEventListener("click", () => openAsset(img.dataset.uuid)));
    $("#d-download").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await downloadProtected("/api/assets/" + encodeURIComponent(uuid) + "/file", asset.filename || "asset");
        toast("Download started");
      } catch (err) {
        toast(err.message || "Download failed.", "err");
      } finally {
        button.disabled = false;
      }
    });

    if (canEdit) {
      $("#d-save").addEventListener("click", async () => {
        const body = {};
        ["title", "description", "alt_text", "dc_creator", "rights_statement", "retention_until"].forEach(field => {
          const input = $("#e-" + field);
          if (input) body[field] = input.value;
        });
        try {
          await api("/api/assets/" + encodeURIComponent(uuid), { method: "PATCH", json: body });
          toast("Saved");
          await openAsset(uuid);
          reloadCurrentPage();
        } catch (err) {
          toast(err.message || "Save failed.", "err");
        }
      });
      $("#tag-add-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const input = $("#tag-add");
        const submit = $("#tag-add-submit");
        const name = input.value.trim();
        if (!name) {
          toast("Enter a tag name.", "err");
          input.focus();
          return;
        }
        if (submit.disabled) return;
        submit.disabled = true;
        try {
          await api("/api/assets/" + encodeURIComponent(uuid) + "/tags", { json: { name } });
          await openAsset(uuid);
          reloadCurrentPage();
        } catch (err) {
          toast(err.message || "Could not add tag.", "err");
          submit.disabled = false;
          input.focus();
        }
      });
      $$("#tag-row .rm").forEach(remove => remove.addEventListener("click", async () => {
        try {
          await api("/api/assets/" + encodeURIComponent(uuid) + "/tags/" + encodeURIComponent(remove.dataset.tag), { method: "DELETE" });
          await openAsset(uuid);
          reloadCurrentPage();
        } catch (err) {
          toast(err.message || "Could not remove tag.", "err");
        }
      }));
      $("#d-share").addEventListener("click", async () => {
        try {
          const result = await api("/api/shares", { json: { target_type: "asset", target_id: asset.id, permission: "download" } });
          await navigator.clipboard?.writeText(result.url).catch(() => {});
          toast("Share link copied: " + result.url);
        } catch (err) {
          toast(err.message || "Could not create share link.", "err");
        }
      });
    }

    $("#d-delete")?.addEventListener("click", async () => {
      if (!confirm("Delete this asset?")) return;
      try {
        await api("/api/assets/" + encodeURIComponent(uuid), { method: "DELETE" });
        toast("Deleted");
        closeDrawer();
        reloadCurrentPage();
      } catch (err) {
        toast(err.message || "Delete failed.", "err");
      }
    });
  } catch (err) {
    $("#drawer-body").innerHTML = `<p class="error">${esc(err.message || "Could not load asset.")}</p>`;
  }
}

// ---------- upload ----------
const uploadModal = $("#upload-modal");
const dropzone = $("#dropzone");
const fileInput = $("#file-input");
const UPLOAD_REQUEST_TIMEOUT_MS = 5 * 60 * 1000;
let uploadBusy = false;
let uploadOptionsLoading = false;

function updateUploadInteractionState() {
  const blocked = uploadBusy || uploadOptionsLoading;
  $("#upload-collection").disabled = blocked;
  $("#ai-toggle").disabled = uploadBusy;
  fileInput.disabled = blocked;
  dropzone.classList.toggle("disabled", blocked);
}

function renderUploadCollectionOptions() {
  const select = $("#upload-collection");
  const current = state.collection;
  const collections = [...state.collections].sort((a, b) => a.name.localeCompare(b.name));
  select.innerHTML = '<option value="">No collection</option>' +
    collections.map(collection => `<option value="${collection.id}">${esc(collection.name)}</option>`).join("");
  if (current && collections.some(collection => String(collection.id) === String(current))) {
    select.value = String(current);
  } else {
    select.value = "";
  }
}

async function openUploadModal() {
  uploadModal.classList.remove("hidden");
  syncModalState();
  if (uploadBusy) {
    updateUploadInteractionState();
    return;
  }

  $("#upload-list").innerHTML = "";
  $("#upload-summary").classList.add("hidden");
  $("#upload-summary").textContent = "";
  fileInput.value = "";
  renderUploadCollectionOptions();
  uploadOptionsLoading = true;
  updateUploadInteractionState();
  try {
    await fetchCollections(true);
    renderUploadCollectionOptions();
  } catch (err) {
    toast(err.message || "Could not refresh collections. Using the cached list.", "err");
  } finally {
    uploadOptionsLoading = false;
    updateUploadInteractionState();
  }
}

function closeUploadModal() {
  if (uploadBusy && !window.confirm("Uploads are still running. Close the window and let them finish in the background?")) {
    return;
  }
  uploadModal.classList.add("hidden");
  syncModalState();
  reloadCurrentPage();
}

$("#btn-upload").addEventListener("click", openUploadModal);
$("#upload-cancel").addEventListener("click", closeUploadModal);
$("#upload-modal .modal-backdrop").addEventListener("click", closeUploadModal);

dropzone.addEventListener("click", () => {
  if (uploadBusy || uploadOptionsLoading) return;
  fileInput.click();
});
dropzone.addEventListener("dragover", event => {
  if (uploadBusy || uploadOptionsLoading) return;
  event.preventDefault();
  dropzone.classList.add("drag");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", event => {
  event.preventDefault();
  dropzone.classList.remove("drag");
  if (uploadBusy || uploadOptionsLoading) {
    toast("Wait for the current uploads to finish.", "err");
    return;
  }
  uploadFiles(event.dataTransfer.files);
});
fileInput.addEventListener("change", () => uploadFiles(fileInput.files));

function setUploadStatus(row, text, kind, detail = "") {
  row.className = "upload-item " + kind;
  const status = row.querySelector(".upload-status");
  if (status) status.textContent = text;
  row.title = detail || text;
}

function uploadResultLabel(created, collectionId, collectionLabel) {
  if (!created) throw new Error("Unexpected upload response");
  if (created.collection_error) {
    const base = created.duplicate ? "duplicate" : "uploaded";
    return [`${base}, not added to collection`, "warn", created.collection_error];
  }
  if (collectionId) {
    const base = created.duplicate ? "duplicate" : "uploaded";
    if (created.collection_added) {
      return [`${base}, added to ${collectionLabel}`, created.duplicate ? "dup" : "ok", ""];
    }
    return [`${base}, already in ${collectionLabel}`, "dup", ""];
  }
  return created.duplicate
    ? ["duplicate (deduped)", "dup", ""]
    : ["done", "ok", ""];
}

async function uploadFiles(files) {
  const fileList = [...files];
  if (!fileList.length) return;
  if (uploadBusy) {
    toast("Wait for the current uploads to finish.", "err");
    return;
  }

  const collectionSelect = $("#upload-collection");
  const collectionId = collectionSelect.value || null;
  const collectionLabel = collectionSelect.selectedOptions[0]?.textContent || "collection";
  const runAi = $("#ai-toggle").checked;
  const list = $("#upload-list");
  list.innerHTML = "";
  $("#upload-summary").classList.add("hidden");

  uploadBusy = true;
  updateUploadInteractionState();
  let successCount = 0;
  let failureCount = 0;

  try {
    for (const file of fileList) {
      const row = document.createElement("div");
      row.className = "upload-item";
      row.innerHTML = `<span class="upload-name">${esc(file.name)}</span><span class="upload-status">uploading...</span>`;
      list.appendChild(row);
      setUploadStatus(row, "uploading...", "uploading");

      const controller = new AbortController();
      let timedOut = false;
      const timeout = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, UPLOAD_REQUEST_TIMEOUT_MS);

      try {
        const formData = new FormData();
        formData.append("files", file);
        formData.append("run_ai", runAi ? "true" : "false");
        if (collectionId) formData.append("collection_id", collectionId);
        const result = await api("/api/assets", {
          method: "POST",
          body: formData,
          signal: controller.signal,
        });
        const [label, kind, detail] = uploadResultLabel(
          result.created && result.created[0],
          collectionId,
          collectionLabel
        );
        setUploadStatus(row, label, kind, detail);
        successCount += 1;
      } catch (err) {
        if (timedOut || err.name === "AbortError") {
          setUploadStatus(
            row,
            "timed out, refresh the library to check",
            "warn",
            "The browser stopped waiting for this upload. The server may still finish it."
          );
        } else {
          setUploadStatus(row, "failed", "err", err.message || "Upload failed.");
        }
        failureCount += 1;
      } finally {
        clearTimeout(timeout);
      }
    }
  } finally {
    uploadBusy = false;
    fileInput.value = "";
    updateUploadInteractionState();
    const total = successCount + failureCount;
    const summary = $("#upload-summary");
    summary.textContent = failureCount
      ? `${successCount} succeeded, ${failureCount} failed`
      : `${successCount} uploaded successfully`;
    summary.classList.remove("hidden");
    if (successCount) {
      fetchCollections(true).catch(() => {});
      reloadCurrentPage();
    }
    if (uploadModal.classList.contains("hidden") && total) {
      toast(
        failureCount
          ? `Upload finished: ${successCount} succeeded, ${failureCount} failed`
          : `Upload finished: ${successCount} succeeded`,
        failureCount ? "err" : "ok"
      );
    }
  }
}

// ---------- collections ----------
async function loadCollections() {
  const data = await fetchCollections(true);
  const list = $("#collection-list");
  list.innerHTML = data.map(collection =>
    `<div class="collection-card" data-id="${collection.id}"><span class="n">${esc(collection.name)}</span><span class="muted">${collection.asset_count} assets</span><span class="muted">${esc(collection.description || "")}</span></div>`).join("")
    || '<p class="muted">No collections yet.</p>';
  $$("#collection-list .collection-card").forEach(card => card.addEventListener("click", () => {
    state.collection = String(card.dataset.id);
    state.media = null;
    state.includeTags = [];
    state.excludeTags = [];
    activateView("library");
    setFilterControls();
    renderActiveFilters();
    queueFilterReload();
  }));
}

$("#btn-new-collection").addEventListener("click", async () => {
  const name = prompt("Collection name");
  if (!name) return;
  try {
    await api("/api/collections", { json: { name } });
    await loadCollections();
  } catch (err) {
    toast(err.message || "Could not create collection.", "err");
  }
});

// ---------- dashboard ----------
async function loadDashboard() {
  const stats = await api("/api/stats");
  const cards = [
    ["Total assets", stats.total_assets],
    ["Storage used", fmtBytes(stats.total_bytes)],
    ["Saved by dedup", fmtBytes(stats.dedup_saved_bytes)],
    ["Tags", stats.tags],
    ["Collections", stats.collections],
    ...Object.entries(stats.by_type).map(([key, value]) => [key.charAt(0).toUpperCase() + key.slice(1), value]),
  ];
  $("#stat-cards").innerHTML = cards.map(([label, value]) => `<div class="stat-card"><div class="n">${value}</div><div class="l">${label}</div></div>`).join("");
}
$("#btn-fixity").addEventListener("click", async () => {
  $("#fixity-result").textContent = "Checking...";
  try {
    const result = await api("/api/admin/fixity", { method: "POST" });
    $("#fixity-result").textContent = result.ok
      ? `All ${result.checked} blobs verified. Integrity intact.`
      : `${result.failures.length} of ${result.checked} failed fixity.`;
  } catch (err) {
    $("#fixity-result").textContent = err.message || "Fixity check failed.";
  }
});

// ---------- users ----------
let userCache = [];
let editingUserId = null;
let pendingDeleteUser = null;

async function loadUsers() {
  const data = await api("/api/users");
  userCache = data.users || [];
  const selfId = state.user && state.user.id;
  const activeAdmins = userCache.filter(user => user.role === "admin" && user.is_active);
  $("#user-rows").innerHTML = userCache.map(user => {
    const isSelf = user.id === selfId;
    const lastActiveAdmin = user.role === "admin" && user.is_active && activeAdmins.length <= 1;
    const toggleLabel = user.is_active ? "Disable" : "Enable";
    const toggleDisabled = isSelf || (user.is_active && lastActiveAdmin);
    const toggleTitle = isSelf
      ? "You cannot change your own active state"
      : (lastActiveAdmin ? "At least one active administrator must remain" : "");
    const deleteDisabled = isSelf || user.is_active;
    const deleteTitle = isSelf
      ? "You cannot delete yourself"
      : (user.is_active ? "Deactivate the user before permanent deletion" : "");
    return `<tr data-uid="${user.id}">
      <td>${esc(user.username)}${isSelf ? '<span class="self-badge">You</span>' : ""}</td>
      <td>${esc(user.full_name || "")}</td>
      <td>${esc(user.email || "")}</td>
      <td>${esc(user.role)}</td>
      <td><span class="status-pill ${user.is_active ? "active" : "inactive"}">${user.is_active ? "Active" : "Inactive"}</span></td>
      <td>${esc((user.last_login || "").slice(0, 16).replace("T", " "))}</td>
      <td><div class="table-actions">
        <button class="btn btn-sm" type="button" data-user-action="edit" data-uid="${user.id}">Edit</button>
        <button class="btn btn-sm" type="button" data-user-action="toggle" data-uid="${user.id}"
                ${toggleDisabled ? "disabled" : ""} title="${esc(toggleTitle)}">${toggleLabel}</button>
        <button class="btn btn-sm btn-danger" type="button" data-user-action="delete" data-uid="${user.id}"
                ${deleteDisabled ? "disabled" : ""} title="${esc(deleteTitle)}">Delete</button>
      </div></td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="muted">No users found.</td></tr>';
}

function closeUserModal() {
  $("#user-modal").classList.add("hidden");
  editingUserId = null;
  syncModalState();
}

function closeDeleteUserModal() {
  $("#delete-user-modal").classList.add("hidden");
  pendingDeleteUser = null;
  syncModalState();
}

function openUserModal(user = null) {
  const isEdit = Boolean(user);
  const isSelf = isEdit && user.id === state.user.id;
  editingUserId = isEdit ? user.id : null;
  $("#user-modal-title").textContent = isEdit ? "Edit user" : "Add user";
  $("#user-username").value = isEdit ? user.username : "";
  $("#user-username").readOnly = isEdit;
  $("#user-email").value = isEdit ? (user.email || "") : "";
  $("#user-full-name").value = isEdit ? (user.full_name || "") : "";
  $("#user-role").value = isEdit ? user.role : "viewer";
  $("#user-role").disabled = isSelf;
  $("#user-active").checked = isEdit ? Boolean(user.is_active) : true;
  $("#user-active").disabled = isSelf;
  $("#user-password").value = "";
  $("#user-password").required = !isEdit;
  $("#user-password-hint").textContent = isEdit
    ? "Leave blank to keep the current password. Changing it invalidates existing sessions."
    : "Required for new users. Changing it invalidates existing sessions.";
  if (isSelf) {
    $("#user-password-hint").textContent += " You cannot change your own role or active state.";
  }
  $("#user-modal-error").classList.add("hidden");
  $("#user-modal-error").textContent = "";
  $("#user-save").disabled = false;
  $("#user-modal").classList.remove("hidden");
  syncModalState();
  $(isEdit ? "#user-email" : "#user-username").focus();
}

function openDeleteUserModal(user) {
  if (user.is_active || user.id === state.user.id) return;
  pendingDeleteUser = user;
  $("#delete-user-name").textContent = user.username;
  $("#delete-user-input").value = "";
  $("#delete-user-confirm").disabled = true;
  $("#delete-user-error").classList.add("hidden");
  $("#delete-user-error").textContent = "";
  $("#delete-user-modal").classList.remove("hidden");
  syncModalState();
  $("#delete-user-input").focus();
}

$("#btn-new-user").addEventListener("click", () => openUserModal());
$("#user-close").addEventListener("click", closeUserModal);
$("#user-cancel").addEventListener("click", closeUserModal);
$("#user-modal .modal-backdrop").addEventListener("click", closeUserModal);

$("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const isEdit = editingUserId !== null;
  const username = $("#user-username").value.trim();
  const email = $("#user-email").value.trim();
  const fullName = $("#user-full-name").value.trim();
  const role = $("#user-role").value;
  const active = $("#user-active").checked;
  const password = $("#user-password").value;
  const error = $("#user-modal-error");
  const save = $("#user-save");
  const isSelf = isEdit && editingUserId === state.user.id;

  error.classList.add("hidden");
  error.textContent = "";
  if (!isEdit && !username) {
    error.textContent = "Username is required.";
    error.classList.remove("hidden");
    return;
  }
  if (!isEdit && !password) {
    error.textContent = "Initial password is required.";
    error.classList.remove("hidden");
    return;
  }

  if (isEdit) {
    const body = { email: email || null, full_name: fullName || null };
    if (password) body.password = password;
    if (!isSelf) {
      body.role = role;
      body.is_active = active;
    }
    save.disabled = true;
    try {
      await api("/api/users/" + editingUserId, { method: "PATCH", json: body });
      const selfPasswordChanged = isSelf && Boolean(password);
      closeUserModal();
      toast("User updated");
      if (selfPasswordChanged) {
        toast("Password changed. Please sign in again.");
        logout();
      } else {
        await loadUsers();
      }
    } catch (err) {
      error.textContent = err.message || "Could not update the user.";
      error.classList.remove("hidden");
      save.disabled = false;
    }
  } else {
    save.disabled = true;
    try {
      await api("/api/users", {
        json: {
          username,
          email: email || null,
          full_name: fullName || null,
          role,
          is_active: active,
          password,
        },
      });
      closeUserModal();
      toast("User created");
      await loadUsers();
    } catch (err) {
      error.textContent = err.message || "Could not create the user.";
      error.classList.remove("hidden");
      save.disabled = false;
    }
  }
});

$("#user-rows").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-user-action]");
  if (!button) return;
  const uid = Number(button.dataset.uid);
  const target = userCache.find(user => user.id === uid);
  if (!target) return;
  const action = button.dataset.userAction;
  if (action === "edit") {
    openUserModal(target);
    return;
  }
  if (action === "delete") {
    openDeleteUserModal(target);
    return;
  }
  if (action === "toggle") {
    const nextActive = !target.is_active;
    if (!nextActive && !window.confirm(`Deactivate ${target.username}?`)) return;
    button.disabled = true;
    try {
      await api("/api/users/" + uid, { method: "PATCH", json: { is_active: nextActive } });
      toast(nextActive ? "User enabled" : "User disabled");
      await loadUsers();
    } catch (err) {
      toast(err.message || "Could not update the user.", "err");
      button.disabled = false;
    }
  }
});

$("#delete-user-input").addEventListener("input", (event) => {
  const matches = pendingDeleteUser && event.target.value.trim() === pendingDeleteUser.username;
  $("#delete-user-confirm").disabled = !matches;
});
$("#delete-user-close").addEventListener("click", closeDeleteUserModal);
$("#delete-user-cancel").addEventListener("click", closeDeleteUserModal);
$("#delete-user-modal .modal-backdrop").addEventListener("click", closeDeleteUserModal);
$("#delete-user-confirm").addEventListener("click", async () => {
  if (!pendingDeleteUser) return;
  const user = pendingDeleteUser;
  const confirm = $("#delete-user-confirm");
  const error = $("#delete-user-error");
  confirm.disabled = true;
  error.classList.add("hidden");
  try {
    await api("/api/users/" + user.id, { method: "DELETE" });
    closeDeleteUserModal();
    toast("User permanently deleted");
    await loadUsers();
  } catch (err) {
    error.textContent = err.message || "Could not delete the user.";
    error.classList.remove("hidden");
    confirm.disabled = false;
  }
});

// ---------- audit ----------
async function loadAudit() {
  const data = await api("/api/audit");
  $("#audit-rows").innerHTML = data.entries.map(entry =>
    `<tr><td>${esc((entry.ts || "").slice(0, 19).replace("T", " "))}</td><td>${esc(entry.username || "-")}</td><td>${esc(entry.action)}</td>
     <td>${esc(entry.target_type || "")} ${esc((entry.target_id || "").slice(0, 8))}</td><td>${esc(entry.detail || "")}</td><td>${esc(entry.ip || "")}</td></tr>`).join("");
}

// ---------- CSV export ----------
function exportFilterSummary() {
  const parts = [];
  if (state.q.trim()) parts.push(["Keyword", state.q.trim()]);
  if (state.media) parts.push(["Media", state.media]);
  if (state.collection) parts.push(["Collection", collectionName(state.collection)]);
  if (state.includeTags.length) parts.push(["Include", state.includeTags.join(", ")]);
  if (state.excludeTags.length) parts.push(["Exclude", state.excludeTags.join(", ")]);
  parts.push(["Sort", SORT_LABELS[state.sort] || state.sort]);
  return parts;
}

function renderExportColumns() {
  $("#export-required").innerHTML = REQUIRED_EXPORT_COLUMNS.map(([key, label]) =>
    `<label class="check column-option locked"><input type="checkbox" checked disabled> ${esc(label)} <span class="muted">(${esc(key)})</span></label>`).join("");
  $("#export-optional").innerHTML = OPTIONAL_EXPORT_COLUMNS.map(([key, label]) =>
    `<label class="check column-option"><input type="checkbox" data-export-column="${key}" ${state.exportOptional.has(key) ? "checked" : ""}> ${esc(label)}</label>`).join("");
}

function saveExportPreferences() {
  localStorage.setItem(EXPORT_PREFS_KEY, JSON.stringify([...state.exportOptional].sort()));
}

function openExportModal() {
  if (state.loading) return;
  if (!state.libraryOK) {
    toast("Load the current asset list before exporting.", "err");
    return;
  }
  state.exportLastFocus = document.activeElement;
  $("#export-count").textContent = state.total === 0
    ? "No matching assets. A header-only CSV will be downloaded."
    : `${state.total} matching asset${state.total === 1 ? "" : "s"} will be exported.`;
  $("#export-filters").innerHTML = exportFilterSummary().map(([label, value]) =>
    `<span class="export-filter"><span class="filter-label">${esc(label)}</span> ${esc(value)}</span>`).join("");
  renderExportColumns();
  $("#export-sanitize").checked = true;
  $("#export-error").classList.add("hidden");
  $("#export-error").textContent = "";
  $("#export-modal").classList.remove("hidden");
  syncModalState();
  $("#export-close").focus();
}

function closeExportModal() {
  const modal = $("#export-modal");
  if (modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  syncModalState();
  if (state.exportLastFocus && typeof state.exportLastFocus.focus === "function") {
    state.exportLastFocus.focus();
  }
}

function buildExportParams() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.media) params.set("media_type", state.media);
  if (state.collection) params.set("collection_id", state.collection);
  state.includeTags.forEach(tag => params.append("tags", tag));
  state.excludeTags.forEach(tag => params.append("exclude_tags", tag));
  params.set("sort", state.sort);
  [...state.exportOptional].sort().forEach(column => params.append("columns", column));
  params.set("sanitize_formulas", $("#export-sanitize").checked ? "true" : "false");
  return params;
}

$("#btn-export").addEventListener("click", openExportModal);
$("#export-close").addEventListener("click", closeExportModal);
$("#export-cancel").addEventListener("click", closeExportModal);
$("#export-modal .modal-backdrop").addEventListener("click", closeExportModal);
$("#export-optional").addEventListener("change", (event) => {
  const input = event.target.closest("input[data-export-column]");
  if (!input) return;
  if (input.checked) state.exportOptional.add(input.dataset.exportColumn);
  else state.exportOptional.delete(input.dataset.exportColumn);
  saveExportPreferences();
});
$("#export-reset-columns").addEventListener("click", () => {
  state.exportOptional = new Set();
  saveExportPreferences();
  renderExportColumns();
});
$("#export-download").addEventListener("click", async () => {
  const button = $("#export-download");
  const error = $("#export-error");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Exporting...";
  error.classList.add("hidden");
  try {
    const params = buildExportParams();
    await downloadProtected("/api/exports/assets.csv?" + params.toString(), "assets.csv");
    toast("CSV download started");
  } catch (err) {
    let message = err.message || "Export failed.";
    if (err instanceof ApiError && err.code === "export_too_large" && err.detail) {
      message = `Matched ${err.detail.matched} assets, above the ${err.detail.limit}-row limit. Narrow the filters and try again.`;
    }
    if (err instanceof ApiError && err.status === 401) {
      message = "Your session expired. Sign in again.";
    }
    error.textContent = message;
    error.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});

// ---------- global keyboard and boot ----------
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("#export-modal").classList.contains("hidden")) {
    closeExportModal();
  } else if (!uploadModal.classList.contains("hidden")) {
    closeUploadModal();
  } else if (!$("#delete-user-modal").classList.contains("hidden")) {
    closeDeleteUserModal();
  } else if (!$("#user-modal").classList.contains("hidden")) {
    closeUserModal();
  } else if (!drawer.classList.contains("hidden")) {
    closeDrawer();
  } else {
    closeTagResults();
  }
});

(async function boot() {
  setFilterControls();
  renderActiveFilters();
  if (state.token) {
    try {
      state.user = await api("/api/auth/me");
      enterApp();
      return;
    } catch {
      /* fall through to the login form */
    }
  }
  $("#login").classList.remove("hidden");
})();
