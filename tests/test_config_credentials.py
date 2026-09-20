"""Secret applicatif : aucune valeur par défaut dans le code source (point 3).

La borne ne s'authentifie plus que par son identité machine (``app_secret`` ->
jeton applicatif -> ticket de session patient) : les champs ``username`` et
``password`` ont été supprimés. Le dépôt ne livre donc :

- aucun secret par défaut (``app_secret`` naît VIDE) ;
- ``validate()`` refuse une configuration incomplète (la borne ne démarre pas) ;
- une denylist attrape les installations existantes restées sur la valeur
  d'exemple (comparaison insensible à la casse et aux espaces), en production
  comme en développement (refus / avertissement, cf. main.py et editor_logic).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from config import (
    INSECURE_APP_SECRETS,
    Settings,
)


def _settings(**overrides):
    base = {"base_url": "http://127.0.0.1:5000", "debug": True,
            "app_secret": "secret-propre-borne1"}
    base.update(overrides)
    return Settings(**base)


# --- Aucune valeur par défaut ----------------------------------------------

def test_source_ships_no_secret():
    fresh = Settings()
    assert fresh.app_secret == ""


def test_no_username_or_password_fields():
    """Régression : la borne ne doit plus connaître de compte utilisateur —
    l'authentification passe par le ticket de session (identité machine)."""
    fresh = Settings()
    assert not hasattr(fresh, "username")
    assert not hasattr(fresh, "password")


def test_fresh_settings_refuse_to_start():
    """Une borne non configurée doit être REFUSÉE au démarrage (main.py utilise
    validate()), pas démarrer avec un accès trivial."""
    errors = Settings().validate()
    assert any("secret" in e.lower() for e in errors)


def test_fresh_settings_are_flagged_as_insecure():
    assert Settings().has_insecure_default_credentials() is True


# --- Denylist ---------------------------------------------------------------

def test_clean_secret_is_accepted():
    settings = _settings()
    assert settings.insecure_credentials_reasons() == []
    assert settings.has_insecure_default_credentials() is False


def test_example_app_secret_is_refused():
    example = next(s for s in INSECURE_APP_SECRETS if s)
    reasons = _settings(app_secret=example).insecure_credentials_reasons()
    assert len(reasons) == 1
    assert "secret" in reasons[0].lower()


def test_empty_app_secret_is_refused():
    assert _settings(app_secret="").has_insecure_default_credentials() is True


def test_denylist_is_case_and_space_insensitive():
    example = next(s for s in INSECURE_APP_SECRETS if s)
    settings = _settings(app_secret=f"  {example.upper()} ")
    assert settings.has_insecure_default_credentials() is True


def test_wrong_types_do_not_crash_the_check():
    # Les types invalides sont signalés par validate() ; la détection
    # de secret trivial ne doit pas lever pour autant.
    settings = _settings(app_secret=[])
    assert settings.has_insecure_default_credentials() is True


def test_denylist_is_not_empty():
    # Garde-fou : une denylist vidée par erreur désactiverait le garde-fou.
    assert "" in INSECURE_APP_SECRETS
