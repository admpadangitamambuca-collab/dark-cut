"""Local editing: original procedural soundtrack, conservative face-based reframing.

No downloaded music, paid service, credentials or remote inference required.
Face detections are samples, not speaker identification or continuous tracking.
"""
from __future__ import annotations

import logging
import math
import os
import subprocess
import wave
from array import array
from functools import lru_cache
from pathlib import Path
from statistics import median

from .engine import run_command

logger = logging.getLogger(__name__)

# Original programmed motifs, no sampled or third-party recordings.
TRACKS = {
    "ambient": (84, ((48, 55, 60, 64), (45, 52, 57, 60), (41, 48, 53, 57), (43, 50, 55, 62))),
    "pulse": (104, ((45, 52, 57, 64), (48, 55, 60, 67), (43, 50, 55, 62), (41, 48, 53, 60))),
    "cinematic": (72, ((50, 57, 62, 65), (46, 53, 58, 62), (53, 60, 65, 69), (48, 55, 60, 67))),
}


@lru_cache(maxsize=3)
def soundtrack_pcm(track: str) -> tuple[int, bytes]:
    """Create one four-bar loop as mono PCM; cache less than 2 MB per track."""
    bpm, chords = TRACKS[track]
    rate = 16000
    beat = 60 / bpm
    duration = beat * 16
    pcm = array("h")
    tau = 2 * math.pi
    frequencies = [[440 * 2 ** ((note - 69) / 12) for note in chord] for chord in chords]
    for i in range(round(duration * rate)):
        t = i / rate
        bar = min(3, int(t / (4 * beat)))
        local = t - bar * 4 * beat
        fs = frequencies[bar]
        envelope = max(0., min(1., local / .10, (4 * beat - local) / .18))
        pad = sum(math.sin(tau * freq * local) for freq in fs) * .075 * envelope
        step = beat / (2 if track == "pulse" else 1)
        at = local % step
        note = fs[(int(local / step) + bar) % 4] * 2
        pluck = .11 * math.sin(tau * note * at) * min(1., at / .012) * math.exp(-at * 7)
        low = .045 * math.sin(tau * fs[0] / 2 * local) * envelope
        if track == "pulse":
            p = local % beat
            low += .055 * math.sin(tau * (52 * p + 4 * (1 - math.exp(-20 * p)))) * math.exp(-p * 18) * min(1., p / .008)
        fade = min(1., t / .035, max(0., (duration - t) / .035))
        pcm.append(round(max(-1., min(1., (pad + pluck + low) * fade)) * 32767))
    if os.sys.byteorder != "little":
        pcm.byteswap()
    return rate, pcm.tobytes()


def write_track(track: str, path: Path) -> None:
    rate, pcm = soundtrack_pcm(track)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def safe_face_center(samples: list[tuple[float, list[tuple[float, float]]]]) -> float | None:
    """Find a stable horizontal crop that contains all sampled faces, with margin."""
    if len(samples) < 2:
        return None
    detected = [(aspect, faces) for aspect, faces in samples if faces]
    if len(detected) < max(2, math.ceil(len(samples) * .4)):
        return None
    aspect = median([a for a, _ in detected])
    if aspect < 9 / 16 - .015:
        return None
    if aspect <= 9 / 16:
        return .5
    width = (9 / 16) / aspect
    left = min(x for _, faces in detected for x, _ in faces)
    right = max(x + w for _, faces in detected for x, w in faces)
    margin = min(.035, width * .10)
    if right - left + 2 * margin > width:
        return None
    low = max(width / 2, right + margin - width / 2)
    high = min(1 - width / 2, left - margin + width / 2)
    if low > high:
        return None
    return min(high, max(low, (left + right) / 2))


