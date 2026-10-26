"""Code PIN de maintenance : empreinte, vérification, saisie de l'éditeur,
validation de la configuration."""

import pytest

import maintenance_pin
from config import Settings


@pytest.mark.parametrize("pin", ["1234", "000000", "123456789012"])
def test_formats_acceptes(pin):
    assert maintenance_pin.pin_format_error(pin) is None


@pytest.mark.parametrize("pin", ["", "123", "1234567890123", "12a4", "12 34",
                                 "١٢٣٤", None, 1234])
def test_formats_refuses(pin):
    # « ١٢٣٤ » : chiffres arabes, que str.isdigit accepterait seul.
    assert maintenance_pin.pin_format_error(pin)


def test_empreinte_puis_verification():
    stored = maintenance_pin.hash_pin("2468")
    assert maintenance_pin.verify_pin("2468", stored)
    assert not maintenance_pin.verify_pin("2469", stored)
    assert not maintenance_pin.verify_pin("", stored)


def test_le_pin_n_apparait_pas_dans_l_empreinte_et_le_sel_change():
    first = maintenance_pin.hash_pin("2468")
    second = maintenance_pin.hash_pin("2468")
    assert "2468" not in first
    assert first != second  # sel aléatoire
    assert maintenance_pin.verify_pin("2468", second)


def test_hash_pin_refuse_un_format_invalide():
    with pytest.raises(ValueError):
        maintenance_pin.hash_pin("12")


@pytest.mark.parametrize("stored", [
    "", None, 42, "n'importe quoi", "md5$1$00$00",
    "pbkdf2_sha256$x$00$" + "00" * 32,
    "pbkdf2_sha256$1000$zz$" + "00" * 32,
    "pbkdf2_sha256$1000$00$" + "00" * 8,      # empreinte tronquée
    "pbkdf2_sha256$0$00$" + "00" * 32,
])
def test_empreintes_illisibles(stored):
    assert not maintenance_pin.is_valid_hash(stored)
    assert not maintenance_pin.pin_required(stored)
    assert not maintenance_pin.verify_pin("1234", stored)


def test_pin_requis_seulement_si_empreinte_lisible():
    assert maintenance_pin.pin_required(maintenance_pin.hash_pin("1234"))
    assert not maintenance_pin.pin_required("")


# --- Saisie de l'éditeur -----------------------------------------------------

def test_saisie_vide_conserve_le_pin_actuel():
    assert maintenance_pin.resolve_pin_change("", "", False, "actuel") == ("actuel", None)


def test_nouveau_pin_confirme():
    new_hash, error = maintenance_pin.resolve_pin_change("9876", "9876", False, "")
    assert error is None
    assert maintenance_pin.verify_pin("9876", new_hash)


def test_nouveau_pin_prioritaire_sur_la_suppression():
    new_hash, error = maintenance_pin.resolve_pin_change("9876", "9876", True, "x")
    assert error is None and maintenance_pin.verify_pin("9876", new_hash)


def test_confirmation_differente_refusee_et_pin_conserve():
    new_hash, error = maintenance_pin.resolve_pin_change("9876", "9875", False, "actuel")
    assert error and "correspondent" in error
    assert new_hash == "actuel"


def test_confirmation_seule_refusee():
    _, error = maintenance_pin.resolve_pin_change("", "9876", False, "actuel")
    assert error


def test_pin_mal_forme_refuse():
    _, error = maintenance_pin.resolve_pin_change("12", "12", False, "")
    assert error and "chiffres" in error


def test_suppression():
    assert maintenance_pin.resolve_pin_change("", "", True, "actuel") == ("", None)


# --- Validation de la configuration -------------------------------------------

def _valid_settings(**overrides):
    values = {"base_url": "https://serveur.example", "app_secret": "s3cr3t-borne",
              "debug": False}
    values.update(overrides)
    return Settings(**values)


def test_configuration_sans_pin_valide():
    assert _valid_settings().validate() == []


def test_configuration_avec_pin_valide():
    settings = _valid_settings(maintenance_pin_hash=maintenance_pin.hash_pin("1357"))
    assert settings.validate() == []


def test_empreinte_illisible_signalee():
    errors = _valid_settings(maintenance_pin_hash="abimé").validate()
    assert any("PIN de maintenance" in e for e in errors)


def test_empreinte_de_mauvais_type_signalee():
    errors = _valid_settings(maintenance_pin_hash=1234).validate()
    assert any("maintenance_pin_hash" in e for e in errors)
