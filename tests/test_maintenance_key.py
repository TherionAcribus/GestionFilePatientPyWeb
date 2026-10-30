"""Touche de maintenance (F10) : raccourci Qt, menu par-dessus la borne,
fermeture de la borne (sans couper un ticket), ouverture de l'éditeur et
relance.

PySide6 n'est pas installé en CI : le raccourci et le menu sont testés avec
de faux modules ; des tests supplémentaires utilisent le vrai Qt quand il
est présent. Les fenêtres du menu elles-mêmes : test_maintenance_gate.py.
"""

import builtins
import os
import sys
import threading
import types
from pathlib import Path

import pytest

import main
import maintenance_pin
import pin_lockout


class _FakePrinterAPI:
    def __init__(self, printing=False, idle=True):
        self._printing = printing
        self._idle = idle
        self.waits = []
        self.events = []

    def printing(self):
        return self._printing

    def wait_until_idle(self, timeout):
        self.waits.append(timeout)
        return self._idle

    def report_event(self, code, message, is_error=False):
        self.events.append((code, message, is_error))


def _client(printer_api=None):
    client = main.WebViewClient.__new__(main.WebViewClient)
    client.maintenance_requested = False
    client.maintenance_action = None
    client._maintenance_gate = None
    client.open_maintenance_on_start = False
    client._close_thread = None
    client.is_fullscreen = True
    client.printer_api = printer_api or _FakePrinterAPI()
    return client


def _wait_closed(client):
    if client._close_thread is not None:
        client._close_thread.join(timeout=5)
        assert not client._close_thread.is_alive()


class _FakeWindow:
    def __init__(self, native=None, fail=False, toggle_fails=False):
        self.native = native
        self.destroyed = 0
        self.toggles = 0
        self._fail = fail
        self._toggle_fails = toggle_fails

    def destroy(self):
        if self._fail:
            raise RuntimeError("fenêtre déjà fermée")
        self.destroyed += 1

    def toggle_fullscreen(self):
        if self._toggle_fails:
            raise RuntimeError("fenêtre indisponible")
        self.toggles += 1
        if self.native is not None and hasattr(self.native, "fullscreen"):
            self.native.fullscreen = not self.native.fullscreen


class _FakeNative:
    """Fenêtre Qt minimale : état plein écran et activation."""

    def __init__(self, fullscreen=True):
        self.fullscreen = fullscreen
        self.enabled = True

    def isFullScreen(self):
        return self.fullscreen

    def setEnabled(self, enabled):
        self.enabled = enabled


# --- Faux menu de maintenance -----------------------------------------------------

class _FakeGateModule(types.ModuleType):
    """Remplace maintenance_gate : le menu « répond » ``answer`` dès son
    ouverture, ou attend (``answer=_PENDING``) que le test le termine."""

    def __init__(self, answer, fails=False):
        super().__init__("maintenance_gate")
        self.answer = answer
        self.fails = fails
        self.opened = []
        module = self

        class MaintenanceGate:
            def __init__(self, parent, stored_hash, on_done, state_path=None,
                         on_lockout=None):
                if module.fails:
                    raise RuntimeError("Qt indisponible")
                self.parent = parent
                self.stored_hash = stored_hash
                self.on_done = on_done
                self.state_path = state_path
                self.on_lockout = on_lockout
                module.opened.append(self)

            def start(self):
                if module.answer is not _PENDING:
                    self.on_done(module.answer)

        self.MaintenanceGate = MaintenanceGate


_PENDING = object()


def _fake_gate(monkeypatch, answer=_PENDING, fails=False):
    module = _FakeGateModule(answer, fails)
    monkeypatch.setitem(sys.modules, "maintenance_gate", module)
    return module


# --- Raccourci Qt (faux modules) ----------------------------------------------

class _Created(list):
    timers = None


