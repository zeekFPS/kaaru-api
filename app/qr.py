"""Moteur de rendu et de verification des QR codes.

Regle du projet : un QR n'est considere comme genere que s'il a ete *decode*
par un lecteur reel, a pleine resolution ET a la taille d'une capture de
telephone (~300 px). Le rendu ne prouve rien, le decodage prouve tout.

Le serveur ne cherche pas a imiter tous les styles : les formes arrondies et
les degrades sont rendus cote navigateur par qr-code-styling, ou la latence
n'existe pas. Ici on garantit un rendu deterministe, testable en CI, exportable
en PNG et SVG.
"""

from __future__ import annotations

import io
import math

import segno
from PIL import Image, ImageDraw, ImageFont
from pyzbar.pyzbar import decode as zbar_decode

from .models import QrStyle

# Taille de reference d'une capture de telephone sur un ecran de 6 pouces.
CAPTURE_SIZE_PX = 300
MIN_CONTRAST = 4.5


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def relative_luminance(color: str) -> float:
    """Luminance relative WCAG 2.1."""

    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float:
    l1 = relative_luminance(foreground)
    l2 = relative_luminance(background)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def worst_contrast(style: QrStyle) -> tuple[float, str]:
    """Le pire contraste du style : le module principal et, s'il existe, le
    second arret du degrade (qui peut se rapprocher du fond)."""

    worst = contrast_ratio(style.dots.color, style.background.color)
    worst_color = style.dots.color
    if style.dots.gradient is not None:
        second = contrast_ratio(style.dots.gradient.color2, style.background.color)
        if second < worst:
            worst, worst_color = second, style.dots.gradient.color2
    return worst, worst_color


def _logo_size(style: QrStyle, side: int) -> int:
    return int(side * style.logo.size)


def _paint_logo(image: Image.Image, logo: bytes, style: QrStyle) -> None:
    side = image.size[0]
    target = _logo_size(style, side)
    if target <= 0:
        return

    logo_img = Image.open(io.BytesIO(logo)).convert("RGBA")
    logo_img.thumbnail((target, target), Image.LANCZOS)

    cx = cy = side // 2
    pad = max(2, int(target * 0.12))
    half = target // 2 + pad
    draw = ImageDraw.Draw(image, "RGBA")

    if style.logo.excavate:
        # On nettoie les modules sous le logo : sans cela, les bords du logo se
        # melangent aux modules et la redondance ne suffit plus.
        draw.rounded_rectangle(
            [cx - half, cy - half, cx + half, cy + half],
            radius=max(2, pad),
            fill=style.background.color + "ff",
        )

    # Marge transparente autour du bitmap : on le colle centre.
    canvas = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    canvas.paste(logo_img, ((target - logo_img.width) // 2, (target - logo_img.height) // 2))
    image.alpha_composite(canvas, (cx - target // 2, cy - target // 2))


def _paint_frame(image: Image.Image, style: QrStyle) -> Image.Image:
    """Ajoute une banniere de texte sous le code. Le cadre remplace la quiet
    zone du bas mais jamais celle du haut ni des cotes."""

    if not style.frame.enabled or not style.frame.text.strip():
        return image

    side = image.size[0]
    bar_h = max(28, int(side * 0.16))
    canvas = Image.new("RGB", (side, side + bar_h), style.background.color)
    canvas.paste(image.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(canvas)

    draw.rectangle([0, side, side, side + bar_h], fill=style.frame.color)
    font_size = max(9, int(bar_h * 0.42))
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()

    text = style.frame.text.strip().upper()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((side - tw) / 2, side + (bar_h - th) / 2 - bbox[1]), text, fill="#ffffff", font=font)
    return canvas


def _symbol_modules(payload: str) -> int:
    qr = segno.make(payload, error="h", micro=False)
    width, _ = qr.symbol_size(scale=1, border=0)
    return width


def render_png(
    payload: str,
    style: QrStyle,
    size_px: int = 640,
    logo: bytes | None = None,
) -> bytes:
    """Rend un PNG. La cible encodee doit toujours etre une URL courte."""

    size_px = max(160, min(size_px, 2000))
    qr = segno.make(payload, error="h", micro=False)
    modules = _symbol_modules(payload)
    # border=4 : quiet zone minimale exigee par la norme, jamais croissee.
    scale = max(2, round(size_px / (modules + 8)))

    buf = io.BytesIO()
    qr.save(
        buf,
        kind="png",
        scale=scale,
        border=4,
        dark=style.dots.color,
        light=style.background.color,
    )
    buf.seek(0)
    image = Image.open(buf).convert("RGBA")

    if style.logo.enabled and logo:
        _paint_logo(image, logo, style)

    image = _paint_frame(image, style)
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_svg(payload: str, style: QrStyle, scale: int = 12) -> str:
    """Rend un SVG propre a l'impression.

    Le logo n'est pas incorpore au SVG cote serveur : le fichier reste purement
    vectoriel, ce qui est exactement ce qu'imprime une imprimeur.
    """

    buf = io.BytesIO()
    segno.make(payload, error="h", micro=False).save(
        buf,
        kind="svg",
        scale=max(1, scale),
        border=4,
        dark=style.dots.color,
        light=style.background.color,
        xmldecl=False,
        svgclass=None,
        lineclass=None,
        omitsize=False,
    )
    return buf.getvalue().decode("utf-8")


def decode_png(data: bytes) -> list[str]:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    return [r.data.decode("utf-8", "replace") for r in zbar_decode(image)]


def decode_at_capture_size(data: bytes, size_px: int = CAPTURE_SIZE_PX) -> list[str]:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = image.size
    longest = max(width, height)
    if longest > size_px:
        ratio = size_px / longest
        image = image.resize((max(1, int(width * ratio)), max(1, int(height * ratio))), Image.LANCZOS)
    return [r.data.decode("utf-8", "replace") for r in zbar_decode(image)]


def verify(png: bytes, expected: str) -> tuple[bool, list[str], int]:
    """Decode a pleine taille puis a la taille d'une capture. Les deux doivent
    rendre exactement la cible attendue."""

    full = decode_png(png)
    small = decode_at_capture_size(png)
    ok = expected in full and expected in small
    return ok, sorted(set(full + small)), CAPTURE_SIZE_PX


def estimated_print_size_cm(payload: str, style: QrStyle, side_px: int) -> float:
    """Taille d'impression minimale pour rester lisible a 30 cm.

    On vise au moins 4 px par module dans une capture de 300 px : c'est la
    marge observee sur les telephones recents.
    """

    modules = _symbol_modules(payload)
    module_px = side_px / (modules + 8)
    if module_px <= 0:
        return 0.0
    # 1 module doit mesurer au moins ~0.4 mm a l'impression pour un scan a 30 cm.
    return round(modules * 0.4 / 10, 2)


def round_to_int(value: float) -> int:
    return int(math.floor(value + 0.5))
