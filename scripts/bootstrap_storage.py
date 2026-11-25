"""Cree les conteneurs, tables et files attendus par l'application.

En production, c'est Terraform qui cree ces ressources. Ce script sert a deux
choses :
- amorcer un environnement local (Azurite) pour tester le vrai chemin Azure ;
- verifier, apres un `terraform apply`, que tout ce que l'application attend
  existe reellement.

Usage :
    python scripts/bootstrap_storage.py            # idempotent
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import ResourceExistsError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.store import AzureRepository  # noqa: E402


def main() -> int:
    settings = get_settings()
    if settings.store_backend != "azure":
        print("STORE_BACKEND n'est pas 'azure' : rien a amorcer.", file=sys.stderr)
        return 1

    repo = AzureRepository(settings)

    for container in (settings.blob_container_logos, settings.blob_container_exports):
        try:
            repo.blob.create_container(container)
            print(f"conteneur cree      : {container}")
        except ResourceExistsError:
            print(f"conteneur existant  : {container}")

    for name in (settings.table_links, settings.table_stats):
        client = repo._table_client(name)
        try:
            client.create_table()
            print(f"table creee         : {name}")
        except ResourceExistsError:
            print(f"table existante     : {name}")

    try:
        repo.queue.create_queue()
        print(f"file creee          : {settings.queue_scans}")
    except ResourceExistsError:
        print(f"file existante      : {settings.queue_scans}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
