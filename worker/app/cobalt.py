"""Cobalt-only YouTube media acquisition for DarkCut AI.

Configure COBALT_API_URL with an owned or explicitly permitted Cobalt instance.
No public Cobalt API is used implicitly; no credentials enter browser responses.
Direct HTTPS media files remain supported without yt-dlp.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PUBLIC_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
}
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".m4v")
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _safe_https_url(raw: str) -> str:
    """Reject non-public hosts in Cobalt-provided media/tunnel URLs."""
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ValueError("A origem de mídia não é um endereço HTTPS público válido.")
    if parsed.port not in (None, 443):
        raise ValueError("A origem de mídia utiliza uma porta não permitida.")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("A origem de mídia não pode apontar para uma rede privada.")
    except socket.gaierror as exc:
        raise ValueError("Não foi possível resolver o endereço do arquivo de vídeo.") from exc
    return raw


def _open_public_media(url: str):
    for _ in range(MAX_REDIRECTS + 1):
        _safe_https_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "DarkCutAI/2.2"}, method="GET")
        try:
            return _OPENER.open(req, timeout=75)
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECT_CODES and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                exc.close()
                continue
            code = exc.code
            exc.close()
            raise ValueError(f"A origem do vídeo recusou o download (HTTP {code}).") from exc
    raise ValueError("A origem do vídeo fez redirecionamentos demais.")


def _download_stream(url: str, directory: Path) -> Path:
    """Stream to disk with size limits; do not follow redirects to private hosts."""
    maximum = int(os.getenv("MAX_SOURCE_BYTES", "1073741824"))
    target = directory / "original.mp4"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with _open_public_media(url) as response, target.open("wb") as output:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > maximum:
                raise ValueError("O vídeo excede o tamanho máximo permitido pelo motor.")
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ValueError("O vídeo excede o tamanho máximo permitido pelo motor.")
                output.write(chunk)
        if not target.stat().st_size:
            raise ValueError("O Cobalt não retornou dados de vídeo.")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _cobalt_request(endpoint: str, youtube_url: str, key: str) -> dict:
    payload = {
        "url": youtube_url,
        "downloadMode": "auto",
        "videoQuality": "720",
        "youtubeVideoCodec": "h264",
        "youtubeVideoContainer": "mp4",
        "localProcessing": "disabled",
        "alwaysProxy": True,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Api-Key " + key
    req = urllib.request.Request(endpoint, json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=60) as response:
            body = response.read(256 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (401, 403):
            raise ValueError("A instância Cobalt recusou a autorização de acesso (HTTP 401/403).") from exc
        if status == 429:
            raise ValueError("A instância Cobalt atingiu seu limite de solicitações. Tente novamente mais tarde.") from exc
        raise ValueError(f"A instância Cobalt retornou erro HTTP {status}.") from exc
    except urllib.error.URLError as exc:
        raise ValueError("Não foi possível estabelecer conexão com a instância Cobalt configurada.") from exc
    if len(body) > 256 * 1024:
        raise ValueError("A instância Cobalt retornou uma resposta muito grande.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("A instância Cobalt retornou uma resposta inválida.") from exc
    if not isinstance(data, dict):
        raise ValueError("A instância Cobalt retornou uma resposta inesperada.")
    return data


def download_source(url: str, directory: Path) -> Path:
    """Use Cobalt for YouTube, direct HTTPS streaming for uploaded/public video files."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in PUBLIC_YOUTUBE_HOSTS:
        if not parsed.path.lower().endswith(VIDEO_SUFFIXES):
            raise ValueError("O endereço não contém um arquivo de vídeo compatível.")
        return _download_stream(url, directory)

    instance = os.getenv("COBALT_API_URL", "").strip()
    if not instance:
        raise ValueError(
            "Motor Cobalt selecionado, mas falta configurar COBALT_API_URL no Render "
            "com uma instância própria ou autorizada. Não usamos APIs públicas de terceiros sem permissão."
        )
    parsed_instance = urllib.parse.urlsplit(instance)
    if (parsed_instance.scheme != "https" or not parsed_instance.hostname or
            parsed_instance.username or parsed_instance.password or parsed_instance.query or
            parsed_instance.fragment or parsed_instance.path not in ("", "/")):
        raise ValueError("COBALT_API_URL deve ser o endereço HTTPS raiz da instância Cobalt autorizada.")
    # This endpoint is configured by the service operator, never by the browser.
    _safe_https_url(instance)
    data = _cobalt_request(instance.rstrip("/") + "/", url, os.getenv("COBALT_API_KEY", "").strip())
    status = data.get("status")
    if status == "error":
        code = str((data.get("error") or {}).get("code", "desconhecido"))
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,120}", code):
            code = "inesperado"
        raise ValueError(f"A instância Cobalt não conseguiu obter este vídeo ({code}).")
    if status in ("local-processing", "picker"):
        raise ValueError(
            "A instância Cobalt solicitou processamento local ou seleção manual, "
            "que ainda não são suportados no DarkCut AI."
        )
    if status not in ("tunnel", "redirect") or not isinstance(data.get("url"), str):
        raise ValueError("A instância Cobalt não retornou um link de vídeo utilizável.")
    return _download_stream(data["url"], directory)