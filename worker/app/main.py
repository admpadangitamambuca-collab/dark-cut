"""HTTP worker. Use one always-on instance for demo; production needs durable queue/storage."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .engine import ai_metadata, download_source, inspect_duration, render_clip, select_clips, transcribe, validate_source_url, write_srt

ROOT = Path(os.getenv("DATA_DIR", "/tmp/darkcut-jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
POOL = ThreadPoolExecutor(max_workers=MAX_JOBS)
LOCK = threading.Lock()
ACTIVE = set()
app = FastAPI(title="DarkCut AI Worker", version="2.2.0", docs_url=None, redoc_url=None)
allowed = [s.strip() for s in os.getenv("CORS_ORIGINS", "http://localhost:8888,http://localhost:8000").split(",") if s.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allowed, allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


class JobRequest(BaseModel):
    url: str = Field(min_length=12, max_length=2048)
    count: int = Field(default=3, ge=1, le=8)
    min_seconds: int = Field(default=25, ge=10, le=90)
    max_seconds: int = Field(default=65, ge=15, le=120)
    subtitles: bool = True


def snapshot(job_id: str) -> dict:
    p = ROOT / job_id / "status.json"
    if not p.is_file():
        raise HTTPException(404, "Processamento não encontrado ou expirado.")
    return json.loads(p.read_text(encoding="utf-8"))


def save(job_id: str, state: dict) -> None:
    p = ROOT / job_id
    p.mkdir(exist_ok=True, parents=True)
    tmp = p / "status.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p / "status.json")


def do_job(job_id: str, payload: JobRequest) -> None:
    directory = ROOT / job_id
    state = snapshot(job_id)

    def progress(pct, message):
        state.update({"progress": pct, "message": message})
        save(job_id, state)

    try:
        source = download_source(payload.url, directory, progress)
        duration = inspect_duration(source)
        segments = transcribe(source, progress)
        progress(47, "Selecionando trechos de maior interesse")
        clips = select_clips(segments, payload.count, payload.min_seconds, payload.max_seconds)
        clips = [{**c, "end": min(duration, c["end"])} for c in clips if c["start"] < duration]
        if not clips:
            raise ValueError("Não foi encontrado nenhum trecho de fala com duração adequada.")
        all_files = []
        for i, clip in enumerate(clips, 1):
            progress(50 + int((i - 1) / len(clips) * 43), f"Renderizando o corte {i} de {len(clips)}")
            srt = directory / f"corte_{i:02d}.srt"
            has_srt = write_srt(segments, clip["start"], clip["end"], srt) if payload.subtitles else False
            if not has_srt:
                srt = None
            urls = {}
            for fmt in ("vertical", "horizontal"):
                filename = f"corte_{i:02d}_{fmt}.mp4"
                render_clip(source, clip["start"], clip["end"], directory / filename, fmt, srt)
                urls[fmt] = f"/api/jobs/{job_id}/files/{filename}"
                all_files.append(filename)
            metadata = ai_metadata(clip["text"], i)
            state["clips"].append({
                "id": i, "start": clip["start"], "end": clip["end"],
                "duration": round(clip["end"] - clip["start"], 1),
                **metadata, "transcript": clip["text"][:10000], "files": urls,
            })
            save(job_id, state)
        progress(95, "Compactando resultados")
        with zipfile.ZipFile(directory / "todos_os_cortes.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename in all_files:
                archive.write(directory / filename, arcname=filename)
            archive.writestr("titulos_hashtags_resumos.json", json.dumps(state["clips"], indent=2, ensure_ascii=False))
        state.update({"status": "completed", "progress": 100, "message": "Cortes prontos para download",
                      "bundle_url": f"/api/jobs/{job_id}/files/todos_os_cortes.zip"})
        save(job_id, state)
    except Exception as e:
        state.update({"status": "failed", "message": str(e)[:600], "progress": 0})
        save(job_id, state)
    finally:
        # Drop original media to save disk, preserve generated files until TTL expiry.
        for candidate in directory.glob("original.*"):
            candidate.unlink(missing_ok=True)
        with LOCK:
            ACTIVE.discard(job_id)


def cleanup_old_jobs():
    ttl = int(os.getenv("JOB_TTL_HOURS", "24")) * 3600
    now = time.time()
    for p in ROOT.iterdir():
        if p.is_dir() and (now - p.stat().st_mtime) > ttl and p.name not in ACTIVE:
            shutil.rmtree(p, ignore_errors=True)


@app.get("/api/health")
def health():
    return {"ok": True, "version": "2.2.0", "ai_metadata_enabled": bool(os.getenv("OPENAI_API_KEY")),
            "transcription_model": os.getenv("WHISPER_MODEL", "base")}


@app.post("/api/jobs", status_code=202)
def start_job(payload: JobRequest):
    if payload.min_seconds > payload.max_seconds:
        raise HTTPException(422, "A duração mínima não pode ultrapassar a máxima.")
    try:
        payload.url = validate_source_url(payload.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    with LOCK:
        if len(ACTIVE) >= MAX_JOBS:
            raise HTTPException(429, "O motor está ocupado. Tente novamente após finalizar o corte atual.")
        cleanup_old_jobs()
        job_id = uuid.uuid4().hex
        ACTIVE.add(job_id)
    state = {"id": job_id, "status": "processing", "progress": 2, "message": "Preparando processamento",
             "clips": [], "bundle_url": None, "created_at": int(time.time())}
    save(job_id, state)
    POOL.submit(do_job, job_id, payload)
    return state


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str):
    if not re_job_id(job_id):
        raise HTTPException(404, "Processamento inválido.")
    return snapshot(job_id)


def re_job_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


@app.get("/api/jobs/{job_id}/files/{filename}")
def get_output(job_id: str, filename: str, request: Request):
    if not re_job_id(job_id) or not (filename.endswith(".mp4") and filename.startswith("corte_") or
                                     filename == "todos_os_cortes.zip"):
        raise HTTPException(404, "Arquivo inválido.")
    job = snapshot(job_id)
    if job["status"] not in {"completed", "processing"}:
        raise HTTPException(404, "Arquivo não disponível.")
    output = ROOT / job_id / filename
    if not output.is_file():
        raise HTTPException(404, "Arquivo não disponível.")
    return FileResponse(output, media_type="video/mp4" if filename.endswith(".mp4") else "application/zip",
                        filename=filename, headers={"Cache-Control": "private, no-store"})
