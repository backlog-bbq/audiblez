// Audiblez web UI — Editorial Paper layout.

const state = {
  job: null,
  chapters: [],
  edits: {},               // {chapterIndex: editedText}
  textLoaded: new Set(),   // chapters whose text has been fetched into the row
  voices: [],
  cudaAvailable: false,
  eventSource: null,
  logLines: [],
};

const $ = (id) => document.getElementById(id);
const fmt = (n) => Number(n).toLocaleString();

async function init() {
  const sys = await fetch("/api/system").then((r) => r.json());
  state.cudaAvailable = sys.cuda_available;
  $("system-info").textContent =
    `CUDA ${sys.cuda_available ? "ON" : "OFF"} · FFMPEG ${sys.ffmpeg_available ? "OK" : "MISSING"}`;
  const dot = $("status-dot");
  if (!sys.ffmpeg_available) dot.className = "dot is-bad";
  else if (!sys.cuda_available) dot.className = "dot is-warn";
  else dot.className = "dot";
  if (sys.cuda_available) {
    $("engine-cuda").checked = true;
  } else {
    $("engine-cuda").disabled = true;
    $("engineHint").textContent = "No GPU detected — CUDA is disabled on this host.";
  }

  const v = await fetch("/api/voices").then((r) => r.json());
  state.voices = v.flat;
  state.samplePhrases = v.sample_phrases || {};
  const sel = $("voice-select");
  sel.innerHTML = "";
  for (const entry of v.flat) {
    const opt = document.createElement("option");
    opt.value = entry.voice;
    opt.textContent = entry.label;
    sel.appendChild(opt);
  }
  // Default voice — pick af_bella if available, else the first option.
  const PREFERRED_VOICE = "af_bella";
  if (state.voices.some((e) => e.voice === PREFERRED_VOICE)) {
    sel.value = PREFERRED_VOICE;
  }
  sel.addEventListener("change", updateVoiceHint);
  updateVoiceHint();

  bindDropZone("dropZone", "browseBtn");
  bindDropZone("dropZoneCompact", "browseBtnCompact");
  $("epub-input").addEventListener("change", (e) => uploadFile(e.target.files[0]));
  $("about-btn").addEventListener("click", () => $("about-dialog").showModal());
  $("preview-btn").addEventListener("click", onPreview);
  $("start-btn").addEventListener("click", onStart);
  $("resume-btn").addEventListener("click", onResume);
  $("selAll").addEventListener("click", () => quickSelect("all"));
  $("selNone").addEventListener("click", () => quickSelect("none"));
  $("selSubstantive").addEventListener("click", () => quickSelect("substantive"));

  const speedInput = $("speed-input");
  const speedValue = $("speed-value");
  const renderSpeed = () => { speedValue.textContent = `${Number(speedInput.value).toFixed(2)}×`; };
  speedInput.addEventListener("input", renderSpeed);
  renderSpeed();

  await renderRecentJobs();
}

/* ─── Drop zone wiring ─────────────────────────────────────────────── */

function bindDropZone(zoneId, browseId) {
  const zone = $(zoneId);
  const browse = $(browseId);
  if (!zone) return;
  if (browse) browse.addEventListener("click", () => $("epub-input").click());
  zone.addEventListener("click", (e) => {
    if (e.target.closest("button, a")) return;
    $("epub-input").click();
  });
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("is-hover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("is-hover"); }));
  zone.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
}

async function uploadFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".epub")) {
    $("upload-status").textContent = "Only .epub files are supported.";
    return;
  }
  $("upload-status").textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append("file", file);
  let res;
  try { res = await fetch("/api/upload", { method: "POST", body: form }); }
  catch (e) { $("upload-status").textContent = `Upload failed: ${e.message || e}`; return; }
  if (!res.ok) { $("upload-status").textContent = `Upload failed: ${await res.text()}`; return; }
  $("upload-status").textContent = "";
  hydrateJob(await res.json());
}

/* ─── Recent jobs ──────────────────────────────────────────────────── */

