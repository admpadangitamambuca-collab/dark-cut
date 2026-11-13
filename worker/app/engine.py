"""DarkCut: extraction, speech transcription, selection, export and factual metadata.

The optional external language model is only used with a private server-side key.
When unavailable, metadata is transparently marked as an extractive suggestion.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

INTEREST_WORDS = {
    "segredo", "incrível", "erro", "verdade", "surpresa", "importante", "melhor",
    "nunca", "porque", "como", "quando", "atenção", "dica", "descobri", "resultado",
    "revelou", "precisa", "precisamos", "aprendi", "funciona", "mudou", "veja",
    "problema", "solução", "impressionante", "primeiro", "finalmente", "diferença",
}
STOPWORDS = {
    "para", "sobre", "entre", "esse", "essa", "isso", "aqui", "agora", "então", "também",
    "quando", "onde", "como", "porque", "minha", "nosso", "nossa", "você", "vocês", "gente",
    "estou", "estava", "eles", "elas", "muito", "mais", "ainda", "depois", "antes",
    "pela", "pelo", "pelos", "pelas", "com", "sem", "uma", "umas", "uns", "que", "ser",
    "tem", "ter", "vai", "pode", "fazer", "fiquei", "aquela", "aquele", "dessa", "daqui",
    "there", "about", "that", "this", "with", "from", "have", "what", "when", "they", "your",
}


def run_command(args: list[str], timeout: int = 1200) -> None:
    cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if cp.returncode:
        raise RuntimeError((cp.stderr or cp.stdout)[-1300:] or "O comando de vídeo falhou.")




def inspect_duration(source: Path) -> float:
    cp = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)], capture_output=True, text=True, timeout=30)
    if cp.returncode or not cp.stdout.strip():
        raise ValueError("Não foi possível ler a duração deste vídeo.")
    value = float(cp.stdout.strip())
    if value < 12:
        raise ValueError("O vídeo precisa ter pelo menos 12 segundos.")
    if value > int(os.getenv("MAX_SOURCE_SECONDS", "10800")):
        raise ValueError("O vídeo ultrapassa a duração máxima configurada (3 horas).")
    return value


def transcribe(source: Path, progress: Callable[[int, str], None]) -> list[dict]:
    progress(28, "Transcrevendo o áudio para localizar os melhores momentos")
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("Motor de transcrição não instalado. Instale os requisitos Python do worker.") from e
    model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(source), beam_size=3, vad_filter=True, condition_on_previous_text=False)
    output = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments if s.text.strip()]
    if not output:
        raise ValueError("O áudio não gerou uma transcrição utilizável. Confira se há fala inteligível.")
    return output


def segment_score(text: str) -> float:
    lower = text.lower()
    tokens = re.findall(r"[\wÀ-ÿ]+", lower)
    unique = set(tokens)
    return (min(len(tokens), 170) / 24 + sum(w in unique for w in INTEREST_WORDS) * 1.5
            + (1.6 if "?" in text else 0) + (0.9 if "!" in text else 0)
            + (1.1 if re.search(r"\b(\d+|primeiro|segundo|third|first)\b", lower) else 0))


def select_clips(segments: list[dict], count: int, minimum: int, maximum: int) -> list[dict]:
    """Rank transcription windows, avoiding overlaps; never fabricate timecodes."""
    if not segments:
        return []
    candidates = []
    for i in range(0, len(segments), 2):
        start = max(0.0, float(segments[i]["start"]) - 0.7)
        for j in range(i, len(segments)):
            finish = float(segments[j]["end"]) + 0.7
            duration = finish - start
            if duration > maximum:
                break
            if duration < minimum:
                continue
            text = " ".join(s["text"] for s in segments[i:j + 1])
            center_bonus = 1.3 - abs(duration - (minimum + maximum) / 2) / maximum
            candidates.append({"start": round(start, 2), "end": round(finish, 2), "text": text,
                               "score": round(segment_score(text) / max(duration, 1) * 40 + center_bonus, 3)})
    if not candidates:
        # Short videos: provide one short cut if usable, instead of faking a normal length.
        total = float(segments[-1]["end"]) - float(segments[0]["start"])
        if total >= 8:
            return [{"start": max(0, float(segments[0]["start"])), "end": float(segments[-1]["end"]),
                     "text": " ".join(s["text"] for s in segments), "score": 0}]
        return []
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for candidate in candidates:
        if any(candidate["start"] < s["end"] + 2 and s["start"] < candidate["end"] + 2 for s in selected):
            continue
        selected.append(candidate)
        if len(selected) == count:
            break
    return sorted(selected, key=lambda x: x["start"])


def extract_keywords(text: str, maximum: int = 8) -> list[str]:
    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9]{4,}", text.lower())
    counts = {}
    for w in tokens:
        if w not in STOPWORDS and not w.isdigit():
            counts[w] = counts.get(w, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda z: (-z[1], z[0]))[:maximum]]


def extractive_metadata(text: str, index: int) -> dict:
    """Transparent metadata drafts based strictly on words from the selected excerpt."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if len(p.strip()) > 12]
    if not parts:
        parts = [text.strip()]
    title = re.sub(r"\s+", " ", parts[0]).strip(" .!?:,-")[:82] or f"Destaque do vídeo #{index}"
    if len(title) > 78:
        title = title[:75].rstrip() + "…"
    description = " ".join(parts[:3])[:580].strip()
    if not description:
        description = title
    tags = ["#Cortes", "#MelhoresMomentos"] + ["#" + w.capitalize() for w in extract_keywords(text)[:7]]
    # A social-post caption is distinct from a descriptive video synopsis.
    caption = f"{title}.\n\n{description}\n\nQual sua opinião sobre esse trecho?"
    return {
        "title": title, "description": description, "caption": caption,
        "summary": description, "hashtags": tags, "metadata_source": "extrativo",
    }


