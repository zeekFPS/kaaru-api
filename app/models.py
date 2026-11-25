"""Modeles de donnees de Kaaru.

Tout ce qui decrit un QR code vit ici : la cible, le style graphique et les
statistiques. Le style est valide a la frontiere de l'API (Pydantic), pour
qu'aucune valeur incoherente n'atteigne le moteur de rendu.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

DotType = Literal["square", "rounded", "dots", "classy", "classy-rounded", "extra-rounded"]
CornerSquareType = Literal["square", "dot", "extra-rounded"]
CornerDotType = Literal["square", "dot"]
GradientType = Literal["linear", "radial"]


def _check_hex(value: str) -> str:
    if not HEX_COLOR.match(value):
        raise ValueError("couleur attendue au format #RRGGBB")
    return value.lower()


class Gradient(BaseModel):
    """Degrade sur les modules. Le serveur ne rend que le premier arret,
    le degrade complet est rendu cote navigateur par qr-code-styling."""

    type: GradientType = "linear"
    rotation: int = Field(default=0, ge=0, le=360)
    color2: str = "#0ea5e9"

    _color2 = field_validator("color2")(_check_hex)


class DotsOptions(BaseModel):
    type: DotType = "rounded"
    color: str = "#111827"
    gradient: Gradient | None = None

    _color = field_validator("color")(_check_hex)


class CornersOptions(BaseModel):
    square: CornerSquareType = "extra-rounded"
    dot: CornerDotType = "dot"
    color: str | None = None

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return _check_hex(value) if value else value


class BackgroundOptions(BaseModel):
    color: str = "#ffffff"

    _color = field_validator("color")(_check_hex)


class LogoOptions(BaseModel):
    # Au-dela de 0.4, la zone de correction d'erreur ne suffit plus en pratique.
    enabled: bool = False
    size: float = Field(default=0.3, ge=0.1, le=0.4)
    excavate: bool = True


class FrameOptions(BaseModel):
    enabled: bool = False
    text: str = Field(default="", max_length=32)
    color: str = "#1d4ed8"

    _color = field_validator("color")(_check_hex)


class QrStyle(BaseModel):
    dots: DotsOptions = Field(default_factory=DotsOptions)
    corners: CornersOptions = Field(default_factory=CornersOptions)
    background: BackgroundOptions = Field(default_factory=BackgroundOptions)
    logo: LogoOptions = Field(default_factory=LogoOptions)
    frame: FrameOptions = Field(default_factory=FrameOptions)


class LinkCreate(BaseModel):
    target_url: HttpUrl
    title: str = Field(default="", max_length=80)
    style: QrStyle = Field(default_factory=QrStyle)


class LinkUpdate(BaseModel):
    target_url: HttpUrl | None = None
    title: str | None = Field(default=None, max_length=80)
    style: QrStyle | None = None
    active: bool | None = None


class Link(BaseModel):
    slug: str
    target_url: str
    title: str = ""
    style: QrStyle = Field(default_factory=QrStyle)
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DayCount(BaseModel):
    day: str
    scans: int


class LinkStats(BaseModel):
    slug: str
    total_scans: int
    first_scan: datetime | None = None
    last_scan: datetime | None = None
    per_day: list[DayCount] = Field(default_factory=list)
    top_referers: list[dict] = Field(default_factory=list)
    top_countries: list[dict] = Field(default_factory=list)
    recent: list[dict] = Field(default_factory=list)


class ScanEvent(BaseModel):
    slug: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    user_agent: str = ""
    referer: str = ""
    country: str = ""


class VerifyResult(BaseModel):
    decoded: list[str]
    expected: str
    ok: bool
    size_px: int
    contrast_ratio: float
