"""FastAPI web UI for audiblez.

Mirrors the wxPython desktop UI: upload an EPUB, browse and edit chapters,
preview a voice, pick parameters, and start synthesis. Progress streams over
Server-Sent Events. Outputs persist under OUTPUTS_DIR/<job_id>/ and are
downloadable via the API.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import aiofiles
import numpy as np
import soundfile
import torch.cuda
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from audiblez import core
from audiblez.voices import flags, voices

OUTPUTS_DIR = Path(os.environ.get("AUDIBLEZ_OUTPUTS_DIR", "outputs")).resolve()
STATIC_DIR = Path(__file__).parent / "web_static"
MAX_UPLOAD_MB = int(os.environ.get("AUDIBLEZ_MAX_UPLOAD_MB", "200"))
EVENT_KEEPALIVE_SECS = 15

# Short, language-appropriate phrases for the "how does this voice sound?"
# button. Keyed by the first character of the Kokoro voice code (which is its
# language code: a/b = English, e = Spanish, f = French, h = Hindi, i = Italian,
# j = Japanese, p = Portuguese, z = Chinese).
SAMPLE_PHRASES = {
    "a": "She turned the page slowly, savoring the silence between paragraphs.",
    "b": "She turned the page slowly, savoring the silence between paragraphs.",
    "e": "Pasaba las páginas despacio, saboreando el silencio entre los párrafos.",
    "f": "Elle tournait les pages lentement, savourant le silence entre les paragraphes.",
    "h": "वह धीरे-धीरे पन्ने पलट रही थी, अनुच्छेदों के बीच की चुप्पी का स्वाद लेते हुए।",
    "i": "Voltava le pagine lentamente, assaporando il silenzio tra i paragrafi.",
    "j": "彼女はゆっくりとページをめくり、段落の合間の静けさを味わった。",
    "p": "Ela virava as páginas devagar, saboreando o silêncio entre os parágrafos.",
    "z": "她慢慢地翻着书页，品味着段落之间的寂静。",
}


@dataclass
class Job:
    job_id: str
    folder: Path
    epub_path: Path
    title: str = ""
    creator: str = ""
    cover_bytes: bytes = b""
    book: Any = None  # ebooklib book (not serialized)
    document_chapters: list = field(default_factory=list)
    auto_selected_indexes: list[int] = field(default_factory=list)
    status: str = "ready"  # ready | queued | running | finished | error | interrupted
    error: str | None = None
    loop: asyncio.AbstractEventLoop | None = None
    events_log: list[dict] = field(default_factory=list)
    events_wakeup: asyncio.Event | None = None
    last_stats: dict | None = None
    chapter_status: dict[int, str] = field(default_factory=dict)
    # Last-used synthesis params — enough to /resume without further UI input.
    params: dict | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    broken: str | None = None  # set when the EPUB is missing or unreadable

    def snapshot(self) -> dict:
        selected_idx = set((self.params or {}).get("selected_chapter_indexes", []))
        return {
            "job_id": self.job_id,
            "title": self.title,
            "author": self.creator,
            "status": self.status,
            "error": self.error,
            "broken": self.broken,
            "params": self.params,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_chars": sum(len(c.extracted_text) for c in self.document_chapters),
            "chapters": [
                {
                    "index": c.chapter_index,
                    "name": getattr(c, "short_name", c.get_name()),
                    "length": len(c.extracted_text),
                    "auto_selected": c.chapter_index in self.auto_selected_indexes,
                    "selected": c.chapter_index in selected_idx,
                    "status": self.chapter_status.get(c.chapter_index, ""),
                    "preview": core.chapter_beginning_one_liner(c, 50),
                }
                for c in self.document_chapters
            ],
            "stats": self.last_stats,
            "cover_url": f"/api/jobs/{self.job_id}/cover" if self.cover_bytes else None,
        }


JOBS: dict[str, Job] = {}
SYNTH_LOCK = threading.Lock()


def _state_path(folder: Path) -> Path:
    return folder / "job.json"


def _save_job_state(job: Job) -> None:
    """Persist enough of a job to resume after a server restart."""
    job.updated_at = time.time()
    state = {
        "job_id": job.job_id,
        "epub_filename": job.epub_path.name,
        "title": job.title,
        "author": job.creator,
        "status": job.status,
        "error": job.error,
        "params": job.params,
        "chapter_status": {str(k): v for k, v in job.chapter_status.items()},
        "auto_selected_indexes": list(job.auto_selected_indexes),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    tmp = _state_path(job.folder).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_state_path(job.folder))


def _load_job_state(folder: Path) -> Job | None:
    """Rebuild a Job from a folder. Returns None if the folder isn't a job."""
    state_path = _state_path(folder)
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Skipping malformed job state at {state_path}: {e}")
        return None

    epub_path = folder / state["epub_filename"]
    job = Job(
        job_id=state["job_id"],
        folder=folder,
        epub_path=epub_path,
        title=state.get("title", ""),
        creator=state.get("author", ""),
        # If we crashed mid-run, the on-disk state still says "running".
        # Treat that as "interrupted" so the user can /resume.
        status="interrupted" if state.get("status") == "running" else state.get("status", "ready"),
        error=state.get("error"),
        params=state.get("params"),
        chapter_status={int(k): v for k, v in (state.get("chapter_status") or {}).items()},
        auto_selected_indexes=state.get("auto_selected_indexes", []),
        created_at=state.get("created_at", time.time()),
        updated_at=state.get("updated_at", time.time()),
    )

    if not epub_path.exists():
        job.broken = "EPUB file missing"
        return job

    try:
        from ebooklib import epub
        book = epub.read_epub(str(epub_path))
    except Exception as e:
        job.broken = f"Could not re-parse EPUB: {e}"
        return job

    job.book = book
    cover_maybe = core.find_cover(book)
    job.cover_bytes = cover_maybe.get_content() if cover_maybe else b""
    document_chapters = core.find_document_chapters_and_extract_texts(book)
    for c in document_chapters:
        c.short_name = (
            c.get_name()
            .replace(".xhtml", "")
            .replace("xhtml/", "")
            .replace(".html", "")
            .replace("Text/", "")
        )
    job.document_chapters = document_chapters

    # Re-apply edits so the editor and resume see the same text as last run.
    edited_texts = (job.params or {}).get("edited_texts") or {}
    for c in document_chapters:
        edited = edited_texts.get(str(c.chapter_index))
        if edited is not None:
            c.extracted_text = edited

    return job


