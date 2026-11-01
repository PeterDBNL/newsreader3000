// Newsreader3000 single-page UI (vanilla JS, no build step)
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = () => $("#main");

function fmtSize(b) {
  b = Number(b) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(b < 10 ? 2 : 1) : b) + " " + u[i];
}
function fmtAge(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body && !(o.body instanceof FormData) && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}
const post = (p, body) => api(p, { method: "POST", body: body || {} });

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), err ? 7000 : 3500);
}
async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}
function openModal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-body").innerHTML = ""; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

const state = {
  settings: null,
  group: localStorage.getItem("nr.group") || "",
  browse: { mode: localStorage.getItem("nr.mode") || "releases", q: "", offset: 0, hide_read: 0, min_size: "", sort: "date", max_age: "" },
  search: { q: "", groups: "", mode: "releases", offset: 0, min_size: "", max_size: "", max_age: "", poster: "", sort: "date" },
  groups: { q: "", offset: 0, subscribed: -1 },
  selected: new Map(),
  lastResults: [],
  view: "browse",
};

// ------------------------------------------------------------------ routing
function go(view) { location.hash = view; }
window.addEventListener("hashchange", route);
function route() {
  const v = (location.hash || "#browse").slice(1).split("/")[0];
  state.view = v;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
  state.selected.clear();
  ({ browse: viewBrowse, search: viewSearch, groups: viewGroups, downloads: viewDownloads, history: viewHistory, settings: viewSettings }[v] || viewBrowse)();
}
$$("#nav a").forEach((a) => (a.onclick = () => go(a.dataset.view)));

