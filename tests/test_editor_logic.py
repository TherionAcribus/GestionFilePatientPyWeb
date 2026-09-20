"""Logique pure de l'éditeur de configuration de la borne (point 17).

Couvre, sans construire d'interface tkinter :
- la détection des modifications non enregistrées (values_differ) ;
- le refus du secret d'application trivial hors mode développement
  explicitement activé (default_credentials_error) et l'avertissement associé
  (default_credentials_warning).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import editor_logic
from config import INSECURE_APP_SECRETS, Settings

# --- values_differ ---------------------------------------------------------

def test_identical_forms_are_not_dirty():
    loaded = {"app_secret": "borne1", "debug": False}
    assert editor_logic.values_differ(loaded, dict(loaded)) is False


def test_changed_value_marks_dirty():
    loaded = {"app_secret": "borne1", "debug": False}
    current = {"app_secret": "borne2", "debug": False}
    assert editor_logic.values_differ(loaded, current) is True


def test_changed_bool_marks_dirty():
    loaded = {"app_secret": "borne1", "debug": False}
    current = {"app_secret": "borne1", "debug": True}
    assert editor_logic.values_differ(loaded, current) is True


def test_different_keys_marks_dirty():
    assert editor_logic.values_differ({"a": 1}, {"a": 1, "b": 2}) is True


# --- default_credentials_error --------------------------------------------

def _secure_settings(**overrides):
    base = {"app_secret": "real-secret", "debug": False}
    base.update(overrides)
    return Settings(**base)


def test_secure_secret_never_blocked():
    # Secret propre, en production : aucun refus.
    assert editor_logic.default_credentials_error(_secure_settings()) is None


def test_example_app_secret_refused_in_production():
    # Secret repris de l'exemple de configuration : refusé en production.
    example = next(s for s in INSECURE_APP_SECRETS if s)
    settings = _secure_settings(app_secret=example)
    assert editor_logic.default_credentials_error(settings) is not None


def test_empty_app_secret_refused_in_production():
    settings = _secure_settings(app_secret="")
    assert editor_logic.default_credentials_error(settings) is not None


def test_default_secret_allowed_in_dev_mode():
    # debug=True => mode développement explicitement activé : accepté.
    settings = _secure_settings(app_secret="", debug=True)
    assert editor_logic.default_credentials_error(settings) is None


# --- default_credentials_warning ------------------------------------------

def test_no_warning_when_secret_is_custom():
    assert editor_logic.default_credentials_warning(_secure_settings()) is None


def test_warning_in_production_mentions_refusal():
    settings = _secure_settings(app_secret="")
    msg = editor_logic.default_credentials_warning(settings)
    assert msg is not None
    assert "REFUS" in msg.upper()


def test_warning_in_dev_mode_is_advisory():
    settings = _secure_settings(app_secret="", debug=True)
    msg = editor_logic.default_credentials_warning(settings)
    assert msg is not None
    assert "REFUS" not in msg.upper()
