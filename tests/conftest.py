"""Configuration pytest : stubs des dépendances matérielles.

``printer.py`` importe ``escpos`` (python-escpos) et ``usb`` (pyusb), qui ne
sont pas installés hors d'une vraie borne. On installe ici des modules factices
minimalistes AVANT toute importation de ``printer`` afin que la suite de tests
(contrat de retour, validation des données d'impression) tourne en dev/CI.
"""
import sys
import types


def _install_hardware_stubs():
    if 'escpos' not in sys.modules:
        escpos = types.ModuleType('escpos')
        escpos_printer = types.ModuleType('escpos.printer')
        escpos_exceptions = types.ModuleType('escpos.exceptions')
        escpos_constants = types.ModuleType('escpos.constants')

        class Usb:  # remplacé/monkeypatché dans les tests qui en ont besoin
            def __init__(self, *args, **kwargs):
                pass

        class USBNotFoundError(Exception):
            pass

        escpos_printer.Usb = Usb
        escpos_exceptions.USBNotFoundError = USBNotFoundError
        escpos_constants.RT_STATUS_PAPER = 4
        escpos.printer = escpos_printer
        escpos.exceptions = escpos_exceptions
        escpos.constants = escpos_constants

        sys.modules['escpos'] = escpos
        sys.modules['escpos.printer'] = escpos_printer
        sys.modules['escpos.exceptions'] = escpos_exceptions
        sys.modules['escpos.constants'] = escpos_constants

    if 'usb' not in sys.modules:
        usb = types.ModuleType('usb')
        usb_core = types.ModuleType('usb.core')

        class USBError(Exception):
            pass

        usb_core.USBError = USBError
        usb.core = usb_core
        sys.modules['usb'] = usb
        sys.modules['usb.core'] = usb_core


_install_hardware_stubs()
