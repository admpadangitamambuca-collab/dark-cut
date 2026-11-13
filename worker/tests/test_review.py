"""Real HTTP review -> approve -> render workflow with heavy media operations stubbed."""
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from app import main, storage


def mp4data():
    return b'\x00\x00\x00\x18ftypisom' + b'\x00' * 52


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(main, 'CHUNK_BYTES', 16)
    monkeypatch.setattr(main, 'MAX_UPLOAD_BYTES', 1024)
    monkeypatch.setattr(storage, 'enabled', lambda: False)
    monkeypatch.setattr(main, 'inspect_duration', lambda _: 310)
    scripts = [
        'Você sabia que esse é um erro importante?',
        'Vou explicar como funciona esse processo e a solução.',
        'Mas existe uma diferença muito grande na prática.',
        'Primeiro precisamos entender a questão inicial.',
        'Portanto, a conclusão é essa. Confira o resultado.',
        'Agora mostramos um exemplo específico do assunto.',
    ]
    monkeypatch.setattr(main, 'transcribe', lambda source, progress: [
        {'start':i*5., 'end':i*5.+4.7, 'text':scripts[i%len(scripts)]}
        for i in range(62)
    ])
    monkeypatch.setattr(main, 'ai_metadata', lambda text, idx: {
        'title':f'Corte inteligente {idx}', 'description':'Descrição objetiva',
        'caption':'Legenda preparada', 'summary':'Descrição objetiva',
        'hashtags':['#Cortes','#Destaques'], 'metadata_source':'extrativo',
    })
    monkeypatch.setattr(main, 'render_clip', lambda src,a,b,d,style,srt: d.write_bytes(b'valid_stub_mp4'))
    main.ACTIVE.clear()
    main.UPLOADS.clear()
    yield TestClient(main.app), tmp_path
    main.ACTIVE.clear()
    main.UPLOADS.clear()


def wait(client, job_id, statuses=('review','completed','failed')):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        r = client.get(f'/api/jobs/{job_id}')
        assert r.status_code == 200, r.text
        job = r.json()
        if job['status'] in statuses:
            return job
        time.sleep(.02)
    raise AssertionError('Job did not enter desired status')


def ready(client):
    file = mp4data()
    r = client.post('/api/uploads', json={
        'filename':'exemplo.mp4', 'size':len(file), 'review_before_render': True,
        'shorts_count':2, 'traditional_count':1, 'shorts_min_seconds':20,
        'shorts_max_seconds':40, 'traditional_min_seconds':60,
        'traditional_max_seconds':150, 'subtitles':False,
    })
    assert r.status_code == 201, r.text
    obj = r.json()
    head = {'X-Upload-Token':obj['upload_token']}
    for i in range(0, len(file), 16):
        r = client.put(f"/api/uploads/{obj['id']}/chunks/{i // 16}",content=file[i:i+16],headers=head)
        assert r.status_code == 200, r.text
    assert client.post(f"/api/uploads/{obj['id']}/finish",headers=head).status_code == 202
    job = wait(client, obj['id'])
    assert job['status'] == 'review', job
    return job


def test_review_renders_only_selected_and_preserves_edit(client):
    c,root = client
    job = ready(c)
    assert not main.ACTIVE
    assert len(job['suggestions']) >= 2
    modes = {s['profile'] for s in job['suggestions']}
    assert modes == {'shorts','traditional'}
    assert (root/job['id']/'original.mp4').exists()
    selected = next(s for s in job['suggestions'] if s['profile']=='shorts')
    choice = {'id':selected['id'], 'start':selected['start'], 'end':selected['end'], 'title':'Meu título editado'}
    resp = c.post(f"/api/jobs/{job['id']}/render",json={'clips':[choice]})
    assert resp.status_code == 202,resp.text
    final = wait(c, job['id'], ('completed','failed'))
    assert final['status'] == 'completed', final
    assert len(final['clips']) == 1
    assert final['clips'][0]['title'] == 'Meu título editado'
    assert final['clips'][0]['profile'] == 'shorts'
    assert list(final['clips'][0]['files']) == ['vertical']
    assert c.get(final['clips'][0]['files']['vertical']).content == b'valid_stub_mp4'
    with zipfile.ZipFile(root/job['id']/'DarkCut_cortes_e_publicacoes.zip') as z:
        assert 'corte_01_vertical.mp4' in z.namelist()
        assert 'corte_01_horizontal.mp4' not in z.namelist()
        assert 'Meu título editado' in z.read('publicacao_corte_01.txt').decode()
        assert json.loads(z.read('dados_dos_cortes.json'))[0]['profile'] == 'shorts'
    assert c.post(f"/api/jobs/{job['id']}/render",json={'clips':[choice]}).status_code == 409


