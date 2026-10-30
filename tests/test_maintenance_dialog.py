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


# --- Touche Entrée : bouton qui a le focus ----------------------------------


def _press_enter(window, widget):
    widget.focus_force()
    window.update()
    widget.event_generate("<Return>")
    if _alive(window):
        window.update()


def test_entree_sans_tab_ouvre_la_configuration(dialog):
    default = dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_CONFIGURE]
    _press_enter(dialog, default)
    assert dialog.action == maintenance_pin.MAINTENANCE_ACTION_CONFIGURE


def test_entree_sur_reprendre_reprend_le_service(dialog):
    """Régression : Entrée ouvrait la configuration quel que soit le
    bouton sélectionné avec Tab."""
    _press_enter(dialog,
                 dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_RESUME])
    assert dialog.action == maintenance_pin.MAINTENANCE_ACTION_RESUME


def test_entree_sur_quitter_demande_confirmation(dialog, monkeypatch):
    asked = []
    monkeypatch.setattr(config_editor.messagebox, "askyesno",
                        lambda *a, **k: asked.append(True) or False)
    _press_enter(dialog,
                 dialog.buttons[maintenance_pin.MAINTENANCE_ACTION_QUIT])
    assert asked == [True]
    assert dialog.action is None


@pytest.fixture
def pin_dialog(capsys):
    with capsys.disabled():
        try:
            window = config_editor.MaintenancePinDialog(
                maintenance_pin.hash_pin("1234"))
        except tk.TclError as exc:
            pytest.skip(f"Pas d'affichage pour Tk : {exc}")
        window.update()
    yield window
    if _alive(window):
        window.destroy()


def _buttons_of(widget):
    found = []
    for child in widget.winfo_children():
        if isinstance(child, (tk.Button, config_editor.ttk.Button)):
            found.append(child)
        found.extend(_buttons_of(child))
    return found


def test_pin_entree_dans_le_champ_valide(pin_dialog):
    pin_dialog._pin.set("1234")
    _press_enter(pin_dialog, pin_dialog._entry)
    assert pin_dialog.granted


def test_pin_entree_sur_annuler_annule(pin_dialog):
    """Régression : Entrée sur « Annuler » validait le code saisi."""
    pin_dialog._pin.set("1234")
    cancel = next(b for b in _buttons_of(pin_dialog)
                  if b.cget("text") == "Annuler")
    _press_enter(pin_dialog, cancel)
    assert not pin_dialog.granted
    assert not _alive(pin_dialog)


# --- Code PIN : erreurs comptées d'un appui sur F10 à l'autre ---------------------


class _Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# Empreinte fixe : comme sur une borne, le PIN enregistré ne change pas d'une
# ouverture à l'autre (un nouveau sel = un nouveau PIN = compteur vierge).
_PIN_HASH = maintenance_pin.hash_pin("1234")


def _open_pin(capsys, state_path, clock):
    with capsys.disabled():
        try:
            window = config_editor.MaintenancePinDialog(
                _PIN_HASH, state_path=state_path, clock=clock)
        except tk.TclError as exc:
            pytest.skip(f"Pas d'affichage pour Tk : {exc}")
        window.update()
    return window


def _try(window, code):
    window._pin.set(code)
    window._submit()
    if _alive(window):
        window.update()


def test_trois_erreurs_bloquent_puis_debloquent(capsys, tmp_path):
    clock = _Clock()
    path = tmp_path / "pin_state.json"
    window = _open_pin(capsys, path, clock)
    try:
        _try(window, "0000")
        assert "encore 2 essai(s)" in window._message.cget("text")
        _try(window, "0000")
        _try(window, "0000")
        # 3e erreur : attente de 30 s, saisie désactivée.
        assert window._locked
        assert window._entry.instate(["disabled"])
        assert "30 s" in window._message.cget("text")
        _try(window, "1234")          # même le bon code est ignoré
        assert not window.granted
        # Fin de l'attente : la saisie revient.
        clock.now += 30
        window._tick_lock()
        assert not window._locked
        assert not window._entry.instate(["disabled"])
        _try(window, "1234")
        assert window.granted
    finally:
        if _alive(window):
            window.destroy()
    assert not path.exists()          # bon code : compteur effacé


def test_attente_conservee_au_prochain_appui_sur_f10(capsys, tmp_path):
    """Avant : rappuyer sur F10 redonnait 3 essais neufs."""
    clock = _Clock()
    path = tmp_path / "pin_state.json"
    first = _open_pin(capsys, path, clock)
    for _ in range(3):
        _try(first, "0000")
    first.destroy()

    clock.now += 5
    second = _open_pin(capsys, path, clock)
    try:
        assert second._locked
        assert "25 s" in second._message.cget("text")
        _try(second, "1234")
        assert not second.granted
    finally:
        if _alive(second):
            second.destroy()


def test_erreurs_precedentes_annoncees(capsys, tmp_path):
    clock = _Clock()
    path = tmp_path / "pin_state.json"
    first = _open_pin(capsys, path, clock)
    _try(first, "0000")
    first.destroy()
    second = _open_pin(capsys, path, clock)
    try:
        assert not second._locked
        assert "encore 2 essai(s)" in second._message.cget("text")
    finally:
        second.destroy()
