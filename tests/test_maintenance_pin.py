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


# --- « Quitter l'application » (fenêtre de maintenance) ----------------------


@pytest.mark.parametrize("shortcuts", [True, False])
def test_quitter_dit_que_le_pc_reste_allume(shortcuts):
    """Le bouton ferme l'application, pas le PC : le texte doit le dire,
    et ne plus parler d'« éteindre »."""
    text = maintenance_pin.quit_confirmation_text(shortcuts)
    assert "Le PC reste allumé" in text
    assert "ne se relancera pas" in text
    assert "teindre" not in text
    assert text.endswith("Quitter l'application ?")


def test_quitter_indique_le_raccourci_quand_il_existe():
    text = maintenance_pin.quit_confirmation_text(True)
    assert "menu des applications" in text
    assert "ouverture de session" in text


def test_quitter_sans_raccourci_ne_promet_pas_de_menu():
    text = maintenance_pin.quit_confirmation_text(False)
    assert "menu des applications" not in text
    assert "redémarrez le PC" in text


# --- Boutons de la fenêtre de maintenance -------------------------------------


def test_choix_de_maintenance_dans_l_ordre():
    """Défaut (Entrée) en tête ; l'action qui arrête la borne en dernier."""
    actions = [c.action for c in maintenance_pin.MAINTENANCE_CHOICES]
    assert actions == [maintenance_pin.MAINTENANCE_ACTION_CONFIGURE,
                       maintenance_pin.MAINTENANCE_ACTION_RESUME,
                       maintenance_pin.MAINTENANCE_ACTION_QUIT]


def test_seul_quitter_est_dangereux():
    dangerous = [c.action for c in maintenance_pin.MAINTENANCE_CHOICES
                 if c.danger]
    assert dangerous == [maintenance_pin.MAINTENANCE_ACTION_QUIT]


def test_chaque_choix_est_explique():
    for choice in maintenance_pin.MAINTENANCE_CHOICES:
        assert choice.label and choice.description
        assert "teindre" not in choice.label + choice.description
    quit_choice = maintenance_pin.MAINTENANCE_CHOICES[-1]
    assert "Le PC reste allumé" in quit_choice.description


# --- Touche plein écran (F11) et code PIN ------------------------------------


def test_f11_libre_sans_pin():
    assert maintenance_pin.fullscreen_toggle_allowed(True, "")
    assert maintenance_pin.fullscreen_toggle_allowed(False, "")


def test_f11_ne_sort_pas_du_plein_ecran_avec_pin():
    stored = maintenance_pin.hash_pin("2468")
    assert not maintenance_pin.fullscreen_toggle_allowed(True, stored)
    # Revenir en plein écran reste toujours permis.
    assert maintenance_pin.fullscreen_toggle_allowed(False, stored)


def test_f11_empreinte_illisible_ne_bloque_pas():
    # Même règle que l'accès maintenance (pin_required) : une empreinte
    # illisible est déjà refusée au démarrage par la validation.
    assert maintenance_pin.fullscreen_toggle_allowed(True, "abimé")


def test_reprendre_ne_redemarre_plus_la_borne():
    """Le menu s'affiche par-dessus la borne : « Reprendre » la laisse
    tourner, sans relance."""
    resume = next(c for c in maintenance_pin.MAINTENANCE_CHOICES
                  if c.action == maintenance_pin.MAINTENANCE_ACTION_RESUME)
    assert "sans redémarrer" in resume.description


def test_texte_d_accueil_du_menu():
    text = maintenance_pin.maintenance_intro_text(60)
    assert "60 s" in text and "en pause" in text


# --- Messages au serveur ------------------------------------------------------------


def test_quitter_annonce_un_arret_volontaire():
    code, message = maintenance_pin.shutdown_server_event(
        maintenance_pin.MAINTENANCE_ACTION_QUIT)
    assert code == "kiosk_stopped"
    assert "quittée" in message


def test_configuration_annonce_une_maintenance():
    code, _message = maintenance_pin.shutdown_server_event(
        maintenance_pin.MAINTENANCE_ACTION_CONFIGURE)
    assert code == "kiosk_maintenance"


def test_fermeture_hors_menu_annonce_une_maintenance():
    code, message = maintenance_pin.shutdown_server_event(None)
    assert code == "kiosk_maintenance"
    assert "redémarrage" in message


def test_codes_reconnus_par_le_serveur():
    """Préfixe kiosk_ : le serveur les journalise sans en faire l'état de
    l'imprimante. Longueur ≤ 40 (colonne error_code)."""
    for code in (maintenance_pin.SERVER_EVENT_MAINTENANCE,
                 maintenance_pin.SERVER_EVENT_STOPPED,
                 maintenance_pin.SERVER_EVENT_PIN_LOCKOUT):
        assert code.startswith("kiosk_") and len(code) <= 40


def test_alerte_codes_pin_sans_le_code():
    message = maintenance_pin.pin_lockout_message(4, "1 min")
    assert "4 fois" in message and "1 min" in message
