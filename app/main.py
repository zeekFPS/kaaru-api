"""API Kaaru.

Trois responsabilites, et rien de plus :
1. gerer les liens courts et leur style (CRUD) ;
2. generer des QR verifies par decodage ;
3. rediriger les scans, en deposant l'evenement dans la file pour le worker.

Le rendu d'un QR ne passe jamais par la file : c'est du calcul pur, synchrone
et sans etat. La file sert uniquement a absorber les scans, qui arrivent en
rafale et ne doivent jamais ralentir la redirection.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .models import Link, LinkCreate, LinkStats, LinkUpdate, QrStyle, VerifyResult
from .qr import CAPTURE_SIZE_PX, MIN_CONTRAST, contrast_ratio, decode_at_capture_size, decode_png, render_svg
from .services import (
    LinkNotFound,
    StyleRejected,
    create_link,
    get_link,
    register_scan,
    render_checked,
    render_preview,
    short_url,
)
from .store import Repository, get_repository

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("kaaru.api")

app = FastAPI(
    title="Kaaru",
    version="1.0.0",
    description="QR codes personnalisables et dynamiques, avec statistiques de scan.",
)

STATIC = Path(__file__).parent / "static"

# La bibliotheque de rendu cote navigateur est servie localement : aucune
# dependance a un CDN public au moment de la demo.
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def repo() -> Repository:
    return get_repository()


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.service_name,
        "environment": settings.environment,
        "storage": settings.store_backend,
        "version": app.version,
    }


@app.get("/", include_in_schema=False)
def editor() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# --------------------------------------------------------------------------
# Liens
# --------------------------------------------------------------------------


def request_base(request: Request) -> str:
    """Base publique : PUBLIC_BASE_URL si configure, sinon l'hote de la requete.

    Cela evite un cycle Terraform : le FQDN du Container App n'existe qu'apres sa
    creation, il ne peut donc pas etre passe en variable d'environnement.
    """

    return get_settings().public_base_url or str(request.base_url).rstrip("/")


def _link_payload(link: Link, base: str) -> dict:
    return {
        "link": link.model_dump(mode="json"),
        "short_url": short_url(link.slug, base),
        "qr_png_url": f"{base}/api/links/{link.slug}/qr.png",
        "qr_svg_url": f"{base}/api/links/{link.slug}/qr.svg",
        "stats_url": f"{base}/api/links/{link.slug}/stats",
    }


@app.get("/api/links", response_model=list[Link])
def list_links(limit: int = Query(default=50, ge=1, le=200), r: Repository = Depends(repo)) -> list[Link]:
    return r.list_links(limit=limit)


@app.post("/api/links", status_code=201)
def post_link(payload: LinkCreate, request: Request, r: Repository = Depends(repo)) -> dict:
    try:
        link = create_link(r, payload)
    except StyleRejected as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    # Le style est valide, mais on verifie quand meme le rendu reel de bout en
    # bout : c'est la seule preuve qu'un telephone pourra lire le code.
    base = request_base(request)
    try:
        render_checked(link, r, base=base)
    except StyleRejected as exc:
        r.update_link(link.model_copy(update={"active": False}))
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    return _link_payload(link, base)


@app.get("/api/links/{slug}", response_model=Link)
def read_link(slug: str, r: Repository = Depends(repo)) -> Link:
    try:
        return get_link(r, slug)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="lien inconnu") from exc


@app.patch("/api/links/{slug}")
def patch_link(slug: str, payload: LinkUpdate, request: Request, r: Repository = Depends(repo)) -> dict:
    try:
        link = get_link(r, slug)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="lien inconnu") from exc

    update: dict = {}
    if payload.target_url is not None:
        update["target_url"] = str(payload.target_url)
    if payload.title is not None:
        update["title"] = payload.title
    if payload.style is not None:
        update["style"] = payload.style
    if payload.active is not None:
        update["active"] = payload.active
    if not update:
        raise HTTPException(status_code=400, detail="aucun champ a modifier")

    updated = link.model_copy(update=update)
    base = request_base(request)
    if payload.style is not None:
        try:
            render_checked(updated, r, base=base)
        except StyleRejected as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc

    from .store import utcnow

    updated = updated.model_copy(update={"updated_at": utcnow()})
    r.update_link(updated)
    return _link_payload(updated, base)


@app.delete("/api/links/{slug}", status_code=204)
def delete_link(slug: str, r: Repository = Depends(repo)) -> Response:
    try:
        link = get_link(r, slug)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="lien inconnu") from exc
    from .store import utcnow

    r.update_link(link.model_copy(update={"active": False, "updated_at": utcnow()}))
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------


@app.get("/api/links/{slug}/qr.png")
def link_qr_png(
    slug: str,
    request: Request,
    size: int = Query(default=640, ge=160, le=2000),
    r: Repository = Depends(repo),
) -> Response:
    link = get_link(r, slug)
    logo = r.get_logo(slug) if link.style.logo.enabled else None
    try:
        png = render_checked(link, r, size_px=size, logo=logo, base=request_base(request))
    except StyleRejected as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="kaaru-{slug}.png"'},
    )


@app.get("/api/links/{slug}/qr.svg")
def link_qr_svg(slug: str, request: Request, r: Repository = Depends(repo)) -> Response:
    link = get_link(r, slug)
    svg = render_svg(short_url(slug, request_base(request)), link.style)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="kaaru-{slug}.svg"'},
    )


@app.post("/api/links/{slug}/logo")
async def upload_logo(slug: str, file: UploadFile = File(...), r: Repository = Depends(repo)) -> dict:
    try:
        get_link(r, slug)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="lien inconnu") from exc

    settings = get_settings()
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"logo limite a {settings.max_upload_bytes} octets")
    if file.content_type not in {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}:
        raise HTTPException(status_code=415, detail="formats acceptes : png, jpeg, webp, svg")

    import io

    from PIL import Image

    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="fichier image illisible") from exc

    key = r.put_logo(slug, data, file.content_type)
    return {"logo_key": key}


@app.post("/api/preview")
def preview(
    payload: str = Form(...),
    style: str = Form(...),
    logo: UploadFile | None = File(default=None),
) -> Response:
    """Rendu de demonstration, sans creation de lien. Sert a l'editeur."""

    try:
        parsed = QrStyle.model_validate(json.loads(style))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"style invalide : {exc}") from exc

    logo_bytes = logo.file.read() if logo is not None else None
    try:
        png = render_preview(parsed, payload, size_px=640, logo=logo_bytes)
    except StyleRejected as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    return Response(content=png, media_type="image/png")