def test_rejects_unapproved_ids_and_bad_boundaries(client):
    c,_ = client
    job = ready(c)
    item = job['suggestions'][0]
    original = {'id':item['id'], 'start':item['start'], 'end':item['end'], 'title':'Título editado'}
    path = f"/api/jobs/{job['id']}/render"
    assert c.post(path,json={'clips':[{**original,'id':'S99'}]}).status_code == 422
    assert c.post(path,json={'clips':[{**original,'end':job['source_duration'] + 2}]}).status_code == 422
    assert c.post(path,json={'clips':[original,original]}).status_code == 422
    assert c.post(path,json={'clips':[]}).status_code == 422
    assert c.get(f"/api/jobs/{job['id']}").json()['status']=='review'


def test_review_short_video_skips_traditional(client, monkeypatch):
    c,_=client
    monkeypatch.setattr(main, 'inspect_duration', lambda _: 46)
    monkeypatch.setattr(main, 'transcribe', lambda source, progress: [
        {'start':i*5., 'end':i*5.+4.7, 'text':'O segredo desse resultado é entender o problema.'}
        for i in range(9)
    ])
    job=ready(c)
    assert all(s['profile']=='shorts' for s in job['suggestions'])
    assert 'cortes tradicionais' in job['omitted_profiles']


def test_traditional_generates_only_horizontal_file(client):
    c,root = client
    job=ready(c)
    item=next(x for x in job['suggestions'] if x['profile']=='traditional')
    choice={'id':item['id'],'start':item['start'],'end':item['end'],'title':'Explicação completa'}
    assert c.post(f"/api/jobs/{job['id']}/render",json={'clips':[choice]}).status_code == 202
    final=wait(c,job['id'],('completed','failed'))
    assert final['status']=='completed',final
    clip=final['clips'][0]
    assert list(clip['files']) == ['horizontal']
    assert c.get(clip['files']['horizontal']).content==b'valid_stub_mp4'
    with zipfile.ZipFile(root/job['id']/'DarkCut_cortes_e_publicacoes.zip') as z:
        assert 'corte_01_horizontal.mp4' in z.namelist()
        assert 'corte_01_vertical.mp4' not in z.namelist()


def test_retry_failed_render_without_r2(client, monkeypatch):
    c,root = client
    job=ready(c)
    item=next(x for x in job['suggestions'] if x['profile']=='shorts')
    choice={'id':item['id'],'start':item['start'],'end':item['end'],'title':'Título corrigido'}
    def broken(*args,**kwargs):
        raise RuntimeError('Falta de capacidade para renderizar')
    monkeypatch.setattr(main,'render_clip',broken)
    path=f"/api/jobs/{job['id']}/render"
    assert c.post(path,json={'clips':[choice]}).status_code==202
    failed=wait(c,job['id'],('failed',))
    assert failed['phase']=='render'
    assert (root/job['id']/'original.mp4').is_file()
    monkeypatch.setattr(main,'render_clip',lambda source,start,end,dest,style,srt: dest.write_bytes(b'recovered'))
    assert c.post(path,json={'clips':[choice]}).status_code==202
    final=wait(c,job['id'],('completed','failed'))
    assert final['status']=='completed',final