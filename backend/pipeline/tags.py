"""Stage 5 — Tags.

Écrit les métadonnées ID3 (+ cover art APIC) sur chaque MP3 via mutagen,
et les métadonnées MP4 via ffmpeg -metadata.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3

from ..manifest import Manifest


def _cover_bytes(project_dir: Path, manifest: Manifest) -> tuple[bytes | None, str]:
    cover_rel = manifest.data.get("album", {}).get("cover")
    if not cover_rel:
        return None, ""
    cover_path = project_dir / cover_rel
    if not cover_path.exists():
        return None, ""
    mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
    return cover_path.read_bytes(), mime


def tag_mp3(path: Path, track: dict, album: dict, total: int,
            cover: bytes | None, cover_mime: str) -> None:
    try:
        audio = MP3(str(path), ID3=ID3)
    except ID3NoHeaderError:
        audio = MP3(str(path))
        audio.add_tags()
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    tags.delall("APIC")
    tags["TIT2"] = TIT2(encoding=3, text=track["title"])
    tags["TRCK"] = TRCK(encoding=3, text=f"{track['n']}/{total}")
    tags["TALB"] = TALB(encoding=3, text=album.get("title", ""))
    tags["TPE1"] = TPE1(encoding=3, text=album.get("artist", ""))
    tags["TPE2"] = TPE2(encoding=3, text=album.get("artist", ""))
    tags["TCON"] = TCON(encoding=3, text="Live")
    if album.get("date"):
        tags["TDRC"] = TDRC(encoding=3, text=str(album["date"])[:4])
    if cover:
        tags["APIC"] = APIC(encoding=3, mime=cover_mime, type=3,
                            desc="Cover", data=cover)
    audio.save(v2_version=3)


def tag_mp4(path: Path, track: dict, album: dict, total: int) -> None:
    tmp = path.with_suffix(".tagged.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-map_metadata", "-1", "-c", "copy",
        "-metadata", f"title={track['title']}",
        "-metadata", f"track={track['n']}/{total}",
        "-metadata", f"album={album.get('title', '')}",
        "-metadata", f"artist={album.get('artist', '')}",
        "-metadata", f"album_artist={album.get('artist', '')}",
        "-metadata", "genre=Live",
    ]
    if album.get("date"):
        cmd += ["-metadata", f"date={str(album['date'])[:4]}"]
    cmd.append(str(tmp))
    subprocess.run(cmd, check=True, capture_output=True)
    tmp.replace(path)


def run(project_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    album = m.data.get("album", {})
    total = len(m.tracks)
    cover, cover_mime = _cover_bytes(project_dir, m)
    audio_dir = project_dir / "build" / "audio"
    video_dir = project_dir / "build" / "video"

    tagged = {"audio": [], "video": []}
    for track in m.tracks:
        if track.get("start") is None or track.get("end") is None:
            continue
        mp3 = audio_dir / m.track_filename(track, "mp3")
        if mp3.exists():
            tag_mp3(mp3, track, album, total, cover, cover_mime)
            tagged["audio"].append(str(mp3))
        mp4 = video_dir / m.track_filename(track, "mp4")
        if mp4.exists():
            tag_mp4(mp4, track, album, total)
            tagged["video"].append(str(mp4))

    m.set_state("tags", "done")
    return tagged


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
