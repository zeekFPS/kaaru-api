"""Tests du moteur de QR.

Le contrat : tout QR livre doit se decoder a pleine resolution et a la taille
d'une capture de telephone. On teste aussi les refus, parce qu'un style qui ne
se scanne pas est un bug, pas un detail esthetique.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.models import (
    DotsOptions,
    LogoOptions,
    QrStyle,
)
from app.qr import (
    MIN_CONTRAST,
    contrast_ratio,
    decode_at_capture_size,
    decode_png,
    render_png,
    render_svg,
    verify,
    worst_contrast,
)

PAYLOAD = "https://kaaru.local/r/abcd234"


def _logo_png(color: str = "#e11d48", size: int = 128) -> bytes:
    image = Image.new("RGB", (size, size), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_contrast_ratio_black_on_white() -> None:
    assert round(contrast_ratio("#000000", "#ffffff"), 1) == 21.0


def test_contrast_ratio_identical_colors() -> None:
    assert contrast_ratio("#1d4ed8", "#1d4ed8") == pytest.approx(1.0)


def test_worst_contrast_takes_gradient_into_account() -> None:
    from app.models import Gradient

    style = QrStyle(dots=DotsOptions(color="#111827", gradient=Gradient(color2="#fefefe")))
    ratio, culprit = worst_contrast(style)
    assert ratio < MIN_CONTRAST
    assert culprit == "#fefefe"


def test_each_dot_shape_decodes_at_capture_size() -> None:
    for shape in ("square", "rounded", "dots", "classy", "classy-rounded", "extra-rounded"):
        style = QrStyle(dots=DotsOptions(type=shape, color="#111827"))
        png = render_png(PAYLOAD, style, size_px=640)
        assert PAYLOAD in decode_png(png), shape
        assert PAYLOAD in decode_at_capture_size(png), shape


def test_small_export_still_decodes_at_capture_size() -> None:
    style = QrStyle()
    png = render_png(PAYLOAD, style, size_px=160)
    ok, decoded, _ = verify(png, PAYLOAD)
    assert ok, decoded


def test_logo_does_not_break_decoding() -> None:
    style = QrStyle(logo=LogoOptions(enabled=True, size=0.25))
    png = render_png(PAYLOAD, style, size_px=640, logo=_logo_png())
    ok, decoded, _ = verify(png, PAYLOAD)
    assert ok, decoded


def test_verification_detects_wrong_payload() -> None:
    png = render_png(PAYLOAD, QrStyle(), size_px=640)
    ok, _, _ = verify(png, "https://autre.example/r/zzzz")
    assert ok is False


def test_svg_is_vector_and_contains_no_raster() -> None:
    svg = render_svg(PAYLOAD, QrStyle())
    assert svg.lstrip().startswith("<svg")
    assert "<path" in svg
    assert "base64" not in svg


def test_frame_extends_the_canvas_but_keeps_the_code_readable() -> None:
    from app.models import FrameOptions

    plain = Image.open(io.BytesIO(render_png(PAYLOAD, QrStyle(), size_px=640)))
    framed_png = render_png(PAYLOAD, QrStyle(frame=FrameOptions(enabled=True, text="Scannez-moi")), size_px=640)
    framed = Image.open(io.BytesIO(framed_png))
    assert framed.height > plain.height
    assert PAYLOAD in decode_at_capture_size(framed_png)


def test_light_modules_on_dark_background_are_rejected_by_contrast_rule() -> None:
    from app.models import BackgroundOptions

    style = QrStyle(dots=DotsOptions(color="#ffffff"), background=BackgroundOptions(color="#111827"))
    ratio, _ = worst_contrast(style)
    assert ratio >= MIN_CONTRAST  # blanc sur bleu nuit reste valide
    style = QrStyle(dots=DotsOptions(color="#333333"), background=BackgroundOptions(color="#222222"))
    ratio, _ = worst_contrast(style)
    assert ratio < MIN_CONTRAST
