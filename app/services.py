"""Regles metier de Kaaru.

Le point central : un QR encode toujours l'URL *courte*, jamais la destination.
C'est ce qui permet de changer la cible apres impression, et c'est aussi ce qui
garde la charge utile assez courte pour que le niveau de correction d'erreur H
reste imprimable.
"""

from __future__ import annotations

import logging
import secrets

from .config import get_settings
from .models import Link, LinkCreate, QrStyle, ScanEvent
from .qr import MIN_CONTRAST, render_png, verify, worst_contrast
from .store import Repository

log = logging.getLogger(__name__)

# Sans 0/o/1/l/i : un slug se lit a voix haute au telephone sans ambiguite.
SLUG_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
SLUG_LENGTH = 7


class LinkNotFound(Exception):
    pass


class StyleRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SlugCollision(Exception):
    pass


def new_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))


def short_url(slug: str, base: str | None = None) -> str:
    """URL courte. Par defaut l'hote de la requete, sinon PUBLIC_BASE_URL."""

    root = (base or get_settings().public_base_url or "http://localhost:8000").rstrip("/")
    return f"{root}/r/{slug}"


def validate_style(style: QrStyle) -> None:
    ratio, culprit = worst_contrast(style)
    if ratio < MIN_CONTRAST:
        raise StyleRejected(
            f"contraste insuffisant ({ratio:.1f}:1) entre {culprit} et le fond "
            f"{style.background.color} : minimum {MIN_CONTRAST}:1 pour rester scannable"
        )


def create_link(repo: Repository, payload: LinkCreate) -> Link:
    validate_style(payload.style)
    for _ in range(5):
        link = Link(slug=new_slug(), target_url=str(payload.target_url), title=payload.title, style=payload.style)
        if repo.get_link(link.slug) is None:
            repo.create_link(link)
            return link
    raise SlugCollision("impossible de generer un slug libre")


def get_link(repo: Repository, slug: str) -> Link:
    link = repo.get_link(slug)
    if link is None:
        raise LinkNotFound(slug)
    return link


def register_scan(repo: Repository, slug: str, user_agent: str, referer: str, country: str) -> None:
    """Depose l'evenement dans la file. Un echec de file ne doit jamais casser
    la redirection : le scan est plus important que sa statistique."""

    event = ScanEvent(slug=slug, user_agent=user_agent[:200], referer=referer[:200], country=country[:8])
    try:
        repo.enqueue_scan(event)
    except Exception:
        log.exception("file indisponible, evenement de scan perdu pour %s", slug)


def render_checked(
    link: Link,
    repo: Repository,
    size_px: int = 640,
    logo: bytes | None = None,
    base: str | None = None,
) -> bytes:
    """Rend et decode. Un style qui ne se decode pas est refuse, pas livre."""

    payload = short_url(link.slug, base)
    png = render_png(payload, link.style, size_px=size_px, logo=logo)
    ok, decoded, _ = verify(png, payload)
    if not ok:
        raise StyleRejected(
            f"le QR genere ne se decode pas (lu: {decoded[:1] or 'rien'}). "
            "Reduisez le logo ou augmentez le contraste."
        )
    return png


def render_preview(style: QrStyle, payload: str, size_px: int = 640, logo: bytes | None = None) -> bytes:
    """Rendu de demonstration, non persiste. Sert a l'editeur et aux tests."""

    validate_style(style)
    png = render_png(payload, style, size_px=size_px, logo=logo)
    ok, decoded, _ = verify(png, payload)
    if not ok:
        raise StyleRejected(
            f"ce style ne se decode pas (lu: {decoded[:1] or 'rien'}). "
            "Reduisez le logo ou augmentez le contraste."
        )
    return png
