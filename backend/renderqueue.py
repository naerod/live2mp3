"""File d'attente des rendus — RQ + état partagé dans Redis.

Le rendu tournait auparavant dans un `threading.Thread` du conteneur API. Trois
défauts : ffmpeg disputait ses cœurs à l'API, un redémarrage du conteneur tuait
le job sans reprise, et rien n'empêchait deux rendus simultanés d'écrire les
mêmes fichiers. Le worker RQ existait déjà mais n'a jamais rien consommé
(aucun `enqueue` dans le code) — il est ici enfin branché.

Concurrence 1 : un seul worker, donc un seul rendu actif ; les suivants
attendent leur tour et peuvent être réordonnés.

**Deux natures de job par album** (découplage du 2026-08-03) : l'audio
(`render`) et la vidéo (`video`). Les 28 MP3 d'un concert sortent en quelques
minutes, le MP4 demande un ré-encodage x264 de deux heures : les garder dans le
même job faisait attendre la vidéo pour rien à qui voulait juste son album.
L'audio est donc un job à part entière qui, une fois terminé, met la vidéo en
file derrière lui. Chaque nature a son propre identifiant de job et son propre
jeu de clés d'état — sans quoi le `finally` du job audio effacerait les
métadonnées du job vidéo qu'il vient d'enfiler.

État partagé (Redis), lu par l'API et écrit par le worker ou l'inverse :
- `l2m:cancel:<kind>:<slug>` demande d'annulation, sondée pendant l'encodage
- `l2m:pause:<kind>:<slug>`  demande de pause, appliquée par SIGSTOP sur ffmpeg
- `l2m:job:<kind>:<slug>`    métadonnées d'affichage (mp3/mp4, demandeur, date)
- `l2m:pct:<kind>:<slug>`    avancement 0-100 (clé à part : réécrite à chaque
  pour-cent, elle ne doit pas faire relire/réécrire le blob de métadonnées)
"""
from __future__ import annotations

import json
import logging
import os
import time

import redis
from rq import Queue
from rq.job import Job
from rq.registry import StartedJobRegistry

log = logging.getLogger(__name__)

QUEUE_NAME = "live2mp3"
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Natures de job. `render` = audio + métadonnées + pochettes + image disque
# (rapide, l'utilisateur attend) ; `video` = ré-encodage du MP4 (lent, en fond).
KIND_RENDER = "render"
KIND_VIDEO = "video"
KINDS = (KIND_RENDER, KIND_VIDEO)
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


# Deux connexions volontairement distinctes :
# - `rq_conn()` sans décodage, car RQ sérialise ses jobs en binaire ; une
#   connexion `decode_responses=True` fait échouer Job.fetch silencieusement
#   (et donc le garde-fou anti-doublon, qui laissait alors passer deux rendus).
# - `conn()` avec décodage pour nos propres clés (drapeaux, métadonnées JSON).
_rq_conn: redis.Redis | None = None
_kv_conn: redis.Redis | None = None


def rq_conn() -> redis.Redis:
    global _rq_conn
    if _rq_conn is None:
        _rq_conn = redis.from_url(REDIS_URL)
    return _rq_conn


def conn() -> redis.Redis:
    global _kv_conn
    if _kv_conn is None:
        _kv_conn = redis.from_url(REDIS_URL, decode_responses=True)
    return _kv_conn


def queue() -> Queue:
    return Queue(QUEUE_NAME, connection=rq_conn(), default_timeout=JOB_TIMEOUT)


JOB_PREFIX = "render-"          # conservé : nature `render`
VIDEO_PREFIX = "video-"
_PREFIXES = {KIND_RENDER: JOB_PREFIX, KIND_VIDEO: VIDEO_PREFIX}


def job_id(slug: str, kind: str = KIND_RENDER) -> str:
    """Un job par (slug, nature) : c'est ce qui rend le doublon impossible.

    Pas de deux-points dans l'identifiant : RQ n'accepte que lettres, chiffres,
    tirets et soulignés (les slugs respectent déjà cette contrainte).
    """
    return f"{_PREFIXES[kind]}{slug}"


