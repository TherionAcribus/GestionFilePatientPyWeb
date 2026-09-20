"""Tests de la gestion des secrets par ``config.Config`` (point 5).

Couvre, sans jamais toucher au vrai magasin d'identifiants :
- le secret n'est PAS écrit en clair dans ``settings.json`` lorsqu'un
  magasin sécurisé est disponible ;
- migration automatique d'un ancien fichier contenant le secret en clair ;
- les clés supprimées (``username``/``password`` héritées) sont ignorées et
  disparaissent à la réécriture ;
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

import config as config_mod
import secret_store
from config import Config


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


def _legacy_file(**overrides):
    """Ancien settings.json d'une version avec compte utilisateur."""
    data = {
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
    data.update(overrides)
    return data


def test_fresh_config_has_no_secret_and_writes_nothing_in_clear(store):
    tmp_path = store["_path"]
    cfg = Config()
    # Le code ne fournit AUCUN secret : une borne neuve naît sans (elle
    # refusera de démarrer tant qu'il n'est pas configuré).
    assert cfg.settings.app_secret == ""
    # ...et le fichier ne contient évidemment rien en clair, ni les clés
    # d'identifiants supprimées.
    data = _read_json(tmp_path)
    assert data["app_secret"] == ""
    assert "username" not in data
    assert "password" not in data


def test_legacy_plaintext_secret_is_migrated_and_erased(store):
    tmp_path = store["_path"]
    # Ancien fichier avec secret en clair + identifiants hérités.
    with open(tmp_path / "settings.json", "w", encoding="utf-8") as f:
        json.dump(_legacy_file(), f)

    cfg = Config()
    # Migré vers le magasin sécurisé.
    assert store["app_secret"] == "secret-en-clair"
    # Chargé en mémoire.
    assert cfg.settings.app_secret == "secret-en-clair"
    # Fichier réécrit : secret effacé, clés supprimées disparues (inconnues).
    data = _read_json(tmp_path)
    assert data["app_secret"] == ""
    assert "username" not in data
    assert "password" not in data


def test_first_run_preserves_existing_keyring_secret(store):
    """Fichier absent mais secret déjà dans le magasin : ne pas l'écraser avec
    la valeur par défaut."""
    tmp_path = store["_path"]
    store["app_secret"] = "conserve-secret"
    # Aucun settings.json présent (premier démarrage).
    assert not (tmp_path / "settings.json").exists()

    cfg = Config()
    assert cfg.settings.app_secret == "conserve-secret"
    # Toujours rien en clair dans le fichier créé.
    data = _read_json(tmp_path)
    assert data["app_secret"] == ""


def test_keyring_value_takes_priority_over_file(store):
    tmp_path = store["_path"]
    store["app_secret"] = "secret-keyring"
    with open(tmp_path / "settings.json", "w", encoding="utf-8") as f:
        json.dump(_legacy_file(app_secret="obsolete-en-clair"), f)

    cfg = Config()
    assert cfg.settings.app_secret == "secret-keyring"


def test_production_refuses_cleartext_when_store_unavailable(store, monkeypatch):
    # D'abord une config normale (magasin dispo).
    cfg = Config()
    # Puis le magasin devient indisponible.
    monkeypatch.setattr(secret_store, "available", lambda: False)
    monkeypatch.setattr(secret_store, "set_secret", lambda name, value: False)
    cfg.settings.debug = False  # production
    cfg.settings.app_secret = "nouveau-secret"
    with pytest.raises(secret_store.SecretStoreUnavailableError):
        cfg.save_settings()


def test_dev_fallback_writes_cleartext_with_warning(store, monkeypatch, caplog):
    tmp_path = store["_path"]
    cfg = Config()
    monkeypatch.setattr(secret_store, "available", lambda: False)
    monkeypatch.setattr(secret_store, "set_secret", lambda name, value: False)
    cfg.settings.debug = True  # développement
    cfg.settings.app_secret = "sec-dev"
    with caplog.at_level("WARNING"):
        cfg.save_settings()
    data = _read_json(tmp_path)
    # Repli en clair toléré en dev...
    assert data["app_secret"] == "sec-dev"
    # ...mais jamais silencieux.
    assert any("EN CLAIR" in r.message for r in caplog.records)
