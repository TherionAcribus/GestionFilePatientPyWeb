"""Code PIN et menu de maintenance (Qt), affichés par-dessus la borne.

Vrai Qt (plateforme « offscreen », sans écran). Ignoré quand PySide6 n'est
pas installé (CI)."""

import os

import pytest

import maintenance_pin

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

import maintenance_gate as gate

CONFIGURE = maintenance_pin.MAINTENANCE_ACTION_CONFIGURE
RESUME = maintenance_pin.MAINTENANCE_ACTION_RESUME
QUIT = maintenance_pin.MAINTENANCE_ACTION_QUIT
POWEROFF = maintenance_pin.MAINTENANCE_ACTION_POWEROFF
ALL_CHOICES = maintenance_pin.MAINTENANCE_CHOICES

# Empreinte fixe : comme sur une borne, le PIN enregistré ne change pas d'une
# ouverture à l'autre (un nouveau sel = un nouveau PIN = compteur vierge).
_PIN_HASH = maintenance_pin.hash_pin("1234")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def kiosk(qapp):
    """Fausse fenêtre de borne, parent des dialogues."""
    window = QMainWindow()
    window.show()
    yield window
    window.close()
    window.deleteLater()
    qapp.processEvents()


class _Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _confirm(answer, asked=None):
    def confirm(_parent, action):
        if asked is not None:
            asked.append(action)
        return answer
    return confirm


# --- Menu -------------------------------------------------------------------------


@pytest.fixture
def menu(kiosk):
    dialog = gate.ActionDialog(kiosk, timeout_s=60, confirm=_confirm(False),
                               choices=ALL_CHOICES)
    dialog.open()
    yield dialog
    dialog.close()


def test_un_bouton_par_choix_empiles_dans_l_ordre(menu):
    choices = ALL_CHOICES
    assert list(menu.buttons) == [c.action for c in choices]
    tops = [menu.buttons[c.action].y() for c in choices]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)
    for choice in choices:
        assert menu.buttons[choice.action].text() == choice.label


def test_boutons_assez_grands_pour_le_doigt(menu):
    for button in menu.buttons.values():
        assert button.height() >= 44


def test_arrets_en_rouge_et_les_autres_non(menu):
    for action, button in menu.buttons.items():
        red = gate.DANGER_COLOR in button.styleSheet()
        assert red == (action in (QUIT, POWEROFF))


def test_un_seul_separateur_avant_les_arrets(menu):
    lines = [f for f in menu.findChildren(gate.QFrame)
             if f.frameShape() == gate.QFrame.Shape.HLine]
    assert len(lines) == 1
    assert lines[0].y() < menu.buttons[QUIT].y()
    assert lines[0].y() > menu.buttons[RESUME].y()


def test_eteindre_absent_si_le_systeme_ne_sait_pas(kiosk, monkeypatch):
    monkeypatch.setattr(gate.system_power, "supported", lambda: False)
    dialog = gate.ActionDialog(kiosk)
    assert POWEROFF not in dialog.buttons
    assert QUIT in dialog.buttons


def test_eteindre_present_si_le_systeme_sait(kiosk, monkeypatch):
    monkeypatch.setattr(gate.system_power, "supported", lambda: True)
    dialog = gate.ActionDialog(kiosk)
    assert list(dialog.buttons)[-1] == POWEROFF


def test_eteindre_confirme(kiosk):
    asked = []
    dialog = gate.ActionDialog(kiosk, confirm=_confirm(True, asked),
                               choices=ALL_CHOICES)
    dialog.open()
    dialog.buttons[POWEROFF].click()
    assert asked == [POWEROFF]
    assert dialog.action == POWEROFF


def test_eteindre_refuse_ne_ferme_rien(menu):
    menu.buttons[POWEROFF].click()
    assert menu.action is None
    assert menu.isVisible()


def test_menu_annonce_la_reprise_automatique(menu):
    assert "60 s" in maintenance_pin.maintenance_intro_text(60)
    assert maintenance_pin.maintenance_intro_text(60) in [
        label.text() for label in menu.findChildren(gate.QLabel)]


def test_reprendre_le_service(menu):
    menu.buttons[RESUME].click()
    assert menu.action == RESUME
    assert not menu.isVisible()


def test_ouvrir_la_configuration(menu):
    menu.buttons[CONFIGURE].click()
    assert menu.action == CONFIGURE