async function renderRecentJobs() {
  let data;
  try { data = await fetch("/api/jobs").then((r) => r.json()); }
  catch { return; }
  if (!data.jobs || !data.jobs.length) return;
  for (const listId of ["recent-jobs-list", "recent-jobs-list-inline"]) {
    const list = $(listId);
    if (!list) continue;
    list.innerHTML = "";
    for (const j of data.jobs) {
      const li = document.createElement("li");
      li.className = `job-row status-${j.status}`;
      const a = document.createElement("a");
      a.href = "#"; a.textContent = j.title || j.job_id.slice(0, 8);
      a.addEventListener("click", (e) => { e.preventDefault(); loadExistingJob(j.job_id); });
      const tag = document.createElement("span");
      tag.textContent = j.broken ? "broken" : j.status;
      li.append(a, tag);
      list.appendChild(li);
    }
  }
  const empty = $("recent-jobs"); if (empty) empty.hidden = false;
  const inline = $("recent-jobs-inline"); if (inline) inline.hidden = false;
}

async function loadExistingJob(jobId) {
  let snapshot;
  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error(await res.text());
    snapshot = await res.json();
  } catch (e) {
    alert(`Couldn't load job: ${e.message || e}`);
    return;
  }
  hydrateJob(snapshot);
  // Always offer existing artifacts — a finished job has its M4B; an
  // errored/interrupted job may have partial WAVs the user wants to grab.
  refreshFiles();
  if (snapshot.status === "error" || snapshot.status === "interrupted") {
    showError(snapshot.error || `Job ended in ${snapshot.status} state.`);
  }
}

/* ─── Job hydration ────────────────────────────────────────────────── */

function hydrateJob(job) {
  state.job = job;
  state.chapters = job.chapters || [];
  state.edits = {};
  state.textLoaded = new Set();
  state.logLines = [];
  $("logText").textContent = "";

  $("empty-state").classList.add("hidden");
  $("book-view").classList.remove("hidden");

  // Book card
  $("bookCard").hidden = false;
  $("bookTitle").textContent = job.title || "Untitled";
  $("bookAuthor").textContent = job.author || "Unknown author";
  const cover = $("cover-img");
  if (job.cover_url) { cover.src = job.cover_url; cover.removeAttribute("hidden"); }
  else { cover.removeAttribute("src"); cover.setAttribute("hidden", ""); }

  // Sidebar bits
  $("output-folder").textContent = `outputs/${job.job_id}/`;
  $("files-card").hidden = true;
  $("files-list").innerHTML = "";
  $("error-message").hidden = true;
  $("resume-btn").hidden = true;
  $("progress-wrap").hidden = true;
  $("logBox").hidden = true;
  $("preview-audio-wrap").hidden = true;
  $("voiceHint").textContent = "Pick a voice; tap ▶ to hear a sample.";

  renderChapters();
  $("chaptersWrap").hidden = false;
  updateStats();
}

