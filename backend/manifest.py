"""Manifest = source unique de vérité pour un concert.

Charge/sauvegarde/valide `manifest.yaml`, gère le slug projet et le
nettoyage des caractères spéciaux dans les noms de fichiers.

Règle anti-écrasement : les timecodes d'une piste `locked: true` ne sont
jamais réécrits par l'IA (voir `merge_ai_markers`).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"

STAGES = [
    "download",
    "waveform",
    "ai_markers",
    "render",
    "tags",
    "artwork",
    "disc",
    # Rendu MP4, découplé de l'audio le 2026-08-03 : il s'exécute dans un job
    # séparé, bien après que l'album audio est prêt, et a donc son propre état.
    "video",
]

VALID_TARGETS = {"data_disc", "audio_cd"}  # dvd_video hors V1


class ManifestError(ValueError):
    """Manifest invalide."""


def slugify(value: str) -> str:
    """Slug ASCII sûr pour un nom de dossier/fichier."""
    # Normalise les séparateurs typographiques avant l'encodage ASCII
    # (sinon un tiret cadratin disparaît au lieu de séparer les mots).
    value = re.sub(r"[·–—/]", " ", value)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "untitled"


def sanitize_filename(title: str) -> str:
    """Supprime les caractères interdits dans un nom de fichier.

    Convention bibliothèque (« 01. Titre.mp3 ») : on garde espaces et accents,
    on ne retire que ce que les systèmes de fichiers refusent.
    """
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title).strip(' .')


def clean_filename(title: str) -> str:
    """Nettoie un titre de piste pour en faire un nom de fichier.

    Retire les séparateurs de medley et caractères spéciaux (`/`, `·`,
    `–`, etc.) tout en gardant la lisibilité.
    """
    value = unicodedata.normalize("NFKD", title)
    value = value.encode("ascii", "ignore").decode("ascii")
    # Séparateurs de medley -> tiret simple
    value = re.sub(r"\s*[/·–—-]\s*", "-", value)
    value = re.sub(r"[^A-Za-z0-9\-_ ]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace(" ", "_")
    return value or "track"


def project_slug(manifest: dict[str, Any]) -> str:
    album = manifest.get("album", {})
    artist = album.get("artist", "")
    date = album.get("date", "")
    return f"{slugify(artist)}-{slugify(str(date))}".strip("-")


def numbered_title(n: Any, title: str) -> str:
    """Titre préfixé du numéro de piste sur 2 chiffres : ``01. Overcompensate``.

    Utilisé **uniquement** pour le tag ID3/metadata (TIT2) — le titre affiché
    dans l'app reste nu (lu depuis le manifeste). Le numéro dans le titre force
    l'ordre des pistes sur les lecteurs qui trient alphabétiquement (Spotify
    local, etc.).
    """
    title = title or ""
    try:
        nn = int(n)
    except (TypeError, ValueError):
        return title
    return f"{nn:02d}. {title}".rstrip()


def download_stem(manifest: dict[str, Any], fallback: str = "") -> str:
    """Base du nom de fichier de téléchargement d'un album :
    ``YYYY-MM-DD_artiste_titre`` (champs vides omis).

    Calculé à la volée depuis le manifeste → le nom suit automatiquement toute
    modification des infos de l'album (date, artiste, titre) depuis le site.
    Repli sur `fallback` (le slug) si aucune métadonnée exploitable.
    """
    album = manifest.get("album", {})
    date = str(album.get("date", "") or "").strip()
    parts: list[str] = []
    # Date : conservée telle quelle si elle est bien au format ISO AAAA-MM-JJ.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        parts.append(date)
    for field in ("artist", "title"):
        s = slugify(str(album.get(field, "") or ""))
        if s and s != "untitled":
            parts.append(s)
    return "_".join(parts) or slugify(fallback) or "album"


@dataclass
class Manifest:
    """Wrapper autour du dict manifest avec accès disque."""

    data: dict[str, Any]
    path: Path | None = None

    # ---- I/O -------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        m = cls(data=data, path=path)
        m.validate()
        return m

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ManifestError("Aucun chemin de sauvegarde fourni.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                self.data, fh, sort_keys=False, allow_unicode=True, default_flow_style=False
            )
        self.path = target
        return target

    # ---- Validation ------------------------------------------------------
    def validate(self) -> None:
        d = self.data
        if "album" not in d or not isinstance(d["album"], dict):
            raise ManifestError("Champ 'album' manquant ou invalide.")
        album = d["album"]
        for key in ("artist", "title"):
            if not album.get(key):
                raise ManifestError(f"album.{key} requis.")
        target = d.get("target")
        if target not in VALID_TARGETS:
            raise ManifestError(
                f"target invalide: {target!r} (attendu: {sorted(VALID_TARGETS)})"
            )
        tracks = d.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            raise ManifestError("Au moins une piste requise dans 'tracks'.")
        seen = set()
        for t in tracks:
            n = t.get("n")
            if n in seen:
                raise ManifestError(f"Numéro de piste dupliqué: {n}")
            seen.add(n)
            if not t.get("title"):
                raise ManifestError(f"Piste {n}: titre requis.")
        # normalise pipeline_state
        d.setdefault("pipeline_state", {})
        for s in STAGES:
            d["pipeline_state"].setdefault(s, "pending")

    # ---- Helpers ---------------------------------------------------------
    @property
    def slug(self) -> str:
        return project_slug(self.data)

    @property
    def project_dir(self) -> Path:
        if self.path is not None:
            return self.path.parent
        return PROJECTS_DIR / self.slug

    @property
    def tracks(self) -> list[dict[str, Any]]:
        return self.data.get("tracks", [])

    def track_filename(self, track: dict[str, Any], ext: str) -> str:
        """Nom de fichier au format bibliothèque : « 01. Titre.ext »."""
        n = int(track.get("n", 0))
        safe = sanitize_filename(track["title"]) or f"Track {n}"
        return f"{n:02d}. {safe}.{ext}"

    def set_state(self, stage: str, value: str) -> None:
        if stage not in STAGES:
            raise ManifestError(f"Stage inconnu: {stage}")
        self.data.setdefault("pipeline_state", {})[stage] = value
        if self.path:
            self.save()

    def state(self, stage: str) -> str:
        return self.data.get("pipeline_state", {}).get(stage, "pending")

    def merge_ai_markers(self, markers: dict[int, dict[str, float]]) -> int:
        """Applique les timecodes IA en respectant la règle anti-écrasement.

        `markers` : {n_piste: {"start": float, "end": float}}.
        Seules les pistes `locked=False` sont modifiées. Retourne le nombre
        de pistes réellement mises à jour.
        """
        updated = 0
        for track in self.tracks:
            n = track.get("n")
            if track.get("locked"):
                continue
            if n in markers:
                track["start"] = markers[n]["start"]
                track["end"] = markers[n]["end"]
                updated += 1
        if self.path:
            self.save()
        return updated

    def lock_all(self) -> None:
        """Verrouille toutes les pistes (après validation utilisateur)."""
        for track in self.tracks:
            track["locked"] = True
        if self.path:
            self.save()


def new_manifest(album: dict[str, Any], tracks: list[dict[str, Any]], target: str,
                 source_url: str = "",
                 clips: list[dict[str, Any]] | None = None) -> Manifest:
    """Construit un manifest neuf depuis les données du formulaire.

    `clips` (optionnel) : mode multi-liens — plusieurs sources concaténées en
    un master unique à la préparation, une piste par clip. `source.url` reste
    vide ; c'est `source.clips` (ordonné) qui pilote le téléchargement.
    """
    source: dict[str, Any] = {"url": source_url, "master_mkv": "source/master.mkv",
                              "master_wav": "source/master.wav"}
    if clips:
        source["multi"] = True
        source["clips"] = [{"url": c["url"], "duration": float(c.get("duration") or 0.0)}
                           for c in clips]
    data: dict[str, Any] = {
        "album": album,
        "target": target,
        "source": source,
        "pipeline_state": {s: "pending" for s in STAGES},
        "tracks": [],
    }
    for i, t in enumerate(tracks, start=1):
        track = {
            "n": t.get("n", i),
            "title": t["title"],
            "start": t.get("start"),
            "end": t.get("end"),
            "locked": t.get("locked", False),
        }
        if t.get("artist"):
            track["artist"] = t["artist"]
        if t.get("parts"):
            track["parts"] = t["parts"]
        data["tracks"].append(track)
    m = Manifest(data=data)
    m.validate()
    return m
