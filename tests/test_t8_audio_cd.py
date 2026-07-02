"""T8 — audio_cd : CUE valide + WAV Red Book 44.1 kHz 16 bit gapless."""
import re
import wave
from pathlib import Path

from backend.pipeline import disc


def test_wav_is_redbook(synth_audio_only):
    out = disc.run(synth_audio_only, gap_seconds=2.0)
    with wave.open(out["wav"], "rb") as w:
        assert w.getframerate() == 44100
        assert w.getsampwidth() == 2      # 16 bit
        assert w.getnchannels() == 2      # stéréo


def test_cue_structure(synth_audio_only):
    out = disc.run(synth_audio_only, gap_seconds=2.0)
    cue = Path(out["cue"]).read_text()
    assert cue.startswith("PERFORMER") or "PERFORMER" in cue.splitlines()[0]
    assert 'FILE "audio_cd.wav" WAVE' in cue
    tracks = re.findall(r"TRACK (\d+) AUDIO", cue)
    assert tracks == ["01", "02", "03", "04"]
    # chaque piste a un INDEX 01 au format MM:SS:FF
    indexes = re.findall(r"INDEX 01 (\d{2}:\d{2}:\d{2})", cue)
    assert len(indexes) == 4
    assert indexes[0] == "00:00:00"


def test_gap_increases_offsets(synth_audio_only):
    # Avec un gap de 2 s, la piste 2 démarre après piste1(3s)+gap(2s) = 5 s
    out = disc.run(synth_audio_only, gap_seconds=2.0)
    cue = Path(out["cue"]).read_text()
    indexes = re.findall(r"INDEX 01 (\d{2}:\d{2}:\d{2})", cue)
    mm, ss, ff = (int(x) for x in indexes[1].split(":"))
    seconds = mm * 60 + ss + ff / 75
    assert abs(seconds - 5.0) < 0.2


def test_wav_total_duration(synth_audio_only):
    # 4 pistes de 3 s + 3 gaps de 2 s = 18 s
    out = disc.run(synth_audio_only, gap_seconds=2.0)
    with wave.open(out["wav"], "rb") as w:
        duration = w.getnframes() / w.getframerate()
    assert abs(duration - 18.0) < 0.3
