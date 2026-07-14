"""Tests de la gestion des secrets par ``config.Config`` (point 5).

Couvre, sans jamais toucher au vrai magasin d'identifiants :
- les secrets ne sont PAS écrits en clair dans ``settings.json`` lorsqu'un
  magasin sécurisé est disponible ;
- migration automatique d'un ancien fichier contenant des secrets en clair ;
- priorité au magasin sécurisé sur le fichier ;
- refus d'enregistrer en clair en production quand le magasin est indisponible
  (``SecretStoreUnavailableError``) ;
- repli en clair TOLÉRÉ en développement (avec avertissement).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import secret_store  # noqa: E402
import config as config_mod  # noqa: E402
from config import Config, DEFAULT_APP_SECRET  # noqa: E402


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Isolation complète : singleton réinitialisé, chemin de config en tmp,
    magasin sécurisé en mémoire (disponible par défaut)."""
    Config._instance = None
    monkeypatch.setattr(config_mod.Config, "_get_config_path", lambda self: tmp_path)

    mem = {}
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "get_secret", lambda name: mem.get(name, ""))

    def _set(name, value):
        mem[name] = value or ""
        return True

    monkeypatch.setattr(secret_store, "set_secret", _set)
    # store_secrets et _apply_secret_store réutilisent ces fonctions globales.
    mem["_path"] = tmp_path
    return mem


def _read_json(tmp_path):
    with open(tmp_path / "settings.json", encoding="utf-8") as f:
        return json.load(f)


def test_fresh_config_stores_secrets_in_keyring_not_in_file(store):
    tmp_path = store["_path"]
    cfg = Config()
    # Les valeurs par défaut sont bien en mémoire...
    assert cfg.settings.password == "admin"
    assert cfg.settings.app_secret == DEFAULT_APP_SECRET
    # ...déposées dans le magasin sécurisé...
    assert store["password"] == "admin"
    assert store["app_secret"] == DEFAULT_APP_SECRET
    # ...et JAMAIS en clair dans le fichier.
    data = _read_json(tmp_path)
    assert data["password"] == ""
    assert data["app_secret"] == ""


def test_legacy_plaintext_is_migrated_and_erased(store):
    tmp_path = store["_path"]
    # Ancien fichier avec secrets en clair, magasin vide.
    legacy = {
        "base_url": "http://127.0.0.1:5000",
        "debug": True,
        "username": "borne1",
        "password": "motdepasse-en-clair",
        "app_secret": "secret-en-clair",
        "printer_id_vendor": "0x04b8",
        "printer_id_product": "0x0202",
        "printer_model": "TM-T88II",
        "check_paper": True,
    }
    with open(tmp_path / "settings.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    cfg = Config()
    # Migrés vers le magasin sécurisé.
    assert store["password"] == "motdepasse-en-clair"
    assert store["app_secret"] == "secret-en-clair"
    # Chargés en mémoire.
    assert cfg.settings.password == "motdepasse-en-clair"
    assert cfg.settings.app_secret == "secret-en-clair"
    # Effacés du fichier (réécrit).
    data = _read_json(tmp_path)
    assert data["password"] == ""
    assert data["app_secret"] == ""
    # Les valeurs non secrètes restent intactes.
    assert data["username"] == "borne1"


def test_first_run_preserves_existing_keyring_secrets(store):
    """Fichier absent mais secrets déjà dans le magasin : ne pas les écraser
    avec les valeurs par défaut."""
    tmp_path = store["_path"]
    store["password"] = "conserve"
    store["app_secret"] = "conserve-secret"
    # Aucun settings.json présent (premier démarrage).
    assert not (tmp_path / "settings.json").exists()

    cfg = Config()
    assert cfg.settings.password == "conserve"
    assert cfg.settings.app_secret == "conserve-secret"
    # Toujours rien en clair dans le fichier créé.
    data = _read_json(tmp_path)
    assert data["password"] == ""
    assert data["app_secret"] == ""


def test_keyring_value_takes_priority_over_file(store):
    tmp_path = store["_path"]
    store["password"] = "depuis-keyring"
    store["app_secret"] = "secret-keyring"
    on_disk = {
        "base_url": "http://127.0.0.1:5000",
        "debug": True,
        "username": "borne1",
        "password": "obsolete-en-clair",
        "app_secret": "obsolete-en-clair",
        "printer_id_vendor": "0x04b8",
        "printer_id_product": "0x0202",
        "printer_model": "TM-T88II",
        "check_paper": True,
    }
    with open(tmp_path / "settings.json", "w", encoding="utf-8") as f:
        json.dump(on_disk, f)

    cfg = Config()
    assert cfg.settings.password == "depuis-keyring"
    assert cfg.settings.app_secret == "secret-keyring"


def test_production_refuses_cleartext_when_store_unavailable(store, monkeypatch):
    # D'abord une config normale (magasin dispo).
    cfg = Config()
    # Puis le magasin devient indisponible.
    monkeypatch.setattr(secret_store, "available", lambda: False)
    monkeypatch.setattr(secret_store, "set_secret", lambda name, value: False)
    cfg.settings.debug = False  # production
    cfg.settings.password = "nouveau"
    cfg.settings.app_secret = "nouveau-secret"
    with pytest.raises(secret_store.SecretStoreUnavailableError):
        cfg.save_settings()


def test_dev_fallback_writes_cleartext_with_warning(store, monkeypatch, caplog):
    tmp_path = store["_path"]
    cfg = Config()
    monkeypatch.setattr(secret_store, "available", lambda: False)
    monkeypatch.setattr(secret_store, "set_secret", lambda name, value: False)
    cfg.settings.debug = True  # développement
    cfg.settings.password = "pw-dev"
    cfg.settings.app_secret = "sec-dev"
    with caplog.at_level("WARNING"):
        cfg.save_settings()
    data = _read_json(tmp_path)
    # Repli en clair toléré en dev...
    assert data["password"] == "pw-dev"
    assert data["app_secret"] == "sec-dev"
    # ...mais jamais silencieux.
    assert any("EN CLAIR" in r.message for r in caplog.records)
