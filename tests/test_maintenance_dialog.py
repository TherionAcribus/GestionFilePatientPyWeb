"""Fenêtre de maintenance (touche F10) avec le vrai Tk : grands boutons
empilés, « Quitter l'application » en dernier, en rouge, et confirmé.

Ignoré quand Tk ne peut pas ouvrir de fenêtre (CI sans écran)."""

import pytest

import maintenance_pin

tk = pytest.importorskip("tkinter")
config_editor = pytest.importorskip("config_editor")


@pytest.fixture
def dialog(capsys):
    # Capture de sortie de pytest suspendue pendant la création : sous
    # Windows, elle fait échouer au hasard le chargement des scripts Tcl
    # (« Can't find a usable init.tcl »). Problème du seul environnement de
    # test : la borne, elle, n'a pas de capture.
    with capsys.disabled():
        try:
            window = config_editor.MaintenanceActionDialog(timeout_s=60)
        except tk.TclError as exc:
            pytest.skip(f"Pas d'affichage pour Tk : {exc}")
        window.update()
    yield window
    if _alive(window):
        window.destroy()


def _alive(window):
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False


def test_un_bouton_par_choix_empiles_dans_l_ordre(dialog):
    choices = maintenance_pin.MAINTENANCE_CHOICES
    assert list(dialog.buttons) == [c.action for c in choices]
    tops = [dialog.buttons[c.action].winfo_y() for c in choices]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)
    for choice in choices:
        assert dialog.buttons[choice.action].cget("text") == choice.label


def test_boutons_assez_grands_pour_le_doigt(dialog):
    for button in dialog.buttons.values():
        assert button.winfo_height() >= 44


def test_quitter_en_rouge_et_les_autres_non(dialog):
    quit_button = dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_QUIT]
    assert quit_button.cget("bg") == config_editor._DANGER_COLOR
    for action, button in dialog.buttons.items():
        if action != maintenance_pin.MAINTENANCE_ACTION_QUIT:
            assert button.cget("bg") != config_editor._DANGER_COLOR


def test_reprendre_le_service(dialog):
    dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_RESUME].invoke()
    assert dialog.action == maintenance_pin.MAINTENANCE_ACTION_RESUME
    assert not _alive(dialog)


def test_ouvrir_la_configuration(dialog):
    dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_CONFIGURE].invoke()
    assert dialog.action == maintenance_pin.MAINTENANCE_ACTION_CONFIGURE


def test_quitter_refuse_ne_ferme_rien(dialog, monkeypatch):
    monkeypatch.setattr(config_editor.messagebox, "askyesno",
                        lambda *a, **k: False)
    dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_QUIT].invoke()
    assert dialog.action is None
    assert _alive(dialog)


def test_quitter_confirme(dialog, monkeypatch):
    asked = []
    monkeypatch.setattr(config_editor.messagebox, "askyesno",
                        lambda title, text, **k: asked.append(text) or True)
    dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_QUIT].invoke()
    assert dialog.action == maintenance_pin.MAINTENANCE_ACTION_QUIT
    assert "Le PC reste allumé" in asked[0]