def _fake_qt(monkeypatch):
    created = _Created()
    created.timers = []

    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self):
            for slot in self.slots:
                slot()

    class QShortcut:
        def __init__(self, sequence, parent):
            self.sequence = sequence
            self.parent = parent
            self.context = None
            self.activated = Signal()
            created.append(self)

        def setContext(self, context):
            self.context = context

    class QKeySequence(str):
        pass

    class QTimer:
        @staticmethod
        def singleShot(delay, slot):
            created.timers.append((delay, slot))

    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QShortcut = QShortcut
    qtgui.QKeySequence = QKeySequence
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.Qt = types.SimpleNamespace(ShortcutContext=types.SimpleNamespace(
        ApplicationShortcut="application"))
    qtcore.QTimer = QTimer
    pyside = types.ModuleType("PySide6")
    pyside.QtGui, pyside.QtCore = qtgui, qtcore
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    return created


def test_raccourcis_poses_sur_la_fenetre_native(monkeypatch):
    created = _fake_qt(monkeypatch)
    native = _FakeNative()
    client = _client()
    client.window = _FakeWindow(native)

    client._install_kiosk_shortcuts()

    assert [s.sequence for s in created] == ["F10", "F11"]
    for shortcut in created:
        assert shortcut.parent is native
        assert shortcut.context == "application"
    # Références conservées (sinon Qt peut perdre les raccourcis).
    assert native.maintenance_shortcut is created[0]
    assert native.fullscreen_shortcut is created[1]

    gate = _fake_gate(monkeypatch)
    _settings_with_pin(monkeypatch, "")
    created[0].activated.emit()
    assert len(gate.opened) == 1
    assert client.window.destroyed == 0      # la borne reste ouverte


def test_relance_hors_menu_ouvre_le_menu_au_demarrage(monkeypatch):
    created = _fake_qt(monkeypatch)
    client = _client()
    client.open_maintenance_on_start = True
    client.window = _FakeWindow(_FakeNative())
    client._install_kiosk_shortcuts()
    assert created.timers == [(main.MAINTENANCE_AT_START_DELAY_MS,
                               client.request_maintenance)]


def test_lancement_normal_n_ouvre_pas_le_menu(monkeypatch):
    created = _fake_qt(monkeypatch)
    client = _client()
    client.window = _FakeWindow(_FakeNative())
    client._install_kiosk_shortcuts()
    assert created.timers == []


def test_f11_natif_bascule_le_plein_ecran(monkeypatch):
    created = _fake_qt(monkeypatch)
    _settings_with_pin(monkeypatch, "")
    native = _FakeNative(fullscreen=True)
    client = _client()
    client.window = _FakeWindow(native)
    client._install_kiosk_shortcuts()

    created[1].activated.emit()
    assert client.window.toggles == 1
    assert client.is_fullscreen is False
    assert not client.maintenance_requested


def test_sans_fenetre_native_la_borne_continue(monkeypatch):
    created = _fake_qt(monkeypatch)
    client = _client()
    client.window = _FakeWindow(native=None)
    client._install_kiosk_shortcuts()   # ne lève pas
    assert created == []


def test_sans_qt_la_borne_continue(monkeypatch):
    # Import de Qt impossible. Pas via sys.modules["PySide6"] = None : si le
    # vrai Qt est déjà chargé (autres tests), son crochet d'import plante.
    real_import = builtins.__import__

    def no_qt(name, *args, **kwargs):
        if name.startswith("PySide6"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_qt)
    client = _client()
    client.window = _FakeWindow(types.SimpleNamespace())
    client._install_kiosk_shortcuts()   # ne lève pas


def test_f11_n_est_pas_la_touche_de_maintenance():
    assert maintenance_pin.MAINTENANCE_KEY != maintenance_pin.FULLSCREEN_KEY


# --- F11 et code PIN --------------------------------------------------------------

def test_f11_sans_pin_sort_du_plein_ecran(monkeypatch):
    _settings_with_pin(monkeypatch, "")
    client = _client()
    client.window = _FakeWindow(_FakeNative(fullscreen=True))
    client.request_fullscreen_toggle()
    assert client.window.toggles == 1
    assert client.window.native.fullscreen is False


