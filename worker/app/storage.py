"""Optional private S3-compatible storage (Cloudflare R2) for durable job artifacts.

Storage is disabled unless all R2_* environment variables are present. Secrets
stay on the server. Files are kept until the bucket owner deletes them or adds
an explicit lifecycle policy; no expiry is configured here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
PREFIX = "darkcut/jobs/"


def enabled() -> bool:
    return all(os.getenv(key, "").strip() for key in REQUIRED)


def _client():
    if not enabled():
        raise RuntimeError("Armazenamento R2 ainda não configurado no servidor.")
    endpoint = os.environ["R2_ENDPOINT"].strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("R2_ENDPOINT deve ser HTTPS, sem caminho, usuário ou senha.")
    # Imported only when used, to avoid using memory on constrained workers.
    import boto3
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _key(job_id: str, filename: str) -> str:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("ID de projeto inválido.")
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError("Arquivo inválido.")
    return f"{PREFIX}{job_id}/{filename}"


def put_file(job_id: str, path: Path):
    if not enabled():
        return
    from boto3.s3.transfer import TransferConfig
    config = TransferConfig(
        multipart_threshold=16 * 1048576, multipart_chunksize=8 * 1048576,
        max_concurrency=1, use_threads=False,
    )
    _client().upload_file(str(path), os.environ["R2_BUCKET"], _key(job_id, path.name), Config=config)


def fetch_file(job_id: str, filename: str, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    _client().download_file(os.environ["R2_BUCKET"], _key(job_id, filename), str(destination))


def put_state(job_id: str, state: dict):
    if not enabled():
        return
    payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
    if len(payload) > 1024 * 1024:
        raise ValueError("Metadados excederam o limite de segurança.")
    _client().put_object(
        Bucket=os.environ["R2_BUCKET"], Key=_key(job_id, "status.json"),
        Body=payload, ContentType="application/json; charset=utf-8",
    )


def get_state(job_id: str) -> dict | None:
    if not enabled():
        return None
    try:
        obj = _client().get_object(Bucket=os.environ["R2_BUCKET"], Key=_key(job_id, "status.json"))
        raw = obj["Body"].read(1024 * 1024 + 1)
    except Exception as exc:
        # Not-found state means a previous job was not archived or does not exist;
        # other storage failures should surface in the worker's error logs.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    if len(raw) > 1024 * 1024:
        raise ValueError("Metadados remotos muito grandes.")
    return json.loads(raw)


def signed_download(job_id: str, filename: str) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["R2_BUCKET"],
            "Key": _key(job_id, filename),
            "ResponseContentDisposition": f'{"inline" if filename.endswith(".mp4") else "attachment"}; filename="{filename}"',
        },
        ExpiresIn=3600,
    )