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
    "(segments avec start/end en secondes) et une liste de silences détectés. "
    "Renvoie UNIQUEMENT un objet JSON, sans markdown ni texte, de la forme : "
    '{\"tracks\": [{\"n\": 1, \"start\": 0.0, \"end\": 182.4}, ...]}. '
    "Un start/end par piste de la setlist, dans l'ordre, en secondes (float). "
    "Les medleys comptent comme une seule piste. Aligne les frontières sur les "
    "silences quand c'est cohérent."
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
                      silences: list[dict]) -> str:
    lines = ["SETLIST:"]
    for t in setlist:
        lines.append(f"  {t['n']}. {t['title']}")
    lines.append("\nTRANSCRIPTION (start,end,text):")
    for seg in transcript:
        lines.append(f"  [{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
    lines.append("\nSILENCES (start,end):")
    for s in silences:
        lines.append(f"  [{s['start']:.1f}-{s['end']:.1f}]")
    return "\n".join(lines)


def request_markers(setlist: list[dict], transcript: list[dict],
                    silences: list[dict], *, timeout: int = 120) -> dict[int, dict[str, float]]:
    """Appelle DeepSeek et renvoie les timecodes proposés."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY absent de l'environnement.")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(setlist, transcript, silences)},
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
    content = resp.json()["choices"][0]["message"]["content"]
    return parse_markers(content)
