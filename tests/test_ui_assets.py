"""Tests des écrans HTML et scripts JS extraits de ``main.py`` (point 5).

``main.py`` n'est pas importable en test (il importe ``webview``, absent de
l'environnement CI) : sortir ces ressources dans ``assets/`` + ``ui_assets.py``
les rend justement testables. On vérifie ici ce qui protégeait déjà la borne
quand tout était inline :

- l'échappement des messages de configuration (pas d'injection de balise) ;
- la substitution en UNE passe (une valeur substituée n'est jamais réinterprétée
  comme un marqueur) ;
- les replis quand une ressource est illisible (la borne affiche quelque chose
  plutôt que rien).

Le script de connexion automatique (``assets/login.js``) a été retiré : la
borne n'injecte plus d'identifiants dans le DOM — la session patient est
obtenue par ticket signé navigué directement par la WebView (cf. main.py).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import ui_assets
from ui_assets import AssetError


@pytest.fixture(autouse=True)
def _clear_cache():
    """Le cache de ressources est global : on le vide autour de chaque test."""
    ui_assets._cache.clear()
    yield
    ui_assets._cache.clear()


# --- render ----------------------------------------------------------------

def test_render_replaces_known_placeholders():
    assert ui_assets.render("a __X__ b", {"X": "1"}) == "a 1 b"


def test_render_is_single_pass():
    # La valeur insérée contient elle-même un marqueur : il doit rester
    # littéral (sinon un mot de passe bien choisi pourrait déclencher une
    # seconde substitution).
    out = ui_assets.render("__A__ / __B__", {"A": "__B__", "B": "vrai"})
    assert out == "__B__ / vrai"


def test_render_refuses_unknown_placeholder():
    with pytest.raises(AssetError):
        ui_assets.render("valeur = __INCONNU__", {})


# --- Écran d'erreur de configuration ---------------------------------------

def test_config_error_escapes_messages():
    html = ui_assets.build_config_error_html(["<script>alert(1)</script>"])
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_config_error_lists_every_message():
    html = ui_assets.build_config_error_html(["URL invalide", "Secret vide"])
    assert html.count("<li>") == 2
    assert "URL invalide" in html and "Secret vide" in html


def test_config_error_without_message_still_explains():
    html = ui_assets.build_config_error_html([])
    assert "Configuration invalide" in html


# --- Scripts ---------------------------------------------------------------

def test_kiosk_input_script_injects_cursor_setting():
    assert "var hideCursor = true;" in ui_assets.kiosk_input_script(True)
    assert "var hideCursor = false;" in ui_assets.kiosk_input_script(False)
    # Aucun marqueur ne doit subsister dans le script injecté.
    assert "__HIDE_CURSOR__" not in ui_assets.kiosk_input_script(True)


def test_login_script_is_gone():
    """Régression : aucun mécanisme d'injection d'identifiants ne doit subsister
    — la borne s'authentifie par ticket signé, pas par formulaire."""
    assert not hasattr(ui_assets, "login_script")
    assert not (ui_assets.ASSETS_DIR / "login.js").exists()


def test_scripts_are_idempotent_guarded():
    # Les scripts sont réinjectés à chaque chargement de page : ils doivent être
    # protégés par un drapeau global.
    assert "_contextMenuDisabled" in ui_assets.kiosk_input_script(True)
    assert "_kioskProtected" in ui_assets.kiosk_protection_script()


def test_keyboard_script_binds_f11():
    assert "F11" in ui_assets.keyboard_script()


# --- Robustesse : ressource illisible --------------------------------------

def test_missing_asset_raises_asset_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_assets, "ASSETS_DIR", tmp_path)
    with pytest.raises(AssetError):
        ui_assets.load("offline.html")


def test_offline_screen_falls_back_when_asset_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_assets, "ASSETS_DIR", tmp_path)
    html = ui_assets.offline_html()
    assert "<html" in html.lower()
    assert "Borne indisponible" in html


def test_config_error_falls_back_when_asset_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_assets, "ASSETS_DIR", tmp_path)
    html = ui_assets.build_config_error_html(["<b>URL</b> invalide"])
    assert "<html" in html.lower()
    # Le repli échappe lui aussi les messages.
    assert "<b>URL</b>" not in html
    assert "&lt;b&gt;URL&lt;/b&gt;" in html


def test_missing_script_asset_yields_empty_injection(monkeypatch, tmp_path):
    # Une injection vide est sans effet : la page reste utilisable même si la
    # ressource manque (on ne fait pas échouer le chargement).
    monkeypatch.setattr(ui_assets, "ASSETS_DIR", tmp_path)
    assert ui_assets.kiosk_protection_script() == ""
    assert ui_assets.keyboard_script() == ""


def test_each_placeholder_appears_once_per_asset():
    """Régression : un marqueur cité dans le COMMENTAIRE d'une ressource y était
    substitué lui aussi (le mot de passe se retrouvait dans un commentaire JS,
    d'où il pouvait s'échapper avec une fin de commentaire). Chaque marqueur ne
    doit donc apparaître qu'à son unique point d'insertion."""
    expected = {
        "config_error.html": ["ERROR_ITEMS"],
        "kiosk_input.js": ["HIDE_CURSOR"],
        "offline.html": [],
        "kiosk_protection.js": [],
        "keyboard.js": [],
    }
    for name, placeholders in expected.items():
        content = ui_assets.load(name)
        found = ui_assets._PLACEHOLDER_RE.findall(content)
        assert found == placeholders, f"{name}: marqueurs {found}"


def test_assets_are_cached(monkeypatch, tmp_path):
    asset = tmp_path / "sample.txt"
    asset.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(ui_assets, "ASSETS_DIR", tmp_path)
    assert ui_assets.load("sample.txt") == "v1"
    asset.write_text("v2", encoding="utf-8")
    assert ui_assets.load("sample.txt") == "v1"  # relecture évitée
