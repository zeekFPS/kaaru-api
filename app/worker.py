"""Worker de Kaaru.

Execute en Container App Job, declenche par la longueur de la file (KEDA
`azure-queue`). Il ne sert aucune requete : son unique role est d'agreger les
evenements de scan et d'ecrire les compteurs dans la table.

Cette separation est ce qui rend la redirection rapide : /r/{slug} depose un
message et rend la main, sans jamais attendre une ecriture de statistiques.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from .store import Repository, get_repository

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kaaru.worker")
# Le SDK Azure journalise chaque appel HTTP en INFO : trop bruyant en job.
logging.getLogger("azure").setLevel(logging.WARNING)

SHUTDOWN = False


def _stop(signum, frame):  # pragma: no cover - gestion de signal
    global SHUTDOWN
    SHUTDOWN = True
    log.info("arret demande, fin du message en cours")


def run_once(repo: Repository | None = None, max_messages: int = 32) -> int:
    """Traite un lot de messages. Retourne le nombre d'evenements agreges."""

    repo = repo or get_repository()
    processed = 0
    for raw, event in repo.dequeue_scans(max_messages=max_messages):
        try:
            repo.record_scan(event)
            processed += 1
        except Exception:
            # On ne supprime pas le message : il sera relivre apres le
            # visibility timeout, donc aucun scan n'est perdu.
            log.exception("echec d'agregation pour %s", event.slug)
            continue
        deleter = getattr(raw, "delete", None)
        if callable(deleter):
            deleter()
    return processed


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    idle_sleep = float(os.getenv("WORKER_IDLE_SLEEP", "5"))
    run_once_limit = os.getenv("WORKER_RUN_ONCE", "").lower() in {"1", "true", "yes"}

    log.info("worker kaaru demarre")
    while not SHUTDOWN:
        try:
            count = run_once()
        except Exception:
            log.exception("erreur pendant le traitement du lot")
            time.sleep(5)
            continue
        if count:
            log.info("%d evenements agreges", count)
        if run_once_limit:
            return 0
        if count == 0:
            time.sleep(idle_sleep)

    log.info("worker arrete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
