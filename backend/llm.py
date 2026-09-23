"""Client DeepSeek (format OpenAI ChatCompletions).

Utilise `deepseek-v4-flash` pour extraire des timecodes de pistes depuis une
transcription horodatée + la setlist + les silences détectés. Réponse JSON
uniquement (pas de markdown ni prose).

La clé vient de l'environnement (`DEEPSEEK_API_KEY`) — jamais loggée.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

SYSTEM_PROMPT = (
    "Tu es un assistant qui découpe l'enregistrement d'un concert en pistes. "
    "On te donne la setlist (titres dans l'ordre), une transcription horodatée "
    "(segments avec start/end en secondes) et une liste de CANDIDATS de coupe. "
    "Renvoie UNIQUEMENT un objet JSON, sans markdown ni texte, de la forme : "
    '{\"tracks\": [{\"n\": 1, \"start\": 0.0, \"end\": 182.4}, ...]}. '
    "Un start/end par piste de la setlist, dans l'ordre, en secondes (float). "
    "Les medleys comptent comme une seule piste. RÈGLE PRINCIPALE : les "
    "frontières entre pistes doivent être choisies PARMI les candidats fournis "
    "— ce sont les instants où l'énergie sonore plonge, donc les vraies "
    "transitions. Privilégie les candidats de forte profondeur (depth, en dB) ; "
    "un candidat peu profond est souvent un simple pont à l'intérieur d'un "
    "morceau. Utilise la transcription pour savoir QUEL candidat correspond à "
    "QUELLE transition (changement de paroles). Une chanson live dure "
    "typiquement 3 à 8 minutes : méfie-toi d'un découpage qui donnerait des "
    "pistes très inégales. La fin d'une piste est le début de la suivante."
)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse une réponse LLM en JSON, tolère un éventuel fence markdown."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return json.loads(text)


def parse_markers(response_text: str) -> dict[int, dict[str, float]]:
    """Convertit la réponse LLM en {n: {start, end}}."""
    data = _extract_json(response_text)
    markers: dict[int, dict[str, float]] = {}
    for item in data.get("tracks", []):
        n = int(item["n"])
        markers[n] = {"start": float(item["start"]), "end": float(item["end"])}
    return markers


def build_user_prompt(setlist: list[dict], transcript: list[dict],
                      candidates: list[dict]) -> str:
    lines = ["SETLIST:"]
    for t in setlist:
        lines.append(f"  {t['n']}. {t['title']}")
    lines.append("\nTRANSCRIPTION (start,end,text):")
    for seg in transcript:
        lines.append(f"  [{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
    lines.append("\nCANDIDATS DE COUPE (instant en s, profondeur du creux en dB) :")
    for c in candidates:
        lines.append(f"  t={c['time']:.1f} depth={c.get('depth', 0):.1f}")
    return "\n".join(lines)


def _chat(system: str, user: str, *, timeout: int = 120) -> str:
    """Appel ChatCompletions JSON-only ; renvoie le contenu brut."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY absent de l'environnement.")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def request_markers(setlist: list[dict], transcript: list[dict],
                    candidates: list[dict], *, timeout: int = 120) -> dict[int, dict[str, float]]:
    """Appelle DeepSeek et renvoie les timecodes proposés.

    `candidates` : creux d'énergie mesurés (backend.pipeline.boundaries) parmi
    lesquels l'IA choisit les frontières.
    """
    content = _chat(SYSTEM_PROMPT,
                    build_user_prompt(setlist, transcript, candidates),
                    timeout=timeout)
    return parse_markers(content)


# --- Extraction des métadonnées d'album depuis une vidéo -------------------
ALBUM_INFO_PROMPT = (
    "Tu extrais les métadonnées d'un enregistrement de concert depuis les "
    "informations d'une vidéo (titre, chaîne, date de mise en ligne, durée en "
    "secondes, description, chapitres). Réponds UNIQUEMENT avec un objet JSON, "
    "sans markdown : {\"artist\": str, \"title\": str, \"date\": \"YYYY-MM-DD\" ou null, "
    "\"venue\": str, \"city\": str, \"festival\": str, "
    "\"tracks\": [{\"n\": int, \"title\": str, \"artist\": str ou null, "
    "\"start\": float ou null, \"end\": float ou null}]}. Règles : "
    "artist = artiste principal du concert (pas le nom de la chaîne, sauf chaîne "
    "officielle de l'artiste). Un festival, une émission, une salle, une ville "
    "ou une année n'est JAMAIS l'artiste : dans un titre de la forme « X - Y » "
    "(ex. « Main Square 2026 - Twenty One Pilots »), l'artiste est le nom de "
    "groupe ou de personne, l'autre moitié va dans festival ou venue. title = titre d'album court et évocateur (ex. "
    "\"Live in Times Square 2014\"), sans le nom de l'artiste ni les mots parasites "
    "(full show, HD, 4K, pro shot). date = date du CONCERT uniquement si elle est ÉCRITE "
    "dans le titre ou la description (jamais la date de mise en ligne si le "
    "concert a une autre date), sinon null. Ne DEVINE jamais une date à partir "
    "d'une simple année ou d'un nom d'édition de festival : dans le doute, null. venue = salle/lieu précis, city = ville, festival = "
    "tournée/festival/événement — chaîne vide si inconnu. tracks = setlist dans "
    "l'ordre : utilise en priorité les chapitres, sinon les timecodes de la "
    "description, sinon les titres de chansons mentionnés ; n'invente JAMAIS un "
    "titre absent des données ; renvoie [] si aucune setlist n'est déductible. "
    "Nettoie les titres de piste (retire numéros, timecodes, emojis). "
    "tracks[].artist uniquement si la piste est interprétée par un autre artiste "
    "que l'artiste principal (invité, duo) explicitement mentionné. start/end en "
    "secondes si des timecodes existent ; end d'une piste = start de la suivante "
    "si non précisé, end de la dernière = durée de la vidéo ; sinon null."
)


