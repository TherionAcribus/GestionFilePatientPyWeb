"""Exceptions typées (point 1) et arrêt borné des threads (point 4).

Point 1 — le code levait/rattrapait des ``Exception`` génériques : impossible de
distinguer une panne attendue (serveur injoignable, charge d'impression refusée)
d'un bogue. On vérifie ici la hiérarchie des exceptions métier et le type
réellement levé par la validation des charges d'impression.

Point 4 — ``status_thread.join()`` était SANS timeout : un thread bloqué (envoi
HTTP au timeout réseau maximal) figeait la fermeture de la borne. ``cleanup()``
passe désormais par ``join_with_timeout``, qui rend la main et journalise.
"""

import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import printer as printer_module
from errors import (
    BorneError,
    PrinterNotReadyError,
    PrintPayloadError,
    TokenUnavailableError,
)
from printer import decode_and_validate_print_payload, join_with_timeout

# --- Hiérarchie des exceptions ---------------------------------------------

def test_business_errors_share_a_common_base():
    for exc in (TokenUnavailableError, PrinterNotReadyError, PrintPayloadError):
        assert issubclass(exc, BorneError)


def test_payload_error_stays_a_value_error():
    """Compatibilité : le code (et les tests) qui filtrent ValueError sur la
    validation des charges continuent de fonctionner."""
    assert issubclass(PrintPayloadError, ValueError)


@pytest.mark.parametrize("payload", [
    None,                     # type invalide
    "",                       # vide
    "pas du base64 !!",       # alphabet invalide
])
def test_invalid_payload_raises_typed_error(payload):
    with pytest.raises(PrintPayloadError):
        decode_and_validate_print_payload(payload)


def test_typed_payload_error_is_catchable_as_borne_error():
    with pytest.raises(BorneError):
        decode_and_validate_print_payload("###")


# --- Arrêt borné des threads ------------------------------------------------

def test_join_with_timeout_returns_true_for_finished_thread():
    thread = threading.Thread(target=lambda: None)
    thread.start()
    assert join_with_timeout(thread, "test", timeout=1) is True


def test_join_with_timeout_accepts_missing_thread():
    # cleanup() est appelable même si un thread n'a jamais été démarré.
    assert join_with_timeout(None, "test") is True


def test_join_with_timeout_gives_up_and_warns(caplog):
    """Un thread qui ne s'arrête pas ne doit pas bloquer la fermeture : la
    fonction rend la main au bout du délai ET le signale dans les logs."""
    keep_running = threading.Event()
    thread = threading.Thread(target=keep_running.wait, daemon=True)
    thread.start()
    try:
        with caplog.at_level(logging.WARNING, logger="borne.printer"):
            assert join_with_timeout(thread, "thread récalcitrant", timeout=0.05) is False
        assert "thread récalcitrant" in caplog.text
    finally:
        keep_running.set()
        thread.join(timeout=2)


def test_cleanup_does_not_hang_on_a_stuck_status_thread(monkeypatch):
    """Régression du point 4 : avec un ``join()`` sans timeout, cleanup() ne
    rendait jamais la main si le thread de statut restait bloqué."""
    blocked = threading.Event()

    class StuckStatusThread(threading.Thread):
        def __init__(self):
            super().__init__(daemon=True)
            self.stopped = False

        def run(self):
            blocked.wait()  # ne s'arrête pas malgré stop()

        def stop(self):
            self.stopped = True

    printer = printer_module.Printer.__new__(printer_module.Printer)
    printer.p = None
    printer._usb_lock = threading.RLock()
    printer._closing = threading.Event()
    printer._health_thread = None
    printer.status_thread = StuckStatusThread()
    printer.status_thread.start()

    # Délai d'attente ramené à ~0 pour ne pas ralentir la suite de tests.
    monkeypatch.setattr(printer_module, "THREAD_JOIN_TIMEOUT", 0.05)
    try:
        printer.cleanup()
    finally:
        blocked.set()
        printer.status_thread.join(timeout=2)

    assert printer.status_thread.stopped is True
    assert printer._closing.is_set()
