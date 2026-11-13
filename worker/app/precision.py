"""Lightweight editorial candidate ranking for two independent clip profiles.

Only transcript timing and words are used. Scores are explainable heuristics,
not calibrated virality probabilities or guarantees of engagement.
"""
from __future__ import annotations

import bisect
import math
import re
from collections import Counter

KEY_TERMS = {
    "erro", "erros", "segredo", "segredos", "incrível", "surpresa", "verdade",
    "importante", "melhor", "pior", "nunca", "sempre", "porque", "como", "dica",
    "descobri", "resultado", "problema", "solução", "mudou", "impacto", "atenção",
    "impressionante", "risco", "diferença", "funciona", "primeiro", "último",
    "entenda", "precisa", "aprendi", "difficult", "important", "secret", "why",
}
CONTRAST_TERMS = {"mas", "porém", "entretanto", "embora", "apesar", "but", "however", "só", "instead"}
WEAK_STARTS = {
    "e", "aí", "então", "daí", "assim", "isso", "esse", "essa", "ele", "ela",
    "eles", "elas", "aquilo", "porque", "continuando", "tipo", "né", "é",
    "and", "so", "that", "it", "he", "she", "they", "this", "also",
}
WEAK_ENDS = {"mas", "e", "ou", "que", "porque", "quando", "se", "então", "but", "and", "or"}
STOP = {"para", "como", "isso", "esse", "essa", "aqui", "você", "vocês", "gente", "sobre", "então", "quando", "porque", "there", "from", "with", "that"}
METRIC_WEIGHTS = {"hook": .24, "clarity": .23, "impact": .18, "context": .20, "closure": .15}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\wÀ-ÿ]+", text.lower(), re.UNICODE)


def editorial_scores(segment_group: list[dict], duration: float) -> dict:
    """Transparent bounded text-based score across five editorial attributes."""
    text = " ".join(s["text"] for s in segment_group).strip()
    first = segment_group[0]["text"].strip()
    last = segment_group[-1]["text"].strip()
    words = _tokens(text)
    intro = _tokens(first)
    ending = _tokens(last)
    unique = set(words)
    density = len(words) / max(duration, 1.0)
    prominent = len(unique & KEY_TERMS)
    hooks = len(unique & CONTRAST_TERMS)
    has_question = "?" in first
    has_number = any(w.isdigit() for w in words[:20])
    weak_start = bool(intro and intro[0] in WEAK_STARTS)
    weak_end = bool(ending and ending[-1] in WEAK_ENDS)
    ends_punct = bool(re.search(r"[.!?][\"'”’)?\s]*$", last))
    starts_punct = bool(re.match(r"^[A-ZÀ-Ý0-9¿¡]", first))

    hook = 47 + (17 if has_question else 0) + min(20, prominent * 6) + (7 if has_number else 0)
    hook += 5 if "!" in first else 0
    hook -= 20 if weak_start else 0
    hook -= 13 if len(intro) < 3 else 0

    clarity = 55 + (21 if .95 <= density <= 3.3 else 2 if .55 <= density <= 4.2 else -20)
    clarity += min(12, len(unique) / 8)
    clarity -= 15 if len(words) < 14 else 0

    impact = 41 + min(27, prominent * 9) + min(13, hooks * 7)
    impact += 12 if "!" in text else 0
    impact += 9 if "?" in text else 0
    impact += 8 if any(w.isdigit() for w in words) else 0

    context = 75 - (31 if weak_start else 0) + (7 if starts_punct else 0)
    context += min(10, len(intro) / 2)
    context -= 17 if len(intro) <= 3 else 0

    closure = 49 + (31 if ends_punct else -10) - (23 if weak_end else 0)
    closure += 8 if any(w in ending[-8:] for w in ("portanto", "finalmente", "conclusão", "resultado", "resumo", "fim")) else 0
    result = {
        "hook": int(round(max(0, min(100, hook)))),
        "clarity": int(round(max(0, min(100, clarity)))),
        "impact": int(round(max(0, min(100, impact)))),
        "context": int(round(max(0, min(100, context)))),
        "closure": int(round(max(0, min(100, closure)))),
    }
    result["overall"] = int(round(sum(result[k] * v for k, v in METRIC_WEIGHTS.items())))
    return result


def _candidate_slices(segments: list[dict], minimum: float, maximum: float):
    """Bounded candidate generation using binary search rather than quadratic loops."""
    ends = [float(s["end"]) for s in segments]
    starts = [float(s["start"]) for s in segments]
    targets = (minimum, minimum + .35 * (maximum - minimum), minimum + .70 * (maximum - minimum), maximum)
    used = set()
    for i in range(len(segments)):
        start = max(0.0, starts[i] - .3)
        for target in targets:
            j = bisect.bisect_left(ends, start + target, lo=i)
            for k in (j - 1, j, j + 1):
                if not i <= k < len(segments) or (i, k) in used:
                    continue
                finish = ends[k] + .3
                if minimum <= finish - start <= maximum:
                    used.add((i, k))
                    yield i, k, round(start, 2), round(finish, 2)


def select_precision_clips(segments: list[dict], profile: str, count: int,
                           minimum: int, maximum: int, source_duration: float) -> list[dict]:
    """Return non-overlapping, independently ranked suggestions per profile."""
    if profile not in ("shorts", "traditional"):
        raise ValueError("Perfil de corte desconhecido.")
    if count < 0 or count > 8 or minimum < 8 or maximum < minimum:
        raise ValueError("Configuração de duração inválida.")
    if not segments or not count or source_duration < minimum:
        return []

    # Ensure boundaries remain inside the original video.
    valid = [s for s in segments if float(s["end"]) > float(s["start"]) and float(s["start"]) < source_duration]
    valid.sort(key=lambda s: float(s["start"]))
    if not valid:
        return []
    suggestions = []
    seen = set()
    for i, k, start, end in _candidate_slices(valid, minimum, maximum):
        end = min(end, source_duration)
        if end - start < minimum:
            continue
        group = valid[i:k + 1]
        scores = editorial_scores(group, end - start)
        keywords = tuple(w for w, n in Counter(_tokens(" ".join(s["text"] for s in group))).most_common(5) if w not in STOP)
        key = (round(start), round(end))
        if key in seen:
            continue
        seen.add(key)
        penalty = 0
        if len(group) <= 1:
            penalty += 9
        # Do not rank a nearly silent passage highly merely because of punctuation.
        if len(_tokens(" ".join(s["text"] for s in group))) / max(end - start, 1) < .5:
            penalty += 15
        scores["overall"] = max(0, scores["overall"] - penalty)
        suggestions.append({
            "start": start, "end": round(end, 2), "duration": round(end - start, 2),
            "transcript": " ".join(s["text"].strip() for s in group)[:12000],
            "scores": scores, "keywords": keywords,
        })
    suggestions.sort(key=lambda s: (s["scores"]["overall"], len(s["keywords"]), -s["start"]), reverse=True)
    chosen = []
    for candidate in suggestions:
        if any(candidate["start"] < s["end"] + 1 and candidate["end"] > s["start"] - 1 for s in chosen):
            continue
        # Suppress near-duplicate text openings even when they are far apart.
        opening = set(_tokens(candidate["transcript"])[:12]) - STOP
        if len(opening) >= 4 and any(len(opening & (set(_tokens(s["transcript"])[:12]) - STOP)) / len(opening) > .82 for s in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) >= count:
            break
    for number, candidate in enumerate(chosen, 1):
        candidate["id"] = ("S" if profile == "shorts" else "N") + f"{number:02d}"
        candidate["profile"] = profile
        candidate.pop("keywords", None)
    return chosen