"""Détection des frontières de pistes par l'énergie du signal.

Dans une captation live il n'y a jamais de silence réel entre deux morceaux :
applaudissements, foule, artiste qui parle. Mesuré sur un concert de 20 min,
`silencedetect` ne trouve qu'**un seul** silence à -30 dB (aucun à -35 dB) :
l'IA se retrouvait donc sans repère temporel et plaçait les coupes au milieu
des chansons.

En revanche l'énergie **plonge nettement** à chaque transition — c'est ce que
l'œil voit sur la forme d'onde. On mesure donc l'enveloppe RMS et on retient
les creux locaux les plus marqués comme candidats de coupe.

Deux garde-fous rendent la détection utilisable telle quelle :
- le creux est jugé face au **niveau ambiant local** (médiane glissante) et non
  à un seuil absolu — les captations live sont très compressées ;
- deux coupes sont séparées d'au moins `MIN_GAP_S`, ce qui écarte les ponts et
  passages calmes *à l'intérieur* d'un morceau.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

STEP = 0.25          # résolution de l'enveloppe (s)
SMOOTH_S = 3.0       # lissage : gomme le grain, garde les creux de transition
AMBIENT_S = 45.0     # fenêtre du niveau ambiant local
MIN_GAP_S = 120.0    # écart minimal entre deux coupes (durée plancher d'un titre)
EDGE_S = 60.0        # on ignore le tout début et la toute fin
MIN_DEPTH_DB = 1.0   # en deçà, le creux n'est pas significatif
TOP = 12             # nombre de candidats proposés à l'IA


def energy_envelope(wav_path: str | Path, step: float = STEP) -> np.ndarray:
    """Enveloppe RMS du fichier : un point tous les `step` secondes.

    Lecture par blocs : un WAV de concert pèse plusieurs centaines de Mo.
    """
    out: list[float] = []
    with wave.open(str(wav_path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        win = max(1, int(sr * step))
        while True:
            raw = w.readframes(win)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if ch > 1:
                a = a[:len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
            out.append(float(np.sqrt((a * a).mean())) if a.size else 0.0)
    return np.asarray(out, dtype=np.float32)


def _smooth(x: np.ndarray, sec: float, step: float) -> np.ndarray:
    k = max(1, int(sec / step))
    return np.convolve(x, np.ones(k) / k, mode="same")


def _ambient(db: np.ndarray, step: float) -> np.ndarray:
    """Niveau ambiant local (médiane glissante).

    Calculée sur une grille d'un point par seconde puis interpolée : la médiane
    exacte à pleine résolution coûterait cher sur un concert de deux heures,
    pour un résultat identique à cette échelle.
    """
    w = max(1, int(AMBIENT_S / step))
    grid_step = max(1, int(1.0 / step))
    idx = np.arange(0, len(db), grid_step)
    coarse = np.array([np.median(db[max(0, i - w):i + w]) for i in idx])
    return np.interp(np.arange(len(db)), idx, coarse)


def candidates_from_envelope(env: np.ndarray, step: float = STEP,
                             min_gap: float = MIN_GAP_S, edge: float = EDGE_S,
                             top: int = TOP,
                             min_depth: float = MIN_DEPTH_DB) -> list[dict]:
    """Creux d'énergie -> candidats de coupe (triés par instant croissant)."""
    if env.size == 0:
        return []
    dur = len(env) * step
    sm = _smooth(env, SMOOTH_S, step)
    # Échelle log : à l'oreille comme à l'œil, un creux est un rapport, pas
    # une différence linéaire.
    db = 20 * np.log10(np.maximum(sm, 1.0) / 32768.0)
    depth = _ambient(db, step) - db

    picked: list[dict] = []
    for i in np.argsort(-depth):
        if depth[i] < min_depth:
            break
        t = float(i) * step
        if t < edge or t > dur - edge:
            continue
        if any(abs(t - c["time"]) < min_gap for c in picked):
            continue
        picked.append({"time": round(t, 2),
                       "depth": round(float(depth[i]), 2),
                       "level": round(float(db[i]), 2)})
        if len(picked) >= top:
            break
    return sorted(picked, key=lambda c: c["time"])


def detect(wav_path: str | Path, top: int = TOP) -> list[dict]:
    """Candidats de coupe d'un fichier audio."""
    return candidates_from_envelope(energy_envelope(wav_path), top=top)


def pick(candidates: list[dict], n_cuts: int) -> list[float]:
    """Les `n_cuts` creux les plus marqués, remis dans l'ordre chronologique."""
    if n_cuts <= 0:
        return []
    best = sorted(candidates, key=lambda c: -c["depth"])[:n_cuts]
    return sorted(c["time"] for c in best)


def snap(times, candidates: list[dict], tol: float = 25.0) -> list[float]:
    """Aligne des frontières proposées sur le creux le plus proche.

    Corrige la dérive de quelques secondes de l'IA sans déplacer une frontière
    qui n'aurait aucun creux à portée (medley, enchaînement sans coupure).
    """
    out = []
    for t in times:
        near = [c for c in candidates if abs(c["time"] - t) <= tol]
        out.append(min(near, key=lambda c: abs(c["time"] - t))["time"]
                   if near else float(t))
    return out
