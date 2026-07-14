"""Écriture atomique de la configuration de la borne (point 10).

Couvre :
- l'écriture passe par un fichier temporaire du même dossier puis os.replace
  (aucun fichier temporaire résiduel après succès) ;
- une copie .bak de l'ancienne version est conservée lors d'un remplacement ;
- si l'écriture échoue (os.replace lève), l'ancien objet en mémoire est restauré
  ET le fichier sur disque reste inchangé (pas de configuration à moitié écrite) ;
- le contenu écrit est un JSON valide et complet.

Réutilise la même isolation que test_config_secrets (singleton réinitialisé,
config en tmp, magasin de secrets en mémoire disponible).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import secret_store  # noqa: E402
import config as config_mod  # noqa: E402
from config import Config, Settings  # noqa: E402


@pytest.fixture
def store(monkeypatch, tmp_path):
    Config._instance = None
    monkeypatch.setattr(config_mod.Config, "_get_config_path", lambda self: tmp_path)

    mem = {}
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "get_secret", lambda name: mem.get(name, ""))
    monkeypatch.setattr(secret_store, "set_secret",
                        lambda name, value: (mem.__setitem__(name, value or ""), True)[1])
    mem["_path"] = tmp_path
    return mem


def _read_json(tmp_path):
    with open(tmp_path / "settings.json", encoding="utf-8") as f:
        return json.load(f)


def _new_settings(**overrides):
    base = dict(base_url="http://127.0.0.1:5000", debug=True, username="borne1",
                printer_id_vendor="0x04b8", printer_id_product="0x0202",
                printer_model="TM-T88II")
    base.update(overrides)
    return Settings(**base)


def test_no_leftover_temp_file_after_success(store):
    tmp_path = store["_path"]
    cfg = Config()  # écrit déjà le fichier par défaut
    cfg.save_settings(_new_settings(username="borne-ok"))
    assert not list(tmp_path.glob("settings-*.tmp"))
    assert _read_json(tmp_path)["username"] == "borne-ok"


def test_bak_copy_created_on_overwrite(store):
    tmp_path = store["_path"]
    cfg = Config()                                   # crée settings.json
    cfg.save_settings(_new_settings(username="v1"))  # 1re réécriture
    cfg.save_settings(_new_settings(username="v2"))  # remplace -> .bak = v1
    bak = tmp_path / "settings.json.bak"
    assert bak.exists()
    with open(bak, encoding="utf-8") as f:
        assert json.load(f)["username"] == "v1"
    assert _read_json(tmp_path)["username"] == "v2"


def test_write_failure_restores_previous_and_leaves_file_intact(store, monkeypatch):
    tmp_path = store["_path"]
    cfg = Config()
    cfg.save_settings(_new_settings(username="stable"))
    previous = cfg.settings

    # os.replace échoue : le remplacement atomique n'a pas lieu.
    def _boom(*a, **k):
        raise OSError("disque plein")
    monkeypatch.setattr(config_mod.os, "replace", _boom)

    with pytest.raises(OSError):
        cfg.save_settings(_new_settings(username="jamais-ecrit"))

    # L'objet en mémoire est restauré à l'ancienne configuration...
    assert cfg.settings is previous
    assert cfg.settings.username == "stable"
    # ...le fichier sur disque n'a pas changé...
    assert _read_json(tmp_path)["username"] == "stable"
    # ...et aucun fichier temporaire ne subsiste.
    assert not list(tmp_path.glob("settings-*.tmp"))


def test_written_content_is_valid_and_complete(store):
    tmp_path = store["_path"]
    cfg = Config()
    cfg.save_settings(_new_settings(username="complet", printer_model="TM-T88III"))
    data = _read_json(tmp_path)
    assert data["username"] == "complet"
    assert data["printer_model"] == "TM-T88III"
    # Secrets jamais en clair (magasin disponible).
    assert data["password"] == ""
    assert data["app_secret"] == ""