def test_quitter_refuse_ne_ferme_rien(menu):
    menu.buttons[QUIT].click()
    assert menu.action is None
    assert menu.isVisible()
    assert menu.idle_timer.isActive()   # le délai d'inactivité reprend


def test_quitter_confirme(kiosk):
    asked = []
    dialog = gate.ActionDialog(kiosk, confirm=_confirm(True, asked))
    dialog.open()
    dialog.buttons[QUIT].click()
    assert asked == [QUIT]
    assert dialog.action == QUIT


def test_echap_reprend_le_service(menu):
    QTest.keyClick(menu, Qt.Key.Key_Escape)
    assert menu.action is None
    assert not menu.isVisible()


def test_entree_sans_tab_ouvre_la_configuration(menu):
    assert menu.buttons[CONFIGURE].isDefault()
    QTest.keyClick(menu.buttons[CONFIGURE], Qt.Key.Key_Return)
    assert menu.action == CONFIGURE


def test_entree_sur_reprendre_reprend_le_service(menu):
    """Entrée active le bouton sélectionné avec Tab, pas la configuration."""
    QTest.keyClick(menu.buttons[RESUME], Qt.Key.Key_Return)
    assert menu.action == RESUME


def test_entree_sur_quitter_demande_confirmation(kiosk):
    asked = []
    dialog = gate.ActionDialog(kiosk, confirm=_confirm(False, asked))
    dialog.open()
    QTest.keyClick(dialog.buttons[QUIT], Qt.Key.Key_Return)
    assert asked == [QUIT]
    assert dialog.action is None
    dialog.close()


def test_menu_oublie_se_ferme_seul(kiosk):
    dialog = gate.ActionDialog(kiosk, timeout_s=0.05)
    dialog.open()
    QTest.qWait(300)
    assert not dialog.isVisible()
    assert dialog.action is None


def test_activite_retient_le_menu(kiosk):
    dialog = gate.ActionDialog(kiosk, timeout_s=0.3)
    dialog.open()
    for _ in range(4):
        QTest.qWait(150)
        QTest.keyClick(dialog.buttons[RESUME], Qt.Key.Key_Shift)
    assert dialog.isVisible()
    dialog.close()


# --- Code PIN ----------------------------------------------------------------------


def _pin(kiosk, state_path=None, clock=None, timeout_s=60):
    dialog = gate.PinDialog(_PIN_HASH, kiosk, state_path=state_path,
                            timeout_s=timeout_s, clock=clock or _Clock())
    dialog.open()
    return dialog


def _try(dialog, code):
    dialog.entry.setText(code)
    dialog.submit()


def test_pin_entree_dans_le_champ_valide(kiosk):
    dialog = _pin(kiosk)
    dialog.entry.setText("1234")
    QTest.keyClick(dialog.entry, Qt.Key.Key_Return)
    assert dialog.granted


def test_pin_entree_sur_annuler_annule(kiosk):
    """Entrée sur « Annuler » n'envoie pas le code saisi."""
    dialog = _pin(kiosk)
    dialog.entry.setText("1234")
    QTest.keyClick(dialog.cancel_button, Qt.Key.Key_Return)
    assert not dialog.granted
    assert not dialog.isVisible()


def test_pin_masque(kiosk):
    dialog = _pin(kiosk)
    assert dialog.entry.echoMode() == gate.QLineEdit.EchoMode.Password
    dialog.close()


def test_trois_erreurs_bloquent_puis_debloquent(kiosk, tmp_path):
    clock = _Clock()
    path = tmp_path / "pin_state.json"
    dialog = _pin(kiosk, path, clock)
    lockouts = []
    dialog.lockout.connect(lambda failures, wait: lockouts.append(
        (failures, wait)))
    _try(dialog, "0000")
    assert "encore 2 essai(s)" in dialog.message.text()
    _try(dialog, "0000")
    _try(dialog, "0000")
    # 3e erreur : attente de 30 s, saisie désactivée, alerte émise.
    assert dialog.locked
    assert not dialog.entry.isEnabled()
    assert not dialog.submit_button.isEnabled()
    assert "30 s" in dialog.message.text()
    assert lockouts == [(3, 30.0)]
    _try(dialog, "1234")              # même le bon code est ignoré
    assert not dialog.granted
    # Fin de l'attente : la saisie revient.
    clock.now += 30
    dialog._tick_lock()
    assert not dialog.locked
    assert dialog.entry.isEnabled()
    _try(dialog, "1234")
    assert dialog.granted
    assert not path.exists()          # bon code : compteur effacé