function renderChapters() {
  const list = $("chapters-list");
  list.innerHTML = "";
  // If this job has been started before, restore the saved selection.
  // Otherwise fall back to the heuristic auto-selection so first-time
  // uploads still show the obvious chapters checked.
  const hasSavedSelection =
    state.job && state.job.params &&
    Array.isArray(state.job.params.selected_chapter_indexes);
  for (const c of state.chapters) {
    const initiallyChecked = hasSavedSelection ? !!c.selected : !!c.auto_selected;
    c.auto_selected = initiallyChecked;

    const li = document.createElement("li");
    li.className = "chapter";
    li.dataset.index = c.index;

    const row = document.createElement("div");
    row.className = "chapter__row";

    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = initiallyChecked;
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", () => {
      c.auto_selected = cb.checked;
      updateStats();
    });

    const title = document.createElement("span");
    title.className = "chap-title";
    title.textContent = c.name;
    title.title = c.preview || "";

    // Single info cell: shows the char count by default, swapped to the
    // run status (planned / in_progress / done) once one is set.
    const info = document.createElement("span");
    info.className = "chap-info chap-meta";
    info.dataset.statusCell = "1";
    info.dataset.length = c.length;
    if (c.status) setStatusText(info, c.status);
    else info.textContent = `${fmt(c.length)} ch`;

    const toggle = document.createElement("button");
    toggle.type = "button"; toggle.className = "chap-toggle";
    toggle.setAttribute("aria-label", "Expand chapter");
    toggle.textContent = "▾";

    row.append(cb, title, info, toggle);

    const preview = document.createElement("div");
    preview.className = "chapter__preview";
    const snippetView = document.createElement("div");
    snippetView.className = "chap-snippet";
    snippetView.textContent = "Loading…";

    const editToggle = document.createElement("button");
    editToggle.type = "button"; editToggle.className = "linkbtn chap-edit-toggle";
    editToggle.textContent = "Edit full text";

    const ta = document.createElement("textarea");
    ta.className = "chap-text"; ta.spellcheck = false; ta.dataset.index = c.index;
    ta.placeholder = "Loading…"; ta.hidden = true;
    ta.addEventListener("input", () => { state.edits[c.index] = ta.value; });

    editToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const editing = ta.hidden;
      ta.hidden = !editing;
      snippetView.hidden = editing;
      editToggle.textContent = editing ? "Show snippet" : "Edit full text";
      if (editing) ta.focus();
    });

    preview.append(snippetView, editToggle, ta);
    li.append(row, preview);

    // Row click = toggle the checkbox. Only the chevron expands.
    row.addEventListener("click", (e) => {
      if (e.target.closest(".chap-toggle")) return;
      if (e.target.matches('input[type=checkbox]')) return;
      cb.checked = !cb.checked;
      c.auto_selected = cb.checked;
      updateStats();
    });
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleChapter(li, c.index);
    });

    list.appendChild(li);
  }
}

function snippetOf(text, headSentences = 3, tailSentences = 3) {
  const sents = String(text).split(/(?<=[.!?])\s+/).map((s) => s.trim()).filter(Boolean);
  if (sents.length <= headSentences + tailSentences) return sents.join(" ");
  return sents.slice(0, headSentences).join(" ") +
         "\n\n…\n\n" +
         sents.slice(-tailSentences).join(" ");
}

async function toggleChapter(li, idx) {
  const willOpen = !li.classList.contains("is-open");
  // Single-open: collapse all others when opening one.
  for (const other of document.querySelectorAll(".chapter.is-open")) {
    if (other !== li) other.classList.remove("is-open");
  }
  li.classList.toggle("is-open", willOpen);
  if (!willOpen) return;
  if (state.textLoaded.has(idx)) return;
  const snippet = li.querySelector(".chap-snippet");
  const ta = li.querySelector(".chap-text");
  snippet.textContent = "Loading…";
  try {
    const data = await fetch(`/api/jobs/${state.job.job_id}/chapter/${idx}`).then((r) => r.json());
    const text = (state.edits[idx] !== undefined) ? state.edits[idx] : data.text;
    state.edits[idx] = text;
    state.textLoaded.add(idx);
    snippet.textContent = snippetOf(text);
    ta.value = text;
  } catch (e) {
    snippet.textContent = `Failed to load chapter: ${e.message || e}`;
  }
}

function setStatusText(node, status) {
  // Single info cell — swap between length (chap-meta) and run status.
  if (status === "done")          { node.textContent = "Done";        node.className = "chap-info chap-status status-done"; }
  else if (status === "in_progress") { node.textContent = "In progress"; node.className = "chap-info chap-status status-in_progress"; }
  else if (status === "Planned")  { node.textContent = "Planned";     node.className = "chap-info chap-status status-planned"; }
  else if (status)                { node.textContent = status;        node.className = "chap-info chap-status"; }
  else {
    // Empty status — revert to char count.
    const length = node.dataset.length || "0";
    node.textContent = `${fmt(length)} ch`;
    node.className = "chap-info chap-meta";
  }
}

