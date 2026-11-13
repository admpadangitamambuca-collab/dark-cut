from app.engine import select_clips, extractive_metadata, srt_timestamp, write_srt, ai_metadata
import pytest


def test_metadata_contains_four_publish_fields(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    m=ai_metadata('Uma dica incrível para organizar seu tempo. Descubra como ganhar horas no dia.',1)
    assert m['title'].startswith('Uma dica')
    assert m['description'] and m['caption']
    assert m['summary']==m['description']
    assert m['metadata_source']=='extrativo'
    assert all(tag.startswith('#') for tag in m['hashtags'])
    assert 'Qual sua opinião' in m['caption']


def test_timecode():
    assert srt_timestamp(61.025)=='00:01:01,025'


def test_srt(tmp_path):
    path=tmp_path/'corte.srt'
    assert write_srt([{'start':10,'end':12,'text':'Olá'}],10.5,13,path)
    assert '00:00:00,000 --> 00:00:01,500' in path.read_text()


def test_selection_nonoverlap():
    seg=[{'start':i*5,'end':i*5+4.5,'text':'A verdade é que essa dica é importante!'} for i in range(40)]
    clips=select_clips(seg,3,20,35)
    assert 1<=len(clips)<=3
    assert all(x['end']<=y['start'] for x,y in zip(clips,clips[1:]))


def test_no_segments_no_clips():
    assert select_clips([],3,20,35)==[]


def test_transcription_import_failure_has_actionable_error(monkeypatch, tmp_path):
    import builtins
    from app import engine

    native_import = builtins.__import__

    def missing_openmp(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("libgomp.so.1: cannot open shared object file")
        return native_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_openmp)
    engine.transcription_backend_ready.cache_clear()
    assert engine.transcription_backend_ready() is False
    with pytest.raises(RuntimeError, match="não pôde ser carregado no servidor"):
        engine.transcribe(tmp_path / "entrada.mp4", lambda *args: None)
    engine.transcription_backend_ready.cache_clear()