"""Stage 2 — Pré-analyse.

2a. waveform.dat via bbc/audiowaveform (-b 8, binaire) ; fallback Python au
    même format si le binaire est absent (tests hors Docker).
2b. Détection IA des frontières :
    - transcription faster-whisper (GPU auto via nvidia-smi, fallback CPU medium)
    - silencedetect ffmpeg
    - appel DeepSeek -> timecodes proposés (locked=False)
"""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from ..manifest import Manifest


# ---- 2a. Waveform --------------------------------------------------------
def _audiowaveform_available() -> bool:
    return shutil.which("audiowaveform") is not None


def generate_waveform(master_wav: Path, out_dat: Path,
                      pixels_per_second: int = 100, bits: int = 8) -> Path:
    """Génère waveform.dat. Binaire BBC si dispo, sinon fallback Python."""
    out_dat.parent.mkdir(parents=True, exist_ok=True)
    if _audiowaveform_available():
        subprocess.run([
            "audiowaveform", "-i", str(master_wav), "-o", str(out_dat),
            "-b", str(bits), "--pixels-per-second", str(pixels_per_second),
        ], check=True, capture_output=True)
        return out_dat
    return _waveform_fallback(master_wav, out_dat, pixels_per_second, bits)


def _waveform_fallback(master_wav: Path, out_dat: Path,
                       pixels_per_second: int, bits: int) -> Path:
    """Reproduit le format binaire audiowaveform v2 en pur Python.

    Header (little-endian) : int32 version=2, uint32 flags (bit0=1 -> 8 bits),
    int32 sample_rate, int32 samples_per_pixel, int32 length (nb paires).
    Données : paires min/max (int8 si 8 bits, sinon int16).
    """
    with wave.open(str(master_wav), "rb") as w:
        sample_rate = w.getframerate()
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    if sampwidth != 2:
        raise ValueError("Fallback waveform : WAV 16 bits attendu.")
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    # mono mix
    if n_channels > 1:
        mono = array.array("h", [0] * (len(samples) // n_channels))
        for i in range(len(mono)):
            mono[i] = samples[i * n_channels]
        samples = mono

    samples_per_pixel = max(1, sample_rate // pixels_per_second)
    n_pairs = (len(samples) + samples_per_pixel - 1) // samples_per_pixel
    is8 = bits == 8

    with out_dat.open("wb") as f:
        flags = 1 if is8 else 0
        f.write(struct.pack("<iIiii", 2, flags, sample_rate,
                            samples_per_pixel, n_pairs))
        for p in range(n_pairs):
            chunk = samples[p * samples_per_pixel:(p + 1) * samples_per_pixel]
            if not chunk:
                mn = mx = 0
            else:
                mn, mx = min(chunk), max(chunk)
            if is8:
                f.write(struct.pack("<bb", mn >> 8, mx >> 8))
            else:
                f.write(struct.pack("<hh", mn, mx))
    return out_dat


# ---- 2b. Silences --------------------------------------------------------
_SIL_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SIL_END = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silences(master_wav: Path, noise_db: str = "-30dB",
                    min_dur: float = 0.6) -> list[dict]:
    """ffmpeg silencedetect -> liste [{start, end}]."""
    proc = subprocess.run([
        "ffmpeg", "-i", str(master_wav),
        "-af", f"silencedetect=noise={noise_db}:d={min_dur}",
        "-f", "null", "-",
    ], capture_output=True, text=True)
    log = proc.stderr
    silences, cur = [], None
    for line in log.splitlines():
        ms = _SIL_START.search(line)
        me = _SIL_END.search(line)
        if ms:
            cur = float(ms.group(1))
        if me and cur is not None:
            silences.append({"start": cur, "end": float(me.group(1))})
            cur = None
    return silences


# ---- 2b. Whisper ---------------------------------------------------------
def detect_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None and \
        subprocess.run(["nvidia-smi"], capture_output=True).returncode == 0


def whisper_model_choice() -> tuple[str, str]:
    """Retourne (device, model) selon la présence d'un GPU."""
    if detect_gpu():
        return "cuda", "large-v3"
    return "cpu", "medium"


def transcribe(master_wav: Path, model_size: str | None = None,
               device: str | None = None) -> list[dict]:
    """Transcription horodatée via faster-whisper."""
    from faster_whisper import WhisperModel  # import tardif (dépendance lourde)

    if device is None or model_size is None:
        device, model_size = whisper_model_choice()
    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_size, device=device, compute_type=compute)
    segments, _ = model.transcribe(str(master_wav))
    return [{"start": s.start, "end": s.end, "text": s.text.strip()}
            for s in segments]


# ---- Orchestration -------------------------------------------------------
def run(project_dir: str | Path, call_llm: bool = True) -> dict:
    from .. import llm

    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    master_wav = project_dir / m.data["source"]["master_wav"]

    dat = generate_waveform(master_wav, project_dir / "source" / "waveform.dat")
    m.set_state("waveform", "done")

    silences = detect_silences(master_wav)
    transcript = transcribe(master_wav)
    setlist = [{"n": t["n"], "title": t["title"]} for t in m.tracks]

    updated = 0
    if call_llm:
        markers = llm.request_markers(setlist, transcript, silences)
        updated = m.merge_ai_markers(markers)
        m.set_state("ai_markers", "done")

    return {"waveform": str(dat), "silences": silences,
            "segments": len(transcript), "tracks_updated": updated}


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
