"""Configuration pytest partagée.

Le module ``printer`` importe ``escpos`` (python-escpos) et ``usb`` (pyusb) au
niveau module. Ces bibliothèques dépendent du matériel USB et ne sont pas
installées dans l'environnement de test/CI. On enregistre donc des stubs
minimalistes dans ``sys.modules`` avant toute collecte de tests, afin que
``import printer`` réussisse sans imprimante. Le découplage matériel de
``Printer`` (fabrique de périphérique injectable) permet ensuite d'utiliser une
fausse imprimante dans les tests.
"""
import gc
import os
import sys
import types

import pytest


def _install_escpos_stub():
    if 'escpos' in sys.modules:
        return

    escpos = types.ModuleType('escpos')

    printer_mod = types.ModuleType('escpos.printer')

    class Usb:  # base minimale surchargée par CustomUsb
        def __init__(self, *args, **kwargs):
            pass

    printer_mod.Usb = Usb

    exceptions_mod = types.ModuleType('escpos.exceptions')

    class DeviceNotFoundError(Exception):
        pass

    class USBNotFoundError(DeviceNotFoundError):
        pass

    exceptions_mod.DeviceNotFoundError = DeviceNotFoundError
    exceptions_mod.USBNotFoundError = USBNotFoundError

    constants_mod = types.ModuleType('escpos.constants')
    constants_mod.RT_STATUS_PAPER = 1

    escpos.printer = printer_mod
    escpos.exceptions = exceptions_mod
    escpos.constants = constants_mod

    sys.modules['escpos'] = escpos
    sys.modules['escpos.printer'] = printer_mod
    sys.modules['escpos.exceptions'] = exceptions_mod
    sys.modules['escpos.constants'] = constants_mod


def _install_usb_stub():
    if 'usb' in sys.modules:
        return

    usb = types.ModuleType('usb')
    core_mod = types.ModuleType('usb.core')

    class USBError(Exception):
        pass

    class NoBackendError(ValueError):
        pass

    core_mod.USBError = USBError
    core_mod.NoBackendError = NoBackendError
    usb.core = core_mod

    sys.modules['usb'] = usb
    sys.modules['usb.core'] = core_mod


def _install_webview_stub():
    """``main`` importe ``webview`` (pywebview) au niveau module ; la
    bibliothèque n'est pas installée en CI (pas de backend GUI). Un module vide
    suffit : les tests n'ouvrent jamais de fenêtre."""
    if 'webview' in sys.modules:
        return
    sys.modules['webview'] = types.ModuleType('webview')


_install_escpos_stub()
_install_usb_stub()
_install_webview_stub()


# --- Fin de session avec Qt (CI Linux) -------------------------------------
# Les tests du menu de maintenance créent de vraies fenêtres Qt sans parent.
# Sous Linux, pytest (≥ 9, plugin unraisableexception) appelle gc.collect()
# dans son propre pytest_unconfigure : ces fenêtres y sont détruites et le
# processus plante (segfault, code 139) APRÈS le dernier test — constaté sur
# la CI de l'App Comptoir, invisible sous Windows. Quand Qt a été chargé, on
# gèle donc les objets (gc.freeze : gc.collect() les ignore) puis on quitte
# sans finaliser l'interpréteur, en gardant le code de sortie de pytest.
# Sans Qt (PySide6 absent), rien ne change.

_exit_status = None


def _qt_loaded():
    return "PySide6.QtWidgets" in sys.modules


def pytest_sessionfinish(session, exitstatus):
    global _exit_status
    if _qt_loaded():
        _exit_status = int(exitstatus)
        gc.freeze()


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if _exit_status is None:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_exit_status)
