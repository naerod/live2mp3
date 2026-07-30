"""File d'attente des rendus — RQ + état partagé dans Redis.

Le rendu tournait auparavant dans un `threading.Thread` du conteneur API. Trois
défauts : ffmpeg disputait ses cœurs à l'API, un redémarrage du conteneur tuait
le job sans reprise, et rien n'empêchait deux rendus simultanés d'écrire les
mêmes fichiers. Le worker RQ existait déjà mais n'a jamais rien consommé
(aucun `enqueue` dans le code) — il est ici enfin branché.

Concurrence 1 : un seul worker, donc un seul rendu actif ; les suivants
attendent leur tour et peuvent être réordonnés.

État partagé (Redis), lu par l'API et écrit par le worker ou l'inverse :
- `l2m:cancel:<slug>` demande d'annulation, sondée pendant l'encodage
- `l2m:pause:<slug>`  demande de pause, appliquée par SIGSTOP sur le ffmpeg
- `l2m:job:<slug>`    métadonnées d'affichage (mp3/mp4, demandeur, date)
"""
from __future__ import annotations

import json
import os
import time

import redis
from rq import Queue
from rq.job import Job
from rq.registry import StartedJobRegistry

QUEUE_NAME = "live2mp3"
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# Un concert long en MP4 dépasse l'heure ; large marge avant que RQ considère
# le job perdu et le déclare échoué.
JOB_TIMEOUT = 6 * 3600
META_TTL = 24 * 3600

class DuplicateRender(Exception):
    """Un rendu occupe déjà ce slug.

    Exception dédiée plutôt qu'un ValueError générique : RQ lève lui aussi des
    ValueError (identifiant invalide, file inconnue), et les confondre avait
    déguisé une erreur de configuration en simple refus de doublon.
    """


_conn: redis.Redis | None = None


def conn() -> redis.Redis:
    global _conn
    if _conn is None:
        _conn = redis.from_url(REDIS_URL, decode_responses=True)
    return _conn


def queue() -> Queue:
    return Queue(QUEUE_NAME, connection=conn(), default_timeout=JOB_TIMEOUT)


JOB_PREFIX = "render-"


def job_id(slug: str) -> str:
    """Un job par slug : c'est ce qui rend le doublon impossible.

    Pas de deux-points dans l'identifiant : RQ n'accepte que lettres, chiffres,
    tirets et soulignés (les slugs respectent déjà cette contrainte).
    """
    return f"{JOB_PREFIX}{slug}"


def _slug_of(jid: str) -> str:
    return jid[len(JOB_PREFIX):]


# --- Drapeaux d'annulation / pause ----------------------------------------

def request_cancel(slug: str) -> None:
    conn().set(f"l2m:cancel:{slug}", "1", ex=META_TTL)


def cancel_requested(slug: str) -> bool:
    try:
        return conn().exists(f"l2m:cancel:{slug}") == 1
    except redis.RedisError:
        return False


def clear_cancel(slug: str) -> None:
    conn().delete(f"l2m:cancel:{slug}")


def set_paused(slug: str, paused: bool) -> None:
    if paused:
        conn().set(f"l2m:pause:{slug}", "1", ex=META_TTL)
    else:
        conn().delete(f"l2m:pause:{slug}")


def is_paused(slug: str) -> bool:
    try:
        return conn().exists(f"l2m:pause:{slug}") == 1
    except redis.RedisError:
        return False


# --- Métadonnées d'affichage ----------------------------------------------

def set_meta(slug: str, meta: dict) -> None:
    conn().set(f"l2m:job:{slug}", json.dumps(meta), ex=META_TTL)


def get_meta(slug: str) -> dict:
    try:
        raw = conn().get(f"l2m:job:{slug}")
        return json.loads(raw) if raw else {}
    except (redis.RedisError, ValueError):
        return {}


def clear_meta(slug: str) -> None:
    conn().delete(f"l2m:job:{slug}")


# --- Cycle de vie ----------------------------------------------------------

