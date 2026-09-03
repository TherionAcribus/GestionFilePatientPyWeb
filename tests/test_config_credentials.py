"""Identifiants : aucune valeur par défaut dans le code source (point 3).

Le dépôt livrait ``username="admin"``, ``password="admin"`` et un
``app_secret`` d'exemple : une borne installée sans passer par l'éditeur
démarrait donc avec des accès triviaux. Désormais :

- les champs d'identification naissent VIDES ;
- ``validate()`` refuse une configuration incomplète (la borne ne démarre pas) ;
- une denylist attrape les installations existantes restées sur ces valeurs
  (comparaison insensible à la casse et aux espaces), en production comme en
  développement (refus / avertissement, cf. main.py et editor_logic).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from config import (
    INSECURE_APP_SECRETS,
    INSECURE_PASSWORDS,
    INSECURE_USERNAMES,
    Settings,
)


def _settings(**overrides):
    base = {"base_url": "http://127.0.0.1:5000", "debug": True,
            "username": "borne1", "password": "mdp-propre-borne1",
            "app_secret": "secret-propre-borne1"}
    base.update(overrides)
    return Settings(**base)


# --- Aucune valeur par défaut ----------------------------------------------

def test_source_ships_no_credentials():
    fresh = Settings()
    assert fresh.username == ""
    assert fresh.password == ""
    assert fresh.app_secret == ""


def test_fresh_settings_refuse_to_start():
    """Une borne non configurée doit être REFUSÉE au démarrage (main.py utilise
    validate()), pas démarrer avec des accès triviaux."""
    errors = Settings().validate()
    assert any("utilisateur" in e for e in errors)
    assert any("mot de passe" in e.lower() for e in errors)
    assert any("secret" in e.lower() for e in errors)


def test_fresh_settings_are_flagged_as_insecure():
    assert Settings().has_insecure_default_credentials() is True


# --- Denylist ---------------------------------------------------------------

def test_clean_credentials_are_accepted():
    settings = _settings()
    assert settings.insecure_credentials_reasons() == []
    assert settings.has_insecure_default_credentials() is False


def test_admin_admin_is_refused():
    reasons = _settings(username="admin", password="admin").insecure_credentials_reasons()
    assert len(reasons) == 1
    assert "trivia" in reasons[0].lower()


def test_denylist_is_case_and_space_insensitive():
    settings = _settings(username="  Admin ", password="ADMIN")
    assert settings.has_insecure_default_credentials() is True


def test_example_app_secret_is_refused():
    example = next(s for s in INSECURE_APP_SECRETS if s)
    reasons = _settings(app_secret=example).insecure_credentials_reasons()
    assert len(reasons) == 1
    assert "secret" in reasons[0].lower()


def test_empty_app_secret_is_refused():
    assert _settings(app_secret="").has_insecure_default_credentials() is True


def test_strong_password_saves_a_weak_username():
    """Un nom d'utilisateur banal n'est signalé QUE s'il est associé à un mot de
    passe trivial : « admin » avec un vrai mot de passe reste acceptable."""
    settings = _settings(username="admin", password="mot-de-passe-solide-42")
    assert settings.insecure_credentials_reasons() == []


def test_both_problems_are_reported_together():
    reasons = _settings(username="admin", password="admin",
                        app_secret="").insecure_credentials_reasons()
    assert len(reasons) == 2


def test_wrong_types_do_not_crash_the_check():
    # Les types invalides sont signalés par validate() ; la détection
    # d'identifiants triviaux ne doit pas lever pour autant.
    settings = _settings(username=None, password=123, app_secret=[])
    assert settings.has_insecure_default_credentials() is True


def test_denylists_are_not_empty():
    # Garde-fou : une denylist vidée par erreur désactiverait le garde-fou.
    assert "admin" in INSECURE_USERNAMES
    assert "admin" in INSECURE_PASSWORDS
    assert "" in INSECURE_APP_SECRETS
