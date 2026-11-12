from app.engine import select_clips, extractive_metadata, srt_timestamp, write_srt, validate_source_url, user_friendly_download_error
import pytest


def test_metadata():
    m = extractive_metadata("Uma dica incrível para organizar seu tempo. Descubra como ganhar horas no dia.", 1)
    assert m["title"].startswith("Uma dica")
    assert m["metadata_source"] == "extrativo"
    assert all(t.startswith("#") for t in m["hashtags"])


def test_timecode():
    assert srt_timestamp(61.025) == "00:01:01,025"


def test_srt(tmp_path):
    path = tmp_path / "corte.srt"
    assert write_srt([{"start": 10, "end": 12, "text": "Olá"}], 10.5, 13, path)
    assert "00:00:00,000 --> 00:00:01,500" in path.read_text()


def test_selection_nonoverlap():
    seg = [{"start": i * 5, "end": i * 5 + 4.5, "text": "A verdade é que essa dica é importante!"} for i in range(40)]
    clips = select_clips(seg, 3, 20, 35)
    assert 1 <= len(clips) <= 3
    assert all(x["end"] <= y["start"] for x, y in zip(clips, clips[1:]))


def test_bad_url():
    with pytest.raises(ValueError):
        validate_source_url("http://127.0.0.1/test.mp4")


def test_youtube_url():
    assert validate_source_url("https://www.youtube.com/watch?v=abcd1234")


def test_youtube_bot_block_message():
    original = "ERROR: [youtube] JM1gEnmaUao: Sign in to confirm you’re not a bot. Use --cookies."
    error = user_friendly_download_error(RuntimeError(original))
    assert "YouTube" in error
    assert "Nao informe senha nem cookies" in error
    assert "--cookies" not in error


def test_youtube_private_message():
    assert "privado" in user_friendly_download_error(RuntimeError("This video is private"))
