"""Logique pure de l'éditeur de configuration de la borne (point 17).

Couvre, sans construire d'interface tkinter :
- la détection des modifications non enregistrées (values_differ) ;
- le refus des identifiants par défaut hors mode développement explicitement
  activé (default_credentials_error) et l'avertissement associé
  (default_credentials_warning).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import editor_logic  # noqa: E402
from config import Settings, DEFAULT_APP_SECRET  # noqa: E402


# --- values_differ ---------------------------------------------------------

def test_identical_forms_are_not_dirty():
    loaded = {"username": "borne1", "debug": False}
    assert editor_logic.values_differ(loaded, dict(loaded)) is False


def test_changed_value_marks_dirty():
    loaded = {"username": "borne1", "debug": False}
    current = {"username": "borne2", "debug": False}
    assert editor_logic.values_differ(loaded, current) is True


def test_changed_bool_marks_dirty():
    loaded = {"username": "borne1", "debug": False}
    current = {"username": "borne1", "debug": True}
    assert editor_logic.values_differ(loaded, current) is True


def test_different_keys_marks_dirty():
    assert editor_logic.values_differ({"a": 1}, {"a": 1, "b": 2}) is True


# --- default_credentials_error --------------------------------------------

def _secure_settings(**overrides):
    base = dict(username="borne1", password="s3cret", app_secret="real-secret",
                debug=False)
    base.update(overrides)
    return Settings(**base)


def test_secure_credentials_never_blocked():
    # Identifiants propres, en production : aucun refus.
    assert editor_logic.default_credentials_error(_secure_settings()) is None


def test_default_admin_refused_in_production():
    settings = _secure_settings(username="admin", password="admin")
    msg = editor_logic.default_credentials_error(settings)
    assert msg is not None
    assert "développement" in msg


def test_default_app_secret_refused_in_production():
    settings = _secure_settings(app_secret=DEFAULT_APP_SECRET)
    assert editor_logic.default_credentials_error(settings) is not None


def test_empty_app_secret_refused_in_production():
    settings = _secure_settings(app_secret="")
    assert editor_logic.default_credentials_error(settings) is not None


def test_default_credentials_allowed_in_dev_mode():
    # debug=True => mode développement explicitement activé : accepté.
    settings = _secure_settings(username="admin", password="admin", debug=True)
    assert editor_logic.default_credentials_error(settings) is None


# --- default_credentials_warning ------------------------------------------

def test_no_warning_when_credentials_are_custom():
    assert editor_logic.default_credentials_warning(_secure_settings()) is None


def test_warning_in_production_mentions_refusal():
    settings = _secure_settings(username="admin", password="admin")
    msg = editor_logic.default_credentials_warning(settings)
    assert msg is not None
    assert "REFUS" in msg.upper()


def test_warning_in_dev_mode_is_advisory():
    settings = _secure_settings(username="admin", password="admin", debug=True)
    msg = editor_logic.default_credentials_warning(settings)
    assert msg is not None
    assert "REFUS" not in msg.upper()