def fetch(slug: str) -> Job | None:
    try:
        return Job.fetch(job_id(slug), connection=conn())
    except Exception:
        return None


def active_status(slug: str) -> str | None:
    """`queued` | `started` si un rendu occupe déjà ce slug, sinon None."""
    job = fetch(slug)
    if job is None:
        return None
    try:
        status = job.get_status(refresh=True)
    except Exception:
        return None
    return status if status in ("queued", "started", "deferred") else None


def enqueue(slug: str, *, media: str, gap: float, video: bool,
            republish: bool, requested_by: str = "") -> Job:
    """Met un rendu en file. Lève DuplicateRender si le slug en a déjà un."""
    if active_status(slug):
        raise DuplicateRender("un rendu est déjà en cours ou en attente")
    # Un job terminé garde son id : sans purge, RQ refuserait de réutiliser
    # `render-<slug>` pour le rendu suivant du même album.
    old = fetch(slug)
    if old is not None:
        try:
            old.delete()
        except Exception:
            pass
    clear_cancel(slug)
    set_paused(slug, False)
    set_meta(slug, {"slug": slug, "video": bool(video), "media": media,
                    "requested_by": requested_by, "queued_at": time.time()})
    return queue().enqueue(
        "backend.jobs.render_job",
        kwargs={"slug": slug, "media": media, "gap": gap,
                "video": video, "republish": republish},
        job_id=job_id(slug), job_timeout=JOB_TIMEOUT, result_ttl=3600,
    )


def cancel(slug: str) -> str:
    """Annule un rendu en file (retrait) ou en cours (drapeau + SIGSTOP levé).

    Retourne `queued` ou `started` selon l'état trouvé, `none` si rien.
    """
    status = active_status(slug)
    if status is None:
        return "none"
    request_cancel(slug)
    # Une pause laisserait le ffmpeg gelé et l'annulation sans effet : le
    # process doit tourner pour observer le drapeau et s'arrêter.
    set_paused(slug, False)
    if status in ("queued", "deferred"):
        job = fetch(slug)
        if job is not None:
            try:
                job.cancel()
                job.delete()
            except Exception:
                pass
        clear_meta(slug)
    return status


def _started_slugs() -> list[str]:
    try:
        reg = StartedJobRegistry(QUEUE_NAME, connection=conn())
        return [_slug_of(i) for i in reg.get_job_ids()
                if i.startswith(JOB_PREFIX)]
    except Exception:
        return []


def _queued_slugs() -> list[str]:
    try:
        return [_slug_of(i) for i in queue().job_ids
                if i.startswith(JOB_PREFIX)]
    except Exception:
        return []


def listing() -> list[dict]:
    """File courante : le rendu actif en tête, puis les suivants dans l'ordre."""
    out: list[dict] = []
    for slug in _started_slugs():
        meta = get_meta(slug)
        meta.update({"slug": slug, "state": "paused" if is_paused(slug)
                     else "running"})
        out.append(meta)
    for slug in _queued_slugs():
        meta = get_meta(slug)
        meta.update({"slug": slug, "state": "queued"})
        out.append(meta)
    return out


def reorder(slugs: list[str]) -> list[str]:
    """Réordonne les rendus **en attente** selon `slugs`.

    RQ n'expose pas de réordonnancement : on retire les jobs concernés de la
    liste de la file et on les réinsère dans l'ordre voulu. Le job en cours
    d'exécution n'est pas dans cette liste, il ne peut donc pas être déplacé —
    c'est aussi la sémantique attendue côté interface.
    """
    q = queue()
    current = [i for i in q.job_ids if i.startswith(JOB_PREFIX)]
    wanted = [job_id(s) for s in slugs if job_id(s) in current]
    # Tout job en attente absent de la demande garde sa place relative, à la fin.
    wanted += [i for i in current if i not in wanted]
    key = q.key
    pipe = conn().pipeline()
    for jid in current:
        pipe.lrem(key, 0, jid)
    for jid in wanted:
        pipe.rpush(key, jid)
    pipe.execute()
    return [_slug_of(i) for i in wanted]
