"""Offline persistence and recovery tests. No cloud credentials are used."""
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import types

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main, storage
from app import engine


def test_storage_not_active_without_keys(monkeypatch):
    for key in storage.REQUIRED:
        monkeypatch.delenv(key, raising=False)
    assert not storage.enabled()
    assert TestClient(main.app).get('/api/health').json()['persistent_storage_enabled'] is False


def test_storage_rejects_invalid_object_names():
    with pytest.raises(ValueError):
        storage._key('not-a-job', 'a.mp4')
    with pytest.raises(ValueError):
        storage._key('a' * 32, '../secret.txt')
    assert storage._key('a' * 32, 'corte_01_vertical.mp4').endswith('/corte_01_vertical.mp4')


def test_remote_job_survives_local_restart(monkeypatch, tmp_path):
    jid = 'b' * 32
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(storage, 'enabled', lambda: True)
    remote_state = {
        'id': jid, 'status': 'processing', 'progress': 30,
        'settings': {'count': 1, 'min_seconds': 20, 'max_seconds': 30, 'subtitles': False},
        'clips': [], 'bundle_url': None,
    }
    monkeypatch.setattr(storage, 'get_state', lambda ident: remote_state)
    assert main.snapshot(jid)['status'] == 'interrupted'
    assert main.snapshot(jid)['can_resume']


def test_remote_completed_result_survives_local_restart(monkeypatch, tmp_path):
    jid = 'c' * 32
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(storage, 'enabled', lambda: True)
    monkeypatch.setattr(storage, 'get_state', lambda ident: {
        'id': jid, 'status': 'completed', 'clips': [], 'bundle_url': f'/api/jobs/{jid}/files/DarkCut_cortes_e_publicacoes.zip'
    })
    monkeypatch.setattr(storage, 'signed_download', lambda ident, name: 'https://private.example/signed')
    resp = TestClient(main.app).get(f'/api/jobs/{jid}/files/DarkCut_cortes_e_publicacoes.zip', follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers['location'] == 'https://private.example/signed'


def test_transcribe_extracts_bounded_audio_windows(monkeypatch, tmp_path):
    class FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            pass
        def transcribe(self, audio, **kwargs):
            assert audio.dtype == np.float32
            assert len(audio) <= 45 * 16000 + 1000
            segment = SimpleNamespace(start=0.25, end=5.0, text=' Teste de fala compreensível. ')
            info = SimpleNamespace(language='pt')
            return iter([segment]), info
    monkeypatch.setitem(sys.modules, 'faster_whisper', types.SimpleNamespace(WhisperModel=FakeWhisperModel))
    monkeypatch.setenv('WHISPER_WINDOW_SECONDS', '45')
    monkeypatch.setattr(engine, 'inspect_duration', lambda _: 100.0)
    calls = []
    def fake_run(command, capture_output, timeout):
        calls.append(command)
        audio = np.full(45 * 16000, 0.3, dtype=np.float32).tobytes()
        return SimpleNamespace(returncode=0, stdout=audio, stderr=b'')
    monkeypatch.setattr(engine.subprocess, 'run', fake_run)
    progress = []
    output = engine.transcribe(tmp_path / 'original.mp4', lambda pct, msg: progress.append(pct))
    assert len(calls) == 3 and len(output) == 3
    assert [item['start'] for item in output] == [0.25, 45.25, 90.25]
    assert len(progress) == 3