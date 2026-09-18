"""Stage 7 — Image disque.

- data_disc : ISO de données (genisoimage) contenant MP3 ou MP4.
- audio_cd  : CUE + WAV Red Book (44.1 kHz / 16 bit / stéréo, gapless),
              TOC dérivée du manifest. Gap configurable (défaut 2 s).
- dvd_video : hors V1 (placeholder).
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

from ..manifest import Manifest, clean_filename

RED_BOOK_RATE = 44100
RED_BOOK_CHANNELS = 2
RED_BOOK_WIDTH = 2  # 16 bit


class DvdVideoNotSupported(NotImplementedError):
    """dvd_video est hors périmètre V1."""


# ---- data_disc -----------------------------------------------------------
def build_data_disc(project_dir: Path, out_iso: Path, media: str = "audio",
                    volume_id: str = "LIVE2MP3") -> Path:
    """ISO de données depuis build/audio (mp3) ou build/video-full (mp4)."""
    from .render import video_dir
    audio = media == "audio"
    src = (project_dir / "build" / "audio") if audio else video_dir(project_dir)
    ext = "mp3" if audio else "mp4"
    files = sorted(src.glob(f"*.{ext}")) if src.exists() else []
    if not files:
        raise FileNotFoundError(f"Aucun média à graver dans {src}")
    out_iso.parent.mkdir(parents=True, exist_ok=True)
    # Les fichiers sont listés un par un : `build/video-full` contient aussi les
    # images sidecar Jellyfin, qui n'ont rien à faire sur le disque gravé.
    subprocess.run([
        "genisoimage", "-quiet", "-r", "-J", "-V", volume_id[:32],
        "-o", str(out_iso), *[str(f) for f in files],
    ], check=True, capture_output=True)
    return out_iso


# ---- audio_cd ------------------------------------------------------------
def _decode_to_redbook(src_wav: Path, start: float, end: float) -> bytes:
    """Extrait un segment en PCM Red Book brut (sans header)."""
    proc = subprocess.run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(src_wav),
        "-ar", str(RED_BOOK_RATE), "-ac", str(RED_BOOK_CHANNELS),
        "-f", "s16le", "-",
    ], check=True, capture_output=True)
    return proc.stdout


def _silence_pcm(seconds: float) -> bytes:
    n = int(seconds * RED_BOOK_RATE) * RED_BOOK_CHANNELS * RED_BOOK_WIDTH
    return b"\x00" * n


def _frames_to_msf(total_frames: int) -> str:
    """Convertit un nombre de frames CD (75/s) en MM:SS:FF pour le CUE."""
    ff = total_frames % 75
    total_secs = total_frames // 75
    ss = total_secs % 60
    mm = total_secs // 60
    return f"{mm:02d}:{ss:02d}:{ff:02d}"


def build_audio_cd(project_dir: Path, out_wav: Path, out_cue: Path,
                   gap_seconds: float = 2.0) -> tuple[Path, Path]:
    """Génère un WAV concaténé Red Book + un CUE avec la TOC.

    Le CUE référence des offsets INDEX 01 par piste ; le gap est inséré comme
    audio silencieux avant chaque piste (sauf la première) — gravure gapless.
    """
    m = Manifest.load(project_dir / "manifest.yaml")
    master_wav = project_dir / m.data["source"]["master_wav"]
    album = m.data.get("album", {})
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    tracks = [t for t in m.tracks
              if t.get("start") is not None and t.get("end") is not None]
    if not tracks:
        raise ValueError("Aucune piste avec timecodes pour l'audio CD.")

    bytes_per_sec = RED_BOOK_RATE * RED_BOOK_CHANNELS * RED_BOOK_WIDTH
    bytes_per_frame = bytes_per_sec // 75

    cue_lines = [
        f'PERFORMER "{album.get("artist", "")}"',
        f'TITLE "{album.get("title", "")}"',
        f'FILE "{out_wav.name}" WAVE',
    ]

    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(RED_BOOK_CHANNELS)
        w.setsampwidth(RED_BOOK_WIDTH)
        w.setframerate(RED_BOOK_RATE)
        written = 0  # octets écrits
        for i, track in enumerate(tracks, start=1):
            if i > 1 and gap_seconds > 0:
                gap = _silence_pcm(gap_seconds)
                w.writeframes(gap)
                written += len(gap)
            # offset de la piste (aligné frame CD)
            frame_offset = written // bytes_per_frame
            cue_lines.append(f"  TRACK {i:02d} AUDIO")
            cue_lines.append(f'    TITLE "{track["title"]}"')
            cue_lines.append(f'    PERFORMER "{album.get("artist", "")}"')
            cue_lines.append(f"    INDEX 01 {_frames_to_msf(frame_offset)}")
            pcm = _decode_to_redbook(master_wav, float(track["start"]),
                                     float(track["end"]))
            w.writeframes(pcm)
            written += len(pcm)

    out_cue.write_text("\n".join(cue_lines) + "\n", encoding="utf-8")
    return out_wav, out_cue


# ---- Orchestration -------------------------------------------------------
def run(project_dir: str | Path, media: str = "audio",
        gap_seconds: float = 2.0) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    target = m.data.get("target")
    disc_dir = project_dir / "build" / "disc"
    outputs: dict = {"target": target}

    if target == "data_disc":
        iso = build_data_disc(project_dir, disc_dir / "disc.iso", media=media)
        outputs["iso"] = str(iso)
    elif target == "audio_cd":
        wav, cue = build_audio_cd(project_dir, disc_dir / "audio_cd.wav",
                                  disc_dir / "audio_cd.cue", gap_seconds)
        outputs["wav"] = str(wav)
        outputs["cue"] = str(cue)
    else:
        raise DvdVideoNotSupported(f"target non supporté en V1: {target}")

    m.set_state("disc", "done")
    return outputs


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
