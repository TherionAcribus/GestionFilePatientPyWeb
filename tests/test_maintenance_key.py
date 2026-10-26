"""Touche de maintenance (F10) : raccourci Qt, fermeture de la borne, code
PIN, ouverture de l'éditeur et relance.

PySide6 n'est pas installé en CI : le raccourci est testé avec de faux
modules Qt ; un test supplémentaire utilise le vrai Qt quand il est présent.
"""

import os
import sys
import types

import pytest

import main
import maintenance_pin


def _client():
    client = main.WebViewClient.__new__(main.WebViewClient)
    client.maintenance_requested = False
    return client


class _FakeWindow:
    def __init__(self, native=None, fail=False):
        self.native = native
        self.destroyed = 0
        self._fail = fail

    def destroy(self):
        if self._fail:
            raise RuntimeError("fenêtre déjà fermée")
        self.destroyed += 1


# --- Raccourci Qt (faux modules) ----------------------------------------------

def _fake_qt(monkeypatch):
    created = []

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

    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QShortcut = QShortcut
    qtgui.QKeySequence = QKeySequence
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.Qt = types.SimpleNamespace(ShortcutContext=types.SimpleNamespace(
        ApplicationShortcut="application"))
    pyside = types.ModuleType("PySide6")
    pyside.QtGui, pyside.QtCore = qtgui, qtcore
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    return created


def test_raccourci_pose_sur_la_fenetre_native(monkeypatch):
    created = _fake_qt(monkeypatch)
    native = types.SimpleNamespace()
    client = _client()
    client.window = _FakeWindow(native)

    client._install_maintenance_shortcut()

    assert len(created) == 1
    shortcut = created[0]
    assert shortcut.sequence == maintenance_pin.MAINTENANCE_KEY == "F10"
    assert shortcut.parent is native
    assert shortcut.context == "application"
    assert native.maintenance_shortcut is shortcut  # référence conservée

    shortcut.activated.emit()
    assert client.maintenance_requested
    assert client.window.destroyed == 1


def test_sans_fenetre_native_la_borne_continue(monkeypatch):
    created = _fake_qt(monkeypatch)
    client = _client()
    client.window = _FakeWindow(native=None)
    client._install_maintenance_shortcut()   # ne lève pas
    assert created == []


def test_sans_qt_la_borne_continue(monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6", None)  # import impossible
    client = _client()
    client.window = _FakeWindow(types.SimpleNamespace())
    client._install_maintenance_shortcut()   # ne lève pas


def test_f11_n_est_pas_la_touche_de_maintenance():
    assert maintenance_pin.MAINTENANCE_KEY != "F11"


# --- Fermeture de la borne ------------------------------------------------------

def test_touche_pressee_deux_fois_ne_ferme_qu_une_fois():
    client = _client()
    client.window = _FakeWindow()
    client.request_maintenance()
    client.request_maintenance()
    assert client.window.destroyed == 1
    assert client.maintenance_requested


def test_echec_de_fermeture_annule_la_demande():
    client = _client()
    client.window = _FakeWindow(fail=True)
    client.request_maintenance()
    assert not client.maintenance_requested


# --- Code PIN puis éditeur -------------------------------------------------------

def _settings_with_pin(monkeypatch, pin_hash):
    fake_config = types.SimpleNamespace(
        settings=types.SimpleNamespace(maintenance_pin_hash=pin_hash))
    monkeypatch.setattr(main, "Config", lambda: fake_config)


def _fake_editor(monkeypatch, calls, pin_granted=True, pin_fails=False):
    module = types.ModuleType("config_editor")

    class ConfigEditor:
        def __init__(self, **options):
            calls.append(("editor", options))

        def mainloop(self):
            pass

    def ask_maintenance_pin(stored_hash):
        calls.append(("pin", stored_hash))
        if pin_fails:
            raise RuntimeError("Tk indisponible")
        return pin_granted

    module.ConfigEditor = ConfigEditor
    module.ask_maintenance_pin = ask_maintenance_pin
    monkeypatch.setitem(sys.modules, "config_editor", module)


def test_sans_pin_l_editeur_s_ouvre_directement(monkeypatch):
    calls = []
    _settings_with_pin(monkeypatch, "")
    _fake_editor(monkeypatch, calls)
    main._run_maintenance()
    assert calls == [("editor", {
        "kiosk_follows": True,
        "idle_timeout_s": main.MAINTENANCE_EDITOR_IDLE_TIMEOUT_S})]


def test_pin_correct_ouvre_l_editeur(monkeypatch):
    calls = []
    stored = maintenance_pin.hash_pin("1234")
    _settings_with_pin(monkeypatch, stored)
    _fake_editor(monkeypatch, calls, pin_granted=True)
    main._run_maintenance()
    assert [c[0] for c in calls] == ["pin", "editor"]
    assert calls[0][1] == stored


def test_pin_refuse_n_ouvre_pas_l_editeur(monkeypatch):
    calls = []
    _settings_with_pin(monkeypatch, maintenance_pin.hash_pin("1234"))
    _fake_editor(monkeypatch, calls, pin_granted=False)
    main._run_maintenance()
    assert [c[0] for c in calls] == ["pin"]


def test_fenetre_pin_en_panne_refuse_l_acces(monkeypatch):
    calls = []
    _settings_with_pin(monkeypatch, maintenance_pin.hash_pin("1234"))
    _fake_editor(monkeypatch, calls, pin_fails=True)
    main._run_maintenance()
    assert [c[0] for c in calls] == ["pin"]


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
    client._install_maintenance_shortcut()

    QTest.keyClick(field, Qt.Key.Key_F11)
    assert not client.maintenance_requested
    QTest.keyClick(field, Qt.Key.Key_F10)
    assert client.maintenance_requested
    native.close()
