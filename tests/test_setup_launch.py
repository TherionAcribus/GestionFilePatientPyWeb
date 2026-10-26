"""Démarrage : assistant de premier lancement, puis éditeur seulement si la
configuration reste refusée (sans enchaîner deux fenêtres)."""

import sys
import types

import pytest

import main


@pytest.fixture
def startup(monkeypatch):
    calls = []
    state = {"setup_completed": False, "config_error": False,
             "wizard": "done", "editor": True}

    monkeypatch.setattr(main, "Config", lambda: types.SimpleNamespace(
        settings=types.SimpleNamespace(
            setup_completed=state["setup_completed"])))

    class FakeClient:
        def __init__(self):
            calls.append("client")
            self._config_error = state["config_error"]

    monkeypatch.setattr(main, "WebViewClient", FakeClient)

    def wizard(**options):
        calls.append(("wizard", options))
        return state["wizard"]

    def editor(**options):
        calls.append(("editor", options))
        return state["editor"]

    monkeypatch.setattr(main, "_launch_setup_wizard", wizard)
    monkeypatch.setattr(main, "_launch_config_editor", editor)
    return state, calls


def test_premier_lancement_ouvre_l_assistant_avant_la_borne(startup):
    _state, calls = startup
    main._prepare_client()
    assert calls == [("wizard", {"kiosk_follows": True}), "client"]


def test_borne_configuree_demarre_directement(startup):
    state, calls = startup
    state["setup_completed"] = True
    main._prepare_client()
    assert calls == ["client"]


def test_configuration_avancee_demandee(startup):
    state, calls = startup
    state["wizard"] = "advanced"
    main._prepare_client()
    assert calls == [("wizard", {"kiosk_follows": True}),
                     ("editor", {"kiosk_follows": True}), "client"]


def test_assistant_abandonne_pas_d_editeur_en_plus(startup):
    # Configuration toujours invalide : l'écran d'erreur (avec F10) suffit,
    # on n'enchaîne pas l'éditeur derrière l'assistant qu'on vient de fermer.
    state, calls = startup
    state["wizard"] = "cancelled"
    state["config_error"] = True
    main._prepare_client()
    assert calls == [("wizard", {"kiosk_follows": True}), "client"]


def test_assistant_indisponible_repli_sur_l_editeur(startup):
    state, calls = startup
    state["wizard"] = None          # Tk absent ou en panne
    state["config_error"] = True
    main._prepare_client()
    assert calls == [("wizard", {"kiosk_follows": True}), "client",
                     ("editor", {"kiosk_follows": True}), "client"]


def test_configuration_refusee_hors_premier_lancement(startup):
    state, calls = startup
    state["setup_completed"] = True
    state["config_error"] = True
    main._prepare_client()
    assert calls == ["client", ("editor", {"kiosk_follows": True}), "client"]


# --- Lanceur de l'assistant ------------------------------------------------------

def _fake_wizard_module(result=None, fail=False):
    module = types.ModuleType("setup_wizard")

    def run_setup_wizard(**_options):
        if fail:
            raise RuntimeError("Tk a planté")
        return result

    module.run_setup_wizard = run_setup_wizard
    return module


def test_lanceur_renvoie_le_resultat(monkeypatch):
    monkeypatch.setitem(sys.modules, "setup_wizard", _fake_wizard_module("done"))
    assert main._launch_setup_wizard(kiosk_follows=True) == "done"


def test_assistant_qui_plante_n_emporte_pas_la_borne(monkeypatch):
    monkeypatch.setitem(sys.modules, "setup_wizard",
                        _fake_wizard_module(fail=True))
    assert main._launch_setup_wizard() is None


def test_relance_sans_option_setup():
    command = main._restart_command(argv=["FlowelisBorne", "--setup"],
                                    executable="FlowelisBorne", frozen=True)
    assert command == ["FlowelisBorne"]