def _rehydrate_all() -> None:
    """Scan OUTPUTS_DIR and populate JOBS from any job folders we find."""
    if not OUTPUTS_DIR.exists():
        return
    for sub in sorted(OUTPUTS_DIR.iterdir()):
        if not sub.is_dir():
            continue
        j = _load_job_state(sub)
        if j is None:
            continue
        JOBS[j.job_id] = j
        note = f" ({j.broken})" if j.broken else ""
        print(f"Rehydrated job {j.job_id}: status={j.status}{note}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    _rehydrate_all()
    yield


app = FastAPI(title="audiblez web", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(500, "Static UI missing; check the package install.")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/system")
async def system_info():
    return {
        "cuda_available": torch.cuda.is_available(),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.get("/api/voices")
async def list_voices():
    return {
        "flags": flags,
        "voices": voices,
        "sample_phrases": SAMPLE_PHRASES,
        "flat": [
            {"code": code, "voice": v, "flag": flags[code], "label": f"{flags[code]} {v}"}
            for code, vlist in voices.items()
            for v in vlist
        ],
    }


@app.get("/api/voices/{voice}/preview")
async def voice_preview(voice: str, speed: float = 1.0):
    """Synthesize a short, language-appropriate sample of `voice` and stream
    the WAV bytes back. No job context needed — this is purely 'what does
    this voice sound like'."""
    if not any(voice in vlist for vlist in voices.values()):
        raise HTTPException(404, f"Unknown voice: {voice}")
    sample = SAMPLE_PHRASES.get(voice[0], SAMPLE_PHRASES["a"])

    def _generate() -> bytes:
        from kokoro import KPipeline
        import io
        core.set_espeak_library()
        core.load_spacy()
        pipeline = KPipeline(lang_code=voice[0])
        segments = core.gen_audio_segments(pipeline, sample, voice=voice, speed=speed)
        if not segments:
            raise RuntimeError("Voice produced no audio")
        final_audio = np.concatenate(segments)
        buf = io.BytesIO()
        soundfile.write(buf, final_audio, core.sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    try:
        wav_bytes = await asyncio.to_thread(_generate)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Voice preview failed: {e}")
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/upload")
async def upload_epub(request: Request, file: UploadFile):
    if not file.filename or not file.filename.lower().endswith(".epub"):
        raise HTTPException(400, "Only .epub uploads are accepted.")

    job_id = uuid.uuid4().hex
    folder = OUTPUTS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    epub_path = folder / file.filename

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    async with aiofiles.open(epub_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                await f.close()
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(413, f"File too large; max {MAX_UPLOAD_MB} MB.")
            await f.write(chunk)

    try:
        from ebooklib import epub
        book = epub.read_epub(str(epub_path))
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, f"Could not parse EPUB: {e}")

    meta_title = book.get_metadata("DC", "title")
    title = meta_title[0][0] if meta_title else ""
    meta_creator = book.get_metadata("DC", "creator")
    creator = meta_creator[0][0] if meta_creator else ""

    cover_maybe = core.find_cover(book)
    cover_bytes = cover_maybe.get_content() if cover_maybe else b""

    document_chapters = core.find_document_chapters_and_extract_texts(book)
    for c in document_chapters:
        c.short_name = (
            c.get_name()
            .replace(".xhtml", "")
            .replace("xhtml/", "")
            .replace(".html", "")
            .replace("Text/", "")
        )

    auto_selected = core.find_good_chapters(document_chapters)
    auto_selected_indexes = [c.chapter_index for c in auto_selected]

    now = time.time()
    job = Job(
        job_id=job_id,
        folder=folder,
        epub_path=epub_path,
        title=title,
        creator=creator,
        cover_bytes=cover_bytes,
        book=book,
        document_chapters=document_chapters,
        auto_selected_indexes=auto_selected_indexes,
        created_at=now,
        updated_at=now,
    )
    JOBS[job_id] = job
    _save_job_state(job)
    return job.snapshot()


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.get("/api/jobs")
async def list_jobs():
    """All known jobs (active + rehydrated from disk), newest first."""
    rows = sorted(JOBS.values(), key=lambda j: j.updated_at or 0, reverse=True)
    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "title": j.title,
                "author": j.creator,
                "status": j.status,
                "error": j.error,
                "broken": j.broken,
                "created_at": j.created_at,
                "updated_at": j.updated_at,
                "can_resume": j.status in ("error", "interrupted") and not j.broken and bool(j.params),
            }
            for j in rows
        ]
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return _get_job(job_id).snapshot()


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "Job is running; cannot delete.")
    shutil.rmtree(job.folder, ignore_errors=True)
    JOBS.pop(job_id, None)
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/chapter/{idx}")
async def get_chapter_text(job_id: str, idx: int):
    job = _get_job(job_id)
    if idx < 0 or idx >= len(job.document_chapters):
        raise HTTPException(404, "Chapter not found.")
    c = job.document_chapters[idx]
    return {"index": idx, "name": getattr(c, "short_name", c.get_name()), "text": c.extracted_text}


@app.get("/api/jobs/{job_id}/cover")
async def get_cover(job_id: str):
    job = _get_job(job_id)
    if not job.cover_bytes:
        raise HTTPException(404, "No cover image.")
    return Response(content=job.cover_bytes, media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/files")
async def list_files(job_id: str):
    job = _get_job(job_id)
    files = []
    for p in sorted(job.folder.iterdir()):
        if p.is_file() and p.name != job.epub_path.name:
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "download_url": f"/api/jobs/{job_id}/download/{p.name}",
                "is_m4b": p.suffix.lower() == ".m4b",
            })
    return {"files": files}