def test_f11_avec_pin_ne_sort_pas_du_plein_ecran(monkeypatch):
    """Sinon F11 contournerait le PIN : bureau et croix de fermeture."""
    _settings_with_pin(monkeypatch, maintenance_pin.hash_pin("1234"))
    client = _client()
    client.window = _FakeWindow(_FakeNative(fullscreen=True))
    client.request_fullscreen_toggle()
    assert client.window.toggles == 0
    assert client.window.native.fullscreen is True


def test_f11_avec_pin_peut_toujours_revenir_en_plein_ecran(monkeypatch):
    _settings_with_pin(monkeypatch, maintenance_pin.hash_pin("1234"))
    client = _client()
    client.is_fullscreen = False
    client.window = _FakeWindow(_FakeNative(fullscreen=False))
    client.request_fullscreen_toggle()
    assert client.window.toggles == 1
    assert client.is_fullscreen is True


def test_f11_se_fie_a_l_etat_reel_de_la_fenetre(monkeypatch):
    """Le gestionnaire de fenêtres a pu changer l'état : on lit Qt, pas le
    dernier état mémorisé."""
    _settings_with_pin(monkeypatch, maintenance_pin.hash_pin("1234"))
    client = _client()
    client.is_fullscreen = False          # mémoire périmée
    client.window = _FakeWindow(_FakeNative(fullscreen=True))
    client.request_fullscreen_toggle()
    assert client.window.toggles == 0


def test_echec_de_bascule_ne_change_pas_l_etat(monkeypatch):
    _settings_with_pin(monkeypatch, "")
    client = _client()
    client.window = _FakeWindow(_FakeNative(fullscreen=True),
                                toggle_fails=True)
    assert client.toggle_fullscreen() is False
    assert client.is_fullscreen is True


# --- Fermeture sans F10 (Alt+F4, croix) ---------------------------------------------

def test_fermeture_hors_menu_relance_sur_le_menu():
    client = _client()
    client.window = _FakeWindow()
    # Ne renvoie pas False : la fermeture n'est pas bloquée.
    assert client._on_window_closing() is None
    assert client.maintenance_requested
    assert client.maintenance_action is None
    assert main._after_maintenance_close(None) == [main.MAINTENANCE_OPTION]


def test_fermeture_pendant_un_ticket_attend_la_fin(monkeypatch):
    """Alt+F4 pendant une impression : la fermeture est reportée après le
    ticket (et l'enregistrement de son acquittement)."""
    api = _FakePrinterAPI(printing=True)
    client = _client(api)
    client.window = _FakeWindow(_FakeNative())
    assert client._on_window_closing() is False      # fermeture reportée
    _wait_closed(client)
    assert api.waits == [main.MAINTENANCE_PRINT_WAIT_S]
    assert client.window.destroyed == 1
    # Fermeture effective : le second passage ne la bloque plus.
    assert client._on_window_closing() is None


# --- Menu de maintenance par-dessus la borne -------------------------------------

def test_f10_ouvre_le_menu_sans_fermer_la_borne(monkeypatch):
    gate = _fake_gate(monkeypatch)
    stored = maintenance_pin.hash_pin("1234")
    _settings_with_pin(monkeypatch, stored)
    native = _FakeNative()
    client = _client()
    client.window = _FakeWindow(native)
    client.request_maintenance()
    [opened] = gate.opened
    assert opened.parent is native
    assert opened.stored_hash == stored
    # Compteur d'erreurs gardé dans le dossier de configuration.
    assert opened.state_path == _CONFIG_DIR / pin_lockout.STATE_FILENAME
    assert client.window.destroyed == 0
    assert not client.maintenance_requested


def test_f10_pendant_le_menu_n_ouvre_pas_un_second_menu(monkeypatch):
    gate = _fake_gate(monkeypatch)
    _settings_with_pin(monkeypatch, "")
    client = _client()
    client.window = _FakeWindow(_FakeNative())
    client.request_maintenance()
    client.request_maintenance()
    assert len(gate.opened) == 1