@app.post("/api/verify", response_model=VerifyResult)
async def verify_image(expected: str = Query(...), file: UploadFile = File(...)) -> VerifyResult:
    """Verifie qu'une image de QR se decode encore, a taille de capture."""

    data = await file.read()
    full = decode_png(data)
    small = decode_at_capture_size(data)
    return VerifyResult(
        decoded=sorted(set(full + small)),
        expected=expected,
        ok=expected in full and expected in small,
        size_px=CAPTURE_SIZE_PX,
        contrast_ratio=0.0,
    )


@app.get("/api/links/{slug}/stats", response_model=LinkStats)
def link_stats(slug: str, r: Repository = Depends(repo)) -> LinkStats:
    try:
        get_link(r, slug)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="lien inconnu") from exc
    return r.get_stats(slug)


# --------------------------------------------------------------------------
# Redirection
# --------------------------------------------------------------------------


@app.get("/r/{slug}", include_in_schema=False)
def redirect(slug: str, request: Request, r: Repository = Depends(repo)) -> Response:
    link = r.get_link(slug)
    if link is None:
        return HTMLResponse(
            "<h1>404</h1><p>Ce lien Kaaru n'existe pas.</p>",
            status_code=404,
        )
    if not link.active:
        return HTMLResponse(
            "<h1>410</h1><p>Ce lien a ete desactive par son proprietaire.</p>",
            status_code=410,
        )

    # Le referent est tronque : on veut la source, pas la query string complete.
    referer = request.headers.get("referer", "")[:200]
    country = request.headers.get("cf-ipcountry", "") or ""
    ua = request.headers.get("user-agent", "")[:200]

    # On enregistre avant la redirection : si la file tombe, le scan est quand
    # meme servi (register_scan ne propage pas ses erreurs).
    register_scan(r, slug, user_agent=ua, referer=referer, country=country)

    response = RedirectResponse(url=link.target_url, status_code=302)
    response.headers["X-Kaaru-Slug"] = slug
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(LinkNotFound)
def _not_found(request: Request, exc: LinkNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "lien inconnu"})


@app.get("/api/contrast")
def contrast(foreground: str, background: str = "#ffffff") -> dict:
    ratio = contrast_ratio(foreground, background)
    return {"ratio": round(ratio, 2), "ok": ratio >= MIN_CONTRAST}