function quickSelect(kind) {
  for (const li of document.querySelectorAll("#chapters-list li.chapter")) {
    const idx = Number(li.dataset.index);
    const c = state.chapters.find((x) => x.index === idx);
    const cb = li.querySelector('input[type=checkbox]');
    let on = false;
    if (kind === "all") on = true;
    else if (kind === "none") on = false;
    else if (kind === "substantive") on = state.job ? state.chapters.find((x) => x.index === idx) && c.length >= 500 && /chap|part|section/i.test(c.name) : false;
    // Fallback for "substantive" if the regex misses: include any chapter > 500 chars.
    if (kind === "substantive" && !on) on = c.length >= 500;
    cb.checked = on; c.auto_selected = on;
  }
  updateStats();
}

function updateStats() {
  if (!state.chapters.length) return;
  const total = state.chapters.length;
  const chars = state.chapters.reduce((a, c) => a + c.length, 0);
  const selected = state.chapters.filter((c) => c.auto_selected).length;
  $("statChapters").textContent = fmt(total);
  $("statChars").textContent = fmt(chars);
  $("statSelected").textContent = fmt(selected);
}

/* ─── Voice preview ───────────────────────────────────────────────── */

function updateVoiceHint() {
  // Reset the sample text to the language default UNLESS the user has
  // typed their own phrase that doesn't match any default.
  const phrases = state.samplePhrases || {};
  const sample = $("sample-text");
  if (!sample) return;
  const known = new Set(Object.values(phrases));
  if (!sample.value.trim() || known.has(sample.value.trim())) {
    const v = $("voice-select").value;
    sample.value = phrases[v[0]] || phrases["a"] || "";
  }
}

async function onPreview() {
  const voice = $("voice-select").value;
  if (!voice) return;
  const speed = Number($("speed-input").value);
  const text = ($("sample-text").value || "").trim();
  const btn = $("preview-btn");
  btn.disabled = true; btn.textContent = "…";
  try {
    const url =
      `/api/voices/${encodeURIComponent(voice)}/preview` +
      `?speed=${speed}` +
      (text ? `&text=${encodeURIComponent(text)}` : "");
    const res = await fetch(url);
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const audio = $("preview-audio");
    if (audio.src && audio.src.startsWith("blob:")) URL.revokeObjectURL(audio.src);
    audio.src = URL.createObjectURL(blob);
    $("preview-audio-wrap").hidden = false;
    audio.play().catch(() => {});
  } catch (e) {
    alert(`Preview failed: ${e.message || e}`);
  } finally {
    btn.disabled = false; btn.textContent = "▶";
  }
}

/* ─── Start / Resume ──────────────────────────────────────────────── */

async function onStart() {
  if (!state.job) return;
  const selected = state.chapters.filter((c) => c.auto_selected).map((c) => c.index);
  if (!selected.length) {
    alert("Select at least one chapter.");
    return;
  }
  const voice = $("voice-select").value;
  const speed = Number($("speed-input").value);
  const cuda = document.querySelector('input[name="engine"]:checked').value === "cuda";
  const keep_intermediates = $("keep-intermediates").checked;

  beginRunUI("queued");

  // Reset row statuses for the selected set.
  for (const li of document.querySelectorAll("#chapters-list li.chapter")) {
    const idx = Number(li.dataset.index);
    const node = li.querySelector('[data-status-cell="1"]');
    if (selected.includes(idx)) setStatusText(node, "Planned");
    else setStatusText(node, "");
  }

  let res;
  try {
    res = await fetch(`/api/jobs/${state.job.job_id}/start`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        voice, speed, cuda,
        keep_intermediates,
        selected_chapter_indexes: selected,
        edited_texts: state.edits,
      }),
    });
  } catch (e) { failRunUI(`Start failed: ${e.message}`); return; }
  if (!res.ok) { failRunUI(`Start failed: ${await res.text()}`); return; }
  openEventStream();
}