def _parse_jid(jid: str) -> tuple[str, str] | None:
    """(slug, nature) d'un identifiant de job, None s'il ne nous appartient pas."""
    for kind, prefix in _PREFIXES.items():
        if jid.startswith(prefix):
            return jid[len(prefix):], kind
    return None


# --- Drapeaux d'annulation / pause ----------------------------------------
# Toutes les clés d'état sont indexées par (nature, slug) : le job audio d'un
# album et son job vidéo coexistent, l'un enfilant l'autre.

def _key(name: str, slug: str, kind: str) -> str:
    return f"l2m:{name}:{kind}:{slug}"


def request_cancel(slug: str, kind: str = KIND_RENDER) -> None:
    conn().set(_key("cancel", slug, kind), "1", ex=META_TTL)


def cancel_requested(slug: str, kind: str = KIND_RENDER) -> bool:
    try:
        return conn().exists(_key("cancel", slug, kind)) == 1
    except redis.RedisError:
        return False


def clear_cancel(slug: str, kind: str = KIND_RENDER) -> None:
    conn().delete(_key("cancel", slug, kind))


def set_paused(slug: str, paused: bool, kind: str = KIND_RENDER) -> None:
    if paused:
        conn().set(_key("pause", slug, kind), "1", ex=META_TTL)
    else:
        conn().delete(_key("pause", slug, kind))


def is_paused(slug: str, kind: str = KIND_RENDER) -> bool:
    try:
        return conn().exists(_key("pause", slug, kind)) == 1
    except redis.RedisError:
        return False


# --- Avancement ------------------------------------------------------------

def set_pct(slug: str, pct: float, kind: str = KIND_RENDER) -> None:
    """Avancement 0-100 du job, lu par `/api/render-queue`.

    Clé dédiée plutôt qu'un champ des métadonnées : le ré-encodage vidéo la
    réécrit à chaque pour-cent, il ne doit pas relire/réécrire le blob JSON.
    """
    try:
        conn().set(_key("pct", slug, kind), f"{pct:.1f}", ex=META_TTL)
    except redis.RedisError:
        pass


def get_pct(slug: str, kind: str = KIND_RENDER) -> float | None:
    try:
        raw = conn().get(_key("pct", slug, kind))
        return float(raw) if raw is not None else None
    except (redis.RedisError, ValueError):
        return None


def clear_pct(slug: str, kind: str = KIND_RENDER) -> None:
    conn().delete(_key("pct", slug, kind))


def set_step(slug: str, step: dict, kind: str = KIND_RENDER) -> None:
    """Étape courante du job : `{stage, index, total, pct}`.

    Doublon assumé du flux SSE de `progress.py` : la page des brouillons
    interroge `/api/render-queue` toutes les 5 s sans maintenir de flux ouvert,
    elle a besoin d'un état lisible d'un seul coup plutôt que d'un journal à
    rejouer.
    """
    try:
        conn().set(_key("step", slug, kind), json.dumps(step), ex=META_TTL)
    except redis.RedisError:
        pass


def get_step(slug: str, kind: str = KIND_RENDER) -> dict | None:
    try:
        raw = conn().get(_key("step", slug, kind))
        return json.loads(raw) if raw else None
    except (redis.RedisError, ValueError):
        return None


def clear_step(slug: str, kind: str = KIND_RENDER) -> None:
    conn().delete(_key("step", slug, kind))


# --- Métadonnées d'affichage ----------------------------------------------

def set_meta(slug: str, meta: dict, kind: str = KIND_RENDER) -> None:
    conn().set(_key("job", slug, kind), json.dumps(meta), ex=META_TTL)


def get_meta(slug: str, kind: str = KIND_RENDER) -> dict:
    try:
        raw = conn().get(_key("job", slug, kind))
        return json.loads(raw) if raw else {}
    except (redis.RedisError, ValueError):
        return {}


