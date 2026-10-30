"""Fermeture de la borne et impression en cours (maintenance).

Avant : F10 fermait la fenêtre tout de suite et pouvait couper un ticket, ou
fermer la page avant qu'elle note l'acquittement — le patient repartait
avec un ticket absent de la file. ``PrinterAPI`` compte désormais les
tickets en cours, et la fermeture attend (``wait_until_idle``).

Aussi : le dernier message envoyé au serveur à la fermeture
(``Printer.cleanup(final_event=...)``), pour que la borne en maintenance
n'y passe pas pour injoignable."""

import threading

import printer as printer_module
from printer import PrinterAPI


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _api_with_blocking_print():
    started = threading.Event()
    release = threading.Event()

    def slow_print(data, job_id=None):
        started.set()
        release.wait(5)
        return {'success': True, 'code': 'print_ok', 'message': 'ok'}

    api = PrinterAPI()
    api.set_print_callback(slow_print)
    return api, started, release


def test_rien_a_attendre_sans_impression():
    api = PrinterAPI()
    assert not api.printing()
    assert api.wait_until_idle(0.1, settle_s=0) is True


def test_ticket_en_cours_detecte_puis_attendu():
    api, started, release = _api_with_blocking_print()
    worker = threading.Thread(target=api.print_ticket, args=("x",))
    worker.start()
    try:
        assert started.wait(2)
        assert api.printing()
        # Pas fini : l'attente échoue au bout du délai.
        assert api.wait_until_idle(0.05, settle_s=0) is False
        release.set()
        assert api.wait_until_idle(2, settle_s=0) is True
        assert not api.printing()
    finally:
        release.set()
        worker.join(2)


def test_attente_reveillee_des_la_fin_du_ticket():
    api, started, release = _api_with_blocking_print()
    worker = threading.Thread(target=api.print_ticket, args=("x",))
    worker.start()
    assert started.wait(2)
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(api.wait_until_idle(10, settle_s=0)))
    waiter.start()
    release.set()
    waiter.join(2)
    worker.join(2)
    assert result == [True]


def test_delai_laisse_a_la_page_apres_le_ticket():
    """La page doit avoir le temps de noter l'acquittement."""
    clock = _Clock()
    api = PrinterAPI()
    api.set_print_callback(lambda data, job_id=None: {
        'success': True, 'code': 'print_ok', 'message': 'ok'})
    api.print_ticket("x")
    api._last_job_end = clock.now          # ticket fini « à l'instant »
    assert api.wait_until_idle(0, settle_s=2, clock=clock) is False
    clock.now += 2
    assert api.wait_until_idle(0, settle_s=2, clock=clock) is True


def test_ticket_en_erreur_compte_aussi():
    api = PrinterAPI()

    def broken(data, job_id=None):
        raise RuntimeError("USB")

    api.set_print_callback(broken)
    assert api.print_ticket("x")['success'] is False
    assert not api.printing()            # compteur toujours remis à zéro


# --- Dernier message au serveur --------------------------------------------------


class _StatusThread:
    def __init__(self, accept=True):
        self.stopped = False
        self.sent = []
        self._accept = accept

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False

    def send_once(self, item):
        # Envoyé APRÈS l'arrêt du thread : rien ne passe derrière.
        assert self.stopped
        self.sent.append(item)
        return self._accept


def _printer(status_thread):
    printer = printer_module.Printer.__new__(printer_module.Printer)
    printer.p = None
    printer._usb_lock = threading.RLock()
    printer._closing = threading.Event()
    printer._health_thread = None
    printer.borne_id = "borne-test"
    printer.status_thread = status_thread
    return printer


def test_dernier_message_envoye_a_la_fermeture():
    status = _StatusThread()
    _printer(status).cleanup(final_event=("kiosk_maintenance", "Configuration"))
    [item] = status.sent
    assert item["error"] == "kiosk_maintenance"
    assert item["message"] == "Configuration"
    assert item["borne_id"] == "borne-test"
    assert item["is_error"] is False


def test_fermeture_ordinaire_sans_message():
    status = _StatusThread()
    _printer(status).cleanup()
    assert status.sent == []


def test_serveur_injoignable_ne_bloque_pas_la_fermeture():
    status = _StatusThread(accept=False)
    _printer(status).cleanup(final_event=("kiosk_stopped", "Quitté"))
    assert len(status.sent) == 1


def test_envoi_unique_borne_dans_le_temps(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(timeout)
        raise printer_module.RequestException("serveur injoignable")

    monkeypatch.setattr(printer_module.requests, "post", fake_post)
    thread = printer_module.PrinterStatusThread(
        "http://serveur/api/printer/status", {"X-App-Token": "t"}, None)
    assert thread.send_once({"error": "kiosk_stopped"}) is False
    assert calls == [printer_module.FINAL_STATUS_TIMEOUT]