def test_f11_ignore_pendant_le_menu(monkeypatch):
    _fake_gate(monkeypatch)
    _settings_with_pin(monkeypatch, "")
    client = _client()
    client.window = _FakeWindow(_FakeNative(fullscreen=True))
    client.request_maintenance()
    client.request_fullscreen_toggle()
    assert client.window.toggles == 0


@pytest.mark.parametrize("answer", [
    maintenance_pin.MAINTENANCE_ACTION_RESUME, None])
def test_reprendre_ou_abandonner_ne_ferme_rien(monkeypatch, answer):
    """« Reprendre », code refusé, Échap, inactivité : la borne continue,
    sans redémarrage."""
    gate = _fake_gate(monkeypatch, answer)
    _settings_with_pin(monkeypatch, "")
    client = _client()
    client.window = _FakeWindow(_FakeNative())
    client.request_maintenance()
    assert client.window.destroyed == 0
    assert not client.maintenance_requested
    assert client._maintenance_gate is None
    client.request_maintenance()               # F10 fonctionne à nouveau
    assert len(gate.opened) == 2


@pytest.mark.parametrize("answer", [
    maintenance_pin.MAINTENANCE_ACTION_CONFIGURE,
    maintenance_pin.MAINTENANCE_ACTION_QUIT,
    maintenance_pin.MAINTENANCE_ACTION_POWEROFF])
def test_configurer_quitter_ou_eteindre_ferme_la_borne(monkeypatch, answer):
    _fake_gate(monkeypatch, answer)
    _settings_with_pin(monkeypatch, "")
    native = _FakeNative()
    client = _client()
    client.window = _FakeWindow(native)
    client.request_maintenance()
    assert not native.enabled            # plus de toucher pendant la fermeture
    _wait_closed(client)
    assert client.window.destroyed == 1
    assert client.maintenance_requested
    assert client.maintenance_action == answer


def test_menu_en_panne_la_borne_continue(monkeypatch):
    _fake_gate(monkeypatch, fails=True)
    _settings_with_pin(monkeypatch, "")
    client = _client()
    client.window = _FakeWindow(_FakeNative())
    client.request_maintenance()        # ne lève pas
    assert client._maintenance_gate is None
    assert not client.maintenance_requested


def test_trop_de_codes_faux_signale_au_serveur(monkeypatch):
    gate = _fake_gate(monkeypatch)
    _settings_with_pin(monkeypatch, maintenance_pin.hash_pin("1234"))
    client = _client()
    client.window = _FakeWindow(_FakeNative())
    client.request_maintenance()
    gate.opened[0].on_lockout(3, 30.0)
    [(code, message, is_error)] = client.printer_api.events
    assert code == maintenance_pin.SERVER_EVENT_PIN_LOCKOUT
    assert "3 fois" in message and "30 s" in message
    assert is_error


# --- Fermeture : jamais au milieu d'un ticket --------------------------------------

def test_fermeture_attend_la_fin_de_l_impression():
    api = _FakePrinterAPI()
    client = _client(api)
    client.window = _FakeWindow(_FakeNative())
    client.close_for_maintenance(maintenance_pin.MAINTENANCE_ACTION_CONFIGURE)
    _wait_closed(client)
    assert api.waits == [main.MAINTENANCE_PRINT_WAIT_S]
    assert client.window.destroyed == 1


def test_fermeture_apres_le_delai_maximal_meme_si_l_impression_dure():
    client = _client(_FakePrinterAPI(printing=True, idle=False))
    client.window = _FakeWindow(_FakeNative())
    client.close_for_maintenance(maintenance_pin.MAINTENANCE_ACTION_QUIT)
    _wait_closed(client)
    assert client.window.destroyed == 1