def detect_face_center(source: Path, start: float, end: float) -> float | None:
    """Sample at most five small frames; gracefully fall back on difficult footage."""
    try:
        import cv2
        import numpy as np
        cv2.setNumThreads(1)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if cascade.empty():
            return None
        samples = []
        for fraction in (.08, .27, .5, .73, .92):
            at = start + (end - start) * fraction
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-threads", "1", "-ss", str(at), "-i", str(source),
                "-frames:v", "1", "-vf", "scale=480:-2,setsar=1", "-threads:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "pipe:1"
            ], capture_output=True, timeout=12)
            if result.returncode or not result.stdout:
                continue
            frame = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if frame is None:
                continue
            h, w = frame.shape
            faces = cascade.detectMultiScale(cv2.equalizeHist(frame), scaleFactor=1.13,
                                              minNeighbors=5, minSize=(28, 28))
            samples.append((w / h, [(float(x) / w, float(fw) / w) for x, y, fw, fh in faces]))
        return safe_face_center(samples)
    except Exception:
        logger.warning("Automatic face framing unavailable; preserving complete frame", exc_info=True)
        return None


def video_graph(style: str, framing: str, center: float | None, srt: Path | None) -> str:
    w, h = (720, 1280) if style == "vertical" else (1280, 720)
    full = framing == "fit" or (style == "vertical" and framing == "auto" and center is None)
    if full:
        # Blur at low resolution to keep CPU/memory bounded. Foreground never crops.
        graph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={w//2}:{h//2}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={w//2}:{h//2},boxblur=12:2,scale={w}:{h},setsar=1[back];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[front];"
            "[back][front]overlay=(W-w)/2:(H-h)/2[framed]"
        )
    elif framing == "fill" or style == "vertical":
        pos = .5 if center is None else center
        graph = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={w}:{h}:x='max(0,min(iw-ow,iw*{pos:.5f}-ow/2))':y=(ih-oh)/2,setsar=1[framed]"
        )
    else:
        graph = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[framed]"
        )
    if srt:
        escaped = str(srt.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        size, margin = (14, 43) if style == "vertical" else (16, 25)
        graph += (f";[framed]subtitles=filename='{escaped}':force_style="
                  f"'FontName=DejaVu Sans,FontSize={size},Outline=2,Shadow=0,Alignment=2,MarginV={margin},MarginL=12,MarginR=12'[v]")
    else:
        graph += ";[framed]null[v]"
    return graph


def render_edited_clip(source: Path, start: float, end: float, destination: Path,
                       style: str, srt: Path | None, *, framing: str,
                       music: str, music_volume: int) -> None:
    if style not in ("vertical", "horizontal"):
        raise ValueError("Formato desconhecido")
    duration = max(.2, end - start)
    center = detect_face_center(source, start, end) if style == "vertical" and framing == "auto" else None
    graph = video_graph(style, framing, center, srt)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-filter_complex_threads", "1", "-threads", "1", "-ss", str(start), "-i", str(source)]
    track_path = destination.with_suffix(".music.wav")
    try:
        if music != "none":
            write_track(music, track_path)
            args += ["-stream_loop", "-1", "-i", str(track_path)]
            # Preserve speech; attenuate the music further whenever voice is present.
            graph += (
                ";[0:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                "acompressor=threshold=0.125:ratio=2:attack=15:release=200,asplit=2[voice][side];"
                f"[1:a:0]aresample=48000,aformat=channel_layouts=stereo,volume={music_volume/100:.3f},"
                f"atrim=duration={duration:.4f},afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={max(0.,duration-.55):.4f}:d=0.55[bed];"
                "[bed][side]sidechaincompress=threshold=0.025:ratio=6:attack=25:release=350[duck];"
                "[voice][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]"
            )
            audio_map = "[a]"
        else:
            audio_map = "0:a:0?"
        args += ["-t", str(duration), "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
                 "-c:v", "libx264", "-threads:v", "1", "-preset", os.getenv("FFMPEG_PRESET", "veryfast"),
                 "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", str(destination)]
        run_command(args)
    finally:
        track_path.unlink(missing_ok=True)
