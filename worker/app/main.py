"""DarkCut AI File Studio: local video -> selected highlights -> two exports + copy.

Prototype service: one worker, ephemeral disk. Use durable storage, auth and a task
queue before exposing this endpoint for public production use.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .engine import ai_metadata, inspect_duration, render_clip, select_clips, transcribe, transcription_backend_ready, write_srt

logger = logging.getLogger(__name__)

ROOT = Path(os.getenv("DATA_DIR", "/tmp/darkcut-jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
MAX_UPLOAD_BYTES = max(1, int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1048576))))
CHUNK_BYTES = 4 * 1048576
UPLOAD_IDLE_SECONDS = 30 * 60
POOL = ThreadPoolExecutor(max_workers=MAX_JOBS)
LOCK = threading.Lock()
ACTIVE: set[str] = set()
UPLOADS: dict[str, dict] = {}

app = FastAPI(title="DarkCut AI — File Studio", version="3.0.0", docs_url=None, redoc_url=None)
allowed = [s.strip() for s in os.getenv("CORS_ORIGINS", "http://localhost:8888,http://localhost:8000").split(",") if s.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=allowed, allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "X-Upload-Token"],
)


class JobSettings(BaseModel):
    count: int = Field(default=3, ge=1, le=8)
    min_seconds: int = Field(default=25, ge=10, le=90)
    max_seconds: int = Field(default=60, ge=15, le=120)
    subtitles: bool = True


class UploadStart(JobSettings):
    filename: str = Field(min_length=5, max_length=255)
    size: int = Field(ge=12)


def _validated_settings(settings: JobSettings) -> None:
    if settings.min_seconds > settings.max_seconds:
        raise HTTPException(422, "A duração mínima não pode ultrapassar a máxima.")


def _job_id_valid(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def snapshot(job_id: str) -> dict:
    if not _job_id_valid(job_id):
        raise HTTPException(404, "Projeto inválido.")
    p = ROOT / job_id / "status.json"
    if not p.is_file():
        raise HTTPException(404, "Projeto não encontrado ou expirado no servidor.")
    return json.loads(p.read_text(encoding="utf-8"))


def save(job_id: str, state: dict) -> None:
    directory = ROOT / job_id
    directory.mkdir(exist_ok=True, parents=True)
    tmp = directory / "status.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(directory / "status.json")


def cleanup_old_jobs() -> None:
    now = time.time()
    ttl = int(os.getenv("JOB_TTL_HOURS", "24")) * 3600
    with LOCK:
        abandoned = [id for id, data in UPLOADS.items() if now - data["touched"] > UPLOAD_IDLE_SECONDS]
        for job_id in abandoned:
            UPLOADS.pop(job_id, None)
            ACTIVE.discard(job_id)
        in_progress = ACTIVE.copy()
    for job_id in abandoned:
        shutil.rmtree(ROOT / job_id, ignore_errors=True)
    for directory in ROOT.iterdir():
        if directory.is_dir() and directory.name not in in_progress and now - directory.stat().st_mtime > ttl:
            shutil.rmtree(directory, ignore_errors=True)


def reserve_job(message: str = "Preparando o vídeo", uploading: bool = False) -> str:
    cleanup_old_jobs()
    with LOCK:
        if len(ACTIVE) >= MAX_JOBS:
            raise HTTPException(429, "O motor já está processando um vídeo. Tente após a conclusão do projeto atual.")
        job_id = uuid.uuid4().hex
        ACTIVE.add(job_id)
    state = {
        "id": job_id, "status": "uploading" if uploading else "processing",
        "progress": 2, "message": message, "clips": [], "bundle_url": None,
        "created_at": int(time.time()),
    }
    try:
        save(job_id, state)
    except Exception:
        with LOCK:
            ACTIVE.discard(job_id)
        raise
    return job_id


def do_job(job_id: str, settings: JobSettings, uploaded_source: Path) -> None:
    directory = ROOT / job_id
    state = snapshot(job_id)

    def progress(pct: int, message: str):
        state.update({"status": "processing", "progress": pct, "message": message})
        save(job_id, state)

    try:
        progress(25, "Analisando o arquivo de vídeo")
        duration = inspect_duration(uploaded_source)
        segments = transcribe(uploaded_source, progress)
        progress(47, "Identificando os melhores momentos a partir da transcrição")
        clips = select_clips(segments, settings.count, settings.min_seconds, settings.max_seconds)
        clips = [{**c, "end": min(duration, c["end"])} for c in clips if c["start"] < duration]
        if not clips:
            raise ValueError("Não foram encontrados trechos de fala com duração adequada. Experimente reduzir a duração mínima.")
        exported: list[str] = []
        for i, clip in enumerate(clips, 1):
            progress(50 + int((i - 1) / len(clips) * 42), f"Criando o corte {i} de {len(clips)}")
            srt_path = directory / f"corte_{i:02d}.srt"
            srt = srt_path if settings.subtitles and write_srt(segments, clip["start"], clip["end"], srt_path) else None
            formats: dict[str, str] = {}
            for fmt in ("vertical", "horizontal"):
                filename = f"corte_{i:02d}_{fmt}.mp4"
                render_clip(uploaded_source, clip["start"], clip["end"], directory / filename, fmt, srt)
                formats[fmt] = f"/api/jobs/{job_id}/files/{filename}"
                exported.append(filename)
            metadata = ai_metadata(clip["text"], i)
            clip_record = {
                "id": i, "start": clip["start"], "end": clip["end"],
                "duration": round(clip["end"] - clip["start"], 1),
                **metadata, "transcript": clip["text"][:10000], "files": formats,
            }
            description = metadata.get("description") or metadata.get("summary", "")
            caption = metadata.get("caption", description)
            publication = (
                f"CORTE {i:02d}\n\nTÍTULO\n{metadata['title']}\n\n"
                f"DESCRIÇÃO\n{description}\n\nLEGENDA PARA PUBLICAÇÃO\n{caption}\n\n"
                f"HASHTAGS\n{' '.join(metadata['hashtags'])}\n"
            )
            publication_filename = f"publicacao_corte_{i:02d}.txt"
            (directory / publication_filename).write_text(publication, encoding="utf-8")
            exported.append(publication_filename)
            state["clips"].append(clip_record)
            save(job_id, state)
        progress(95, "Preparando os arquivos para download")
        bundle_name = "DarkCut_cortes_e_publicacoes.zip"
        with zipfile.ZipFile(directory / bundle_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for filename in exported:
                archive.write(directory / filename, arcname=filename)
            archive.writestr("dados_dos_cortes.json", json.dumps(state["clips"], indent=2, ensure_ascii=False))
        state.update({
            "status": "completed", "progress": 100,
            "message": "Cortes e textos prontos para download",
            "bundle_url": f"/api/jobs/{job_id}/files/{bundle_name}",
        })
        save(job_id, state)
    except Exception as exc:
        logger.exception("Video processing failed for job %s", job_id)
        state.update({"status": "failed", "progress": 0, "message": str(exc)[:650]})
        save(job_id, state)
    finally:
        uploaded_source.unlink(missing_ok=True)
        with LOCK:
            ACTIVE.discard(job_id)


def _upload_state(job_id: str, token: str) -> dict:
    if not _job_id_valid(job_id):
        raise HTTPException(404, "Upload não encontrado.")
    with LOCK:
        data = UPLOADS.get(job_id)
    if not data:
        raise HTTPException(404, "Upload expirado ou não encontrado. Reenvie o arquivo.")
    if not token or not hmac.compare_digest(data["token"], token):
        raise HTTPException(403, "Identificação de upload inválida.")
    return data


@app.get("/api/health")
def health():
    return {
        "ok": True, "version": "3.0.0", "upload_only": True, "transcription_ready": transcription_backend_ready(),
        "ai_metadata_enabled": bool(os.getenv("OPENAI_API_KEY")),
        "transcription_model": os.getenv("WHISPER_MODEL", "tiny"),
        "max_upload_bytes": MAX_UPLOAD_BYTES, "chunk_bytes": CHUNK_BYTES,
    }


@app.post("/api/uploads", status_code=201)
def begin_upload(payload: UploadStart):
    _validated_settings(payload)
    if not payload.filename.lower().endswith(".mp4"):
        raise HTTPException(415, "Escolha um arquivo MP4 válido.")
    if payload.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // 1048576} MB.")
    job_id = reserve_job("Aguardando o envio do vídeo", uploading=True)
    token = secrets.token_urlsafe(32)
    with LOCK:
        UPLOADS[job_id] = {
            "token": token, "total": payload.size, "received": 0, "index": 0,
            "settings": JobSettings(**payload.model_dump(exclude={"filename", "size"})),
            "lock": asyncio.Lock(), "touched": time.time(),
        }
    return {"id": job_id, "upload_token": token, "chunk_bytes": CHUNK_BYTES, "max_upload_bytes": MAX_UPLOAD_BYTES}


@app.put("/api/uploads/{job_id}/chunks/{index}")
async def put_chunk(job_id: str, index: int, request: Request):
    token = request.headers.get("X-Upload-Token", "")
    data = _upload_state(job_id, token)
    async with data["lock"]:
        if index != data["index"]:
            raise HTTPException(409, "Trecho fora de ordem. Atualize o envio e tente novamente.")
        expected = min(CHUNK_BYTES, data["total"] - data["received"])
        if expected <= 0:
            raise HTTPException(409, "O vídeo já foi recebido integralmente.")
        target = ROOT / job_id / "video.uploading"
        start_offset = data["received"]
        length = 0
        try:
            with target.open("ab") as stream:
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > expected:
                        raise HTTPException(413, "Parte de vídeo maior do que o tamanho permitido.")
                    stream.write(chunk)
                if length != expected:
                    raise HTTPException(422, "A parte enviada está incompleta. Tente reenviar esta parte.")
        except Exception:
            if target.exists():
                with target.open("r+b") as stream:
                    stream.truncate(start_offset)
            raise
        data["index"] += 1
        data["received"] += length
        data["touched"] = time.time()
        state = snapshot(job_id)
        state.update({
            "progress": 2 + int(20 * data["received"] / data["total"]),
            "message": f"Recebendo vídeo — {data['received'] * 100 // data['total']}%",
            "uploaded_bytes": data["received"],
        })
        save(job_id, state)
        return {"received": data["received"], "total": data["total"], "next_index": data["index"]}


@app.post("/api/uploads/{job_id}/finish", status_code=202)
async def finish_upload(job_id: str, request: Request):
    data = _upload_state(job_id, request.headers.get("X-Upload-Token", ""))
    async with data["lock"]:
        if data["received"] != data["total"]:
            raise HTTPException(409, "O vídeo ainda não foi enviado por completo.")
        directory = ROOT / job_id
        source = directory / "video.uploading"
        if not source.is_file() or source.stat().st_size != data["total"]:
            raise HTTPException(422, "Tamanho de vídeo inconsistente; reenvie o arquivo.")
        with source.open("rb") as stream:
            header = stream.read(12)
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise HTTPException(415, "O arquivo enviado não possui estrutura MP4 válida.")
        destination = directory / "original.mp4"
        source.replace(destination)
        state = snapshot(job_id)
        state.update({"status": "processing", "progress": 24, "message": "Upload concluído. Preparando análise de áudio."})
        save(job_id, state)
        with LOCK:
            UPLOADS.pop(job_id, None)
        POOL.submit(do_job, job_id, data["settings"], destination)
        return snapshot(job_id)


@app.post("/api/uploads/{job_id}/cancel")
async def cancel_upload(job_id: str, request: Request):
    data = _upload_state(job_id, request.headers.get("X-Upload-Token", ""))
    async with data["lock"]:
        with LOCK:
            UPLOADS.pop(job_id, None)
            ACTIVE.discard(job_id)
        shutil.rmtree(ROOT / job_id, ignore_errors=True)
    return {"cancelled": True}


@app.post("/api/upload-jobs", status_code=202)
async def legacy_mp4_upload(
    file: UploadFile = File(...),
    count: int = Form(default=3, ge=1, le=8),
    min_seconds: int = Form(default=25, ge=10, le=90),
    max_seconds: int = Form(default=60, ge=15, le=120),
    subtitles: bool = Form(default=True),
):
    """Backward compatibility for sites still using the v2.4 multipart uploader."""
    settings = JobSettings(count=count, min_seconds=min_seconds, max_seconds=max_seconds, subtitles=subtitles)
    _validated_settings(settings)
    if not (file.filename or "").lower().endswith(".mp4"):
        raise HTTPException(415, "Envie um vídeo MP4.")
    if file.content_type not in ("video/mp4", "application/octet-stream", "application/mp4", None):
        raise HTTPException(415, "Formato de arquivo não identificado como MP4.")
    job_id = reserve_job("Recebendo arquivo MP4")
    target = ROOT / job_id / "original.mp4"
    try:
        total = 0
        with target.open("xb") as output:
            while True:
                chunk = await file.read(1048576)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Arquivo maior que {MAX_UPLOAD_BYTES // 1048576} MB.")
                output.write(chunk)
        with target.open("rb") as stream:
            signature = stream.read(12)
        if len(signature) < 12 or signature[4:8] != b"ftyp":
            raise HTTPException(415, "O vídeo não é um arquivo MP4 válido.")
        POOL.submit(do_job, job_id, settings, target)
        return snapshot(job_id)
    except Exception:
        with LOCK:
            ACTIVE.discard(job_id)
        shutil.rmtree(ROOT / job_id, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.post("/api/jobs")
def unsupported_url_job():
    raise HTTPException(410, "O DarkCut AI agora recebe o arquivo MP4 completo. Envie o vídeo pelo aplicativo.")


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str):
    return snapshot(job_id)


@app.get("/api/jobs/{job_id}/files/{filename}")
def get_output(job_id: str, filename: str):
    allowed_file = (
        (filename.startswith("corte_") and filename.endswith(".mp4"))
        or (filename.startswith("publicacao_corte_") and filename.endswith(".txt"))
        or filename == "DarkCut_cortes_e_publicacoes.zip"
    )
    if not _job_id_valid(job_id) or not allowed_file or "/" in filename or ".." in filename:
        raise HTTPException(404, "Arquivo inválido.")
    job = snapshot(job_id)
    if job["status"] not in {"processing", "completed"}:
        raise HTTPException(404, "Arquivo indisponível.")
    path = ROOT / job_id / filename
    if not path.is_file():
        raise HTTPException(404, "Arquivo indisponível.")
    media_type = "video/mp4" if filename.endswith(".mp4") else "application/zip" if filename.endswith(".zip") else "text/plain"
    return FileResponse(path, media_type=media_type, filename=filename, headers={"Cache-Control": "private, no-store"})