async function onResume() {
  if (!state.job) return;
  beginRunUI("resuming");
  $("resume-btn").hidden = true;
  $("error-message").hidden = true;
  try {
    const res = await fetch(`/api/jobs/${state.job.job_id}/resume`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
  } catch (e) { failRunUI(`Resume failed: ${e.message || e}`); return; }
  openEventStream();
}

function beginRunUI(label) {
  $("start-btn").disabled = true;
  $("error-message").hidden = true;
  $("progress-wrap").hidden = false;
  $("logBox").hidden = false;
  $("progress-bar").value = 0;
  $("progress-label").textContent = label;
  $("progress-pct").textContent = "0%";
  $("eta-label").textContent = "ETA —";
  pushLog(`[${label}] starting`);
}

function failRunUI(msg) {
  $("start-btn").disabled = false;
  showError(msg);
}

/* ─── SSE event stream ────────────────────────────────────────────── */

function openEventStream() {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/jobs/${state.job.job_id}/events`);
  state.eventSource = es;
  es.onmessage = (e) => handleEvent(JSON.parse(e.data));
  es.onerror = () => {
    if (!$("start-btn").disabled) es.close();
  };
}

function handleEvent(evt) {
  // The progress bar already shows the per-percent update; the log box
  // would just be a wall of "progress N%" lines, so skip those here.
  if (evt.event !== "CORE_PROGRESS") pushLog(eventToLogLine(evt));
  switch (evt.event) {
    case "CORE_STARTED":
      $("progress-label").textContent = "running";
      break;
    case "CORE_CHAPTER_STARTED":
      setRowStatus(evt.chapter_index, "in_progress");
      break;
    case "CORE_CHAPTER_FINISHED":
      setRowStatus(evt.chapter_index, "done");
      break;
    case "CORE_PROGRESS":
      const s = evt.stats || {};
      $("progress-bar").value = s.progress || 0;
      $("progress-pct").textContent = `${s.progress || 0}%`;
      $("eta-label").textContent = `ETA ${s.eta || "—"} · ${fmt(s.processed_chars || 0)}/${fmt(s.total_chars || 0)} chars`;
      break;
    case "CORE_FINISHED":
      $("progress-label").textContent = "complete";
      $("progress-pct").textContent = "100%";
      $("eta-label").textContent = "done";
      $("start-btn").disabled = false;
      refreshFiles();
      break;
    case "CORE_ERROR":
      $("progress-label").textContent = "error";
      $("start-btn").disabled = false;
      showError(evt.message || "Synthesis error");
      break;
    case "STREAM_END":
      if (state.eventSource) state.eventSource.close();
      refreshFiles();
      break;
  }
}

function eventToLogLine(evt) {
  const t = new Date((evt.ts || Date.now() / 1000) * 1000).toISOString().substr(11, 8);
  const tag = (evt.event || "").replace("CORE_", "").toLowerCase();
  if (evt.event === "CORE_PROGRESS" && evt.stats) {
    return `${t}  progress  ${evt.stats.progress}%  eta ${evt.stats.eta}`;
  }
  if ("chapter_index" in evt) return `${t}  ${tag}  chapter ${evt.chapter_index}`;
  if (evt.message) return `${t}  ${tag}  ${evt.message}`;
  return `${t}  ${tag}`;
}

function pushLog(line) {
  state.logLines.push(line);
  if (state.logLines.length > 200) state.logLines.shift();
  const pre = $("logText");
  pre.textContent = state.logLines.join("\n");
  pre.parentElement.scrollTop = pre.parentElement.scrollHeight;
}

function setRowStatus(idx, status) {
  const li = document.querySelector(`#chapters-list li.chapter[data-index="${idx}"]`);
  if (!li) return;
  const node = li.querySelector('[data-status-cell="1"]');
  setStatusText(node, status);
}

function showError(msg) {
  $("error-text").textContent = msg;
  $("error-message").hidden = false;
  $("resume-btn").hidden = false;
}

async function refreshFiles() {
  if (!state.job) return;
  const res = await fetch(`/api/jobs/${state.job.job_id}/files`).then((r) => r.json());
  const ul = $("files-list");
  ul.innerHTML = "";
  for (const f of res.files) {
    const li = document.createElement("li");
    if (f.is_m4b) li.classList.add("m4b");
    const a = document.createElement("a");
    a.href = f.download_url; a.download = f.name; a.textContent = f.name;
    const size = document.createElement("span");
    size.className = "muted";
    size.textContent = formatBytes(f.size);
    li.append(a, size);
    ul.appendChild(li);
  }
  $("files-card").hidden = res.files.length === 0;
}

function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

init();
