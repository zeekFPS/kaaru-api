"""Configuration des tests.

Les tests doivent etre hermetiques : ils forcent le backend memoire et une base
locale, meme si le shell qui les lance porte des variables d'environnement
Azure (cas frequent quand on vient de tester contre Azurite).
"""

from __future__ import annotations

import os

os.environ["STORE_BACKEND"] = "memory"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8000"
os.environ["ENVIRONMENT"] = "test"
os.environ.pop("STORAGE_CONNECTION_STRING", None)
os.environ.pop("STORAGE_ACCOUNT_URL", None)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
