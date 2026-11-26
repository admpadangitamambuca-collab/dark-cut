"""Tests for the free local editor; no remote inference or licensed recordings."""
import wave
from app import editorial, main


def test_original_tracks(tmp_path):
    tracks = []
    for track in editorial.TRACKS:
        output = tmp_path / (track + ".wav")
        editorial.write_track(track, output)
        with wave.open(str(output), "rb") as file:
            assert file.getnchannels() == 1
            assert file.getframerate() == 16000
            assert file.getnframes() > 100000
            tracks.append(file.readframes(file.getnframes()))
    assert len(set(tracks)) == 3
    assert editorial.soundtrack_pcm("ambient") == editorial.soundtrack_pcm("ambient")


def test_reframing_keeps_faces_or_preserves_full_frame():
    narrow = [(16/9, [(.13, .08)])] * 5
    assert .14 < editorial.safe_face_center(narrow) < .20
    assert editorial.safe_face_center([(16/9, [(.1,.08),(.75,.09)])] * 5) is None
    assert editorial.safe_face_center([(16/9, [])] * 5) is None
    assert "overlay" in editorial.video_graph("vertical", "auto", None, None)
    assert "crop=720:1280" in editorial.video_graph("vertical", "auto", .3, None)


def test_editing_parameters_are_optional_and_bounded():
    assert main.JobSettings().music == "none"
    s = main.JobSettings(review_before_render=True, framing="auto", music="ambient", music_volume=12,
                         shorts_min_seconds=90, shorts_max_seconds=180)
    main._validated_settings(s)
    r = main.RenderChoice(id="S01", start=0, end=15, title="Teste", framing="fit", music="pulse")
    assert r.framing == "fit" and r.music == "pulse"


def test_command_has_ducking_and_removes_temporary_music(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(editorial, "run_command", lambda args: commands.append(args))
    monkeypatch.setattr(editorial, "detect_face_center", lambda *args: None)
    dest = tmp_path / "test.mp4"
    editorial.render_edited_clip(tmp_path/"input.mp4", 1, 15, dest, "vertical", None,
                                framing="auto", music="ambient", music_volume=12)
    args = commands[0]
    graph = args[args.index("-filter_complex")+1]
    assert "sidechaincompress" in graph and "overlay" in graph and "amix" in graph
    assert not dest.with_suffix(".music.wav").exists()