def ai_metadata(text: str, index: int) -> dict:
    """Optional LLM drafting; no external key means an honest extractive fallback."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return extractive_metadata(text, index)
    endpoint = os.getenv("OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions")
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0.35,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "Você é editor de vídeos e cortes para redes sociais em português brasileiro. "
                "Para UM trecho, escreva título chamativo fiel ao texto (até 85 caracteres); "
                "descrição do conteúdo do corte em 2 ou 3 frases (até 600 caracteres); "
                "legenda pronta para publicação, envolvente e natural, com chamada para interação (até 700 caracteres); "
                "e de 5 a 10 hashtags relevantes. Não invente fatos, números, pessoas, contexto ou promessas. "
                "Responda APENAS JSON com title (string), description (string), caption (string), "
                "hashtags (array de strings)."
            )},
            {"role": "user", "content": "Transcrição deste corte:\n" + text[:9000]},
        ],
    }
    try:
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
        with urllib.request.urlopen(req, timeout=55) as resp:
            data = json.loads(resp.read(512 * 1024))
        result = json.loads(data["choices"][0]["message"]["content"])
        title = result.get("title")
        desc = result.get("description")
        caption = result.get("caption")
        tags = result.get("hashtags")
        if not all(isinstance(value, str) and value.strip() for value in (title, desc, caption)) or not isinstance(tags, list):
            raise ValueError("Metadados da IA incompletos")
        hashtags = []
        for t in tags[:12]:
            slug = re.sub(r"[^\wÀ-ÿ]", "", str(t).lstrip("#"))
            if slug and "#" + slug not in hashtags:
                hashtags.append("#" + slug)
        if len(hashtags) < 3:
            raise ValueError("A IA retornou poucas hashtags")
        return {
            "title": title.strip()[:85], "description": desc.strip()[:600],
            "summary": desc.strip()[:600], "caption": caption.strip()[:700],
            "hashtags": hashtags, "metadata_source": "ia",
        }
    except Exception:
        return extractive_metadata(text, index)

def srt_timestamp(seconds: float) -> str:
    millis = round(max(0, seconds) * 1000)
    hours, rest = divmod(millis, 3600 * 1000)
    minutes, rest = divmod(rest, 60 * 1000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def write_srt(segments: list[dict], start: float, end: float, path: Path) -> bool:
    parts = []
    for s in segments:
        a, b = max(start, s["start"]), min(end, s["end"])
        if b - a <= 0.05:
            continue
        parts.append(f"{len(parts) + 1}\n{srt_timestamp(a-start)} --> {srt_timestamp(b-start)}\n{s['text']}\n")
    if not parts:
        return False
    path.write_text("\n".join(parts), encoding="utf-8")
    return True


def render_clip(source: Path, start: float, end: float, destination: Path, style: str, srt: Path | None = None) -> None:
    if style not in {"vertical", "horizontal"}:
        raise ValueError("Formato desconhecido")
    scale_filter = ("scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1"
                    if style == "vertical" else
                    "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if srt:
        # SRT lives in same job directory with a safe static filename; ffmpeg -vf escape.
        escaped = str(srt.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        scale_filter += f",subtitles=filename='{escaped}':force_style='FontName=DejaVu Sans,FontSize=16,Outline=1,Alignment=2,MarginV=28'"
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
        "-i", str(source), "-t", str(max(0.2, end - start)), "-vf", scale_filter,
        "-c:v", "libx264", "-preset", os.getenv("FFMPEG_PRESET", "veryfast"),
        "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(destination)
    ])