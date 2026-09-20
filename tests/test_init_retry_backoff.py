"""Backoff de la boucle d'initialisation (reconnexion de la borne).

Régression : ``_supervise`` annonçait « nouvel essai dans N s » pour les pannes
ATTENDUES (``TokenUnavailableError``, ``PrinterNotReadyError``…) mais
n'attendait jamais — ``_init_stop.wait(delay)`` et le doublement du délai ne
figuraient que dans la branche des exceptions inattendues. Une borne hors
ligne martelait donc ``/api/get_app_token`` et l'USB en boucle serrée.

On vérifie, sans réseau ni matériel, que l'attente et le backoff exponentiel
borné s'appliquent à TOUS les échecs : on espionne ``_init_stop.wait`` (qui
n'attend pas réellement) et on enregistre les délais demandés.
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import main
from errors import TokenUnavailableError


def _bare_client():
    """WebViewClient sans passer par __init__ (qui lirait la config, ouvrirait
    une session réseau et lancerait le thread). On ne peuple que ce que la
    boucle d'init utilise."""
    client = main.WebViewClient.__new__(main.WebViewClient)
    client._init_stop = threading.Event()
    client.connected = False
    client._init_thread = None
    return client


def _spy_waits(client, max_waits):
    """Remplace ``_init_stop.wait`` par un espion qui enregistre le délai
    demandé, n'attend pas, et arrête la boucle après ``max_waits`` attentes."""
    delays = []
    orig_wait = client._init_stop.wait

    def spy(timeout=None):
        delays.append(timeout)
        if len(delays) >= max_waits:
            client._init_stop.set()
        return orig_wait(0)

    client._init_stop.wait = spy
    return delays


@pytest.fixture
def fast_backoff(monkeypatch):
    """Constantes de backoff ramenées à des valeurs minuscules/déterministes."""
    monkeypatch.setattr(main, "INIT_BACKOFF_START", 1)
    monkeypatch.setattr(main, "INIT_BACKOFF_MAX", 4)


def _fail_then_stop(client, attempts, exc):
    def _fail(**_kwargs):
        attempts.append(1)
        # Filet de sécurité : même si l'attente n'était pas appelée (bug), la
        # boucle s'arrête au lieu de tourner indéfiniment dans le test.
        if len(attempts) >= 50:
            client._init_stop.set()
        raise exc
    return _fail


def test_expected_failures_wait_and_back_off(fast_backoff):
    client = _bare_client()
    attempts = []
    client.get_app_token = _fail_then_stop(
        client, attempts, TokenUnavailableError("serveur injoignable"))
    delays = _spy_waits(client, 5)

    client.start_initialization()
    client._init_thread.join(timeout=5)

    assert not client._init_thread.is_alive()
    # Chaque échec attend réellement le délai annoncé, avec doublement borné.
    assert delays == [1, 2, 4, 4, 4]
    # Autant de tentatives que d'attentes (une tentative par itération).
    assert len(attempts) == 5


def test_unexpected_failures_wait_too(fast_backoff):
    """La branche « inattendu » garde le même comportement après la
    mutualisation de l'attente."""
    client = _bare_client()
    client.get_app_token = _fail_then_stop(
        client, [], RuntimeError("bogue quelconque"))
    delays = _spy_waits(client, 3)

    client.start_initialization()
    client._init_thread.join(timeout=5)

    assert delays == [1, 2, 4]


def test_printer_failure_waits_too(fast_backoff):
    """Token OK mais imprimante pas prête : même attente (panne attendue)."""
    from errors import PrinterNotReadyError
    client = _bare_client()
    client.get_app_token = lambda **_kw: True
    attempts = []
    client.initialize_printer = _fail_then_stop(
        client, attempts, PrinterNotReadyError("imprimante absente"))
    client.start_token_refresh = lambda: None
    client._set_operational = lambda v: setattr(client, "operational", v)
    delays = _spy_waits(client, 3)

    client.start_initialization()
    client._init_thread.join(timeout=5)

    assert delays == [1, 2, 4]
    assert len(attempts) == 3


def test_success_stops_loop_without_any_wait(fast_backoff):
    """Quand tout réussit, la boucle se termine sans aucune attente."""
    client = _bare_client()
    client.get_app_token = lambda **_kw: True
    client.initialize_printer = lambda: None
    client.start_token_refresh = lambda: None
    client._set_operational = lambda v: setattr(client, "operational", v)
    delays = _spy_waits(client, 10)

    client.start_initialization()
    client._init_thread.join(timeout=5)

    assert delays == []
    assert client.operational is True
    assert client.connected is True
