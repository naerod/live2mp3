"""T5 — Waveform + silencedetect générés et cohérents."""
import struct
from pathlib import Path

from backend.pipeline import preanalyze


def test_waveform_dat_format(synth_audio_only):
    wav = synth_audio_only / "source" / "master.wav"
    out = synth_audio_only / "source" / "waveform.dat"
    preanalyze.generate_waveform(wav, out, pixels_per_second=50, bits=8)
    assert out.exists()
    header = out.read_bytes()[:20]
    version, flags, sample_rate, spp, length = struct.unpack("<iIiii", header)
    # audiowaveform 1.10.x écrit du v1 (header 20 o) ; le fallback Python fait
    # de même pour que les deux chemins soient interchangeables.
    assert version == 1
    assert flags == 1              # 8 bits
    assert sample_rate == 44100
    assert length > 0
    # taille = header (20) + length * 2 (paires int8)
    assert out.stat().st_size == 20 + length * 2


def test_silence_detection_between_tones(synth_audio_only):
    # Insère un silence au milieu et vérifie qu'il est détecté
    import subprocess
    wav = synth_audio_only / "source" / "silence_test.wav"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex",
        "[0:a]atrim=0:2[a];[1:a]atrim=0:1.5[s];[a][s]concat=n=2:v=0:a=1[out]",
        "-map", "[out]", "-ar", "44100", str(wav),
    ], check=True, capture_output=True)
    silences = preanalyze.detect_silences(wav, noise_db="-30dB", min_dur=0.5)
    assert len(silences) >= 1
    # le silence commence après ~2 s
    assert any(s["start"] >= 1.8 for s in silences)


def test_gpu_model_choice(monkeypatch):
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_MODEL_CPU", raising=False)
    device, model = preanalyze.whisper_model_choice()
    assert device in ("cuda", "cpu")
    if device == "cpu":
        assert model == "small"     # léger : la transcription sert au découpage
    else:
        assert model == "large-v3"
    monkeypatch.setenv("WHISPER_MODEL_CPU", "base")
    if device == "cpu":
        assert preanalyze.whisper_model_choice() == ("cpu", "base")


def test_waveform_state_updated(synth_audio_only):
    # generate + set state via run partiel (sans whisper/llm coûteux)
    from backend.manifest import Manifest
    wav = synth_audio_only / "source" / "master.wav"
    preanalyze.generate_waveform(wav, synth_audio_only / "source" / "waveform.dat")
    m = Manifest.load(synth_audio_only / "manifest.yaml")
    m.set_state("waveform", "done")
    assert m.state("waveform") == "done"


def test_waveform_fallback_meme_format_que_le_binaire(synth_audio_only, monkeypatch):
    """Binaire BBC et fallback Python doivent produire le MÊME format.

    Régression 2026-09-06 : le fallback annonçait `version=2` tout en écrivant
    un en-tête v1 de 20 octets. Les parseurs front sautent 24 octets pour une
    v2 — la forme d'onde était donc décalée de 4 octets dès que le binaire
    `audiowaveform` était absent.
    """
    wav = synth_audio_only / "source" / "master.wav"

    def header(out):
        preanalyze.generate_waveform(wav, out, pixels_per_second=50, bits=8)
        return struct.unpack("<iIiii", out.read_bytes()[:20])

    natif = header(synth_audio_only / "source" / "wf_natif.dat")
    monkeypatch.setattr(preanalyze, "_audiowaveform_available", lambda: False)
    fallback = header(synth_audio_only / "source" / "wf_fallback.dat")

    assert natif[0] == fallback[0] == 1     # même version
    assert natif[1] == fallback[1]          # mêmes flags (8 bits)
    assert natif[2] == fallback[2]          # même fréquence d'échantillonnage