def parse_album_info(response_text: str, duration: float = 0.0) -> dict:
    """Valide et normalise la réponse LLM d'extraction d'album."""
    data = _extract_json(response_text)
    out = {
        "artist": str(data.get("artist") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "date": None,
        "venue": str(data.get("venue") or "").strip(),
        "city": str(data.get("city") or "").strip(),
        "festival": str(data.get("festival") or "").strip(),
        "tracks": [],
    }
    date = str(data.get("date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        out["date"] = date
    for t in data.get("tracks") or []:
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        track = {"n": 0, "title": title,
                 "artist": (str(t.get("artist")).strip()
                            if t.get("artist") else None),
                 "start": None, "end": None}
        try:
            start = float(t["start"]) if t.get("start") is not None else None
            end = float(t["end"]) if t.get("end") is not None else None
        except (TypeError, ValueError):
            start = end = None
        if start is not None and start >= 0:
            if duration:
                start = min(start, duration)
            track["start"] = start
        if end is not None and end > 0:
            if duration:
                end = min(end, duration)
            track["end"] = end
        out["tracks"].append(track)
    for i, track in enumerate(out["tracks"], start=1):
        track["n"] = i
    return out


def extract_album_info(video: dict, *, timeout: int = 120) -> dict:
    """Vidéo sondée -> proposition artiste/titre/date/lieu/setlist."""
    payload = {
        "titre": video.get("title", ""),
        "chaine": video.get("channel", ""),
        "date_mise_en_ligne": video.get("upload_date", ""),
        "duree_secondes": video.get("duration", 0),
        "chapitres": video.get("chapters", []),
        "description": video.get("description", ""),
    }
    content = _chat(ALBUM_INFO_PROMPT,
                    json.dumps(payload, ensure_ascii=False), timeout=timeout)
    return parse_album_info(content, duration=float(video.get("duration") or 0))


# --- Setlist automatique depuis la transcription ---------------------------
AUTO_SETLIST_PROMPT = (
    "Tu identifies les chansons d'un enregistrement de concert à partir d'une "
    "transcription horodatée des paroles et d'une liste de CANDIDATS de coupe. "
    "L'artiste principal et la durée totale sont fournis. Réponds UNIQUEMENT en "
    "JSON : {\"tracks\": [{\"n\": 1, \"title\": str, \"artist\": str ou null, "
    "\"start\": float, \"end\": float}]}. Identifie chaque chanson d'après ses "
    "paroles (titre réel quand tu le reconnais, sinon un titre descriptif "
    "court). tracks[].artist uniquement si un autre interprète est identifiable. "
    "Les frontières doivent être choisies PARMI les candidats fournis (instants "
    "où l'énergie sonore plonge = vraies transitions) ; privilégie les candidats "
    "de forte profondeur, un candidat peu profond étant souvent un pont interne. "
    "Une chanson live dure typiquement 3 à 8 minutes. Couvre tout "
    "l'enregistrement sans chevauchement, dans l'ordre."
)


def request_auto_setlist(transcript: list[dict], candidates: list[dict],
                         artist: str, duration: float,
                         *, timeout: int = 180) -> list[dict]:
    """Transcription + creux d'énergie -> pistes complètes (titres + timecodes)."""
    lines = [f"ARTISTE PRINCIPAL: {artist}",
             f"DUREE TOTALE (s): {duration:.1f}",
             "\nTRANSCRIPTION (start,end,text):"]
    for seg in transcript:
        lines.append(f"  [{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
    lines.append("\nCANDIDATS DE COUPE (instant en s, profondeur du creux en dB) :")
    for c in candidates:
        lines.append(f"  t={c['time']:.1f} depth={c.get('depth', 0):.1f}")
    content = _chat(AUTO_SETLIST_PROMPT, "\n".join(lines), timeout=timeout)
    data = _extract_json(content)
    tracks: list[dict] = []
    for i, t in enumerate(data.get("tracks") or [], start=1):
        title = str(t.get("title") or "").strip()
        try:
            start, end = float(t["start"]), float(t["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not title or end <= start:
            continue
        tracks.append({"n": i, "title": title,
                       "artist": (str(t.get("artist")).strip()
                                  if t.get("artist") else None),
                       "start": start, "end": min(end, duration) if duration else end})
    return tracks
