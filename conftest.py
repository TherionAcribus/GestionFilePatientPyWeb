"""Configuration pytest partagée.

Le module ``printer`` importe ``escpos`` au niveau module. Cette bibliothèque
dépend du matériel USB et n'est pas installée dans l'environnement de test.
On enregistre donc des stubs minimalistes dans ``sys.modules`` avant toute
collecte de tests, afin que ``import printer`` réussisse sans imprimante.
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


_install_escpos_stub()