// ------------------------------------------------------------------ status polling
let lastTasks = [];
async function pollStatus() {
  try {
    const s = await api("/api/status");
    const d = s.download;
    $("#speed").textContent = (d.paused ? "Paused · " : "") + fmtSize(d.speed) + "/s" + (d.limit_kbps ? ` (limit ${d.limit_kbps} KB/s)` : "");
    $("#pauseall").textContent = d.paused ? "Resume" : "Pause";
    $("#pauseall").dataset.paused = d.paused ? "1" : "";
    const qc = $("#qcount");
    qc.textContent = d.queue;
    qc.classList.toggle("hidden", !d.queue);
    const ti = $("#tasksind");
    const finished = lastTasks.filter((t) => !s.tasks.find((x) => x.id === t.id));
    lastTasks = s.tasks;
    if (s.tasks.length) {
      const t = s.tasks[0];
      const pct = t.total ? Math.round((100 * t.progress) / t.total) + "%" : "";
      ti.textContent = `⟳ ${t.label} ${pct}` + (s.tasks.length > 1 ? ` (+${s.tasks.length - 1})` : "");
      ti.classList.remove("hidden");
    } else ti.classList.add("hidden");
    if (finished.length) onTasksFinished(finished);
    if (state.view === "downloads") refreshQueue(false);
  } catch (e) {}
}
async function onTasksFinished(list) {
  const all = await api("/api/tasks").catch(() => []);
  for (const t of list) {
    const full = all.find((x) => x.id === t.id);
    if (full) toast(`${full.label}: ${full.status === "error" ? "failed – " : ""}${full.message || full.status}`, full.status === "error");
  }
  if (state.view === "browse") { loadGroupSidebar(); loadBrowse(); loadGroupInfo(); }
  if (state.view === "groups") loadGroups();
}
$("#tasksind").onclick = showTasks;
async function showTasks() {
  const t = await api("/api/tasks");
  openModal(`<h3>Background tasks</h3><table><tr><th>Task</th><th>Status</th><th>Progress</th><th>Message</th><th></th></tr>
  ${t.map((x) => `<tr><td>${esc(x.label)}</td><td>${esc(x.status)}</td><td class="num">${x.total ? Math.round((100 * x.progress) / x.total) + "%" : ""}</td>
  <td>${esc(x.message)}</td><td>${x.status === "running" ? `<button class="small sec" onclick="post('/api/tasks/${x.id}/cancel').then(showTasks)">Cancel</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">None</td></tr>'}</table>`);
}
$("#pauseall").onclick = () => guard(async () => { await post($("#pauseall").dataset.paused ? "/api/queue/resume" : "/api/queue/pause"); pollStatus(); });

// ------------------------------------------------------------------ result table (shared by browse & search)
function resultTable(res, mode, opts = {}) {
  state.lastResults = res.items;
  if (!res.items.length) return `<div class="empty">${opts.empty || "Nothing found."}</div>`;
  const showGroup = opts.showGroup;
  let rows;
  if (mode === "articles") {
    rows = res.items.map((a, i) => `<tr class="${a.is_read ? "read" : ""}" data-i="${i}">
      <td><input type="checkbox" class="selbox" data-i="${i}"></td>
      <td class="subj"><a onclick="openArticle('${esc(a.msgid)}')">${esc(a.subject)}</a>${showGroup ? `<div class="small muted">${esc(a.grp)}</div>` : ""}</td>
      <td class="small hm">${esc(shortPoster(a.poster))}</td><td class="num">${fmtSize(a.size)}</td>
      <td class="num" title="${esc(fmtDate(a.date))}">${fmtAge(a.date)}</td></tr>`);
  } else {
    rows = res.items.map((r, i) => {
      const comp = r.complete == null ? "" : `<span class="pill ${r.complete >= 100 ? "ok" : r.complete > 90 ? "warn" : "bad"}">${r.complete}%</span>`;
      const extra = mode === "releases" ? `<span class="pill">${r.files} file${r.files === 1 ? "" : "s"}</span>${r.par2 ? '<span class="pill">par2</span>' : ""}${r.nfo ? `<span class="pill"><a onclick="event.stopPropagation();previewFile('${esc(r.grp)}', ${jsStr(r.nfo)}, true)">nfo</a></span>` : ""}` : `<span class="pill">${r.parts}${r.expected ? "/" + r.expected : ""} parts</span>`;
      return `<tr class="${r.is_read ? "read" : ""}" data-i="${i}">
      <td><input type="checkbox" class="selbox" data-i="${i}"></td>
      <td class="subj"><a onclick="openItem(${i})">${esc(displayName(r.name))}</a> ${extra}${comp}${showGroup ? `<div class="small muted">${esc(r.grp)}</div>` : ""}</td>
      <td class="small hm">${esc(shortPoster(r.poster))}</td><td class="num">${fmtSize(r.size)}</td>
      <td class="num" title="${esc(fmtDate(r.date))}">${fmtAge(r.date)}</td></tr>`;
    });
  }
  return `<table class="results"><thead><tr><th style="width:28px"><input type="checkbox" id="selall"></th><th>${mode === "articles" ? "Subject" : mode === "files" ? "File" : "Release"}</th><th class="hm">Poster</th><th class="num">Size</th><th class="num">Age</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}
// Bundled obfuscated posts carry an internal "· #<time>" suffix to keep them apart; don't show it.
const displayName = (n) => String(n ?? "").replace(/ · #\d+$/, "");
const jsStr = (s) => esc(JSON.stringify(s));
const shortPoster = (p) => (p || "").replace(/<[^>]*>/, "").trim() || p;

function selectionBar(mode) {
  return `<div class="toolbar selbar hidden" id="selbar">
    <b id="selcount"></b>
    <select id="dlcat"><option value="">Default category</option>${(state.settings?.downloads.categories || []).map((c) => `<option>${esc(c)}</option>`).join("")}</select>
    <label class="c"><input type="checkbox" id="dlextract" ${state.settings?.downloads.auto_extract ? "checked" : ""}> Auto-extract</label>
    <label class="c" title="Combine all selected items into one download job"><input type="checkbox" id="dlcombine"> Combine</label>
    <button onclick="downloadSelected()">⬇ Download</button>
    <button class="sec" onclick="saveNzb()">Save NZB</button>
    ${mode !== "search" ? `<button class="sec" onclick="markSelected(1)">Mark read</button>` : ""}
    <button class="sec" onclick="clearSel()">Clear</button><span class="muted small hm">Tip: Shift-click selects a range</span></div>`;
}
function bindResults(container, mode) {
  const kind = mode === "articles" ? "article" : mode === "files" ? "file" : "release";
  const update = () => {
    const bar = $("#selbar");
    if (!bar) return;
    bar.classList.toggle("hidden", !state.selected.size);
    $("#selcount").textContent = `${state.selected.size} selected`;
  };
  const boxes = $$(".selbox", container);
  const setSel = (cb, on) => {
    const it = state.lastResults[cb.dataset.i];
    const id = kind === "article" ? "a" + it.id : it.grp + "\u0000" + it.k;
    cb.checked = on;
    if (on) state.selected.set(id, kind === "article" ? { kind, id: it.id, grp: it.grp, name: it.subject } : { kind, grp: it.grp, key: it.k, name: it.name });
    else state.selected.delete(id);
    cb.closest("tr").classList.toggle("sel", on);
  };
  // Click a checkbox or an empty part of a row to select it; hold Shift to select everything in between.
  let last = null;
  const toggle = (i, on, shift) => {
    if (shift && last !== null) {
      const [a, b] = [Math.min(last, i), Math.max(last, i)];
      for (let j = a; j <= b; j++) setSel(boxes[j], on);
    } else setSel(boxes[i], on);
    last = i;
    update();
  };
  boxes.forEach((cb, i) => {
    const it = state.lastResults[cb.dataset.i];
    const id = kind === "article" ? "a" + it.id : it.grp + "\u0000" + it.k;
    cb.checked = state.selected.has(id);
    cb.closest("tr").classList.toggle("sel", cb.checked);
    cb.onclick = (e) => { e.stopPropagation(); toggle(i, cb.checked, e.shiftKey); };
    const tr = cb.closest("tr");
    tr.onmousedown = (e) => { if (e.shiftKey) e.preventDefault(); };  // no text selection on shift-click
    tr.onclick = (e) => { if (e.target.closest("a,button,input")) return; toggle(i, !cb.checked, e.shiftKey); };
  });
  const all = $("#selall", container);
  if (all) all.onchange = () => { boxes.forEach((cb) => setSel(cb, all.checked)); update(); };
  update();
}
function clearSel() { state.selected.clear(); $$(".selbox").forEach((c) => { c.checked = false; c.closest("tr").classList.remove("sel"); }); $("#selbar")?.classList.add("hidden"); }
async function downloadSelected() {
  const items = [...state.selected.values()];
  await guard(async () => {
    const r = await post("/api/download", { items, category: $("#dlcat").value, extract: $("#dlextract").checked, separate: !$("#dlcombine").checked });
    toast(`Added ${r.jobs.length} download${r.jobs.length === 1 ? "" : "s"} to the queue`);
    clearSel();
    pollStatus();
  });
}
async function saveNzb() {
  const items = [...state.selected.values()];
  await guard(async () => {
    const name = items.length === 1 ? items[0].name : "selection";
    const r = await api("/api/nzb", { method: "POST", body: { items, name } });
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (name || "selection").replace(/[\\/:*?"<>|]/g, "_") + ".nzb";
    a.click();
  });
}
async function markSelected(read) {
  const sel = [...state.selected.values()];
  const ids = sel.filter((s) => s.kind === "article").map((s) => s.id);
  const releases = sel.filter((s) => s.kind === "release").map((s) => ({ grp: s.grp, key: s.key }));
  if (!ids.length && !releases.length) return toast("Mark read works on releases and articles", true);
  await guard(() => post("/api/groups/mark-read", { ids, releases, read: !!read }));
  loadGroupSidebar();
  clearSel();
  loadBrowse();
}
let searchSeq = 0;
function setBusy(resultsEl, statusEl, on, text) {
  resultsEl?.classList.toggle("busy", on);
  if (statusEl) statusEl.innerHTML = on ? `<span class="spinner"></span> ${esc(text || "Searching…")}` : esc(text || "");
}
function summary(res, mode, q, ms) {
  const noun = { releases: "release", files: "file", articles: "article" }[mode] || "result";
  const n = res.total.toLocaleString();
  return `${n} ${noun}${res.total === 1 ? "" : "s"}${q ? ` matching “${q}”` : ""} · ${(ms / 1000).toFixed(2)} s`;
}
function pager(res, st, reload) {
  const limit = 100;
  if (res.total <= limit) return `<div class="pager muted small">${res.total.toLocaleString()} result${res.total === 1 ? "" : "s"}</div>`;
  const from = st.offset + 1, to = Math.min(st.offset + limit, res.total);
  setTimeout(() => {
    $("#prev") && ($("#prev").onclick = () => { st.offset = Math.max(0, st.offset - limit); reload(); });
    $("#next") && ($("#next").onclick = () => { st.offset += limit; reload(); });
  });
  return `<div class="pager"><span class="muted small">${from.toLocaleString()}–${to.toLocaleString()} of ${res.total.toLocaleString()}</span>
    <span><button class="sec small" id="prev" ${st.offset ? "" : "disabled"}>‹ Prev</button> <button class="sec small" id="next" ${to >= res.total ? "disabled" : ""}>Next ›</button></span></div>`;
}

// open a release/file row: list the files with preview + download buttons
async function openItem(i) {
  const it = state.lastResults[i];
  const kind = (state.view === "search" ? state.search.mode : state.browse.mode) === "files" ? "file" : "release";
  await guard(async () => {
    const files = await api(`/api/release?group=${encodeURIComponent(it.grp)}&key=${encodeURIComponent(it.k)}&kind=${kind}`);
    openModal(`<h3 style="padding-right:30px">${esc(displayName(it.name))}</h3>
      <div class="muted small">${esc(it.grp)} · ${esc(it.poster)} · ${fmtDate(it.date)} · ${fmtSize(it.size)}</div>
      <div class="toolbar" style="margin-top:10px"><button onclick='quickDownload(${jsStr(JSON.stringify({ kind, grp: it.grp, key: it.k, name: it.name }))})'>⬇ Download all</button></div>
      <table class="files"><tr><th>File</th><th class="num">Parts</th><th class="num">Size</th><th></th></tr>
      ${files.map((f) => `<tr><td class="subj">${esc(f.filename || f.subject)}</td><td class="num">${f.parts}${f.expected ? "/" + f.expected : ""}</td><td class="num">${fmtSize(f.size)}</td>
      <td style="white-space:nowrap">${previewable(f.filename || f.subject) ? `<button class="small sec" onclick='previewFile(${jsStr(it.grp)}, ${jsStr(f.k)})'>View</button>` : ""}
      <button class="small sec" onclick='quickDownload(${jsStr(JSON.stringify({ kind: "file", grp: it.grp, key: f.k, name: f.filename || f.subject }))})'>⬇</button></td></tr>`).join("")}
      </table><div id="pv" class="preview" style="margin-top:12px"></div>`);
  });
}
function previewable(name) { return /\.(jpe?g|png|gif|webp|bmp|nfo|txt|diz|sfv|srt|nzb|mp3|m4a|flac|ogg|mp4|webm|pdf)("|\s|$)/i.test(name || ""); }
async function quickDownload(json) {
  const item = JSON.parse(json);
  await guard(async () => { await post("/api/download", { items: [item] }); toast("Added to queue"); pollStatus(); });
}
async function previewFile(grp, key, standalone) {
  const url = `/api/preview?group=${encodeURIComponent(grp)}&key=${encodeURIComponent(key)}`;
  if (standalone) openModal(`<h3>Preview</h3><div id="pv" class="preview"></div>`);
  const pv = $("#pv");
  pv.innerHTML = '<div class="muted">Loading from server…</div>';
  try {
    const r = await fetch(url);
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail; } catch (e) {} throw new Error(m); }
    const ct = r.headers.get("content-type") || "";
    const blob = await r.blob();
    const obj = URL.createObjectURL(blob);
    const dl = `<div style="margin-top:8px"><a href="${obj}" download="${esc(fileFromCD(r) || "file")}">Save file</a></div>`;
    if (ct.startsWith("image/")) pv.innerHTML = `<img src="${obj}">` + dl;
    else if (ct.startsWith("video/")) pv.innerHTML = `<video src="${obj}" controls></video>` + dl;
    else if (ct.startsWith("audio/")) pv.innerHTML = `<audio src="${obj}" controls></audio>` + dl;
    else if (ct.includes("pdf")) pv.innerHTML = `<iframe src="${obj}" style="width:100%;height:70vh;border:0"></iframe>` + dl;
    else if (ct.startsWith("text/") || /x-nzb/.test(ct)) {
      const text = await blob.text();
      const nfo = /\.(nfo|diz)$/i.test(fileFromCD(r) || "");
      pv.innerHTML = `<pre class="body ${nfo ? "nfo" : ""}">${esc(text)}</pre>` + dl;
    } else pv.innerHTML = dl;
  } catch (e) { pv.innerHTML = `<div class="status-failed">${esc(e.message)}</div>`; }
}
function fileFromCD(r) { const m = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") || ""); return m && m[1]; }

// ------------------------------------------------------------------ article reader
async function openArticle(msgid) {
  openModal('<div class="muted">Loading article…</div>');
  await guard(async () => {
    const a = await api("/api/article?msgid=" + encodeURIComponent(msgid));
    const h = a.headers;
    let html = `<h3 style="padding-right:30px">${esc(h.Subject || "(no subject)")}</h3><div class="hdrs">`;
    for (const k of ["From", "Date", "Newsgroups", "Organization", "User-Agent", "X-Newsreader", "Message-ID"]) if (h[k]) html += `<b>${k}</b><span>${esc(h[k])}</span>`;
    html += `</div><div class="toolbar"><button class="sec small" onclick='composeReply(${jsStr(JSON.stringify(h))})'>↩ Reply</button>
      <button class="sec small" onclick='showThread(${jsStr(msgid)})'>Thread</button>
      <button class="sec small" onclick='toggleRaw()'>Headers</button></div>
      <div id="rawhdrs" class="hidden"><pre class="body small">${esc(Object.entries(h).map(([k, v]) => k + ": " + v).join("\n"))}</pre></div>`;
    const att = `/api/article/attachment?msgid=${encodeURIComponent(msgid)}`;
    if (a.yenc) {
      const y = a.yenc;
      html += `<div class="panel"><b>yEnc binary:</b> ${esc(y.name)} — part ${y.part}${y.total > 1 ? " of " + y.total : ""} · ${fmtSize(y.bytes)}${y.multipart ? " (file " + fmtSize(y.size) + ")" : ""}
        ${y.crc_ok === false ? '<span class="pill bad">CRC error</span>' : y.crc_ok ? '<span class="pill ok">CRC ok</span>' : ""}
        <div style="margin-top:8px"><a class="small" href="${att}&download=1">Download this part</a></div>
        ${!y.multipart && y.mime.startsWith("image/") ? `<div class="preview" style="margin-top:8px"><img src="${att}"></div>` : ""}</div>`;
    }
    for (const at of a.attachments) {
      const u = `${att}&index=${at.index}`;
      html += `<div class="panel"><b>Attachment:</b> ${esc(at.name)} (${fmtSize(at.size)}) · <a href="${u}&download=1">Download</a>
        ${at.mime.startsWith("image/") ? `<div class="preview" style="margin-top:8px"><img src="${u}"></div>` : ""}</div>`;
    }
    if (a.text && a.text.trim()) html += `<pre class="body">${linkify(esc(a.text))}</pre>`;
    html += `<div id="threadbox"></div>`;
    openModal(html);
  });
}
function toggleRaw() { $("#rawhdrs").classList.toggle("hidden"); }
function linkify(s) { return s.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>').replace(/^(&gt;.*)$/gm, '<span class="muted">$1</span>'); }
async function showThread(msgid) {
  const rows = await api("/api/thread?msgid=" + encodeURIComponent(msgid));
  $("#threadbox").innerHTML = `<h3>Thread</h3><table>${rows.map((r) => `<tr class="${r.is_read ? "read" : ""}"><td class="subj"><a onclick='openArticle(${jsStr(r.msgid)})'>${r.msgid === msgid ? "▶ " : ""}${esc(r.subject)}</a></td><td class="small hm">${esc(shortPoster(r.poster))}</td><td class="num">${fmtAge(r.date)}</td></tr>`).join("") || '<tr><td class="muted">Only this article is in the local index.</td></tr>'}</table>`;
}
function composeReply(json) {
  const h = JSON.parse(json);
  const subj = /^re:/i.test(h.Subject || "") ? h.Subject : "Re: " + (h.Subject || "");
  compose({ groups: h["Followup-To"] || h.Newsgroups || state.group, subject: subj, references: ((h.References || "") + " " + (h["Message-ID"] || "")).trim() });
}
function compose(p = {}) {
  openModal(`<h3>${p.references ? "Reply" : "New post"}</h3>
    <div class="grid2"><label class="f">Newsgroup(s)<input id="cg" value="${esc(p.groups || state.group)}"></label>
    <label class="f">Subject<input id="cs" value="${esc(p.subject || "")}"></label></div>
    <label class="f" style="margin-top:10px">Message<textarea id="cb"></textarea></label>
    <div class="toolbar" style="margin-top:10px"><button id="csend">Post</button><span class="muted small">Posting uses your primary server and the name/e-mail from Settings → Posting.</span></div>`);
  $("#csend").onclick = () => guard(async () => {
    await post("/api/post", { groups: $("#cg").value, subject: $("#cs").value, body: $("#cb").value, references: p.references || "" });
    toast("Posted");
    closeModal();
  });
}

// ------------------------------------------------------------------ Browse view
function viewBrowse() {
  const st = state.browse;
  main().innerHTML = `<div class="layout">
    <div class="panel grouplist"><div class="toolbar"><b class="grow">Subscribed</b>
      <button class="small sec" title="Get new headers for all subscribed groups" onclick="updateAll()">⟳ All</button></div>
      <div id="glist" class="muted small">Loading…</div></div>
    <div><div class="panel"><div class="toolbar">
      <b id="gtitle" class="grow" style="word-break:break-all">${esc(state.group || "Pick a group")}</b>
      <button class="small" onclick="fetchHeaders('new')" title="Download new headers">⟳ New headers</button>
      <button class="small sec" onclick="fetchOlder()" title="Download older headers">Older…</button>
      <button class="small sec" onclick="markAllRead()">Mark all read</button>
      <button class="small sec" onclick="showHeaderLog(state.group)" title="Header download history">Header log</button>
      <button class="small sec" onclick="compose()">✎ Post</button>
    </div>
    <div id="ginfo" class="small muted" style="margin:-4px 0 10px"></div>
    <div class="toolbar">
      <select id="bmode"><option value="releases">Releases</option><option value="files">Files</option><option value="articles">Articles</option></select>
      <input id="bq" class="grow" type="search" placeholder="Filter releases… press Enter  (use -word to exclude)" value="${esc(st.q)}">
      <input id="bmin" class="num" placeholder="Min MB" value="${esc(st.min_size)}">
      <input id="bage" class="num" placeholder="Max days" value="${esc(st.max_age)}">
      <select id="bsort"><option value="date">Newest</option><option value="oldest">Oldest</option><option value="size">Largest</option><option value="name">Name</option></select>
      <label class="c"><input type="checkbox" id="bhide" ${st.hide_read ? "checked" : ""}> Hide read</label>
    </div><div id="bstatus" class="statusline"></div>${selectionBar("browse")}<div id="bres"></div></div></div></div>`;
  $("#bmode").value = st.mode;
  $("#bsort").value = st.sort;
  const reload = () => { st.offset = 0; loadBrowse(); };
  $("#bmode").onchange = () => { st.mode = $("#bmode").value; localStorage.setItem("nr.mode", st.mode); state.selected.clear(); reload(); };
  $("#bsort").onchange = () => { st.sort = $("#bsort").value; reload(); };
  $("#bhide").onchange = () => { st.hide_read = $("#bhide").checked ? 1 : 0; reload(); };
  let tmr;
  const apply = () => { clearTimeout(tmr); st.q = $("#bq").value; st.min_size = $("#bmin").value; st.max_age = $("#bage").value; reload(); };
  for (const id of ["bq", "bmin", "bage"]) {
    $("#" + id).oninput = () => { clearTimeout(tmr); tmr = setTimeout(apply, 500); };
    $("#" + id).onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } };
  }
  loadGroupSidebar();
  loadBrowse();
  loadGroupInfo();
}
async function loadGroupInfo() {
  const el = $("#ginfo");
  if (!el || !state.group) return;
  const g = await api("/api/groups/info?name=" + encodeURIComponent(state.group)).catch(() => null);
  if (!g) return;
  const last = g.log && g.log[0];
  el.innerHTML = g.last_fetched
    ? `Stored: articles ${g.first_fetched.toLocaleString()}–${g.last_fetched.toLocaleString()} (${g.stored_articles.toLocaleString()} of ${g.available.toLocaleString()} on server) · ${(g.releases || 0).toLocaleString()} releases, ${(g.unread || 0).toLocaleString()} unread` +
      (g.oldest ? ` · posts from ${new Date(g.oldest * 1000).toLocaleDateString()} to ${new Date(g.newest * 1000).toLocaleDateString()}` : "") +
      (last ? ` · last fetch ${fmtAge(last.started)} ago: ${esc(last.status === "done" ? last.message : last.status + " " + (last.message || ""))}` : "")
    : "No headers downloaded yet for this group.";
}
async function showHeaderLog(group) {
  const rows = await api("/api/headers/log?limit=200" + (group ? "&group=" + encodeURIComponent(group) : "")).catch(() => []);
  openModal(`<div class="toolbar" style="padding-right:30px"><h3 class="grow" style="margin:0">Header downloads${group ? " · " + esc(group) : ""}</h3>
    ${group ? `<button class="small sec" onclick="showHeaderLog('')">All groups</button>` : ""}
    <button class="small sec" onclick="post('/api/headers/log/clear').then(()=>showHeaderLog(${jsStr(group)}))">Clear</button></div>
    ${headerLogTable(rows, !group)}`);
}
function headerLogTable(rows, showGroup) {
  if (!rows.length) return '<div class="empty">No header downloads yet.</div>';
  return `<table><tr><th>When</th>${showGroup ? "<th>Group</th>" : ""}<th>Type</th><th class="hm">Articles</th><th class="num">Received</th><th class="num">New</th><th class="num">Time</th><th class="num hm">Speed</th><th>Result</th></tr>
    ${rows.map((r) => {
      const secs = Math.max(1, (r.finished || Date.now() / 1000) - r.started);
      return `<tr><td class="small" title="${esc(fmtDate(r.started))}">${fmtAge(r.started)} ago</td>${showGroup ? `<td class="small subj">${esc(r.grp)}</td>` : ""}
      <td class="small">${esc({ new: "new", older: "older", reset: "reset" }[r.mode] || r.mode)}${r.compressed ? ' <span class="pill" title="Compressed transfer">gz</span>' : ""}</td>
      <td class="small hm">${r.first ? r.first.toLocaleString() + "–" + r.last.toLocaleString() : ""}</td>
      <td class="num">${(r.received || 0).toLocaleString()}</td><td class="num">${(r.inserted || 0).toLocaleString()}</td>
      <td class="num">${Math.round(secs)}s</td><td class="num hm">${Math.round((r.received || 0) / secs).toLocaleString()}/s</td>
      <td class="small status-${r.status === "done" ? "completed" : r.status === "error" ? "failed" : "paused"}">${esc(r.status === "done" ? "OK" : r.status)}${r.status === "error" ? ": " + esc(r.message) : ""}</td></tr>`;
    }).join("")}</table>`;
}
async function loadGroupSidebar() {
  const el = $("#glist");
  if (!el) return;
  const r = await api("/api/groups?subscribed=1&limit=1000").catch((e) => ({ items: [], err: e.message }));
  if (!r.items.length) { el.innerHTML = `<div class="empty">No groups yet.<br><a onclick="go('groups')">Subscribe to groups</a></div>`; return; }
  el.innerHTML = r.items.map((g) => `<div class="g ${g.name === state.group ? "active" : ""}" data-g="${esc(g.name)}"><span>${esc(g.name)}</span><small title="unread releases">${g.stored ? (g.unread || 0).toLocaleString() : "–"}</small></div>`).join("");
  $$(".g", el).forEach((d) => (d.onclick = () => { state.group = d.dataset.g; localStorage.setItem("nr.group", state.group); state.browse.offset = 0; state.selected.clear(); $$(".g", el).forEach((x) => x.classList.toggle("active", x === d)); $("#gtitle").textContent = state.group; loadBrowse(); loadGroupInfo(); }));
}
async function loadBrowse() {
  const el = $("#bres");
  if (!el) return;
  const st = state.browse;
  if (!state.group) { el.innerHTML = `<div class="empty">Choose a subscribed group on the left.</div>`; return; }
  const p = new URLSearchParams({ group: state.group, mode: st.mode, q: st.q, offset: st.offset, hide_read: st.hide_read, sort: st.sort, min_size: st.min_size || 0, max_age: st.max_age || 0 });
  const seq = ++searchSeq, t0 = performance.now();
  setBusy(el, $("#bstatus"), true, st.q ? `Searching for “${st.q}”…` : "Loading…");
  try {
    const res = await api("/api/browse?" + p);
    if (seq !== searchSeq) return;  // a newer search was started
    setBusy(el, $("#bstatus"), false, summary(res, st.mode, st.q, performance.now() - t0));
    el.innerHTML = resultTable(res, st.mode, { empty: st.q ? "No matches." : `No headers stored for this group yet. <a onclick="fetchHeaders('new')">Download headers now</a>.` }) + pager(res, st, loadBrowse);
    bindResults(el, st.mode);
  } catch (e) {
    if (seq !== searchSeq) return;
    setBusy(el, $("#bstatus"), false, "");
    el.innerHTML = `<div class="empty status-failed">${esc(e.message)}</div>`;
  }
}
async function fetchHeaders(mode, count) {
  if (!state.group) return toast("Pick a group first", true);
  await guard(async () => { await post("/api/groups/fetch", { name: state.group, mode, count }); toast(`Fetching headers for ${state.group}…`); pollStatus(); });
}
function fetchOlder() {
  const n = prompt("How many older headers to download?", "50000");
  if (n) fetchHeaders("older", parseInt(n, 10));
}
async function updateAll() { await guard(async () => { const r = await post("/api/groups/update-all"); toast(`Updating ${r.groups} groups`); pollStatus(); }); }
async function markAllRead() {
  if (!state.group) return;
  await guard(() => post("/api/groups/mark-read", { name: state.group, read: true }));
  loadBrowse(); loadGroupSidebar();
}

// ------------------------------------------------------------------ Search view
async function viewSearch() {
  const st = state.search;
  const subs = await api("/api/groups?subscribed=1&limit=1000").catch(() => ({ items: [] }));
  const chosen = new Set(st.groups ? st.groups.split(",") : []);
  main().innerHTML = `<div class="panel"><form id="sform" class="toolbar">
      <input id="sq" class="grow" placeholder='Search subjects in your groups… e.g. ubuntu 24.04 iso  -beta  "exact words"' value="${esc(st.q)}" autofocus>
      <select id="smode"><option value="releases">Releases</option><option value="files">Files</option><option value="articles">Articles</option></select>
      <button type="submit" id="sbtn">Search</button></form><div id="sstatus" class="statusline"></div>
    <div class="toolbar">
      <input id="smin" class="num" placeholder="Min MB" value="${esc(st.min_size)}"><input id="smax" class="num" placeholder="Max MB" value="${esc(st.max_size)}">
      <input id="sage" class="num" placeholder="Max days" value="${esc(st.max_age)}"><input id="spos" placeholder="Poster contains" value="${esc(st.poster)}">
      <select id="ssort"><option value="date">Newest</option><option value="oldest">Oldest</option><option value="size">Largest</option><option value="name">Name</option></select>
    </div>
    <details><summary class="small muted">Groups to search: <span id="gsum"></span></summary>
      <div class="chips" style="margin-top:8px">${subs.items.map((g) => `<span class="chip ${chosen.has(g.name) ? "on" : ""}" data-g="${esc(g.name)}">${esc(g.name)} <span class="muted">${(g.stored || 0).toLocaleString()}</span></span>`).join("") || '<span class="muted">No subscribed groups.</span>'}</div>
      <div class="small muted" style="margin-top:6px">Search runs over the headers downloaded for these groups. Add groups under <a onclick="go('groups')">Groups</a> and download their headers to search more.</div>
    </details></div>
    <div class="panel">${selectionBar("search")}<div id="sres"><div class="empty">Search the headers of your subscribed groups.</div></div></div>`;
  $("#smode").value = st.mode;
  $("#ssort").value = st.sort;
  const sum = () => ($("#gsum").textContent = chosen.size ? [...chosen].join(", ") : `all ${subs.items.length} subscribed groups`);
  sum();
  $$(".chip").forEach((c) => (c.onclick = () => { const g = c.dataset.g; chosen.has(g) ? chosen.delete(g) : chosen.add(g); c.classList.toggle("on"); st.groups = [...chosen].join(","); sum(); }));
  $("#sform").onsubmit = (e) => { e.preventDefault(); st.offset = 0; runSearch(); };
  $("#smode").onchange = () => { state.selected.clear(); st.offset = 0; if (st.q) runSearch(); };
  $("#ssort").onchange = () => { st.offset = 0; if (st.q) runSearch(); };
  if (st.q) runSearch();
}
async function runSearch() {
  const st = state.search;
  st.q = $("#sq").value; st.mode = $("#smode").value; st.min_size = $("#smin").value; st.max_size = $("#smax").value;
  st.max_age = $("#sage").value; st.poster = $("#spos").value; st.sort = $("#ssort").value;
  const el = $("#sres");
  const p = new URLSearchParams({ q: st.q, groups: st.groups, mode: st.mode, offset: st.offset, min_size: st.min_size || 0, max_size: st.max_size || 0, max_age: st.max_age || 0, poster: st.poster, sort: st.sort });
  const seq = ++searchSeq, t0 = performance.now();
  setBusy(el, $("#sstatus"), true, `Searching for “${st.q}”…`);
  $("#sbtn").disabled = true; $("#sbtn").textContent = "Searching…";
  try {
    const res = await api("/api/search?" + p);
    if (seq !== searchSeq) return;
    setBusy(el, $("#sstatus"), false, summary(res, st.mode, st.q, performance.now() - t0));
    el.innerHTML = resultTable(res, st.mode, { showGroup: true, empty: "No matches in the downloaded headers. Try more groups or fetch older headers." }) + pager(res, st, runSearch);
    bindResults(el, st.mode);
  } catch (e) {
    setBusy(el, $("#sstatus"), false, "");
    el.innerHTML = `<div class="empty status-failed">${esc(e.message)}</div>`;
  } finally {
    if ($("#sbtn")) { $("#sbtn").disabled = false; $("#sbtn").textContent = "Search"; }
  }
}

// ------------------------------------------------------------------ Groups view
async function viewGroups() {
  const st = state.groups;
  const popular = state.settings?.default_groups || [];
  main().innerHTML = `<div class="panel"><h3>Popular groups</h3><div class="chips" id="popular"></div>
    <div class="toolbar" style="margin-top:10px"><input id="addg" class="grow" placeholder="Add a group by name, e.g. alt.binaries.linux (comma separated for several)">
    <label class="c"><input type="checkbox" id="addfetch" checked> Download headers</label><button id="addbtn">Subscribe</button></div></div>
    <div class="panel"><div class="toolbar"><h3 class="grow" style="margin:0">All groups on your server</h3>
      <button id="refreshlist" class="sec">⟳ Download full group list</button></div>
    <div class="toolbar"><input id="gq" class="grow" placeholder="Filter, e.g. linux   or wildcards: alt.binaries.*.hd*" value="${esc(st.q)}">
      <select id="gsub"><option value="-1">All</option><option value="1">Subscribed</option><option value="0">Not subscribed</option></select>
      <select id="gsort"><option value="name">Name</option><option value="size">Most articles</option></select></div>
    <div id="gres"></div></div>
    <div class="panel"><div class="toolbar"><h3 class="grow" style="margin:0">Header download history</h3>
      <button class="small sec" onclick="showHeaderLog('')">Show all</button></div><div id="hlog"></div></div>`;
  const subs = new Set((await api("/api/groups?subscribed=1&limit=1000").catch(() => ({ items: [] }))).items.map((g) => g.name));
  $("#popular").innerHTML = popular.map((g) => `<span class="chip ${subs.has(g) ? "on" : ""}" data-g="${esc(g)}">${esc(g)}</span>`).join("");
  $$("#popular .chip").forEach((c) => (c.onclick = () => guard(async () => {
    const on = !c.classList.contains("on");
    await post("/api/groups/subscribe", { names: [c.dataset.g], subscribe: on, fetch: on });
    c.classList.toggle("on", on);
    toast(on ? `Subscribed to ${c.dataset.g}` : `Unsubscribed from ${c.dataset.g}`);
    loadGroups();
  })));
  $("#addbtn").onclick = () => guard(async () => {
    const names = $("#addg").value.split(/[,\s]+/).filter(Boolean);
    if (!names.length) return;
    await post("/api/groups/subscribe", { names, subscribe: true, fetch: $("#addfetch").checked });
    toast(`Subscribed to ${names.join(", ")}`);
    $("#addg").value = "";
    loadGroups(); pollStatus();
  });
  $("#refreshlist").onclick = () => guard(async () => { await post("/api/groups/refresh"); toast("Downloading the group list…"); pollStatus(); });
  $("#gsub").value = String(st.subscribed);
  $("#gsub").onchange = () => { st.subscribed = +$("#gsub").value; st.offset = 0; loadGroups(); };
  $("#gsort").onchange = () => { st.offset = 0; loadGroups(); };
  let tmr;
  $("#gq").oninput = () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = $("#gq").value; st.offset = 0; loadGroups(); }, 300); };
  loadGroups();
}
async function loadGroups() {
  api("/api/headers/log?limit=15").then((rows) => { if ($("#hlog")) $("#hlog").innerHTML = headerLogTable(rows, true); }).catch(() => {});
  const st = state.groups;
  const el = $("#gres");
  if (!el) return;
  const p = new URLSearchParams({ q: st.q, offset: st.offset, limit: 100, subscribed: st.subscribed, sort: $("#gsort")?.value || "name" });
  const res = await api("/api/groups?" + p).catch((e) => ({ items: [], total: 0, err: e.message }));
  if (!res.items.length) {
    el.innerHTML = `<div class="empty">${res.err ? esc(res.err) : st.q ? "No groups match." : "No groups yet. Click <b>Download full group list</b> to load every group your provider carries."}</div>`;
    return;
  }
  el.innerHTML = `<table><tr><th></th><th>Group</th><th class="num">Articles</th><th class="hm">Description</th><th></th></tr>
    ${res.items.map((g) => `<tr><td><input type="checkbox" class="gsub" data-g="${esc(g.name)}" ${g.subscribed ? "checked" : ""} title="Subscribe"></td>
      <td class="subj"><a onclick='browseGroup(${jsStr(g.name)})'>${esc(g.name)}</a>${g.status === "m" ? '<span class="pill">moderated</span>' : ""}${g.status === "n" ? '<span class="pill">no posting</span>' : ""}</td>
      <td class="num">${g.count ? g.count.toLocaleString() : ""}</td><td class="small muted hm">${esc(g.description)}</td>
      <td style="white-space:nowrap">${g.subscribed ? `<button class="small sec" onclick='fetchGroup(${jsStr(g.name)})'>⟳ Headers</button> <button class="small sec" onclick='clearGroup(${jsStr(g.name)})' title="Delete stored headers">🗑</button>` : ""}</td></tr>`).join("")}</table>` + pager(res, st, loadGroups);
  $$(".gsub", el).forEach((cb) => (cb.onchange = () => guard(async () => {
    await post("/api/groups/subscribe", { names: [cb.dataset.g], subscribe: cb.checked });
    toast(cb.checked ? `Subscribed to ${cb.dataset.g}` : `Unsubscribed`);
    loadGroups();
  })));
}
function browseGroup(name) { state.group = name; localStorage.setItem("nr.group", name); state.browse.offset = 0; go("browse"); }
async function fetchGroup(name) { await guard(async () => { await post("/api/groups/fetch", { name, mode: "new" }); toast(`Fetching headers for ${name}…`); pollStatus(); }); }
async function clearGroup(name) { if (confirm(`Delete all stored headers for ${name}?`)) await guard(async () => { await post("/api/groups/clear", { name }); toast("Headers deleted"); loadGroups(); }); }

// ------------------------------------------------------------------ Downloads view
function viewDownloads() {
  const cats = state.settings?.downloads.categories || [];
  main().innerHTML = `<div class="panel"><h3>Add NZB</h3>
    <div class="drop" id="drop">Drop .nzb files here or <a id="pick">choose files</a><input type="file" id="nzbfile" accept=".nzb" multiple class="hidden"></div>
    <div class="toolbar" style="margin-top:10px"><input id="nzburl" class="grow" placeholder="…or paste an NZB link (e.g. from NZBgeek)">
      <select id="nzbcat"><option value="">Default category</option>${cats.map((c) => `<option>${esc(c)}</option>`).join("")}</select>
      <input id="nzbpw" placeholder="Archive password (optional)" style="width:200px">
      <label class="c"><input type="checkbox" id="nzbx" ${state.settings?.downloads.auto_extract ? "checked" : ""}> Auto-extract</label>
      <button id="addurl">Add</button></div></div>
    <div class="panel"><div class="toolbar"><h3 class="grow" style="margin:0">Queue</h3>
      <span class="muted small" id="qinfo"></span>
      <label class="c small">Speed limit <input id="slimit" class="num" placeholder="KB/s" value="${state.settings?.downloads.speed_limit_kbps || ""}"></label>
      <button class="sec small" id="qpause">Pause all</button></div><div id="qlist"></div></div>`;
  const drop = $("#drop");
  $("#pick").onclick = () => $("#nzbfile").click();
  $("#nzbfile").onchange = () => uploadNzbs($("#nzbfile").files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); uploadNzbs(e.dataTransfer.files); };
  $("#addurl").onclick = () => guard(async () => {
    const url = $("#nzburl").value.trim();
    if (!url) return toast("Paste a link or drop a file", true);
    await post("/api/queue/url", { url, category: $("#nzbcat").value, password: $("#nzbpw").value });
    $("#nzburl").value = ""; toast("Added to queue"); refreshQueue();
  });
  $("#slimit").onchange = () => guard(async () => { await post("/api/queue/speed", { kbps: parseInt($("#slimit").value || "0", 10) }); state.settings.downloads.speed_limit_kbps = parseInt($("#slimit").value || "0", 10); pollStatus(); });
  $("#qpause").onclick = () => $("#pauseall").click();
  refreshQueue();
}
async function uploadNzbs(files) {
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  fd.append("category", $("#nzbcat").value);
  fd.append("password", $("#nzbpw").value);
  fd.append("extract", $("#nzbx").checked ? "1" : "0");
  await guard(async () => {
    const r = await api("/api/queue/upload", { method: "POST", body: fd });
    toast(`Added ${r.jobs.length} NZB${r.jobs.length === 1 ? "" : "s"}`);
    r.errors.forEach((e) => toast(e, true));
    refreshQueue();
  });
}
let queueBusy = false;
async function refreshQueue() {
  const el = $("#qlist");
  if (!el || queueBusy) return;
  queueBusy = true;
  try {
    const q = await api("/api/queue");
    const s = q.status;
    $("#qinfo").textContent = `${q.items.length} item(s) · ${fmtSize(s.remaining)} left · ${fmtSize(s.speed)}/s` + (s.speed && s.remaining ? ` · ETA ${eta(s.remaining / s.speed)}` : "");
    $("#qpause").textContent = s.paused ? "Resume all" : "Pause all";
    if (!q.items.length) { el.innerHTML = '<div class="empty">The queue is empty.</div>'; return; }
    el.innerHTML = `<table><tr><th>Name</th><th>Status</th><th class="num hm">Size</th><th>Progress</th><th></th></tr>${q.items.map((j) => {
      const pct = j.total_bytes ? Math.min(100, (100 * j.done_bytes) / j.total_bytes) : 0;
      return `<tr><td class="subj">${esc(j.name)}<div class="small muted">${esc(j.category || "")}${j.password ? " · 🔑" : ""}${j.extract ? "" : " · no extract"}${j.priority ? " · priority " + j.priority : ""}${j.message ? " · " + esc(j.message) : ""}</div></td>
        <td class="status-${j.status}">${esc(j.status.replace("queued_post", "waiting"))}${j.missing_segments ? `<div class="small status-failed">${j.missing_segments} missing</div>` : ""}</td>
        <td class="num hm">${fmtSize(j.total_bytes)}</td>
        <td style="min-width:90px"><span class="small">${pct.toFixed(1)}%</span><div class="bar"><div style="width:${pct}%"></div></div></td>
        <td style="white-space:nowrap">${j.status === "paused" ? `<button class="small sec" onclick="jobAct(${j.id},'resume')">▶</button>` : ["queued", "downloading"].includes(j.status) ? `<button class="small sec" onclick="jobAct(${j.id},'pause')">⏸</button>` : ""}
          <button class="small sec" title="Move to top" onclick="jobAct(${j.id},'priority',{priority:${(j.priority || 0) + 1}})">▲</button>
          <button class="small sec" title="Edit" onclick='editJob(${jsStr(JSON.stringify(j))})'>✎</button>
          <button class="small danger" title="Remove" onclick="jobDelete(${j.id})">✕</button></td></tr>`;
    }).join("")}</table>`;
  } finally { queueBusy = false; }
}
function eta(sec) { sec = Math.round(sec); const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60); return h ? `${h}h ${m}m` : m ? `${m}m ${sec % 60}s` : `${sec}s`; }
async function jobAct(id, action, body) { await guard(async () => { await post(`/api/queue/${id}/${action}`, body || {}); refreshQueue(); if (state.view === "history") viewHistory(); }); }
async function jobDelete(id) { if (confirm("Remove this download and its partial files?")) jobAct(id, "delete", { delete_files: true }); }
function editJob(json) {
  const j = JSON.parse(json);
  const cats = state.settings?.downloads.categories || [];
  openModal(`<h3>Edit download</h3><div class="grid2">
    <label class="f">Name<input id="ejn" value="${esc(j.name)}"></label>
    <label class="f">Category<select id="ejc"><option value="">(none)</option>${cats.map((c) => `<option ${c === j.category ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
    <label class="f">Archive password<input id="ejp" placeholder="${j.password ? "(set – type to change)" : ""}"></label>
    <label class="f">Priority<input id="ejpr" type="number" value="${j.priority || 0}"></label></div>
    <div class="toolbar" style="margin-top:10px"><label class="c"><input type="checkbox" id="ejx" ${j.extract ? "checked" : ""}> Auto-extract</label>
    <label class="c"><input type="checkbox" id="ejr" ${j.repair ? "checked" : ""}> par2 repair</label>
    <label class="c"><input type="checkbox" id="ejd" ${j.delete_archives ? "checked" : ""}> Delete archives after extract</label></div>
    <div class="toolbar"><button id="ejsave">Save</button><a href="/api/queue/${j.id}/nzb">Download NZB</a></div>`);
  $("#ejsave").onclick = () => guard(async () => {
    const body = { name: $("#ejn").value, category: $("#ejc").value, priority: parseInt($("#ejpr").value || "0", 10), extract: $("#ejx").checked ? 1 : 0, repair: $("#ejr").checked ? 1 : 0, delete_archives: $("#ejd").checked ? 1 : 0 };
    if ($("#ejp").value) body.password = $("#ejp").value;
    await post(`/api/queue/${j.id}/update`, body);
    closeModal(); refreshQueue();
  });
}

// ------------------------------------------------------------------ History view
async function viewHistory() {
  main().innerHTML = `<div class="panel"><div class="toolbar"><h3 class="grow" style="margin:0">History</h3>
    <button class="sec small" onclick="clearHistory('completed')">Clear completed</button><button class="sec small" onclick="clearHistory('failed')">Clear failed</button></div><div id="hlist" class="muted">Loading…</div></div>`;
  const rows = await api("/api/history").catch(() => []);
  $("#hlist").innerHTML = rows.length ? `<table><tr><th>Name</th><th>Status</th><th class="num hm">Size</th><th class="hm">Finished</th><th></th></tr>${rows.map((j) => `<tr>
    <td class="subj">${esc(j.name)}<div class="small muted">${esc(j.out_dir || "")}${j.message ? " · " + esc(j.message) : ""}</div></td>
    <td class="status-${j.status}">${esc(j.status)}</td><td class="num hm">${fmtSize(j.total_bytes)}</td><td class="small hm">${fmtDate(j.finished)}</td>
    <td style="white-space:nowrap">${j.status === "failed" ? `<button class="small sec" onclick="retryJob(${j.id}, ${/password/i.test(j.message) ? 1 : 0})">Retry</button>` : ""}
    <a class="small" href="/api/queue/${j.id}/nzb">NZB</a>
    <button class="small sec" onclick="jobAct(${j.id},'delete',{delete_files:${j.status === "failed"}})">✕</button></td></tr>`).join("")}</table>` : '<div class="empty">No finished downloads yet.</div>';
}
async function retryJob(id, askPw) {
  const pw = askPw ? prompt("Archive password:") : null;
  await jobAct(id, "retry", pw ? { password: pw } : {});
  toast("Re-queued"); viewHistory();
}
async function clearHistory(status) { await guard(async () => { await post("/api/history/clear", { status }); viewHistory(); }); }

// ------------------------------------------------------------------ Settings view
async function viewSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  const st = await api("/api/status");
  const d = s.downloads, h = s.headers, p = s.posting;
  const tools = Object.entries(st.tools).map(([k, v]) => `<span class="pill ${v ? "ok" : "bad"}">${k} ${v ? "✓" : "✗"}</span>`).join("");
  main().innerHTML = `
  <div class="panel"><div class="toolbar"><h3 class="grow" style="margin:0">News servers</h3><button class="sec small" id="addsrv">+ Add server</button></div>
    <div class="small muted" style="margin-bottom:8px">The first enabled server (lowest priority number) is used for browsing; downloads use all servers in priority order, so a backup/block account fills in missing articles. Eweka: news.eweka.nl, port 563, SSL.</div>
    <div id="srvs"></div></div>
  <div class="panel"><h3>Downloads</h3><div class="grid2">
    <label class="f">Completed folder<input data-k="downloads.complete_dir" value="${esc(d.complete_dir)}"></label>
    <label class="f">Incomplete (temp) folder<input data-k="downloads.incomplete_dir" value="${esc(d.incomplete_dir)}"></label>
    <label class="f">Watch folder (NZBs dropped here are queued)<input data-k="downloads.watch_dir" value="${esc(d.watch_dir)}"></label>
    <label class="f">Categories (comma separated; each becomes a subfolder)<input data-k="downloads.categories" data-type="list" value="${esc(d.categories.join(", "))}"></label>
    <label class="f">Default category<input data-k="downloads.default_category" value="${esc(d.default_category)}"></label>
    <label class="f">Speed limit (KB/s, 0 = unlimited)<input data-k="downloads.speed_limit_kbps" data-type="int" value="${d.speed_limit_kbps}"></label>
    <label class="f">Retries per article<input data-k="downloads.max_retries" data-type="int" value="${d.max_retries}"></label></div>
    <div class="grid2" style="margin-top:10px">
    ${chk("downloads.auto_extract", d.auto_extract, "Auto-extract archives (rar, zip, 7z) by default")}
    ${chk("downloads.nested_extract", d.nested_extract, "Also extract archives found inside archives")}
    ${chk("downloads.delete_archives", d.delete_archives, "Delete archive files after successful extraction")}
    ${chk("downloads.par2_repair", d.par2_repair, "Repair with par2 when articles are missing")}
    ${chk("downloads.always_verify", d.always_verify, "Always verify with par2, even when complete")}
    ${chk("downloads.delete_par2", d.delete_par2, "Delete par2 files when done")}</div>
    <div class="small muted" style="margin-top:8px">Tools in container: ${tools}</div></div>
  <div class="panel"><h3>Headers &amp; groups</h3><div class="grid2">
    <label class="f">Headers to fetch the first time per group<input data-k="headers.initial_fetch" data-type="int" value="${h.initial_fetch}"></label>
    <label class="f">Headers per request (batch)<input data-k="headers.batch_size" data-type="int" value="${h.batch_size}"></label>
    <label class="f">Connections for browsing / headers<input data-k="headers.max_connections" data-type="int" value="${h.max_connections}"></label>
    <label class="f">Auto-update subscribed groups every (minutes, 0 = off)<input data-k="headers.auto_update_minutes" data-type="int" value="${h.auto_update_minutes}"></label>
    <label class="f">Delete stored headers older than (days, 0 = keep)<input data-k="headers.retention_days" data-type="int" value="${h.retention_days}"></label>
    <label class="f">Popular groups list (comma separated)<input data-k="default_groups" data-type="list" value="${esc(s.default_groups.join(", "))}"></label></div>
    <div style="margin-top:10px">${chk("headers.fetch_descriptions", h.fetch_descriptions, "Download group descriptions with the group list (slower)")}
    ${chk("headers.compression", h.compression !== false, "Compressed header download when the server supports it (XFEATURE COMPRESS GZIP, 3–5× less data)")}</div></div>
  <div class="panel"><h3>Posting</h3><div class="grid2">
    <label class="f">Your name<input data-k="posting.from_name" value="${esc(p.from_name)}"></label>
    <label class="f">E-mail (shown publicly – consider a munged address)<input data-k="posting.from_email" value="${esc(p.from_email)}"></label>
    <label class="f">Organization (optional)<input data-k="posting.organization" value="${esc(p.organization)}"></label></div></div>
  <div class="panel"><h3>Web UI login</h3><div class="small muted" style="margin-bottom:8px">Set a username and password to protect this page (recommended if it is reachable outside your LAN).</div><div class="grid2">
    <label class="f">Username<input data-k="ui.username" value="${esc(s.ui.username)}" autocomplete="off"></label>
    <label class="f">Password<input data-k="ui.password" type="password" value="${esc(s.ui.password)}" autocomplete="new-password"></label></div></div>
  <div class="toolbar"><button id="savesettings">Save settings</button><span class="muted small">Version ${esc(st.version)}</span></div>`;
  renderServers(s.servers);
  $("#addsrv").onclick = () => { s.servers.push({ id: "", name: "Eweka", host: "news.eweka.nl", port: 563, ssl: true, verify_ssl: true, username: "", password: "", connections: 20, priority: s.servers.length, enabled: true }); renderServers(s.servers); };
  $("#savesettings").onclick = () => guard(async () => {
    const out = { servers: collectServers() };
    $$("[data-k]").forEach((el) => {
      const [sec, key] = el.dataset.k.split(".");
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.dataset.type === "int") v = parseInt(v || "0", 10);
      if (el.dataset.type === "list") v = v.split(",").map((x) => x.trim()).filter(Boolean);
      if (key === undefined) out[sec] = v;
      else (out[sec] = out[sec] || {})[key] = v;
    });
    state.settings = await api("/api/settings", { method: "PUT", body: out });
    toast("Settings saved");
    renderServers(state.settings.servers);
  });
}
const chk = (k, v, label) => `<label class="c"><input type="checkbox" data-k="${k}" ${v ? "checked" : ""}> ${esc(label)}</label>`;
function renderServers(list) {
  $("#srvs").innerHTML = list.length ? list.map((sv, i) => `<div class="server" data-i="${i}" data-id="${esc(sv.id || "")}"><div class="grid2">
    <label class="f">Name<input data-s="name" value="${esc(sv.name)}"></label>
    <label class="f">Host<input data-s="host" value="${esc(sv.host)}" placeholder="news.eweka.nl"></label>
    <label class="f">Port<input data-s="port" type="number" value="${sv.port}"></label>
    <label class="f">Username<input data-s="username" value="${esc(sv.username)}" autocomplete="off"></label>
    <label class="f">Password<input data-s="password" type="password" value="${esc(sv.password)}" autocomplete="new-password"></label>
    <label class="f">Connections<input data-s="connections" type="number" value="${sv.connections}"></label>
    <label class="f">Priority (0 = primary)<input data-s="priority" type="number" value="${sv.priority}"></label></div>
    <div class="toolbar" style="margin-top:8px"><label class="c"><input type="checkbox" data-s="ssl" ${sv.ssl ? "checked" : ""}> SSL</label>
      <label class="c"><input type="checkbox" data-s="verify_ssl" ${sv.verify_ssl !== false ? "checked" : ""}> Verify certificate</label>
      <label class="c"><input type="checkbox" data-s="enabled" ${sv.enabled !== false ? "checked" : ""}> Enabled</label>
      <span class="grow"></span><span class="small" id="srvmsg${i}"></span>
      <button class="small sec" onclick="testServer(${i})">Test</button><button class="small danger" onclick="removeServer(${i})">Remove</button></div></div>`).join("")
    : '<div class="empty">No server yet. Click <b>+ Add server</b> and enter your Eweka login.</div>';
}
function collectServers() {
  return $$("#srvs .server").map((el) => {
    const o = { id: el.dataset.id };
    $$("[data-s]", el).forEach((i) => { o[i.dataset.s] = i.type === "checkbox" ? i.checked : i.type === "number" ? parseInt(i.value || "0", 10) : i.value; });
    return o;
  });
}
async function testServer(i) {
  const sv = collectServers()[i];
  const m = $("#srvmsg" + i);
  m.textContent = "Testing…"; m.className = "small muted";
  const r = await post("/api/servers/test", sv).catch((e) => ({ ok: false, message: e.message }));
  m.textContent = r.message; m.className = "small " + (r.ok ? "status-completed" : "status-failed");
}
function removeServer(i) { const list = collectServers(); list.splice(i, 1); renderServers(list); toast("Click Save settings to apply"); }

// ------------------------------------------------------------------ boot
(async function boot() {
  try { state.settings = await api("/api/settings"); } catch (e) {}
  if (state.settings && !state.settings.servers.length && !location.hash) location.hash = "settings";
  route();
  pollStatus();
  setInterval(pollStatus, 2000);
})();
