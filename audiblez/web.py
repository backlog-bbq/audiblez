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
    status: str = "ready"  # ready | queued | running | finished | error
    error: str | None = None
    loop: asyncio.AbstractEventLoop | None = None
    events_log: list[dict] = field(default_factory=list)
    events_wakeup: asyncio.Event | None = None
    last_stats: dict | None = None
    chapter_status: dict[int, str] = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "author": self.creator,
            "status": self.status,
            "error": self.error,
            "total_chars": sum(len(c.extracted_text) for c in self.document_chapters),
            "chapters": [
                {
                    "index": c.chapter_index,
                    "name": getattr(c, "short_name", c.get_name()),
                    "length": len(c.extracted_text),
                    "auto_selected": c.chapter_index in self.auto_selected_indexes,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
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
        "flat": [
            {"code": code, "voice": v, "flag": flags[code], "label": f"{flags[code]} {v}"}
            for code, vlist in voices.items()
            for v in vlist
        ],
    }


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
    )
    JOBS[job_id] = job
    return job.snapshot()


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


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
        if name == "CORE_CHAPTER_STARTED":
            job.chapter_status[kwargs["chapter_index"]] = "in_progress"
        if name == "CORE_CHAPTER_FINISHED":
            job.chapter_status[kwargs["chapter_index"]] = "done"
        # Set terminal status BEFORE appending so the SSE iterator's STREAM_END
        # payload (read right after the event) sees the right status.
        if name == "CORE_FINISHED":
            job.status = "finished"
        elif name == "CORE_ERROR":
            job.status = "error"
            job.error = kwargs.get("message", str(kwargs))
        job.events_log.append(payload)
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