def test_attente_conservee_au_prochain_appui_sur_f10(kiosk, tmp_path):
    """Rappuyer sur F10 ne redonne pas d'essais."""
    clock = _Clock()
    path = tmp_path / "pin_state.json"
    first = _pin(kiosk, path, clock)
    for _ in range(3):
        _try(first, "0000")
    first.close()

    clock.now += 5
    second = _pin(kiosk, path, clock)
    assert second.locked
    assert "25 s" in second.message.text()
    _try(second, "1234")
    assert not second.granted
    second.close()


def test_erreurs_precedentes_annoncees(kiosk, tmp_path):
    clock = _Clock()
    path = tmp_path / "pin_state.json"
    first = _pin(kiosk, path, clock)
    _try(first, "0000")
    first.close()
    second = _pin(kiosk, path, clock)
    assert not second.locked
    assert "encore 2 essai(s)" in second.message.text()
    second.close()


def test_pendant_l_attente_le_clavier_ne_retient_pas_la_fenetre(kiosk, tmp_path):
    clock = _Clock()
    dialog = _pin(kiosk, tmp_path / "s.json", clock, timeout_s=0.3)
    for _ in range(3):
        _try(dialog, "0000")
    assert dialog.locked
    for _ in range(4):
        QTest.qWait(150)
        QTest.keyClick(dialog.cancel_button, Qt.Key.Key_Shift)
    assert not dialog.isVisible()
    assert not dialog.granted


# --- Enchaînement complet ----------------------------------------------------------


class _Done:
    def __init__(self):
        self.calls = []

    def __call__(self, action):
        self.calls.append(action)


def _gate(kiosk, stored_hash, done, tmp_path, **options):
    flow = gate.MaintenanceGate(kiosk, stored_hash, done,
                                state_path=tmp_path / "pin_state.json",
                                **options)
    flow.start()
    return flow


def test_sans_pin_le_menu_s_ouvre_directement(kiosk, tmp_path):
    done = _Done()
    flow = _gate(kiosk, "", done, tmp_path)
    assert isinstance(flow.dialog, gate.ActionDialog)
    flow.dialog.buttons[CONFIGURE].click()
    assert done.calls == [CONFIGURE]


def test_pin_correct_puis_choix(kiosk, tmp_path):
    done = _Done()
    flow = _gate(kiosk, _PIN_HASH, done, tmp_path)
    assert isinstance(flow.dialog, gate.PinDialog)
    _try(flow.dialog, "1234")
    assert isinstance(flow.dialog, gate.ActionDialog)
    flow.dialog.buttons[RESUME].click()
    assert done.calls == [RESUME]


def test_pin_annule_la_borne_continue(kiosk, tmp_path):
    done = _Done()
    flow = _gate(kiosk, _PIN_HASH, done, tmp_path)
    flow.dialog.cancel_button.click()
    assert done.calls == [None]
    assert flow.dialog is None


def test_menu_abandonne_la_borne_continue(kiosk, tmp_path):
    done = _Done()
    flow = _gate(kiosk, "", done, tmp_path)
    flow.dialog.reject()
    assert done.calls == [None]


def test_attente_signalee(kiosk, tmp_path):
    done = _Done()
    alerts = []
    flow = _gate(kiosk, _PIN_HASH, done, tmp_path,
                 on_lockout=lambda failures, wait: alerts.append(failures))
    for _ in range(3):
        _try(flow.dialog, "0000")
    assert alerts == [3]
    assert done.calls == []           # la fenêtre attend, sans se fermer
    flow.dialog.reject()
    assert done.calls == [None]


def test_dialogues_au_dessus_de_la_borne(kiosk, tmp_path):
    flow = _gate(kiosk, _PIN_HASH, _Done(), tmp_path)
    assert flow.dialog.parent() is kiosk
    assert flow.dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    # Bloque la saisie sur la borne tant qu'il est ouvert.
    assert flow.dialog.windowModality() != Qt.WindowModality.NonModal
    flow.dialog.reject()
