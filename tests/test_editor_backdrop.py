"""Fond « Borne en maintenance » derrière l'éditeur ouvert depuis le menu de
maintenance (vrai Tk, sans plein écran pour ne pas couvrir l'écran du
poste de test). Ignoré quand Tk ne peut pas ouvrir de fenêtre."""

import contextlib

import pytest

tk = pytest.importorskip("tkinter")
config_editor = pytest.importorskip("config_editor")


@pytest.fixture
def editor(capsys):
    # Capture de pytest suspendue pendant la création : sous Windows, elle
    # fait échouer au hasard le chargement des scripts Tcl.
    with capsys.disabled():
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            pytest.skip(f"Pas d'affichage pour Tk : {exc}")
        root.geometry("300x200+40+40")
        root.update()
    yield root
    with contextlib.suppress(tk.TclError):
        root.destroy()


def _editor_above(editor, backdrop):
    editor.update()
    return bool(int(editor.tk.call("wm", "stackorder", ".", "isabove",
                                   str(backdrop))))


def _texts(widget):
    found = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            found.append(child.cget("text"))
        found.extend(_texts(child))
    return found


def test_fond_derriere_l_editeur(editor):
    backdrop = config_editor.create_backdrop(editor, fullscreen=False)
    backdrop.geometry("600x400+0+0")
    assert _editor_above(editor, backdrop)
    assert "Borne momentanément indisponible" in _texts(backdrop)


def test_toucher_le_fond_ramene_l_editeur_devant(editor):
    backdrop = config_editor.create_backdrop(editor, fullscreen=False)
    backdrop.lift()
    editor.update()
    backdrop.event_generate("<ButtonPress>")
    assert _editor_above(editor, backdrop)


def test_le_fond_ne_se_ferme_pas_seul(editor):
    backdrop = config_editor.create_backdrop(editor, fullscreen=False)
    handler = backdrop.protocol("WM_DELETE_WINDOW")
    editor.tk.call(handler)
    assert backdrop.winfo_exists()


def test_le_fond_part_avec_l_editeur(editor):
    backdrop = config_editor.create_backdrop(editor, fullscreen=False)
    editor.destroy()
    with pytest.raises(tk.TclError):
        backdrop.winfo_exists()
