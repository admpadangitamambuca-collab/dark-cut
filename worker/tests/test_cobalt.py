"""Offline tests for the Cobalt acquisition adapter."""
import io
import socket
from pathlib import Path

import pytest

from app import cobalt


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


def test_requires_authorized_instance(monkeypatch, tmp_path):
    monkeypatch.delenv("COBALT_API_URL", raising=False)
    with pytest.raises(ValueError, match="COBALT_API_URL"):
        cobalt.download_source("https://www.youtube.com/watch?v=JM1gEnmaUao", tmp_path)


def test_cobalt_tunnel_download(monkeypatch, tmp_path):
    monkeypatch.setenv("COBALT_API_URL", "https://cobalt.owned.example")
    monkeypatch.setenv("COBALT_API_KEY", "private-test-key")
    monkeypatch.setattr(cobalt.socket, "getaddrinfo", public_dns)
    captured = {}

    def fake_request(endpoint, video, key):
        captured.update(endpoint=endpoint, video=video, key=key)
        return {"status": "tunnel", "url": "https://media.owned.example/file.mp4"}

    def fake_download(url, folder):
        captured["file_url"] = url
        target = folder / "original.mp4"
        target.write_bytes(b"synthetic-video")
        return target

    monkeypatch.setattr(cobalt, "_cobalt_request", fake_request)
    monkeypatch.setattr(cobalt, "_download_stream", fake_download)
    p = cobalt.download_source("https://www.youtube.com/watch?v=JM1gEnmaUao", tmp_path)
    assert p.read_bytes() == b"synthetic-video"
    assert captured == {
        "endpoint": "https://cobalt.owned.example/",
        "video": "https://www.youtube.com/watch?v=JM1gEnmaUao",
        "key": "private-test-key",
        "file_url": "https://media.owned.example/file.mp4",
    }


def test_cobalt_rejects_error(monkeypatch, tmp_path):
    monkeypatch.setenv("COBALT_API_URL", "https://cobalt.owned.example")
    monkeypatch.setattr(cobalt.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(cobalt, "_cobalt_request", lambda *args: {"status": "error", "error": {"code": "api.youtube.login"}})
    with pytest.raises(ValueError, match="api.youtube.login"):
        cobalt.download_source("https://youtu.be/abc123", tmp_path)


@pytest.mark.parametrize("status", ["picker", "local-processing"])
def test_cobalt_rejects_unhandled_status(monkeypatch, tmp_path, status):
    monkeypatch.setenv("COBALT_API_URL", "https://cobalt.owned.example")
    monkeypatch.setattr(cobalt.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(cobalt, "_cobalt_request", lambda *args: {"status": status})
    with pytest.raises(ValueError, match="processamento local ou seleção manual"):
        cobalt.download_source("https://youtu.be/abc123", tmp_path)


def test_rejects_private_media_url(monkeypatch):
    monkeypatch.setattr(cobalt.socket, "getaddrinfo", lambda *args, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    ])
    with pytest.raises(ValueError, match="rede privada"):
        cobalt._safe_https_url("https://internal.example/file.mp4")


def test_direct_mp4_does_not_need_cobalt(monkeypatch, tmp_path):
    monkeypatch.delenv("COBALT_API_URL", raising=False)
    monkeypatch.setattr(cobalt, "_download_stream", lambda url, folder: folder / "original.mp4")
    result = cobalt.download_source("https://videos.example/authorized.mp4", tmp_path)
    assert result.name == "original.mp4"


def test_does_not_allow_embedded_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("COBALT_API_URL", "https://secret@cobalt.example/")
    with pytest.raises(ValueError, match="COBALT_API_URL"):
        cobalt.download_source("https://youtu.be/abc123", tmp_path)


def test_rejects_non_https(monkeypatch):
    with pytest.raises(ValueError, match="HTTPS"):
        cobalt._safe_https_url("http://public.example/media.mp4")


class FakeMediaResponse:
    def __init__(self, data):
        self.stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}
    def read(self, size=-1):
        return self.stream.read(size)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.stream.close()


def test_stream_over_max_size_cleans_partial(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_SOURCE_BYTES", "3")
    monkeypatch.setattr(cobalt, "_open_public_media", lambda url: FakeMediaResponse(b"video"))
    with pytest.raises(ValueError, match="tamanho máximo"):
        cobalt._download_stream("https://media.example/video.mp4", tmp_path)
    assert not (tmp_path / "original.mp4").exists()