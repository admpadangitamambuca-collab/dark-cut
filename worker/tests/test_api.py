import time
import zipfile

from fastapi.testclient import TestClient
from app import main


def test_health_and_bad_url():
    client = TestClient(main.app)
    assert client.get('/api/health').json()['ok'] is True
    bad = client.post('/api/jobs', json={'url':'http://127.0.0.1/segredo.mp4'})
    assert bad.status_code == 422


def test_job_end_to_end_with_stubbed_download_and_transcript(tmp_path, monkeypatch):
    # Real HTTP job flow, results, MP4 paths, and ZIP content; network/FFmpeg mocked.
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(main, 'download_source', lambda url, folder, progress: (folder / 'original.mp4'))
    monkeypatch.setattr(main, 'inspect_duration', lambda _: 50)
    monkeypatch.setattr(main, 'transcribe', lambda source, progress: [
        {'start': i*5, 'end': i*5 + 4, 'text': 'Uma informação importante e uma dica interessante.'}
        for i in range(10)
    ])
    monkeypatch.setattr(main, 'render_clip', lambda source, start, end, destination, style, srt: destination.write_bytes(b'mp4'))
    monkeypatch.setattr(main, 'ai_metadata', lambda text, index: {
        'title':f'Título {index}', 'summary': 'Resumo realista', 'hashtags':['#Teste','#Video'], 'metadata_source':'ia'
    })
    client = TestClient(main.app)
    response = client.post('/api/jobs', json={
        'url':'https://youtu.be/abcd1234', 'count':2,'min_seconds':15,'max_seconds':25,'subtitles':True
    })
    assert response.status_code == 202, response.text
    jid = response.json()['id']
    end = time.monotonic() + 5
    while time.monotonic() < end:
        job = client.get(f'/api/jobs/{jid}').json()
        if job['status'] != 'processing':
            break
        time.sleep(0.02)
    assert job['status'] == 'completed', job
    assert job['clips']
    clip = job['clips'][0]
    assert clip['title'] == 'Título 1'
    assert client.get(clip['files']['vertical']).content == b'mp4'
    assert client.get(clip['files']['horizontal']).content == b'mp4'
    bundle = tmp_path / jid / 'todos_os_cortes.zip'
    with zipfile.ZipFile(bundle) as z:
        assert any(f.endswith('horizontal.mp4') for f in z.namelist())
        assert 'titulos_hashtags_resumos.json' in z.namelist()


def test_upload_mp4_generates_clips_without_downloader(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(main, 'download_source', lambda *args: (_ for _ in ()).throw(AssertionError('download should not run')))
    monkeypatch.setattr(main, 'inspect_duration', lambda _: 60)
    monkeypatch.setattr(main, 'transcribe', lambda source, progress: [
        {'start': i*5, 'end': i*5+4.5, 'text': 'Uma dica importante para o conteúdo.'}
        for i in range(12)
    ])
    monkeypatch.setattr(main, 'render_clip', lambda source, start, end, dest, style, srt: dest.write_bytes(b'mp4'))
    monkeypatch.setattr(main, 'ai_metadata', lambda text, index: {
        'title': f'Vídeo {index}', 'summary': 'Resumo', 'hashtags': ['#Dica'], 'metadata_source':'extrativo'
    })
    client = TestClient(main.app)
    media = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 1024
    response = client.post('/api/upload-jobs',
        files={'file':('meu_video.mp4', media,'video/mp4')},
        data={'count':'1','min_seconds':'15','max_seconds':'35','subtitles':'false'})
    assert response.status_code == 202, response.text
    jid = response.json()['id']
    end = time.monotonic() + 5
    while time.monotonic() < end:
        job = client.get('/api/jobs/' + jid).json()
        if job['status'] != 'processing': break
        time.sleep(0.02)
    assert job['status'] == 'completed', job
    assert client.get(job['clips'][0]['files']['vertical']).content == b'mp4'
    assert client.get(job['clips'][0]['files']['horizontal']).content == b'mp4'


def test_upload_rejects_non_mp4_and_bad_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    client = TestClient(main.app)
    bad_extension = client.post('/api/upload-jobs',files={'file':('video.exe',b'data','application/octet-stream')})
    assert bad_extension.status_code == 415
    bad_signature = client.post('/api/upload-jobs',files={'file':('video.mp4',b'not really mp4','video/mp4')})
    assert bad_signature.status_code == 415
    assert not main.ACTIVE


def test_upload_rejects_excessive_size(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(main, 'MAX_UPLOAD_BYTES', 16)
    client = TestClient(main.app)
    data = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 20
    response = client.post('/api/upload-jobs', files={'file':('x.mp4',data,'video/mp4')})
    assert response.status_code == 413
    assert not main.ACTIVE