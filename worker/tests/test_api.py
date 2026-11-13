"""DarkCut file-only API: signed chunk upload, export and metadata bundle tests."""
from __future__ import annotations

import json
import time
import zipfile

from fastapi.testclient import TestClient
import pytest

from app import main


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(main, 'CHUNK_BYTES', 16)
    monkeypatch.setattr(main, 'MAX_UPLOAD_BYTES', 1024)
    monkeypatch.setattr(main, 'inspect_duration', lambda _: 60)
    monkeypatch.setattr(main, 'transcribe', lambda source, progress: [
        {'start': i*5, 'end': i*5+4.5, 'text': 'Um momento importante sobre o vídeo.'}
        for i in range(12)
    ])
    monkeypatch.setattr(main, 'render_clip', lambda source, start, end, dest, style, srt: dest.write_bytes(b'generated mp4'))
    monkeypatch.setattr(main, 'ai_metadata', lambda text, index: {
        'title': f'Título {index}', 'description': 'Descrição específica do corte',
        'summary': 'Descrição específica do corte', 'caption': 'Legenda para postar o corte',
        'hashtags': ['#Teste', '#Video'], 'metadata_source': 'extrativo',
    })
    main.ACTIVE.clear()
    main.UPLOADS.clear()
    yield TestClient(main.app)
    main.ACTIVE.clear()
    main.UPLOADS.clear()


def mp4data(n=76):
    return b'\x00\x00\x00\x18ftypisom' + b'\x00' * (n-12)


def wait_job(client, jid):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        response = client.get(f'/api/jobs/{jid}')
        assert response.status_code == 200
        job = response.json()
        if job['status'] != 'processing':
            return job
        time.sleep(0.02)
    raise AssertionError('Background job timeout')


def start(client, size=76, **kwargs):
    payload={'filename':'video_completo.mp4','size':size,'count':1,'min_seconds':15,'max_seconds':35,'subtitles':False}
    payload.update(kwargs)
    return client.post('/api/uploads',json=payload)


def send_chunks(client, upload, data):
    jid = upload['id']; token=upload['upload_token']; chunk=upload['chunk_bytes']
    headers={'X-Upload-Token':token,'Content-Type':'application/octet-stream'}
    for idx in range(0,len(data),chunk):
        response=client.put(f'/api/uploads/{jid}/chunks/{idx//chunk}', content=data[idx:idx+chunk],headers=headers)
        assert response.status_code==200, response.text
    return headers


def test_health_only_uploads_and_legacy_link_gone(prepared):
    response=prepared.get('/api/health')
    assert response.json()['version']=='3.1.0'
    assert response.json()['upload_only'] is True
    assert response.json()['max_upload_bytes']==1024
    assert prepared.post('/api/jobs',json={'url':'https://youtu.be/abcd1234'}).status_code==410


def test_chunk_upload_end_to_end_and_publication_bundle(prepared, tmp_path):
    data=mp4data()
    response=start(prepared,len(data))
    assert response.status_code==201,response.text
    upload=response.json()
    headers=send_chunks(prepared,upload,data)
    jid=upload['id']
    done=prepared.post(f'/api/uploads/{jid}/finish',headers=headers)
    assert done.status_code==202,done.text
    job=wait_job(prepared,jid)
    assert job['status']=='completed',job
    clip=job['clips'][0]
    assert clip['title']=='Título 1'
    assert clip['description']=='Descrição específica do corte'
    assert clip['caption']=='Legenda para postar o corte'
    assert clip['hashtags']==['#Teste','#Video']
    assert prepared.get(clip['files']['vertical']).content==b'generated mp4'
    assert prepared.get(clip['files']['horizontal']).content==b'generated mp4'
    bundle=prepared.get(job['bundle_url'])
    assert bundle.status_code==200
    archive=tmp_path/jid/'DarkCut_cortes_e_publicacoes.zip'
    assert archive.exists()
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        names=z.namelist()
        assert 'corte_01_vertical.mp4' in names
        assert 'corte_01_horizontal.mp4' in names
        assert 'publicacao_corte_01.txt' in names
        pub=z.read('publicacao_corte_01.txt').decode()
        assert 'LEGENDA PARA PUBLICAÇÃO' in pub and 'HASHTAGS' in pub
        meta=json.loads(z.read('dados_dos_cortes.json'))
        assert meta[0]['caption']=='Legenda para postar o corte'
    assert not (tmp_path/jid/'original.mp4').exists()


def test_rejects_bad_extension_and_size(prepared):
    assert start(prepared, 76,filename='video.mov').status_code==415
    assert start(prepared, 1025).status_code==413
    assert start(prepared, 76,min_seconds=90,max_seconds=15).status_code==422


def test_chunk_auth_duplicate_and_short_chunk(prepared):
    upload=start(prepared).json(); jid=upload['id']
    data=mp4data()
    url=f'/api/uploads/{jid}/chunks/0'
    assert prepared.put(url,content=data[:16],headers={'X-Upload-Token':'wrong'}).status_code==403
    good={'X-Upload-Token':upload['upload_token'],'Content-Type':'application/octet-stream'}
    assert prepared.put(url,content=data[:10],headers=good).status_code==422
    assert prepared.put(url,content=data[:16],headers=good).status_code==200
    assert prepared.put(url,content=data[:16],headers=good).status_code==409
    assert prepared.post(f'/api/uploads/{jid}/finish',headers=good).status_code==409
    assert prepared.post(f'/api/uploads/{jid}/cancel',headers=good).status_code==200
    assert not main.ACTIVE


def test_corrupt_mp4_signature_is_rejected(prepared):
    data=b'not a mp4!!'+b'0'*64
    up=start(prepared,len(data)).json()
    headers=send_chunks(prepared,up,data)
    assert prepared.post(f"/api/uploads/{up['id']}/finish",headers=headers).status_code==415
    assert prepared.post(f"/api/uploads/{up['id']}/cancel",headers=headers).status_code==200


def test_busy_upload_is_released_by_cancel(prepared):
    upload=start(prepared).json()
    assert start(prepared).status_code==429
    headers={'X-Upload-Token':upload['upload_token']}
    assert prepared.post(f"/api/uploads/{upload['id']}/cancel",headers=headers).status_code==200
    assert start(prepared).status_code==201


def test_legacy_mp4_upload_remains_compatible(prepared):
    result=prepared.post('/api/upload-jobs', files={'file':('video.mp4',mp4data(),'video/mp4')},
                         data={'count':'1','min_seconds':'15','max_seconds':'35','subtitles':'false'})
    assert result.status_code==202,result.text
    assert wait_job(prepared,result.json()['id'])['status']=='completed'


def test_legacy_mp4_rejects_malformed(prepared):
    resp=prepared.post('/api/upload-jobs', files={'file':('bad.mp4',b'not mp4','video/mp4')})
    assert resp.status_code==415
    assert not main.ACTIVE