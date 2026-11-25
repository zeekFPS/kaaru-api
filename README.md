# Kaaru · API

Générateur de QR codes **dynamiques** et personnalisables. Le QR encode un lien
court que l'on contrôle : changer la destination ne demande pas de réimprimer.

Ce dépôt contient le service : API FastAPI, moteur de rendu vérifié, worker
d'agrégation des scans. L'infrastructure Azure est dans le dépôt
[`kaaru-infra`](https://github.com/zeekFPS/kaaru-infra), et le rapport du projet
dans [`infra/docs/rapport.md`](https://github.com/zeekFPS/kaaru-infra/blob/main/docs/rapport.md).

## Le problème

Un QR classique encode l'URL finale. Une faute de frappe, un changement de page,
une campagne terminée : il faut réimprimer. Kaaru encode `https://host/r/8f3k2ab`,
résout ce slug côté serveur et redirige. La destination devient une donnée, pas
une gravure.

## Ce qui est vérifié, pas seulement implémenté

| Contrat | Comment il est tenu |
| --- | --- |
| Tout QR livré se scanne | `render_checked()` décode l'image produite (pyzbar) avant de la renvoyer, sinon 422 |
| Il se scanne aussi sur un téléphone | Second décodage à 300 px, la taille réelle d'une capture |
| Un logo ne casse pas le code | Correction d'erreur H imposée + décodage de contrôle |
| Le style reste lisible | Contraste WCAG ≥ 4.5:1 calculé sur chaque couleur, dégradé inclus |
| La redirection reste rapide | Le scan écrit un message en file et rend la main ; aucune statistique n'est calculée dans la requête |
| Le QR ne change jamais | La cible est modifiable, le slug et donc le code sont stables (test dédié) |
| Aucun secret | `DefaultAzureCredential` + identités managées, clé de compte désactivée |
| Un test hors ligne | `STORE_BACKEND=memory` : la CI ne dépend d'aucun service Azure |

## Démarrage local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload
```

Ouvrir http://127.0.0.1:8000 : l'éditeur est servi par la même application.
Documentation interactive : `/docs`.

## Tests

```bash
pytest -v        # 29 tests, aucun service externe
ruff check app tests
```

Le décodage réel des QR demande la bibliothèque système `libzbar` :

```bash
sudo apt-get install -y libzbar0t64   # Debian/Ubuntu
brew install zbar                     # macOS
```

## Vérifier le vrai chemin Azure (Azurite)

```bash
docker run -d --name azurite -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite \
  azurite --skipApiVersionCheck --loose --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0

cp .env.azurite.example .env.azurite && source .env.azurite
python scripts/bootstrap_storage.py
uvicorn app.main:app --port 8001
WORKER_RUN_ONCE=1 python -m app.worker
```

Ce chemin a été exécuté de bout en bout : création, redirection, deux scans,
worker dans un processus séparé, compteurs à jour. Il valide les trois points
d'entrée distincts du SDK (`*.blob.*`, `*.table.*`, `*.queue.*`), l'erreur la
plus fréquente quand on branche Table Storage sur une URL Blob.

## Architecture

```
navigateur ──► éditeur statique (qr-code-styling, rendu local)
     │
     └──► API FastAPI ──► Table links        (source de vérité)
                 │  └──► Blob logos          (logo du QR)
                 └─────► Queue scan-events   (un message par scan)
                                   │
                        job planifié ──► Table scanstats
```

`GET /r/{slug}` fait deux choses : résoudre le lien et déposer un message. Rien
d'autre. L'agrégation est le travail du job, ce qui garde la latence de
redirection indépendante du volume de scans.

### Endpoints

- `POST /api/links` crée un lien, rend et vérifie le QR (422 si le style ne se scanne pas)
- `GET /api/links` liste les liens
- `GET /api/links/{slug}` détail
- `PATCH /api/links/{slug}` change la cible, le style ou l'état
- `DELETE /api/links/{slug}` désactive (410 ensuite, l'historique est conservé)
- `GET /api/links/{slug}/qr.png?size=` export PNG vérifié
- `GET /api/links/{slug}/qr.svg` export vectoriel
- `POST /api/links/{slug}/logo` logo central (2 Mo max, type contrôlé)
- `GET /api/links/{slug}/stats` compteurs agrégés
- `POST /api/preview` aperçu serveur sans création
- `POST /api/verify` décode une image fournie et compare au lien attendu
- `GET /r/{slug}` redirection 302
- `GET /health` état + backend de stockage réellement utilisé

### Choix techniques

**Le rendu vit dans le navigateur, la preuve vit sur le serveur.** L'éditeur
utilise `qr-code-styling` en local pour un aperçu instantané. L'export fait par
le serveur (segno + Pillow) puis est décodé : le style est un paramètre
d'entrée, jamais une garantie.

**Le slug évite les caractères ambigus** (`23456789abcdefghjkmnpqrstuvwxyz`,
7 caractères, ~8,6·10⁹ combinaisons) pour être dictable au téléphone.

**Le worker ne sert aucune requête.** Il agrège par jour et par référent, avec
un compteur par ligne de table : pas de lecture-modification-écriture sur une
ligne unique, donc pas de contention quand les scans arrivent en rafale.

## Limites assumées

- Le backend mémoire ne partage rien entre processus : pour voir les
  statistiques bouger en local, il faut le backend Azure (Azurite), pas des
  workers séparés en mémoire.
- Les exports SVG ne contiennent ni logo ni bandeau (le format est vectoriel,
  le logo est un raster) : le PNG est la version de référence.
- Les statistiques sont agrégées avec un délai d'un passage de job (2 minutes
  en production).
