"""Tests du magasin de secrets de la borne (secret_store).

Vérifie, sans toucher au vrai gestionnaire d'identifiants du système :
- ``store_secrets`` renvoie ``False`` (sans rien écrire) quand aucun magasin
  sécurisé n'est disponible — pas de repli silencieux ;
- aller-retour store/load lorsque le magasin est disponible ;
- ``load_secrets`` omet les valeurs vides.

On monkeypatche la couche interne (available/get_secret/set_secret) pour ne
jamais dépendre d'un backend réel.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import secret_store  # noqa: E402
from secret_store import store_secrets, load_secrets, SECRET_FIELDS  # noqa: E402


@pytest.fixture
def fake_store(monkeypatch):
    """Magasin sécurisé en mémoire (disponible)."""
    store = {}
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "get_secret", lambda name: store.get(name, ""))

    def _set(name, value):
        store[name] = value or ""
        return True

    monkeypatch.setattr(secret_store, "set_secret", _set)
    return store


@pytest.fixture
def unavailable(monkeypatch):
    """Aucun magasin sécurisé : available() False, set_secret échoue."""
    calls = {"set": 0}
    monkeypatch.setattr(secret_store, "available", lambda: False)
    monkeypatch.setattr(secret_store, "get_secret", lambda name: "")

    def _set(name, value):
        calls["set"] += 1
        return False

    monkeypatch.setattr(secret_store, "set_secret", _set)
    return calls


def test_secret_fields_are_the_two_secrets():
    assert set(SECRET_FIELDS) == {"password", "app_secret"}


def test_store_and_load_roundtrip(fake_store):
    ok = store_secrets({"password": "pw", "app_secret": "sec"})
    assert ok is True
    assert fake_store == {"password": "pw", "app_secret": "sec"}
    assert load_secrets() == {"password": "pw", "app_secret": "sec"}


def test_store_secrets_returns_false_and_writes_nothing_when_unavailable(unavailable):
    ok = store_secrets({"password": "pw", "app_secret": "sec"})
    assert ok is False
    # available() False => on ne tente MÊME PAS d'écrire (pas de repli).
    assert unavailable["set"] == 0


def test_load_secrets_omits_empty(fake_store):
    fake_store["password"] = "pw"
    fake_store["app_secret"] = ""
    assert load_secrets() == {"password": "pw"}


def test_store_secrets_ignores_unknown_fields(fake_store):
    store_secrets({"password": "pw", "not_a_secret": "x"})
    assert "not_a_secret" not in fake_store
    assert fake_store.get("password") == "pw"
