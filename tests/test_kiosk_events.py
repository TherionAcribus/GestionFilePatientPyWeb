"""Incidents de la borne (kiosk_*) : file à part de l'état de l'imprimante.

Régression : les incidents partageaient la file de l'état, qui ne garde que le
dernier message. Pendant une coupure réseau, « liaison coupée » était remplacé
par « liaison rétablie » avant d'avoir pu partir — le serveur ne voyait jamais
la coupure — et un incident pouvait écraser un changement d'état de
l'imprimante (ou l'inverse).

Les scénarios de coupure font tourner le VRAI ``PrinterStatusThread`` avec une
doublure de session HTTP.
"""

import os
import queue
import sys
import threading
import time

import pytest
from requests.exceptions import ConnectionError as ReqConnectionError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import printer as printer_module
from printer import Printer, PrinterStatusThread


def _printer():
    """Printer sans __init__ : seulement les files et leurs verrous."""
    p = Printer.__new__(Printer)
    p.borne_id = 'borne-test'
    p.status_queue = queue.Queue()
    p._status_lock = threading.Lock()
    p._last_status_signature = None
    p.event_queue = queue.Queue(maxsize=printer_module.EVENT_QUEUE_MAX)
    p._last_event_signature = None
    return p


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _Server:
    """Doublure de session : réseau coupé tant que ``up`` est faux ; les
    codes listés dans ``refuse`` reçoivent la réponse HTTP indiquée."""
    def __init__(self, up=True, refuse=None):
        self.up = up
        self.refuse = refuse or {}
        self.received = []
        self._lock = threading.Lock()

    def post(self, url, json=None, headers=None, timeout=None):
        if not self.up:
            raise ReqConnectionError("réseau coupé")
        if json['error'] in self.refuse:
            return _Response(self.refuse[json['error']])
        with self._lock:
            self.received.append(json['error'])
        return _Response(200)

    def close(self):
        pass

    def codes(self):
        with self._lock:
            return list(self.received)


@pytest.fixture()
def fast_backoff(monkeypatch):
    monkeypatch.setattr(printer_module, 'STATUS_BACKOFF_START', 0.02)
    monkeypatch.setattr(printer_module, 'STATUS_BACKOFF_MAX', 0.05)


def _start(printer, server):
    thread = PrinterStatusThread(
        'http://srv/api/printer/status', {'X-App-Token': 't'},
        printer.status_queue, session=server,
        event_queue=printer.event_queue)
    thread.start()
    return thread


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _stop(thread):
    thread.stop()
    thread.join(2)
    assert not thread.is_alive()


# --- Coupure réseau (vrai fil d'envoi) ---------------------------------------

def test_coupure_reseau_les_deux_incidents_arrivent_dans_l_ordre(fast_backoff):
    """Le scénario du constat : la sonde voit la coupure, puis le retour.
    Avant : le serveur ne recevait que « liaison rétablie »."""
    p = _printer()
    server = _Server(up=False)
    thread = _start(p, server)
    try:
        p.report_event('kiosk_link_lost', 'Liaison coupée', is_error=True)
        time.sleep(0.3)                       # plusieurs envois en échec
        p.report_event('kiosk_link_restored', 'Liaison rétablie')
        server.up = True
        assert _wait_for(lambda: len(server.codes()) == 2), server.codes()
        assert server.codes() == ['kiosk_link_lost', 'kiosk_link_restored']
    finally:
        _stop(thread)


def test_etat_imprimante_et_incidents_ne_s_ecrasent_plus(fast_backoff):
    """Pendant la coupure : un changement d'état ET des incidents. Tout
    arrive ; pour l'état, seul le plus récent compte toujours."""
    p = _printer()
    server = _Server(up=False)
    thread = _start(p, server)
    try:
        p.send_printer_status('error_not_found', 'Imprimante absente')
        p.report_event('kiosk_page_lost', 'Page perdue')
        p.send_printer_status('init_ok', 'Imprimante prête')   # remplace l'erreur
        p.report_event('kiosk_link_restored', 'Liaison rétablie')
        time.sleep(0.2)
        server.up = True
        assert _wait_for(lambda: len(server.codes()) >= 3), server.codes()
        time.sleep(0.2)                       # rien d'autre ne doit arriver
        codes = server.codes()
    finally:
        _stop(thread)
    assert [c for c in codes if c.startswith('kiosk_')] == [
        'kiosk_page_lost', 'kiosk_link_restored']
    assert [c for c in codes if not c.startswith('kiosk_')][-1] == 'init_ok'
    assert 'error_not_found' not in codes[codes.index('init_ok'):]