def test_attente_hors_du_fil_appelant():
    """L'attente bloque : jamais dans le fil Qt, qui doit rendre le résultat
    de l'impression à la page."""
    release = threading.Event()

    class SlowAPI(_FakePrinterAPI):
        def wait_until_idle(self, timeout):
            release.wait(5)
            return True

    client = _client(SlowAPI())
    client.window = _FakeWindow(_FakeNative())
    client.close_for_maintenance(maintenance_pin.MAINTENANCE_ACTION_QUIT)
    assert client.window.destroyed == 0     # rendu la main tout de suite
    release.set()
    _wait_closed(client)
    assert client.window.destroyed == 1


def test_echec_de_fermeture_annule_la_demande():
    native = _FakeNative()
    client = _client()
    client.window = _FakeWindow(native, fail=True)
    client.close_for_maintenance(maintenance_pin.MAINTENANCE_ACTION_CONFIGURE)
    _wait_closed(client)
    assert not client.maintenance_requested
    assert client.maintenance_action is None
    assert native.enabled


def test_fermeture_demandee_deux_fois_ne_ferme_qu_une_fois():
    client = _client()
    client.window = _FakeWindow(_FakeNative())
    client.close_for_maintenance(maintenance_pin.MAINTENANCE_ACTION_QUIT)
    _wait_closed(client)
    client.close_for_maintenance(maintenance_pin.MAINTENANCE_ACTION_CONFIGURE)
    assert client.window.destroyed == 1
    assert client.maintenance_action == maintenance_pin.MAINTENANCE_ACTION_QUIT


# --- Après la fermeture : éditeur, relance ----------------------------------------

_CONFIG_DIR = Path("dossier-config")


def _settings_with_pin(monkeypatch, pin_hash):
    fake_config = types.SimpleNamespace(
        settings=types.SimpleNamespace(maintenance_pin_hash=pin_hash),
        config_path=_CONFIG_DIR)
    monkeypatch.setattr(main, "Config", lambda: fake_config)


def _fake_editor(monkeypatch, calls):
    module = types.ModuleType("config_editor")

    class ConfigEditor:
        def __init__(self, **options):
            calls.append(("editor", options))

        def mainloop(self):
            pass

    module.ConfigEditor = ConfigEditor
    monkeypatch.setitem(sys.modules, "config_editor", module)


def test_configuration_ouvre_l_editeur_devant_le_fond_puis_relance(monkeypatch):
    calls = []
    _fake_editor(monkeypatch, calls)
    restart = main._after_maintenance_close(
        maintenance_pin.MAINTENANCE_ACTION_CONFIGURE)
    assert restart == []
    assert calls == [("editor", {
        "kiosk_follows": True,
        "idle_timeout_s": main.MAINTENANCE_EDITOR_IDLE_TIMEOUT_S,
        "backdrop": True})]


def test_eteindre_le_pc_sans_relance(monkeypatch):
    calls = []
    _fake_editor(monkeypatch, calls)
    monkeypatch.setattr(main.system_power, "power_off", lambda: True)
    assert main._after_maintenance_close(
        maintenance_pin.MAINTENANCE_ACTION_POWEROFF) is None
    assert calls == []


def test_extinction_refusee_relance_la_borne(monkeypatch):
    """La borne ne reste pas arrêtée avec le PC allumé : message, relance."""
    notices = []
    monkeypatch.setattr(main.system_power, "power_off", lambda: False)
    monkeypatch.setattr(main, "_show_notice",
                        lambda title, text: notices.append(text))
    assert main._after_maintenance_close(
        maintenance_pin.MAINTENANCE_ACTION_POWEROFF) == []
    assert notices == [maintenance_pin.poweroff_failed_text()]


def test_message_sans_tk_ne_bloque_pas(monkeypatch):
    module = types.ModuleType("config_editor")

    def show_notice(title, text):
        raise RuntimeError("Tk indisponible")

    module.show_notice = show_notice
    monkeypatch.setitem(sys.modules, "config_editor", module)
    main._show_notice("Titre", "Texte")      # ne lève pas


def test_quitter_l_application_empeche_la_relance(monkeypatch):
    calls = []
    _fake_editor(monkeypatch, calls)
    assert main._after_maintenance_close(
        maintenance_pin.MAINTENANCE_ACTION_QUIT) is None
    assert calls == []


