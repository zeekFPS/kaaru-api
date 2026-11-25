"""Configuration par variables d'environnement (12-Factor, facteur III).

Aucun secret n'est lu ici : l'acces a Azure Storage passe par
DefaultAzureCredential (Managed Identity en production, azurite ou emulateur en
local). Le backend de stockage est choisi par STORE_BACKEND, ce qui permet de
faire tourner l'application et ses tests sans aucun service externe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str
    store_backend: str
    storage_account_url: str
    storage_connection_string: str
    blob_container_logos: str
    blob_container_exports: str
    table_links: str
    table_stats: str
    queue_scans: str
    public_base_url: str
    max_upload_bytes: int
    log_level: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        service_name=os.getenv("SERVICE_NAME", "kaaru"),
        environment=os.getenv("ENVIRONMENT", "dev"),
        store_backend=os.getenv("STORE_BACKEND", "memory").lower(),
        storage_account_url=os.getenv("STORAGE_ACCOUNT_URL", ""),
        # Uniquement pour le developpement local contre Azurite. En production
        # la variable est vide et DefaultAzureCredential prend le relais.
        storage_connection_string=os.getenv("STORAGE_CONNECTION_STRING", ""),
        blob_container_logos=os.getenv("BLOB_CONTAINER_LOGOS", "logos"),
        blob_container_exports=os.getenv("BLOB_CONTAINER_EXPORTS", "exports"),
        table_links=os.getenv("TABLE_LINKS", "links"),
        table_stats=os.getenv("TABLE_STATS", "scanstats"),
        queue_scans=os.getenv("QUEUE_SCANS", "scan-events"),
        # Override de la base publique. Vide par defaut : l'URL courte est alors
        # construite depuis l'hote de la requete, ce qui evite un cycle Terraform
        # (le FQDN du Container App ne peut pas etre connu avant sa creation).
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
