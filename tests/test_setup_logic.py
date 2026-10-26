"""Assistant de premier lancement : logique pure (étapes, configuration
finale, récapitulatif, détection USB) et marqueur ``setup_completed``."""

import json

import pytest

import config as config_mod
import maintenance_pin
import secret_store
import setup_logic
from config import Config, Settings
from setup_logic import TEST_FAILED, TEST_NOT_RUN, TEST_OK


def _values(**overrides):
    values = {
        "base_url": "https://flowelis.example", "app_secret": "s3cr3t-borne",
        "printer_id_vendor": "0x04b8", "printer_id_product": "0x0202",
        "printer_model": "TM-T88II", "check_paper": True, "borne_id": "",
        "fullscreen": True, "hide_cursor": True, "debug": False,
        "pin_new": "", "pin_confirm": "", "pin_remove": False,
    }
    values.update(overrides)
    return values


# --- Valeurs de départ ----------------------------------------------------------

def test_premier_lancement_propose_le_mode_kiosque():
    # Valeurs par défaut du code : fenêtre, debug — pas une borne en service.
    values = setup_logic.initial_values(Settings())
    assert values["fullscreen"] is True
    assert values["hide_cursor"] is True
    assert values["debug"] is False
    assert values["pin_new"] == "" and values["pin_remove"] is False
    # L'URL de développement du code n'est pas proposée sur une vraie borne.
    assert values["base_url"] == ""


def test_assistant_relance_garde_les_choix_existants():
    settings = Settings(fullscreen=False, debug=True, setup_completed=True,
                        printer_id_vendor="0x0519")
    values = setup_logic.initial_values(settings)
    assert values["fullscreen"] is False and values["debug"] is True
    assert values["printer_id_vendor"] == "0x0519"


# --- Étapes ------------------------------------------------------------------------

def test_etape_serveur_complete():
    assert setup_logic.step_errors("server", _values()) == []


def test_etape_serveur_accepte_http_local_ou_distant():
    # La règle HTTPS dépend du mode choisi plus loin : vérifiée à la fin.
    values = _values(base_url="http://192.168.1.10:5000")
    assert setup_logic.step_errors("server", values) == []


@pytest.mark.parametrize("url", ["", "flowelis.example", "ftp://x.example"])
def test_etape_serveur_refuse_une_url_mal_formee(url):
    assert setup_logic.step_errors("server", _values(base_url=url))


def test_etape_serveur_exige_le_secret():
    errors = setup_logic.step_errors("server", _values(app_secret="   "))
    assert any("secret" in e for e in errors)


def test_etape_imprimante():
    assert setup_logic.step_errors("printer", _values()) == []
    assert setup_logic.step_errors("printer", _values(printer_id_vendor="zz"))
    assert setup_logic.step_errors("printer", _values(printer_model=" "))


def test_etape_borne_verifie_le_pin():
    assert setup_logic.step_errors("borne", _values()) == []
    assert setup_logic.step_errors("borne", _values(pin_new="1234",
                                                    pin_confirm="4321"))
    assert setup_logic.step_errors("borne", _values(pin_new="12", pin_confirm="12"))


# --- Configuration finale ---------------------------------------------------------------

def test_configuration_finale_marquee_configuree():
    settings, errors = setup_logic.build_settings(
        Settings(), _values(base_url="  https://flowelis.example/  ",
                            app_secret=" s3cr3t-borne\n"))
    assert errors == []
    assert settings.setup_completed is True
    assert settings.base_url == "https://flowelis.example/"
    assert settings.app_secret == "s3cr3t-borne"     # copier-coller nettoyé
    assert settings.fullscreen and settings.hide_cursor and not settings.debug


def test_configuration_finale_avec_pin():
    settings, _ = setup_logic.build_settings(
        Settings(), _values(pin_new="2468", pin_confirm="2468"))
    assert maintenance_pin.verify_pin("2468", settings.maintenance_pin_hash)


def test_pin_existant_conserve_sans_saisie():
    current = Settings(maintenance_pin_hash=maintenance_pin.hash_pin("1357"))
    settings, _ = setup_logic.build_settings(current, _values())
    assert settings.maintenance_pin_hash == current.maintenance_pin_hash


def test_http_distant_refuse_en_production_avec_explication():
    settings, errors = setup_logic.build_settings(
        Settings(), _values(base_url="http://192.168.1.10:5000"))
    assert settings is None
    assert any("Mode développement" in e for e in errors)


def test_http_distant_accepte_en_mode_developpement():
    settings, errors = setup_logic.build_settings(
        Settings(), _values(base_url="http://192.168.1.10:5000", debug=True))
    assert errors == [] and settings.debug


def test_http_local_accepte_en_production():
    _, errors = setup_logic.build_settings(
        Settings(), _values(base_url="http://localhost:5000"))
    assert errors == []


def test_secret_d_exemple_refuse_en_production():
    settings, errors = setup_logic.build_settings(
        Settings(), _values(app_secret="changeme"))
    assert settings is None and errors


def test_champs_hors_assistant_conserves():
    current = Settings(borne_id="ancien")
    settings, _ = setup_logic.build_settings(current, _values(borne_id="accueil-1"))
    assert settings.borne_id == "accueil-1"
    assert current.setup_completed is False   # l'objet courant n'est pas modifié


# --- Récapitulatif ---------------------------------------------------------------------

