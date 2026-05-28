// Audiblez web UI — mirrors the wxPython desktop UI.

const state = {
  job: null,
  chapters: [],
  currentChapterIndex: null,
  edits: {},
  voices: [],
  cudaAvailable: false,
  eventSource: null,
};

const $ = (id) => document.getElementById(id);
const fmt = (n) => n.toLocaleString();

async function init() {
  const sys = await fetch("/api/system").then((r) => r.json());
  state.cudaAvailable = sys.cuda_available;
  $("system-info").textContent =
    `CUDA ${sys.cuda_available ? "ON" : "OFF"} · FFMPEG ${sys.ffmpeg_available ? "OK" : "MISSING"}`;
  const dot = $("status-dot");
  if (dot) {
    if (!sys.ffmpeg_available) dot.className = "dot is-bad";
    else if (!sys.cuda_available) dot.className = "dot is-warn";
    else dot.className = "dot";
  }
  if (sys.cuda_available) {
    $("engine-cuda").checked = true;
  } else {
    $("engine-cuda").disabled = true;
  }

  const v = await fetch("/api/voices").then((r) => r.json());
  state.voices = v.flat;
  const sel = $("voice-select");
  sel.innerHTML = "";
  for (const entry of v.flat) {
    const opt = document.createElement("option");
    opt.value = entry.voice;
    opt.textContent = entry.label;
    sel.appendChild(opt);
  }

  $("epub-input").addEventListener("change", onEpubChosen);
  $("about-btn").addEventListener("click", () => $("about-dialog").showModal());
  $("preview-btn").addEventListener("click", onPreview);
  $("start-btn").addEventListener("click", onStart);
  $("chapter-text").addEventListener("input", onTextEdit);
  $("resume-btn").addEventListener("click", onResume);
  const speedInput = $("speed-input");
  const speedValue = $("speed-value");
  const renderSpeed = () => { speedValue.textContent = `${Number(speedInput.value).toFixed(2)}×`; };
  speedInput.addEventListener("input", renderSpeed);
  renderSpeed();

  await renderRecentJobs();
}

async function renderRecentJobs() {
  let data;
  try {
    data = await fetch("/api/jobs").then((r) => r.json());
  } catch {
    return;
  }
  if (!data.jobs || data.jobs.length === 0) return;
  const section = $("recent-jobs");
  const list = $("recent-jobs-list");
  list.innerHTML = "";
  for (const j of data.jobs) {
    const li = document.createElement("li");
    li.className = `job-row status-${j.status}`;
    const left = document.createElement("a");
    left.href = "#";
    left.textContent = j.title || j.job_id.slice(0, 8);
    left.addEventListener("click", (e) => { e.preventDefault(); loadExistingJob(j.job_id); });
    const right = document.createElement("span");
    right.className = "muted";
    let badge = j.status;
    if (j.broken) badge = "broken";
    right.textContent = badge;
    li.append(left, right);
    list.appendChild(li);
  }
  section.hidden = false;
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
  if (snapshot.status === "error" || snapshot.status === "interrupted") {
    showError(snapshot.error || `Job ended in ${snapshot.status} state.`);
    $("resume-btn").hidden = false;
  } else if (snapshot.status === "finished") {
    refreshFiles();
  }
}

async function onEpubChosen(e) {
  const file = e.target.files[0];
  if (!file) return;
  $("upload-status").textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append("file", file);
  let res;
  try {
    res = await fetch("/api/upload", { method: "POST", body: form });
  } catch (err) {
    $("upload-status").textContent = `Upload failed: ${err}`;
    return;
  }
  if (!res.ok) {
    const err = await res.text();
    $("upload-status").textContent = `Upload failed: ${err}`;
    return;
  }
  const job = await res.json();
  $("upload-status").textContent = "";
  hydrateJob(job);
}

