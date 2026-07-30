"""Progression des jobs, partagée entre le conteneur API et le worker.

Remplace les dictionnaires en mémoire du process API : depuis que le rendu
s'exécute dans le worker RQ, l'émetteur et le lecteur du flux SSE ne sont plus
dans le même process. Redis sert donc à la fois de journal (rejouable pour un
client qui arrive en cours de route ou qui revient après un aller-retour dans
l'interface) et de bus temps réel.

Deux structures par slug :
- `l2m:prog:<slug>`   liste des évènements déjà émis (rejeu à la connexion)
- `l2m:progch:<slug>` canal pub/sub pour le temps réel

Le journal expire tout seul : un projet terminé n'a pas vocation à garder son
historique de progression indéfiniment.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterator

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# Un rendu MP4 d'un concert long dépasse l'heure ; on garde large pour qu'un
# retour tardif sur l'écran de progression retrouve encore son historique.
LOG_TTL = 6 * 3600
MAX_EVENTS = 2000

_client: redis.Redis | None = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def _log_key(slug: str) -> str:
    return f"l2m:prog:{slug}"


def _chan(slug: str) -> str:
    return f"l2m:progch:{slug}"


def publish(slug: str, event: dict) -> None:
    """Journalise puis diffuse un évènement de progression."""
    event.setdefault("ts", time.time())
    payload = json.dumps(event)
    try:
        pipe = client().pipeline()
        pipe.rpush(_log_key(slug), payload)
        pipe.ltrim(_log_key(slug), -MAX_EVENTS, -1)
        pipe.expire(_log_key(slug), LOG_TTL)
        pipe.publish(_chan(slug), payload)
        pipe.execute()
    except redis.RedisError:
        # La progression est un confort d'affichage : une panne Redis ne doit
        # jamais faire échouer un rendu déjà lancé.
        pass


def make_cb(slug: str):
    """Callback au format attendu par `jobs.run_render_pipeline`."""
    def cb(stage: str, status: str, info: dict) -> None:
        publish(slug, {"stage": stage, "status": status, "info": info})
    return cb


def history(slug: str) -> list[dict]:
    try:
        return [json.loads(x) for x in client().lrange(_log_key(slug), 0, -1)]
    except redis.RedisError:
        return []


def reset(slug: str) -> None:
    try:
        client().delete(_log_key(slug))
    except redis.RedisError:
        pass


def stream(slug: str, timeout: float = 30.0) -> Iterator[dict | None]:
    """Évènements temps réel ; `None` à chaque expiration (keepalive SSE)."""
    try:
        pubsub = client().pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(_chan(slug))
    except redis.RedisError:
        return
    try:
        while True:
            msg = pubsub.get_message(timeout=timeout)
            if msg is None:
                yield None
                continue
            try:
                yield json.loads(msg["data"])
            except (ValueError, KeyError):
                continue
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