@app.get("/api/jobs/{job_id}/download/{filename}")
async def download_file(job_id: str, filename: str):
    job = _get_job(job_id)
    target = (job.folder / filename).resolve()
    try:
        target.relative_to(job.folder.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid filename.")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(target, filename=target.name)


@app.post("/api/jobs/{job_id}/preview")
async def preview_chapter(job_id: str, payload: dict):
    job = _get_job(job_id)
    chapter_index = int(payload.get("chapter_index", -1))
    voice = payload.get("voice")
    speed = float(payload.get("speed", 1.0))
    edited_text = payload.get("edited_text")
    if not voice:
        raise HTTPException(400, "voice required")
    if chapter_index < 0 or chapter_index >= len(job.document_chapters):
        raise HTTPException(400, "Invalid chapter_index")

    chapter = job.document_chapters[chapter_index]
    text = (edited_text if edited_text is not None else chapter.extracted_text)[:300]
    if not text.strip():
        raise HTTPException(400, "Chapter is empty.")

    def _generate() -> Path:
        from kokoro import KPipeline
        core.set_espeak_library()
        core.load_spacy()
        pipeline = KPipeline(lang_code=voice[0])
        audio_segments = core.gen_audio_segments(pipeline, text, voice=voice, speed=speed)
        final_audio = np.concatenate(audio_segments)
        out = NamedTemporaryFile(
            prefix="preview_", suffix=".wav", dir=str(job.folder), delete=False
        )
        out.close()
        soundfile.write(out.name, final_audio, core.sample_rate)
        return Path(out.name)

    try:
        wav_path = await asyncio.to_thread(_generate)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Preview failed: {e}")

    return {"wav_url": f"/api/jobs/{job_id}/download/{wav_path.name}"}


@app.post("/api/jobs/{job_id}/start")
async def start_synthesis(job_id: str, payload: dict):
    job = _get_job(job_id)
    if job.broken:
        raise HTTPException(410, f"Job is broken: {job.broken}")
    if job.status == "running":
        raise HTTPException(409, "Job already running.")
    if not SYNTH_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another synthesis is in progress.")

    voice = payload.get("voice")
    speed = float(payload.get("speed", 1.0))
    use_cuda = bool(payload.get("cuda", False))
    selected_indexes = set(int(i) for i in payload.get("selected_chapter_indexes", []))
    edited_texts = payload.get("edited_texts", {}) or {}

    if not voice:
        SYNTH_LOCK.release()
        raise HTTPException(400, "voice required")
    if not selected_indexes:
        SYNTH_LOCK.release()
        raise HTTPException(400, "Select at least one chapter.")

    selected_chapters = []
    for c in job.document_chapters:
        if c.chapter_index in selected_indexes:
            edited = edited_texts.get(str(c.chapter_index))
            if edited is not None:
                c.extracted_text = edited
            selected_chapters.append(c)
            # Keep prior 'done' (so the user can see what's already finished
            # carry over); reset everything else to Planned for this run.
            if job.chapter_status.get(c.chapter_index) != "done":
                job.chapter_status[c.chapter_index] = "Planned"

    try:
        torch.set_default_device("cuda" if use_cuda and torch.cuda.is_available() else "cpu")
    except Exception as e:
        print(f"torch.set_default_device failed: {e}")

    loop = asyncio.get_running_loop()
    job.loop = loop
    job.status = "running"
    job.error = None
    job.events_log.clear()
    job.last_stats = None
    job.events_wakeup = asyncio.Event()
    job.params = {
        "voice": voice,
        "speed": speed,
        "cuda": use_cuda,
        "selected_chapter_indexes": sorted(selected_indexes),
        "edited_texts": edited_texts,
    }
    _save_job_state(job)

    def post_event(name, **kwargs):
        payload = {"event": name, "ts": time.time(), **kwargs}
        if name == "CORE_PROGRESS" and "stats" in kwargs:
            s = kwargs["stats"]
            payload["stats"] = {
                "progress": getattr(s, "progress", 0),
                "eta": getattr(s, "eta", ""),
                "processed_chars": s.processed_chars,
                "total_chars": s.total_chars,
                "chars_per_sec": s.chars_per_sec,
            }
            job.last_stats = payload["stats"]
        persist = False
        if name == "CORE_CHAPTER_STARTED":
            job.chapter_status[kwargs["chapter_index"]] = "in_progress"
            persist = True
        if name == "CORE_CHAPTER_FINISHED":
            job.chapter_status[kwargs["chapter_index"]] = "done"
            persist = True
        # Set terminal status BEFORE appending so the SSE iterator's STREAM_END
        # payload (read right after the event) sees the right status.
        if name == "CORE_FINISHED":
            job.status = "finished"
            persist = True
        elif name == "CORE_ERROR":
            job.status = "error"
            job.error = kwargs.get("message", str(kwargs))
            persist = True
        job.events_log.append(payload)
        if persist:
            try:
                _save_job_state(job)
            except Exception as e:
                print(f"Failed to persist job state: {e}")
        loop.call_soon_threadsafe(job.events_wakeup.set)

    def worker():
        try:
            core.synthesize(
                file_path=str(job.epub_path),
                selected_chapters=selected_chapters,
                voice=voice,
                speed=speed,
                output_folder=str(job.folder),
                title=job.title,
                creator=job.creator,
                cover_image=job.cover_bytes,
                post_event=post_event,
            )
            # synthesize sets terminal status via post_event. If it returned
            # cleanly without emitting a terminal event (e.g. ffmpeg missing
            # path that swallowed CORE_FINISHED), force one so SSE closes.
            if job.status == "running":
                post_event("CORE_ERROR", message="Synthesis finished without producing an M4B.")
        except Exception as e:
            traceback.print_exc()
            # synthesize() already posted CORE_ERROR inside its try block.
            # If somehow it didn't, post one now so the stream terminates.
            if job.status == "running":
                post_event("CORE_ERROR", message=str(e))
        finally:
            SYNTH_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return {"started": job_id}


@app.post("/api/jobs/{job_id}/resume")
async def resume_synthesis(job_id: str):
    """Re-run synthesis with the last-used params. core.synthesize() skips
    chapters whose WAVs already exist, so this picks up roughly where the
    previous run left off."""
    job = _get_job(job_id)
    if job.broken:
        raise HTTPException(410, f"Job is broken: {job.broken}")
    if not job.params:
        raise HTTPException(400, "No saved params for this job; nothing to resume.")
    return await start_synthesis(job_id, dict(job.params))


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str, request: Request):
    job = _get_job(job_id)

    async def gen():
        sent = 0
        terminal = ("CORE_FINISHED", "CORE_ERROR")
        while True:
            # Send any events that have arrived since we last yielded.
            while sent < len(job.events_log):
                evt = job.events_log[sent]
                sent += 1
                yield f"id: {sent}\ndata: {json.dumps(evt)}\n\n"
                if evt.get("event") in terminal:
                    yield f"data: {json.dumps({'event': 'STREAM_END', 'status': job.status})}\n\n"
                    return
            if job.status not in ("running", "ready", "queued"):
                # Job already terminal but no terminal event was logged (edge case).
                yield f"data: {json.dumps({'event': 'STREAM_END', 'status': job.status})}\n\n"
                return
            if await request.is_disconnected():
                return
            if job.events_wakeup is None:
                # Job never started; nothing to stream.
                yield f"data: {json.dumps({'event': 'IDLE'})}\n\n"
                return
            try:
                await asyncio.wait_for(job.events_wakeup.wait(), timeout=EVENT_KEEPALIVE_SECS)
                job.events_wakeup.clear()
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main():
    import uvicorn
    host = os.environ.get("AUDIBLEZ_HOST", "0.0.0.0")
    port = int(os.environ.get("AUDIBLEZ_PORT", "8009"))
    uvicorn.run("audiblez.web:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