def clear_meta(slug: str, kind: str = KIND_RENDER) -> None:
    conn().delete(_key("job", slug, kind))


# --- Cycle de vie ----------------------------------------------------------

def fetch(slug: str, kind: str = KIND_RENDER) -> Job | None:
    try:
        return Job.fetch(job_id(slug, kind), connection=rq_conn())
    except Exception as exc:
        # Absence de job est le cas nominal (album jamais rendu) : niveau debug
        # pour ne pas noyer les logs à chaque consultation de la file.
        log.debug("fetch(%s, %s) : %s", slug, kind, exc)
        return None


def active_status(slug: str, kind: str = KIND_RENDER) -> str | None:
    """`queued` | `started` si un job de cette nature occupe le slug, sinon None."""
    job = fetch(slug, kind)
    if job is None:
        return None
    try:
        status = job.get_status(refresh=True)
    except Exception as exc:
        log.warning("statut du rendu %s illisible : %s", slug, exc)
        return None
    return status if status in ("queued", "started", "deferred") else None


def active_kind(slug: str) -> str | None:
    """Nature du job qui occupe ce slug (audio prioritaire), None si aucun.

    L'API n'a pas à savoir laquelle des deux natures tourne pour mettre en
    pause ou annuler : elle agit sur « le rendu de cet album ».
    """
    for kind in KINDS:
        if active_status(slug, kind):
            return kind
    return None


def _enqueue(slug: str, kind: str, func: str, kwargs: dict, meta: dict,
             at_front: bool = False) -> Job:
    # Un job terminé garde son id : sans purge, RQ refuserait de réutiliser
    # `render-<slug>` pour le rendu suivant du même album.
    old = fetch(slug, kind)
    if old is not None:
        try:
            old.delete()
        except Exception as exc:
            # Si la purge échoue, l'enqueue qui suit lèvera sur l'id déjà pris :
            # sans trace, ce refus était impossible à relier à sa cause.
            log.warning("purge du job terminé %s/%s impossible : %s",
                        kind, slug, exc)
    clear_cancel(slug, kind)
    set_paused(slug, False, kind)
    clear_pct(slug, kind)
    set_meta(slug, {"slug": slug, "kind": kind, **meta}, kind)
    return queue().enqueue(
        func, kwargs=kwargs, at_front=at_front,
        job_id=job_id(slug, kind), job_timeout=JOB_TIMEOUT, result_ttl=3600,
    )


def enqueue(slug: str, *, media: str, gap: float, video: bool,
            republish: bool, requested_by: str = "") -> Job:
    """Met le rendu audio en file. Lève DuplicateRender si le slug est occupé.

    `video` n'est plus exécuté ici : il est transmis au job pour qu'il enfile
    le rendu vidéo une fois l'audio terminé (cf. `jobs.render_job`).

    **Enfilé en tête** (`at_front`) : le worker est unique, et un ré-encodage
    vidéo dure vingt minutes. Sans cette priorité, importer un album pendant
    qu'un MP4 attend son tour ferait patienter l'audio derrière lui — soit
    exactement l'attente que le découplage supprime. Les rendus audio sont
    courts : ils ne peuvent pas affamer la vidéo pour autant.
    """
    if active_kind(slug):
        raise DuplicateRender("un rendu est déjà en cours ou en attente")
    return _enqueue(
        slug, KIND_RENDER, "backend.jobs.render_job",
        {"slug": slug, "media": media, "gap": gap,
         "video": video, "republish": republish},
        {"video": bool(video), "media": media, "requested_by": requested_by,
         "queued_at": time.time()},
        at_front=True,
    )


def enqueue_video(slug: str, *, republish: bool, requested_by: str = "") -> Job:
    """Met le rendu vidéo en file, derrière l'audio déjà terminé.

    Appelé depuis le job audio, donc **sans** le garde-fou `active_kind` : à cet
    instant le job audio est encore « started », il bloquerait sa propre suite.
    Le doublon reste impossible côté vidéo : un job vidéo déjà actif refuse.
    """
    if active_status(slug, KIND_VIDEO):
        raise DuplicateRender("un rendu vidéo est déjà en cours ou en attente")
    return _enqueue(
        slug, KIND_VIDEO, "backend.jobs.render_video_job",
        {"slug": slug, "republish": republish},
        {"video": True, "requested_by": requested_by,
         "queued_at": time.time()},
    )


