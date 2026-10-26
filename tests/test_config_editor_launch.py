"""Lancement de l'éditeur de configuration depuis la borne (main.py).

``--config`` et l'ouverture automatique sur configuration refusée passent par
``main._launch_config_editor`` : import paresseux de ``config_editor``
(tkinter absent en CI). Ces tests vérifient le contrat sans ouvrir Tk.
"""

import sys
import types

import main


def _fake_editor_module(calls, fail=False):
    module = types.ModuleType("config_editor")

    class ConfigEditor:
        def __init__(self, **options):
            calls.append(("init", options))

        def mainloop(self):
            calls.append("mainloop")
            if fail:
                raise RuntimeError("Tk a planté")

    module.ConfigEditor = ConfigEditor
    return module


def test_editor_se_lance(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "config_editor",
                        _fake_editor_module(calls))
    assert main._launch_config_editor() is True
    assert calls == [("init", {}), "mainloop"]


def test_options_transmises_a_l_editeur(monkeypatch):
    # Ouverture automatique / touche de maintenance : la borne repart à la
    # fermeture, l'éditeur doit l'annoncer (et peut se fermer seul).
    calls = []
    monkeypatch.setitem(sys.modules, "config_editor",
                        _fake_editor_module(calls))
    assert main._launch_config_editor(kiosk_follows=True,
                                      idle_timeout_s=600) is True
    assert calls[0] == ("init", {"kiosk_follows": True, "idle_timeout_s": 600})


def test_editor_qui_plante_n_emporte_pas_la_borne(monkeypatch):
    # Une fenêtre Tk qui plante est une frontière : la borne doit pouvoir
    # retomber sur son écran d'erreur local au lieu de mourir.
    monkeypatch.setitem(sys.modules, "config_editor",
                        _fake_editor_module([], fail=True))
    assert main._launch_config_editor() is False