def test_recapitulatif_sans_secret():
    lines = setup_logic.summary_lines(_values(), TEST_OK, TEST_NOT_RUN)
    text = "\n".join(lines)
    assert "s3cr3t-borne" not in text
    assert "testé ✔" in text and "non testé" in text
    assert "production" in text


def test_recapitulatif_signale_le_mode_developpement_et_les_echecs():
    text = "\n".join(setup_logic.summary_lines(
        _values(debug=True), TEST_FAILED, TEST_FAILED))
    assert "DÉVELOPPEMENT" in text and "échec du test" in text


@pytest.mark.parametrize(("overrides", "already", "expected"), [
    ({}, False, "aucun"),
    ({}, True, "inchangé"),
    ({"pin_new": "1234", "pin_confirm": "1234"}, True, "nouveau code défini"),
    ({"pin_remove": True}, True, "supprimé"),
])
def test_recapitulatif_du_pin(overrides, already, expected):
    lines = setup_logic.summary_lines(_values(**overrides), TEST_OK, TEST_OK,
                                      pin_already_set=already)
    assert lines[-1].endswith(expected)


# --- Détection USB ------------------------------------------------------------------------

def _dev(vendor, product, device_class=0, interfaces=()):
    return {"vendor": vendor, "product": product, "device_class": device_class,
            "interface_classes": set(interfaces)}


def test_tri_des_peripheriques_usb():
    candidates = setup_logic.usb_candidates([
        _dev(0x046D, 0xC52B, interfaces=[3]),          # souris Logitech
        _dev(0x1D6B, 0x0002, device_class=9),          # hub racine Linux
        _dev(0x05E3, 0x0610, device_class=9),          # concentrateur
        _dev(0x0519, 0x0003),                          # Star, classe inconnue
        _dev(0x04B8, 0x0202, interfaces=[7]),          # Epson imprimante
        _dev(0x04B8, 0x0202, interfaces=[7]),          # doublon
    ])
    assert [(v, p) for v, p, _ in candidates] == [
        ("0x04b8", "0x0202"), ("0x0519", "0x0003"), ("0x046d", "0xc52b")]
    assert "Epson" in candidates[0][2] and "imprimante" in candidates[0][2]
    assert "Star" in candidates[1][2]
    assert "inconnu" in candidates[2][2]


def test_imprimante_de_classe_avant_fabricant_connu():
    candidates = setup_logic.usb_candidates([
        _dev(0x04B8, 0x0E15),                  # Epson sans interface imprimante
        _dev(0x2D37, 0x0001, interfaces=[7]),  # générique, classe imprimante
    ])
    assert candidates[0][:2] == ("0x2d37", "0x0001")


def test_entrees_illisibles_ignorees():
    assert setup_logic.usb_candidates([{"vendor": None, "product": 1}, {}]) == []


# --- Marqueur « borne configurée » ------------------------------------------------------------

@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    Config._instance = None
    monkeypatch.setattr(config_mod.Config, "_get_config_path", lambda self: tmp_path)
    mem = {}
    monkeypatch.setattr(secret_store, "get_secret", lambda name: mem.get(name, ""))

    def _set(name, value):
        mem[name] = value or ""
        return True

    monkeypatch.setattr(secret_store, "set_secret", _set)
    monkeypatch.setattr(secret_store, "store_secrets",
                        lambda values: all(_set(k, v) for k, v in values.items()))
    yield tmp_path, mem
    Config._instance = None


def _write(path, data):
    (path / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def test_nouvelle_installation_ouvre_l_assistant(config_dir):
    assert Config().settings.setup_completed is False


def test_borne_existante_valide_n_ouvre_pas_l_assistant(config_dir):
    path, mem = config_dir
    mem["app_secret"] = "s3cr3t-borne"
    _write(path, {"base_url": "https://flowelis.example", "debug": False})
    assert Config().settings.setup_completed is True


def test_fichier_preremplie_par_install_sh_ouvre_l_assistant(config_dir):
    path, _mem = config_dir
    _write(path, {"base_url": "", "fullscreen": True, "debug": False})
    assert Config().settings.setup_completed is False


def test_marqueur_present_respecte(config_dir):
    path, mem = config_dir
    mem["app_secret"] = "s3cr3t-borne"
    _write(path, {"base_url": "https://flowelis.example", "debug": False,
                  "setup_completed": False})
    assert Config().settings.setup_completed is False


def test_marqueur_enregistre(config_dir):
    path, _mem = config_dir
    config = Config()
    settings, _ = setup_logic.build_settings(config.settings, _values())
    config.save_settings(settings)
    data = json.loads((path / "settings.json").read_text(encoding="utf-8"))
    assert data["setup_completed"] is True
    assert "s3cr3t-borne" not in json.dumps(data)


def test_enregistrement_efface_une_erreur_de_lecture_perimee(config_dir):
    # Fichier illisible réparé dans l'assistant ou l'éditeur : la borne doit
    # démarrer dans la foulée, sans erreur de lecture restée en mémoire.
    path, _mem = config_dir
    (path / "settings.json").write_text("{pas du json", encoding="utf-8")
    config = Config()
    assert config.load_error
    settings, _ = setup_logic.build_settings(config.settings, _values())
    config.save_settings(settings)
    assert config.load_error is None


def test_marqueur_de_mauvais_type_signale():
    errors = Settings(setup_completed="oui").validate()
    assert any("setup_completed" in e for e in errors)
