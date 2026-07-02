"""Worker RQ — consomme la file des jobs longs (download/whisper/encodage).

Lancé par le conteneur `worker` (docker-compose). Utilise REDIS_URL.
"""
from __future__ import annotations

import os

import redis
from rq import Queue, Worker

LISTEN = ["live2mp3"]


def main() -> None:  # pragma: no cover - point d'entrée conteneur
    conn = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    worker = Worker([Queue(name, connection=conn) for name in LISTEN],
                    connection=conn)
    worker.work(with_scheduler=True)


if __name__ == "__main__":  # pragma: no cover
    main()
