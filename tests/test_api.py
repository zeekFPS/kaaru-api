"""Tests de bout en bout de l'API Kaaru.

On teste le parcours complet : creation, redirection, agregation par le worker,
statistiques, changement de cible apres coup, desactivation. Le stockage est en
memoire, donc aucun service Azure n'est necessaire pour faire tourner la CI.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, worker
from app.store import MemoryRepository, set_repository


@pytest.fixture()
def repo() -> MemoryRepository:
    repository = MemoryRepository()
    set_repository(repository)
    return repository


@pytest.fixture()
def client(repo: MemoryRepository) -> TestClient:
    return TestClient(main.app)


def _create(client: TestClient, url: str = "https://example.com/page?x=1") -> dict:
    response = client.post("/api/links", json={"target_url": url, "title": "demo"})
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["storage"] == "memory"


def test_editor_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Kaaru" in response.text


def test_editor_assets_are_served(client: TestClient) -> None:
    response = client.get("/static/vendor/qr-code-styling.js")
    assert response.status_code == 200
    assert "QRCodeStyling" in response.text


def test_create_link_returns_short_url_and_qr(client: TestClient) -> None:
    body = _create(client)
    assert body["short_url"].endswith(body["link"]["slug"])
    assert body["qr_png_url"].endswith("/qr.png")

    png = client.get(f"/api/links/{body['link']['slug']}/qr.png")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).format == "PNG"


def test_short_slug_is_readable_and_not_ambiguous(client: TestClient) -> None:
    slug = _create(client)["link"]["slug"]
    assert len(slug) == 7
    assert not set(slug) & set("01loi")


def test_redirect_goes_to_target(client: TestClient) -> None:
    slug = _create(client, "https://example.com/hello")["link"]["slug"]
    response = client.get(f"/r/{slug}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/hello"
    assert response.headers["x-kaaru-slug"] == slug


def test_scan_event_is_queued_then_aggregated(client: TestClient, repo: MemoryRepository) -> None:
    slug = _create(client)["link"]["slug"]
    client.get(
        f"/r/{slug}",
        headers={"referer": "https://insta.example", "user-agent": "TestPhone"},
        follow_redirects=False,
    )

    assert len(repo.queue) == 1, "un scan doit produire un message de file"
    assert worker.run_once(repo) == 1

    stats = client.get(f"/api/links/{slug}/stats").json()
    assert stats["total_scans"] == 1
    assert stats["per_day"][0]["scans"] == 1
    assert stats["top_referers"][0]["referer"] == "https://insta.example"


def test_stats_lag_until_worker_runs(client: TestClient) -> None:
    slug = _create(client)["link"]["slug"]
    client.get(f"/r/{slug}", follow_redirects=False)
    stats = client.get(f"/api/links/{slug}/stats").json()
    assert stats["total_scans"] == 0, "l'API ne doit jamais ecrire les stats elle-meme"


def test_target_can_change_after_creation_without_changing_the_qr(client: TestClient) -> None:
    body = _create(client, "https://example.com/avant")
    slug = body["link"]["slug"]
    before = client.get(f"/api/links/{slug}/qr.png").content

    response = client.patch(f"/api/links/{slug}", json={"target_url": "https://example.com/apres"})
    assert response.status_code == 200

    assert client.get(f"/r/{slug}", follow_redirects=False).headers["location"] == "https://example.com/apres"
    after = client.get(f"/api/links/{slug}/qr.png").content
    assert before == after, "le QR encode le lien court, il ne doit pas changer"


def test_style_can_be_updated(client: TestClient) -> None:
    slug = _create(client)["link"]["slug"]
    response = client.patch(f"/api/links/{slug}", json={"style": {"dots": {"color": "#0f766e", "type": "dots"}}})
    assert response.status_code == 200
    assert response.json()["link"]["style"]["dots"]["color"] == "#0f766e"


def test_low_contrast_style_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/links",
        json={
            "target_url": "https://example.com",
            "style": {"dots": {"color": "#eeeeee"}, "background": {"color": "#ffffff"}},
        },
    )
    assert response.status_code == 422
    assert "contraste" in response.json()["detail"]


def test_invalid_color_is_refused_by_schema(client: TestClient) -> None:
    response = client.post(
        "/api/links",
        json={"target_url": "https://example.com", "style": {"dots": {"color": "bleu"}}},
    )
    assert response.status_code == 422


def test_deactivated_link_returns_410(client: TestClient) -> None:
    slug = _create(client)["link"]["slug"]
    assert client.delete(f"/api/links/{slug}").status_code == 204
    assert client.get(f"/r/{slug}", follow_redirects=False).status_code == 410


def test_unknown_slug_returns_404(client: TestClient) -> None:
    assert client.get("/r/zzzzzzz", follow_redirects=False).status_code == 404
    assert client.get("/api/links/zzzzzzz").status_code == 404


def test_svg_export(client: TestClient) -> None:
    slug = _create(client)["link"]["slug"]
    response = client.get(f"/api/links/{slug}/qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in response.text


def test_preview_endpoint_rejects_bad_style(client: TestClient) -> None:
    response = client.post(
        "/api/preview",
        data={"payload": "https://example.com", "style": '{"dots": {"color": "#fafafa"}}'},
    )
    assert response.status_code == 422


def test_preview_endpoint_returns_png(client: TestClient) -> None:
    response = client.post(
        "/api/preview",
        data={"payload": "https://example.com", "style": '{"dots": {"color": "#1d4ed8"}}'},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_list_links_returns_created_links(client: TestClient) -> None:
    _create(client)
    _create(client, "https://example.org")
    assert len(client.get("/api/links").json()) == 2


def test_verify_endpoint_decodes_uploaded_image(client: TestClient) -> None:
    slug = _create(client)["link"]["slug"]
    png = client.get(f"/api/links/{slug}/qr.png").content
    short = f"http://localhost:8000/r/{slug}"
    response = client.post(f"/api/verify?expected={short}", files={"file": ("qr.png", png, "image/png")})
    assert response.status_code == 200
    assert response.json()["ok"] is True
