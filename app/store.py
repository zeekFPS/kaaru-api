"""Couche d'acces aux donnees de Kaaru.

Deux implementations derriere la meme interface :

- MemoryRepository : tests et developpement local, zero dependance externe ;
- AzureRepository : Blob (logos et exports), Table (liens et agregats de scan)
  et Queue (evenements de scan), sans aucune cle partagee. L'authentification
  passe par DefaultAzureCredential, donc par la Managed Identity en production.

L'API et le worker ne connaissent que l'interface. C'est ce qui rend les tests
executables hors ligne et le passage memoire / Azure transparent.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol

from .config import Settings
from .models import DayCount, Link, LinkStats, ScanEvent

log = logging.getLogger(__name__)


class Repository(Protocol):
    def create_link(self, link: Link) -> None: ...
    def get_link(self, slug: str) -> Link | None: ...
    def update_link(self, link: Link) -> None: ...
    def list_links(self, limit: int = 100) -> list[Link]: ...
    def put_logo(self, slug: str, data: bytes, content_type: str) -> str: ...
    def get_logo(self, slug: str) -> bytes | None: ...
    def enqueue_scan(self, event: ScanEvent) -> None: ...
    def dequeue_scans(self, max_messages: int = 16) -> list[tuple[object, ScanEvent]]: ...
    def record_scan(self, event: ScanEvent) -> None: ...
    def get_stats(self, slug: str) -> LinkStats: ...


class MemoryRepository:
    def __init__(self) -> None:
        self.links: dict[str, Link] = {}
        self.logos: dict[str, bytes] = {}
        self.queue: list[ScanEvent] = []
        self._stats: dict[str, dict] = {}

    # --- liens ---------------------------------------------------------
    def create_link(self, link: Link) -> None:
        self.links[link.slug] = link

    def get_link(self, slug: str) -> Link | None:
        return self.links.get(slug)

    def update_link(self, link: Link) -> None:
        self.links[link.slug] = link

    def list_links(self, limit: int = 100) -> list[Link]:
        items = sorted(self.links.values(), key=lambda link: link.created_at, reverse=True)
        return items[:limit]

    # --- blobs ---------------------------------------------------------
    def put_logo(self, slug: str, data: bytes, content_type: str) -> str:
        key = f"{slug}/{uuid.uuid4().hex}"
        self.logos[key] = data
        return key

    def get_logo(self, slug: str) -> bytes | None:
        for key, data in reversed(list(self.logos.items())):
            if key.startswith(f"{slug}/"):
                return data
        return None

    # --- file ----------------------------------------------------------
    def enqueue_scan(self, event: ScanEvent) -> None:
        self.queue.append(event)

    def dequeue_scans(self, max_messages: int = 16) -> list[tuple[object, ScanEvent]]:
        batch, self.queue = self.queue[:max_messages], self.queue[max_messages:]
        return [(None, event) for event in batch]

    # --- agregats ------------------------------------------------------
    def record_scan(self, event: ScanEvent) -> None:
        state = self._stats.setdefault(
            event.slug,
            {"total": 0, "days": Counter(), "referers": Counter(), "countries": Counter(), "recent": []},
        )
        state["total"] += 1
        state["days"][event.at.date().isoformat()] += 1
        if event.referer:
            state["referers"][event.referer] += 1
        if event.country:
            state["countries"][event.country] += 1
        state["recent"].insert(0, event.model_dump(mode="json"))
        del state["recent"][25:]

    def get_stats(self, slug: str) -> LinkStats:
        state = self._stats.get(slug, {})
        recent = state.get("recent", [])
        return LinkStats(
            slug=slug,
            total_scans=state.get("total", 0),
            first_scan=datetime.fromisoformat(recent[-1]["at"]) if recent else None,
            last_scan=datetime.fromisoformat(recent[0]["at"]) if recent else None,
            per_day=[DayCount(day=day, scans=count) for day, count in sorted(state.get("days", {}).items())],
            top_referers=[{"referer": k, "scans": v} for k, v in state.get("referers", Counter()).most_common(5)],
            top_countries=[{"country": k, "scans": v} for k, v in state.get("countries", Counter()).most_common(5)],
            recent=recent[:10],
        )


class AzureRepository:
    """Blob + Table + Queue sur le meme compte de stockage.

    Aucun acces par cle : le compte a `shared_access_key_enabled = false`, tout
    passe par l'identite managee du composant appelant.
    """

    def __init__(self, settings: Settings) -> None:
        from azure.storage.blob import BlobServiceClient
        from azure.storage.queue import QueueClient

        self.settings = settings
        # Trois services, trois points d'entree distincts. Utiliser l'URL Blob
        # pour les tables ou la file est l'erreur classique : les hotes ne sont
        # pas les memes (*.blob.*, *.table.*, *.queue.*).
        if settings.storage_connection_string:
            # Developpement local contre Azurite : pas d'identite a emprunter.
            self.blob = BlobServiceClient.from_connection_string(settings.storage_connection_string)
            self.queue = QueueClient.from_connection_string(
                settings.storage_connection_string, queue_name=settings.queue_scans
            )
            self._endpoint = None
            self._connection_string = settings.storage_connection_string
        else:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            self.blob = BlobServiceClient(account_url=settings.storage_account_url, credential=credential)
            self.queue = QueueClient(
                account_url=self._service_url("queue"),
                queue_name=settings.queue_scans,
                credential=credential,
            )
            self._connection_string = ""
            self._endpoint = self._service_url("table")

        self.logo_container = self.blob.get_container_client(settings.blob_container_logos)
        self.export_container = self.blob.get_container_client(settings.blob_container_exports)
        self._links = None
        self._stats = None

    def _service_url(self, service: str) -> str:
        """https://acct.blob.core.windows.net -> https://acct.<service>.core.windows.net"""

        return self.settings.storage_account_url.replace(".blob.", f".{service}.", 1)

    # --- clients tables (lazy, pour ne pas payer un appel au demarrage) --
    def _table_client(self, name: str):
        from azure.data.tables import TableClient

        if self._connection_string:
            return TableClient.from_connection_string(self._connection_string, table_name=name)
        from azure.identity import DefaultAzureCredential

        return TableClient(
            endpoint=self._endpoint,
            table_name=name,
            credential=DefaultAzureCredential(),
        )

    @property
    def links_table(self):
        if self._links is None:
            self._links = self._table_client(self.settings.table_links)
        return self._links

    @property
    def stats_table(self):
        if self._stats is None:
            self._stats = self._table_client(self.settings.table_stats)
        return self._stats

    # --- liens ---------------------------------------------------------
    def _link_entity(self, link: Link) -> dict:
        return {
            "PartitionKey": "link",
            "RowKey": link.slug,
            "target_url": link.target_url,
            "title": link.title,
            "style": json.dumps(link.style.model_dump(mode="json")),
            "active": link.active,
            "created_at": link.created_at.isoformat(),
            "updated_at": link.updated_at.isoformat(),
        }

    def _link_from_entity(self, entity: dict) -> Link:
        return Link(
            slug=entity["RowKey"],
            target_url=entity["target_url"],
            title=entity.get("title", ""),
            style=json.loads(entity.get("style") or "{}"),
            active=bool(entity.get("active", True)),
            created_at=datetime.fromisoformat(entity["created_at"]),
            updated_at=datetime.fromisoformat(entity["updated_at"]),
        )

    def create_link(self, link: Link) -> None:
        self.links_table.upsert_entity(self._link_entity(link))

    def get_link(self, slug: str) -> Link | None:
        try:
            return self._link_from_entity(self.links_table.get_entity("link", slug))
        except Exception:
            return None

    def update_link(self, link: Link) -> None:
        self.create_link(link)

    def list_links(self, limit: int = 100) -> list[Link]:
        query = self.links_table.query_entities("PartitionKey eq 'link'")
        items = [self._link_from_entity(e) for e in query]
        return sorted(items, key=lambda link: link.created_at, reverse=True)[:limit]

    # --- blobs ---------------------------------------------------------
    def put_logo(self, slug: str, data: bytes, content_type: str) -> str:
        from azure.storage.blob import ContentSettings

        key = f"{slug}/{uuid.uuid4().hex}"
        self.logo_container.upload_blob(
            name=key,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        return key

    def get_logo(self, slug: str) -> bytes | None:
        try:
            names = sorted(
                (blob.name for blob in self.logo_container.list_blobs(name_starts_with=f"{slug}/")),
                reverse=True,
            )
            if not names:
                return None
            return self.logo_container.download_blob(names[0]).readall()
        except Exception:
            return None

    # --- file ----------------------------------------------------------
    def enqueue_scan(self, event: ScanEvent) -> None:
        self.queue.send_message(json.dumps(event.model_dump(mode="json")))

    def dequeue_scans(self, max_messages: int = 16) -> list[tuple[object, ScanEvent]]:
        out: list[tuple[object, ScanEvent]] = []
        for message in self.queue.receive_messages(
            max_messages=max_messages, visibility_timeout=120, messages_per_page=max_messages
        ):
            try:
                out.append((message, ScanEvent.model_validate_json(message.content)))
            except Exception:
                log.exception("message de file illisible, supprime")
                self.queue.delete_message(message)
        return out

    # --- agregats ------------------------------------------------------
    def record_scan(self, event: ScanEvent) -> None:
        self._bump(event.slug, "total", amount=1, last_scan=event.at.isoformat())
        self._bump(event.slug, f"day:{event.at.date().isoformat()}", amount=1)
        if event.referer:
            self._bump(event.slug, f"ref:{event.referer[:120]}", amount=1)
        if event.country:
            self._bump(event.slug, f"ctry:{event.country[:8]}", amount=1)
        self._push_recent(event)

    def _bump(self, slug: str, row_key: str, amount: int = 1, last_scan: str | None = None) -> None:
        """Increment non atomique, avec relecture : Table Storage ne sait pas
        faire d'increment, et le worker est le seul ecrivain de ces compteurs."""

        from azure.core.exceptions import ResourceNotFoundError

        for _ in range(5):
            try:
                entity = self.stats_table.get_entity(slug, row_key)
                entity["scans"] = int(entity.get("scans", 0)) + amount
                if last_scan:
                    entity["last_scan"] = last_scan
                self.stats_table.update_entity(entity, mode="merge")
                return
            except ResourceNotFoundError:
                try:
                    payload = {"PartitionKey": slug, "RowKey": row_key, "scans": amount}
                    if last_scan:
                        payload["last_scan"] = last_scan
                    self.stats_table.create_entity(payload)
                    return
                except Exception:
                    log.exception("creation du compteur %s/%s impossible", slug, row_key)
                    continue
            except Exception:
                log.exception("mise a jour du compteur %s/%s impossible", slug, row_key)
                continue
        log.warning("increment perdu pour %s/%s", slug, row_key)

    def _push_recent(self, event: ScanEvent) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self.stats_table.get_entity(event.slug, "recent")
        except ResourceNotFoundError:
            entity = {"PartitionKey": event.slug, "RowKey": "recent", "events": "[]"}
        events = json.loads(entity.get("events") or "[]")
        events.insert(0, event.model_dump(mode="json"))
        entity["events"] = json.dumps(events[:25])
        self.stats_table.upsert_entity(entity)

    def get_stats(self, slug: str) -> LinkStats:
        rows = list(self.stats_table.query_entities(f"PartitionKey eq '{slug}'"))
        total = 0
        days: list[DayCount] = []
        referers: list[dict] = []
        countries: list[dict] = []
        recent: list[dict] = []
        last_scan: str | None = None

        for row in rows:
            key = row["RowKey"]
            scans = int(row.get("scans", 0))
            if key == "total":
                total = scans
                last_scan = row.get("last_scan")
            elif key.startswith("day:"):
                days.append(DayCount(day=key[4:], scans=scans))
            elif key.startswith("ref:"):
                referers.append({"referer": key[4:], "scans": scans})
            elif key.startswith("ctry:"):
                countries.append({"country": key[5:], "scans": scans})
            elif key == "recent":
                recent = json.loads(row.get("events") or "[]")

        days.sort(key=lambda item: item.day)
        return LinkStats(
            slug=slug,
            total_scans=total,
            first_scan=datetime.fromisoformat(recent[-1]["at"]) if recent else None,
            last_scan=datetime.fromisoformat(last_scan) if last_scan else None,
            per_day=days,
            top_referers=sorted(referers, key=lambda item: -item["scans"])[:5],
            top_countries=sorted(countries, key=lambda item: -item["scans"])[:5],
            recent=recent[:10],
        )


_repo: Repository | None = None


def build_repository(settings: Settings) -> Repository:
    if settings.store_backend == "azure":
        return AzureRepository(settings)
    return MemoryRepository()


def get_repository() -> Repository:
    global _repo
    if _repo is None:
        from .config import get_settings

        _repo = build_repository(get_settings())
    return _repo


def set_repository(repo: Repository) -> None:
    """Injection utilisee par les tests et par le worker."""
    global _repo
    _repo = repo


def reset_repository() -> None:
    global _repo
    _repo = None


def utcnow() -> datetime:
    return datetime.now(UTC)