def test_incident_refuse_par_le_serveur_ne_bloque_pas_la_suite(fast_backoff):
    """Un refus définitif (4xx) abandonne l'incident au lieu de le
    réessayer indéfiniment devant les suivants et l'état."""
    p = _printer()
    server = _Server(up=True, refuse={'kiosk_bad': 400})
    thread = _start(p, server)
    try:
        p.report_event('kiosk_bad', 'Refusé')
        p.report_event('kiosk_link_lost', 'Liaison coupée', is_error=True)
        p.send_printer_status('paper_ok', 'Papier OK')
        assert _wait_for(lambda: len(server.codes()) == 2), server.codes()
    finally:
        _stop(thread)
    assert sorted(server.codes()) == ['kiosk_link_lost', 'paper_ok']


def test_incident_reessaye_apres_erreur_serveur_temporaire(fast_backoff):
    """Une erreur 5xx (serveur qui redémarre) n'abandonne pas l'incident."""
    p = _printer()
    server = _Server(up=True, refuse={'kiosk_link_lost': 503})
    thread = _start(p, server)
    try:
        p.report_event('kiosk_link_lost', 'Liaison coupée', is_error=True)
        time.sleep(0.2)
        server.refuse = {}
        assert _wait_for(lambda: server.codes() == ['kiosk_link_lost'])
    finally:
        _stop(thread)


# --- Files (sans fil d'envoi) ---------------------------------------------------

def test_incident_et_etat_vont_chacun_dans_leur_file():
    p = _printer()
    p.report_event('kiosk_page_dead', 'Page figée', is_error=True)
    p.send_printer_status('no_paper', 'Plus de papier')

    assert p.event_queue.get_nowait()['error'] == 'kiosk_page_dead'
    assert p.event_queue.empty()
    assert p.status_queue.get_nowait()['error'] == 'no_paper'
    assert p.status_queue.empty()


def test_un_incident_ne_fait_pas_renvoyer_un_etat_inchange():
    """Les deux déduplications sont indépendantes : un incident entre deux
    états identiques ne fait pas renvoyer l'état."""
    p = _printer()
    p.send_printer_status('no_paper', 'Plus de papier')
    p.report_event('kiosk_page_lost', 'Page perdue')
    p.send_printer_status('no_paper', 'Plus de papier')

    assert p.status_queue.qsize() == 1


def test_incidents_successifs_gardes_dans_l_ordre():
    p = _printer()
    for code in ('kiosk_link_lost', 'kiosk_page_lost', 'kiosk_link_restored'):
        p.report_event(code, code)

    assert [p.event_queue.get_nowait()['error'] for _ in range(3)] == [
        'kiosk_link_lost', 'kiosk_page_lost', 'kiosk_link_restored']


def test_file_pleine_abandonne_le_plus_ancien(caplog):
    p = _printer()
    total = printer_module.EVENT_QUEUE_MAX + 3
    for i in range(total):
        p.report_event('kiosk_page_lost', f'Page perdue n°{i}')

    messages = []
    while not p.event_queue.empty():
        messages.append(p.event_queue.get_nowait()['message'])
    assert len(messages) == printer_module.EVENT_QUEUE_MAX
    assert messages[0] == 'Page perdue n°3'
    assert messages[-1] == f'Page perdue n°{total - 1}'
    assert any("abandonné" in r.getMessage() for r in caplog.records)


def test_incident_horodate_a_sa_creation():
    """Envoyé tard après une coupure, l'incident garde l'heure où il s'est
    produit (enregistrée par le serveur dans ``generated_at``)."""
    p = _printer()
    p.report_event('kiosk_link_lost', 'Liaison coupée', is_error=True)
    item = p.event_queue.get_nowait()
    assert item['timestamp'] and item['borne_id'] == 'borne-test'
    assert item['is_error'] is True
