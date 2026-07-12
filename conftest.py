"""Configuration pytest partagée.

Le module ``printer`` importe ``escpos`` (python-escpos) et ``usb`` (pyusb) au
niveau module. Ces bibliothèques dépendent du matériel USB et ne sont pas
installées dans l'environnement de test/CI. On enregistre donc des stubs
minimalistes dans ``sys.modules`` avant toute collecte de tests, afin que
``import printer`` réussisse sans imprimante. Le découplage matériel de
``Printer`` (fabrique de périphérique injectable) permet ensuite d'utiliser une
fausse imprimante dans les tests.
"""
import sys
import types


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

    class USBNotFoundError(Exception):
        pass

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

    core_mod.USBError = USBError
    usb.core = core_mod

    sys.modules['usb'] = usb
    sys.modules['usb.core'] = core_mod


_install_escpos_stub()
_install_usb_stub()