function hydrateJob(job) {
  state.job = job;
  state.chapters = job.chapters;
  state.edits = {};
  state.currentChapterIndex = null;
  $("empty-state").classList.add("hidden");
  $("book-view").classList.remove("hidden");

  // Book meta
  const dl = $("book-meta");
  dl.innerHTML = "";
  for (const [k, v] of [
    ["Title", job.title || "—"],
    ["Author", job.author || "—"],
    ["Total Length", `${fmt(job.total_chars)} characters`],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  const cover = $("cover-img");
  if (job.cover_url) {
    cover.src = job.cover_url;
    cover.hidden = false;
  } else {
    cover.hidden = true;
  }
  $("output-folder").textContent = `outputs/${job.job_id}/`;
  $("files-card").hidden = true;
  $("files-list").innerHTML = "";
  $("error-message").classList.add("hidden");
  $("resume-btn").hidden = true;
  $("progress-wrap").classList.add("hidden");

  renderChapters();
  const firstSelected = state.chapters.find((c) => c.auto_selected) || state.chapters[0];
  if (firstSelected) loadChapter(firstSelected.index);
}

function renderChapters() {
  const tbody = $("chapters-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const c of state.chapters) {
    const tr = document.createElement("tr");
    tr.dataset.index = c.index;

    const td0 = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    // Prefer the saved selection from a resumed job; fall back to the auto
    // selection from `find_good_chapters` for fresh uploads.
    const initiallyChecked = (c.selected !== undefined) ? c.selected : c.auto_selected;
    cb.checked = initiallyChecked;
    c.auto_selected = initiallyChecked;
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", () => { c.auto_selected = cb.checked; });
    td0.appendChild(cb);

    const td1 = document.createElement("td");
    td1.textContent = c.name;
    td1.title = c.preview;

    const td2 = document.createElement("td");
    td2.textContent = fmt(c.length);

    const td3 = document.createElement("td");
    td3.dataset.statusCell = "1";
    if (c.status === "done") { td3.textContent = "Done"; td3.className = "status-done"; }
    else if (c.status === "in_progress") { td3.textContent = "In Progress"; td3.className = "status-in_progress"; }
    else if (c.status) td3.textContent = c.status;

    tr.append(td0, td1, td2, td3);
    tr.addEventListener("click", () => loadChapter(c.index));
    tbody.appendChild(tr);
  }
}

async function loadChapter(idx) {
  state.currentChapterIndex = idx;
  // Highlight row
  for (const tr of document.querySelectorAll("#chapters-table tbody tr")) {
    tr.classList.toggle("selected", Number(tr.dataset.index) === idx);
  }
  const c = state.chapters.find((c) => c.index === idx);
  $("chapter-label").textContent = `Edit / Preview: ${c.name}`;
  const cached = state.edits[idx];
  if (cached !== undefined) {
    $("chapter-text").value = cached;
  } else {
    // We don't ship the full text in the listing payload; fetch lazily via /api/jobs/{id}
    // and find this chapter's text. Cheaper: re-use snapshot which doesn't include text either.
    // So we keep an /api/jobs/{id}/chapter/{idx} endpoint expectation OR include text in snapshot.
    // For simplicity, we include text by re-fetching the job snapshot is wasteful; just fetch.
    const txt = await fetch(`/api/jobs/${state.job.job_id}/chapter/${idx}`).then((r) => r.json());
    state.edits[idx] = txt.text;
    $("chapter-text").value = txt.text;
  }
}

function onTextEdit() {
  if (state.currentChapterIndex == null) return;
  state.edits[state.currentChapterIndex] = $("chapter-text").value;
}

async function onPreview() {
  if (state.currentChapterIndex == null) return;
  const btn = $("preview-btn");
  btn.disabled = true;
  btn.textContent = "⏳ Preparing…";
  try {
    const res = await fetch(`/api/jobs/${state.job.job_id}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chapter_index: state.currentChapterIndex,
        voice: $("voice-select").value,
        speed: Number($("speed-input").value),
        edited_text: state.edits[state.currentChapterIndex] || "",
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { wav_url } = await res.json();
    const audio = $("preview-audio");
    audio.src = wav_url;
    audio.hidden = false;
    audio.play().catch(() => {});
  } catch (e) {
    alert(`Preview failed: ${e.message || e}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔊 Preview";
  }
}

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

  $("start-btn").disabled = true;
  $("error-message").classList.add("hidden");
  $("resume-btn").hidden = true;
  $("progress-wrap").classList.remove("hidden");
  $("progress-bar").value = 0;
  $("progress-label").textContent = "Progress 0%";
  $("eta-label").textContent = "ETA —";
  // Mark planned in table
  for (const c of state.chapters) {
    if (selected.includes(c.index)) setChapterStatus(c.index, "Planned", "");
  }

  let res;
  try {
    res = await fetch(`/api/jobs/${state.job.job_id}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        voice,
        speed,
        cuda,
        selected_chapter_indexes: selected,
        edited_texts: state.edits,
      }),
    });
  } catch (e) {
    showError(`Start failed: ${e.message}`);
    $("start-btn").disabled = false;
    return;
  }
  if (!res.ok) {
    showError(`Start failed: ${await res.text()}`);
    $("start-btn").disabled = false;
    return;
  }
  openEventStream();
}

function openEventStream() {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/jobs/${state.job.job_id}/events`);
  state.eventSource = es;
  es.onmessage = (e) => handleEvent(JSON.parse(e.data));
  es.onerror = () => {
    // Browser will auto-reconnect; close manually if job is already terminal.
    if (!$("start-btn").disabled) es.close();
  };
}

function handleEvent(evt) {
  switch (evt.event) {
    case "CORE_STARTED":
      break;
    case "CORE_CHAPTER_STARTED":
      setChapterStatus(evt.chapter_index, "In Progress", "in_progress");
      break;
    case "CORE_CHAPTER_FINISHED":
      setChapterStatus(evt.chapter_index, "Done", "done");
      break;
    case "CORE_PROGRESS":
      const s = evt.stats || {};
      $("progress-bar").value = s.progress || 0;
      $("progress-label").textContent = `Progress ${s.progress || 0}%`;
      $("eta-label").textContent = `ETA ${s.eta || "—"}`;
      break;
    case "CORE_FINISHED":
      $("progress-label").textContent = "Complete";
      $("eta-label").textContent = "Done";
      $("start-btn").disabled = false;
      refreshFiles();
      break;
    case "CORE_ERROR":
      showError(evt.message || "Synthesis error");
      $("start-btn").disabled = false;
      break;
    case "STREAM_END":
      if (state.eventSource) state.eventSource.close();
      refreshFiles();
      break;
  }
}

function setChapterStatus(idx, text, cls) {
  const tr = document.querySelector(`#chapters-table tbody tr[data-index="${idx}"]`);
  if (!tr) return;
  const td = tr.querySelector('[data-status-cell="1"]');
  td.textContent = text;
  td.className = cls ? `status-${cls}` : "";
}

function showError(msg) {
  const el = $("error-message");
  $("error-text").textContent = msg;
  $("resume-btn").hidden = false;
  el.classList.remove("hidden");
}

async function onResume() {
  if (!state.job) return;
  $("resume-btn").hidden = true;
  $("error-message").classList.add("hidden");
  $("start-btn").disabled = true;
  $("progress-wrap").classList.remove("hidden");
  $("progress-bar").value = 0;
  $("progress-label").textContent = "Resuming";
  $("eta-label").textContent = "";
  try {
    const res = await fetch(`/api/jobs/${state.job.job_id}/resume`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
  } catch (e) {
    $("start-btn").disabled = false;
    showError(`Resume failed: ${e.message || e}`);
    return;
  }
  openEventStream();
}

async function refreshFiles() {
  const res = await fetch(`/api/jobs/${state.job.job_id}/files`).then((r) => r.json());
  const ul = $("files-list");
  ul.innerHTML = "";
  for (const f of res.files) {
    const li = document.createElement("li");
    if (f.is_m4b) li.classList.add("m4b");
    const a = document.createElement("a");
    a.href = f.download_url;
    a.download = f.name;
    a.textContent = f.name;
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
