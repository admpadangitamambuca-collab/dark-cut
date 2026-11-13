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
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .engine import ai_metadata, inspect_duration, render_clip, select_clips, transcribe, transcription_backend_ready, write_srt
from . import storage
from .precision import select_precision_clips

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

app = FastAPI(title="DarkCut AI — File Studio", version="3.2.0", docs_url=None, redoc_url=None)
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
    # Review mode is opt-in: sites still running v3.1 retain the existing flow.
    review_before_render: bool = False
    shorts_count: int = Field(default=3, ge=0, le=5)
    traditional_count: int = Field(default=1, ge=0, le=3)
    shorts_min_seconds: int = Field(default=20, ge=8, le=180)
    shorts_max_seconds: int = Field(default=60, ge=8, le=180)
    traditional_min_seconds: int = Field(default=120, ge=30, le=600)
    traditional_max_seconds: int = Field(default=300, ge=30, le=600)


class RenderChoice(BaseModel):
    id: str = Field(min_length=3, max_length=3, pattern=r"^[SN][0-9]{2}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(min_length=1, max_length=100)

    @field_validator("start", "end")
    @classmethod
    def finite_number(cls, value: float) -> float:
        import math
        if not math.isfinite(value):
            raise ValueError("O horário do corte deve ser um número finito.")
        return value


class RenderRequest(BaseModel):
    clips: list[RenderChoice] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_choices(self):
        ids = [item.id for item in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("Não é permitido repetir um corte na seleção.")
        return self


class UploadStart(JobSettings):
    filename: str = Field(min_length=5, max_length=255)
    size: int = Field(ge=12)


def _validated_settings(settings: JobSettings) -> None:
    if settings.min_seconds > settings.max_seconds:
        raise HTTPException(422, "A duração mínima não pode ultrapassar a máxima.")
    if settings.review_before_render:
        if settings.shorts_count + settings.traditional_count < 1:
            raise HTTPException(422, "Escolha pelo menos um formato de corte.")
        if settings.shorts_min_seconds > settings.shorts_max_seconds:
            raise HTTPException(422, "Duração inválida para Shorts.")
        if settings.traditional_min_seconds > settings.traditional_max_seconds:
            raise HTTPException(422, "Duração inválida para cortes tradicionais.")


def _job_id_valid(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def snapshot(job_id: str) -> dict:
    if not _job_id_valid(job_id):
        raise HTTPException(404, "Projeto inválido.")
    p = ROOT / job_id / "status.json"
    if not p.is_file():
        if storage.enabled():
            try:
                restored = storage.get_state(job_id)
            except Exception:
                logger.exception("Persistent status recovery failed for job %s", job_id)
                raise HTTPException(503, "Armazenamento indisponível. Tente novamente.")
            if restored:
                if restored.get("status") in ("processing", "uploading") and job_id not in ACTIVE:
                    restored = {
                        **restored, "status": "interrupted", "can_resume": bool(restored.get("settings")),
                        "message": "O servidor reiniciou. O vídeo foi preservado e pode ser reprocessado.",
                    }
                return restored
        raise HTTPException(404, "Projeto temporário perdido após reinicialização do servidor. "
                                  "Para evitar isso, configure armazenamento R2 no worker.")
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


def do_job(job_id: str, settings: JobSettings, uploaded_source: Path, backed_up: bool = False) -> None:
    directory = ROOT / job_id
    state = snapshot(job_id)

    def progress(pct: int, message: str):
        state.update({"status": "processing", "progress": pct, "message": message})
        save(job_id, state)

    try:
        if not uploaded_source.is_file():
            if not storage.enabled():
                raise RuntimeError("O vídeo não está disponível para continuar o processamento.")
            progress(24, "Restaurando vídeo do armazenamento persistente")
            storage.fetch_file(job_id, "original.mp4", uploaded_source)
        if storage.enabled() and not backed_up:
            progress(25, "Preservando vídeo no armazenamento externo")
            storage.put_file(job_id, uploaded_source)
            state["settings"] = settings.model_dump()
            storage.put_state(job_id, state)
        progress(26, "Analisando o arquivo de vídeo")
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
            if storage.enabled():
                for filename in (f"corte_{i:02d}_vertical.mp4", f"corte_{i:02d}_horizontal.mp4", publication_filename):
                    storage.put_file(job_id, directory / filename)
                storage.put_state(job_id, state)
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
        if storage.enabled():
            storage.put_file(job_id, directory / bundle_name)
            storage.put_state(job_id, state)
    except Exception as exc:
        logger.exception("Video processing failed for job %s", job_id)
        state.update({"status": "failed", "progress": 0, "message": str(exc)[:650]})
        save(job_id, state)
        if storage.enabled():
            try:
                storage.put_state(job_id, state)
            except Exception:
                logger.exception("Could not persist failed job state %s", job_id)
    finally:
        uploaded_source.unlink(missing_ok=True)
        with LOCK:
            ACTIVE.discard(job_id)



def _persist_state(job_id: str, state: dict) -> None:
    save(job_id, state)
    if storage.enabled():
        storage.put_state(job_id, state)


def _restore_original(job_id: str, path: Path) -> None:
    if not path.is_file():
        if not storage.enabled():
            raise RuntimeError("O vídeo original expirou. Reenvie o arquivo para gerar novos cortes.")
        storage.fetch_file(job_id, "original.mp4", path)


def do_precision_analysis(job_id: str, settings: JobSettings, source: Path, backed_up: bool = False) -> None:
    """Analyze once; pause for editorial review before any expensive video exports."""
    directory = ROOT / job_id
    state = snapshot(job_id)

    def progress(pct, message):
        state.update(status="processing", phase="analysis", progress=pct, message=message)
        save(job_id, state)

    try:
        _restore_original(job_id, source)
        state["settings"] = settings.model_dump()
        if storage.enabled() and not backed_up:
            progress(24, "Preservando vídeo no armazenamento persistente")
            storage.put_file(job_id, source)
            storage.put_state(job_id, state)
        progress(26, "Analisando duração e transcrevendo em partes")
        duration = inspect_duration(source)
        segments = transcribe(source, progress)
        transcript_path = directory / "segments.json"
        transcript_path.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        if storage.enabled():
            storage.put_file(job_id, transcript_path)
        progress(46, "Comparando ganchos, contexto e conclusões")
        plans = (
            ("shorts", settings.shorts_count, settings.shorts_min_seconds, settings.shorts_max_seconds),
            ("traditional", settings.traditional_count, settings.traditional_min_seconds, settings.traditional_max_seconds),
        )
        suggestions = []
        omitted = []
        for profile, count, minimum, maximum in plans:
            if not count:
                continue
            found = select_precision_clips(segments, profile, count, minimum, maximum, duration)
            if not found:
                omitted.append("Shorts" if profile == "shorts" else "cortes tradicionais")
            for item in found:
                meta = ai_metadata(item["transcript"], len(suggestions) + 1)
                item.update(meta)
                suggestions.append(item)
        if not suggestions:
            raise ValueError("Nenhum trecho adequado à duração solicitada foi encontrado. Escolha durações menores.")
        state.update(
            status="review", phase="review", progress=50,
            message="Seleção inteligente pronta. Revise e aprove antes de renderizar.",
            suggestions=suggestions, source_duration=duration, clips=[], bundle_url=None,
            omitted_profiles=omitted, settings=settings.model_dump(),
        )
        _persist_state(job_id, state)
    except Exception as exc:
        logger.exception("Precision analysis failed for job %s", job_id)
        state.update(status="failed", progress=0, phase="analysis", message=str(exc)[:550])
        try:
            _persist_state(job_id, state)
        except Exception:
            logger.exception("Could not persist failed analysis %s", job_id)
    finally:
        # The original must remain available while the user reviews proposed cuts.
        with LOCK:
            ACTIVE.discard(job_id)


def _validated_render_choices(job_id: str, state: dict, payload: RenderRequest) -> list[RenderChoice]:
    source_duration = float(state.get("source_duration", 0))
    by_id = {s["id"]: s for s in state.get("suggestions", [])}
    if not by_id:
        raise HTTPException(409, "Nenhuma sugestão disponível para revisão.")
    for choice in payload.clips:
        suggestion = by_id.get(choice.id)
        if not suggestion:
            raise HTTPException(422, f"Corte não reconhecido: {choice.id}")
        length = choice.end - choice.start
        mode = suggestion["profile"]
        cap = 180 if mode == "shorts" else 600
        minimum = 8 if mode == "shorts" else 30
        if (choice.end > source_duration + .01 or length < minimum or length > cap or
                choice.start >= choice.end or not choice.title.strip()):
            raise HTTPException(422, f"Intervalo inválido para o corte {choice.id}. "
                                     f"O formato permite de {minimum} a {cap} segundos.")
    return payload.clips


def do_precision_render(job_id: str, settings: JobSettings, source: Path,
                        choices: list[RenderChoice], backed_up: bool = False) -> None:
    """Render only approved cuts, saving individual artifacts before packaging."""
    directory = ROOT / job_id
    state = snapshot(job_id)

    def progress(pct, message):
        state.update(status="processing", phase="render", progress=pct, message=message)
        save(job_id, state)

    try:
        _restore_original(job_id, source)
        transcript_path = directory / "segments.json"
        if not transcript_path.exists():
            if not storage.enabled():
                raise RuntimeError("A transcrição expirou. Analise o vídeo novamente.")
            storage.fetch_file(job_id, "segments.json", transcript_path)
        segments = json.loads(transcript_path.read_text(encoding="utf-8"))
        original = {x["id"]: x for x in state["suggestions"]}
        state["clips"] = []
        state["bundle_url"] = None
        exported: list[str] = []
        for i, item in enumerate(choices, 1):
            proposal = original[item.id]
            mode = proposal["profile"]
            progress(52 + int(40 * (i - 1) / len(choices)), f"Renderizando corte aprovado {i}/{len(choices)}")
            # For adjusted ranges, derive the transcript and its captions from actual times.
            text = " ".join(str(seg["text"]) for seg in segments
                            if float(seg["start"]) < item.end and float(seg["end"]) > item.start)
            subtitle_file = directory / f"corte_{i:02d}.srt"
            srt = subtitle_file if settings.subtitles and write_srt(segments, item.start, item.end, subtitle_file) else None
            style = "vertical" if mode == "shorts" else "horizontal"
            filename = f"corte_{i:02d}_{style}.mp4"
            render_clip(source, item.start, item.end, directory / filename, style, srt)
            exported.append(filename)
            fields = {k: proposal[k] for k in ("description", "caption", "hashtags", "metadata_source")}
            fields.update(title=item.title.strip(), summary=proposal["description"])
            record = {
                "id": i, "suggestion_id": item.id, "profile": mode,
                "start": round(item.start, 2), "end": round(item.end, 2),
                "duration": round(item.end - item.start, 2), "scores": proposal["scores"],
                "transcript": text[:12000], **fields,
                "files": {style: f"/api/jobs/{job_id}/files/{filename}"},
            }
            publication = (
                f"CORTE {i:02d} | {'SHORTS' if mode == 'shorts' else 'TRADICIONAL'}\n\n"
                f"TÍTULO\n{record['title']}\n\nDESCRIÇÃO\n{record['description']}\n\n"
                f"LEGENDA PARA PUBLICAÇÃO\n{record['caption']}\n\n"
                f"HASHTAGS\n{' '.join(record['hashtags'])}\n"
            )
            txt = f"publicacao_corte_{i:02d}.txt"
            (directory / txt).write_text(publication, encoding="utf-8")
            exported.append(txt)
            state["clips"].append(record)
            save(job_id, state)
            if storage.enabled():
                storage.put_file(job_id, directory / filename)
                storage.put_file(job_id, directory / txt)
                storage.put_state(job_id, state)
        progress(95, "Compactando cortes e textos aprovados")
        bundle_name = "DarkCut_cortes_e_publicacoes.zip"
        with zipfile.ZipFile(directory / bundle_name, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=3) as archive:
            for name in exported:
                archive.write(directory / name, arcname=name)
            archive.writestr("dados_dos_cortes.json", json.dumps(state["clips"], ensure_ascii=False, indent=2))
        state.update(status="completed", phase="completed", progress=100,
                     message="Cortes aprovados prontos para download",
                     bundle_url=f"/api/jobs/{job_id}/files/{bundle_name}")
        if storage.enabled():
            storage.put_file(job_id, directory / bundle_name)
        _persist_state(job_id, state)
    except Exception as exc:
        logger.exception("Precision render failed for job %s", job_id)
        state.update(status="failed", progress=0, phase="render", message=str(exc)[:550])
        try:
            _persist_state(job_id, state)
        except Exception:
            logger.exception("Could not persist failed render %s", job_id)
    finally:
        # Preserve the source for an in-place retry if rendering fails without R2.
        if state.get("status") == "completed" or storage.enabled():
            source.unlink(missing_ok=True)
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
        "ok": True, "version": "3.2.0", "upload_only": True, "review_supported": True,
        "transcription_ready": transcription_backend_ready(),
        "ai_metadata_enabled": bool(os.getenv("OPENAI_API_KEY")),
        "transcription_model": os.getenv("WHISPER_MODEL", "tiny"),
        "max_upload_bytes": MAX_UPLOAD_BYTES, "chunk_bytes": CHUNK_BYTES,
        "persistent_storage_enabled": storage.enabled(),
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
        processor = do_precision_analysis if data["settings"].review_before_render else do_job
        POOL.submit(processor, job_id, data["settings"], destination)
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


@app.post("/api/jobs/{job_id}/render", status_code=202)
def approve_and_render(job_id: str, payload: RenderRequest):
    state = snapshot(job_id)
    if state.get("status") != "review" and not (
        state.get("status") == "failed" and state.get("phase") == "render" and state.get("suggestions")
    ):
        raise HTTPException(409, "Este projeto ainda não está pronto para revisão ou já foi renderizado.")
    choices = _validated_render_choices(job_id, state, payload)
    settings = JobSettings(**state["settings"])
    with LOCK:
        if job_id in ACTIVE or len(ACTIVE) >= MAX_JOBS:
            raise HTTPException(429, "O motor está ocupado. Tente novamente após o processamento atual.")
        ACTIVE.add(job_id)
    try:
        state.update(status="processing", phase="render", progress=51,
                     message="Renderização dos cortes aprovados iniciada",
                     render_choices=[x.model_dump() for x in choices])
        _persist_state(job_id, state)
        POOL.submit(do_precision_render, job_id, settings, ROOT / job_id / "original.mp4", choices)
    except Exception:
        with LOCK:
            ACTIVE.discard(job_id)
        raise
    return state


@app.post("/api/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str):
    if not _job_id_valid(job_id) or not storage.enabled():
        raise HTTPException(404, "Projeto não recuperável; armazenamento persistente não configurado.")
    state = snapshot(job_id)
    if state.get("status") not in ("interrupted", "failed") or not state.get("settings"):
        raise HTTPException(409, "Este projeto não precisa ou não pode ser retomado.")
    with LOCK:
        if job_id in ACTIVE or len(ACTIVE) >= MAX_JOBS:
            raise HTTPException(429, "O motor já está ocupado.")
        ACTIVE.add(job_id)
    try:
        settings = JobSettings(**state["settings"])
        # Only source from our private bucket. The async task restores it if needed.
        source = ROOT / job_id / "original.mp4"
        state.update(status="processing", progress=24, message="Recuperando vídeo arquivado")
        save(job_id, state)
        if settings.review_before_render:
            if state.get("phase") == "render" and state.get("render_choices"):
                choice_models = [RenderChoice(**c) for c in state["render_choices"]]
                POOL.submit(do_precision_render, job_id, settings, source, choice_models, True)
            else:
                POOL.submit(do_precision_analysis, job_id, settings, source, True)
        else:
            POOL.submit(do_job, job_id, settings, source, True)
    except Exception:
        with LOCK:
            ACTIVE.discard(job_id)
        raise
    return state


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
        if storage.enabled():
            try:
                return RedirectResponse(storage.signed_download(job_id, filename), status_code=307,
                                        headers={"Cache-Control": "private, no-store"})
            except Exception:
                logger.exception("Could not provide archived download for %s", job_id)
                raise HTTPException(503, "O armazenamento externo não respondeu.")
        raise HTTPException(404, "Arquivo temporário expirou. Configure armazenamento persistente para preservar downloads.")
    media_type = "video/mp4" if filename.endswith(".mp4") else "application/zip" if filename.endswith(".zip") else "text/plain"
    return FileResponse(path, media_type=media_type, filename=filename, headers={"Cache-Control": "private, no-store"})