# --- Relance -----------------------------------------------------------------------

def test_relance_depuis_les_sources():
    command = main._restart_command(
        argv=["main.py", "--foo"], executable="/venv/bin/python", frozen=False)
    assert command == ["/venv/bin/python", os.path.abspath(main.__file__), "--foo"]


def test_relance_du_binaire():
    command = main._restart_command(
        argv=["/opt/FlowelisBorne", "--foo"], executable="/opt/FlowelisBorne",
        frozen=True)
    assert command == ["/opt/FlowelisBorne", "--foo"]


def test_option_maintenance_valable_pour_un_seul_lancement():
    """Sinon chaque relance rouvrirait le menu de maintenance."""
    command = main._restart_command(
        argv=["FlowelisBorne", main.MAINTENANCE_OPTION, "--foo"],
        executable="FlowelisBorne", frozen=True)
    assert command == ["FlowelisBorne", "--foo"]
    command = main._restart_command(
        argv=["FlowelisBorne", "--foo"], executable="FlowelisBorne",
        frozen=True, extra=[main.MAINTENANCE_OPTION])
    assert command == ["FlowelisBorne", "--foo", main.MAINTENANCE_OPTION]


def test_relance_sans_les_options_hors_borne():
    command = main._restart_command(
        argv=["FlowelisBorne", "--config", "--selfcheck", "--foo"],
        executable="FlowelisBorne", frozen=True)
    assert command == ["FlowelisBorne", "--foo"]


# --- Vrai Qt, si disponible ----------------------------------------------------------

def test_f10_declenche_la_maintenance_avec_le_vrai_qt(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow

    app = QApplication.instance() or QApplication([])
    native = QMainWindow()
    field = QLineEdit()          # le focus est dans un champ de saisie
    native.setCentralWidget(field)
    native.show()
    field.setFocus()
    app.processEvents()

    client = _client()
    client.window = _FakeWindow(native)
    toggles = []
    monkeypatch.setattr(client, "request_fullscreen_toggle",
                        lambda: toggles.append("F11"), raising=False)

    requests = []
    monkeypatch.setattr(client, "request_maintenance",
                        lambda: requests.append("F10"), raising=False)
    client._install_kiosk_shortcuts()

    QTest.keyClick(field, Qt.Key.Key_F11)
    assert toggles == ["F11"]
    assert requests == []
    QTest.keyClick(field, Qt.Key.Key_F10)
    assert requests == ["F10"]
    native.close()


def test_menu_reel_par_dessus_la_borne(monkeypatch, tmp_path):
    """F10 avec le vrai menu Qt : la borne reste ouverte et reprend sans
    redémarrer ; « Ouvrir la configuration » la ferme."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication, QMainWindow

    import maintenance_gate

    app = QApplication.instance() or QApplication([])
    fake_config = types.SimpleNamespace(
        settings=types.SimpleNamespace(maintenance_pin_hash=""),
        config_path=tmp_path)
    monkeypatch.setattr(main, "Config", lambda: fake_config)
    native = QMainWindow()
    native.show()
    client = _client()
    client.window = _FakeWindow(native)

    client.request_maintenance()
    menu = client._maintenance_gate.dialog
    assert isinstance(menu, maintenance_gate.ActionDialog)
    assert menu.parent() is native
    menu.buttons[maintenance_pin.MAINTENANCE_ACTION_RESUME].click()
    assert client._maintenance_gate is None
    assert client.window.destroyed == 0
    assert not client.maintenance_requested

    client.request_maintenance()
    client._maintenance_gate.dialog.buttons[
        maintenance_pin.MAINTENANCE_ACTION_CONFIGURE].click()
    assert not native.isEnabled()
    _wait_closed(client)
    assert client.window.destroyed == 1
    assert client.maintenance_action == (
        maintenance_pin.MAINTENANCE_ACTION_CONFIGURE)
    native.close()
    app.processEvents()
