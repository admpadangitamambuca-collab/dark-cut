"""Precision: independent editorial cuts, bounded score, no invented timecodes."""
import pytest

from app.precision import editorial_scores, select_precision_clips


def recording(seconds=370):
    fragments = [
        "Você sabia que este é o primeiro erro que quase ninguém percebe?",
        "A verdade é que existe uma diferença importante no resultado.",
        "Agora vou explicar com calma o que acontece neste caso.",
        "Mas existe um problema que precisamos entender para resolver.",
        "O segredo é analisar corretamente o exemplo anterior.",
        "Primeiro observamos o comportamento e depois decidimos o caminho.",
        "Portanto, a conclusão mostra como isso funciona na prática.",
    ]
    return [
        {"start": i * 5., "end": i * 5. + 4.8,
         "text": fragments[(i + i//7) % len(fragments)]} for i in range(seconds // 5)
    ]


def test_two_independent_modes_with_different_lengths():
    seg = recording()
    shorts = select_precision_clips(seg, "shorts", 3, 20, 60, 370)
    long = select_precision_clips(seg, "traditional", 2, 120, 240, 370)
    assert 1 <= len(shorts) <= 3
    assert 1 <= len(long) <= 2
    assert all(c['profile'] == 'shorts' and c['id'].startswith('S') for c in shorts)
    assert all(c['profile'] == 'traditional' and c['id'].startswith('N') for c in long)
    assert all(20 <= c['duration'] <= 60 for c in shorts)
    assert all(120 <= c['duration'] <= 240 for c in long)
    for bucket in (shorts, long):
        assert all(x['end'] <= y['start'] or y['end'] <= x['start'] for i, x in enumerate(bucket) for y in bucket[i + 1:])
        assert all(0 <= c['scores']['overall'] <= 100 for c in bucket)
        assert all(0 <= value <= 100 for c in bucket for value in c['scores'].values())


def test_short_source_does_not_fake_long_cut():
    seg = recording(45)
    assert select_precision_clips(seg, "traditional", 2, 120, 300, 45) == []
    assert select_precision_clips(seg, "shorts", 1, 20, 35, 45)


def test_weak_opening_reduces_context():
    weak = [{"text": "E então isso que a gente estava falando", "start": 0, "end": 10},
            {"text": "Pode ser importante entender esse ponto", "start": 10, "end": 20}]
    strong = [{"text": "Entenda como identificar o primeiro problema", "start": 0, "end": 10},
              {"text": "Pode ser importante entender esse ponto.", "start": 10, "end": 20}]
    a = editorial_scores(weak, 20)
    b = editorial_scores(strong, 20)
    assert b['context'] > a['context']
    assert b['closure'] > a['closure']


def test_invalid_profile_rejected():
    with pytest.raises(ValueError):
        select_precision_clips(recording(), 'anything', 1, 20, 40, 370)