def cancel(slug: str, kind: str | None = None) -> str:
    """Annule un rendu en file (retrait) ou en cours (drapeau + SIGSTOP levé).

    Retourne `queued` ou `started` selon l'état trouvé, `none` si rien.
    Sans `kind`, agit sur le job qui occupe l'album (audio ou vidéo).
    """
    kind = kind or active_kind(slug)
    if kind is None:
        return "none"
    status = active_status(slug, kind)
    if status is None:
        return "none"
    request_cancel(slug, kind)
    # Une pause laisserait le ffmpeg gelé et l'annulation sans effet : le
    # process doit tourner pour observer le drapeau et s'arrêter.
    set_paused(slug, False, kind)
    if status in ("queued", "deferred"):
        job = fetch(slug, kind)
        if job is not None:
            try:
                job.cancel()
                job.delete()
            except Exception as exc:
                # Le drapeau d'annulation est déjà posé : le rendu s'arrêtera
                # de toute façon, on signale seulement le retrait incomplet.
                log.warning("retrait du rendu %s de la file impossible : %s",
                            slug, exc)
        clear_meta(slug, kind)
    return status


def _started_jobs() -> list[tuple[str, str]]:
    """(slug, nature) réellement en cours.

    Le StartedJobRegistry se nettoie paresseusement : un job terminé y reste
    listé un moment. Sans revérifier le statut, la file afficherait des rendus
    fantômes « en cours ».
    """
    try:
        reg = StartedJobRegistry(QUEUE_NAME, connection=rq_conn())
        ids = [i for i in reg.get_job_ids() if _parse_jid(i)]
    except Exception as exc:
        log.warning("registre des rendus en cours illisible : %s", exc)
        return []
    out = []
    for jid in ids:
        try:
            job = Job.fetch(jid, connection=rq_conn())
            if job.get_status(refresh=True) == "started":
                out.append(_parse_jid(jid))
        except Exception as exc:
            log.warning("job %s ignoré dans la file : %s", jid, exc)
            continue
    return out


def _queued_jobs() -> list[tuple[str, str]]:
    try:
        return [p for p in (_parse_jid(i) for i in queue().job_ids) if p]
    except Exception as exc:
        log.warning("file d'attente illisible : %s", exc)
        return []


def listing() -> list[dict]:
    """File courante : le rendu actif en tête, puis les suivants dans l'ordre."""
    out: list[dict] = []
    for slug, kind in _started_jobs():
        meta = get_meta(slug, kind)
        meta.update({"slug": slug, "kind": kind, "pct": get_pct(slug, kind),
                     "step": get_step(slug, kind),
                     "state": "paused" if is_paused(slug, kind) else "running"})
        out.append(meta)
    for slug, kind in _queued_jobs():
        meta = get_meta(slug, kind)
        meta.update({"slug": slug, "kind": kind, "pct": None, "step": None,
                     "state": "queued"})
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
    current = [i for i in q.job_ids if _parse_jid(i)]
    # L'interface raisonne en albums : un slug demandé déplace le job en attente
    # de cet album, quelle que soit sa nature (audio ou vidéo).
    wanted = [jid for s in slugs for jid in (job_id(s, k) for k in KINDS)
              if jid in current]
    # Tout job en attente absent de la demande garde sa place relative, à la fin.
    wanted += [i for i in current if i not in wanted]
    key = q.key
    pipe = rq_conn().pipeline()
    for jid in current:
        pipe.lrem(key, 0, jid)
    for jid in wanted:
        pipe.rpush(key, jid)
    pipe.execute()
    return [_parse_jid(i)[0] for i in wanted]
