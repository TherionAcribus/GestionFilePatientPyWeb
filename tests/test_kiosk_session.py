"""Authentification de la borne par ticket de session (point 5).

Régression : la borne injectait un couple utilisateur/mot de passe dans le DOM
de /login (assets/login.js) — credentials réutilisables livrés au contexte de
la page, et un diagnostic éditeur qui ne les testait même pas. Désormais :

- la borne échange son jeton applicatif contre un TICKET signé à courte durée
  de vie (``POST /api/kiosk/session_ticket``) ;
- elle navigue la WebView sur l'URL de connexion signée
  (``/patient/kiosk_login/<ticket>``), qui pose un cookie HttpOnly et
  redirige vers /patient ;
- si la session expire (page /login détectée), elle redemande un ticket —
  borné à une tentative par ``KIOSK_RELOGIN_MIN_INTERVAL`` pour ne pas
  marteler le serveur.

Ces tests n'ouvrent aucune fenêtre ni socket : ``session`` et ``window`` sont
des doublures.
"""

import os
import sys
import threading

import pytest
from requests.exceptions import ConnectionError as ReqConnectionError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import main
from errors import SessionUnavailableError


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeSession:
    """Enregistre les POST et répond selon le scénario configuré."""
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def post(self, url, headers=None, timeout=None, data=None):
        self.calls.append({"url": url, "headers": headers or {}})
        if self.error:
            raise self.error
        return self.response


class _FakeWindow:
    def __init__(self, url):
        self._url = url
        self.loaded_urls = []

    def get_current_url(self):
        return self._url

    def load_url(self, url):
        self._url = url
        self.loaded_urls.append(url)

    def evaluate_js(self, _script):
        pass


def _bare_client(base_url="http://127.0.0.1:5000"):
    """WebViewClient sans __init__ : uniquement ce que le flux ticket utilise."""
    client = main.WebViewClient.__new__(main.WebViewClient)
    client.base_url = base_url
    client.app_token = "token-appli"
    client.session = _FakeSession()
    client.window = None
    client._patient_login_url = None
    client._last_relogin_attempt = None
    client._last_page_recovery_attempt = None
    client._patient_page_shown = False
    client.operational = False
    client._operational_lock = threading.Lock()
    client._window_ready = threading.Event()
    return client


# --- Obtention du ticket ----------------------------------------------------

def test_fetch_login_url_posts_app_token_and_returns_full_url():
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "/patient/kiosk_login/signed-ticket"}))

    url = client._fetch_patient_login_url()

    assert url == "http://127.0.0.1:5000/patient/kiosk_login/signed-ticket"
    call = client.session.calls[0]
    assert call["url"] == "http://127.0.0.1:5000/api/kiosk/session_ticket"
    assert call["headers"]["X-App-Token"] == "token-appli"


def test_fetch_login_url_preserves_absolute_and_prefixed_urls():
    client = _bare_client(base_url="https://site.example/prefix")
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "/patient/kiosk_login/signed-ticket"}))
    assert client._fetch_patient_login_url() == (
        "https://site.example/prefix/patient/kiosk_login/signed-ticket")

    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "https://srv.example/patient/kiosk_login/ticket"}))
    assert client._fetch_patient_login_url() == (
        "https://srv.example/patient/kiosk_login/ticket")


def test_fetch_login_url_refuses_non_200():
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(401, {}))
    with pytest.raises(SessionUnavailableError):
        client._fetch_patient_login_url()


def test_fetch_login_url_refuses_network_error():
    client = _bare_client()
    client.session = _FakeSession(error=ReqConnectionError("down"))
    with pytest.raises(SessionUnavailableError):
        client._fetch_patient_login_url()


def test_fetch_login_url_refuses_unexpected_payload():
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(200, {"oops": "rien"}))
    with pytest.raises(SessionUnavailableError):
        client._fetch_patient_login_url()


# --- Navigation ------------------------------------------------------------

def test_operational_kiosk_navigates_ticket_url_not_patient():
    """La fenêtre doit ouvrir l'URL de connexion signée (qui pose le cookie),
    pas /patient directement — sinon redirection vers /login."""
    client = _bare_client()
    client.operational = True
    client._patient_login_url = "http://127.0.0.1:5000/patient/kiosk_login/t1"
    client.window = _FakeWindow("about:blank")
    client._window_ready.set()

    client._maybe_show_patient_page()

    assert client.window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/t1"]


# --- Re-connexion bornée ----------------------------------------------------

def test_login_page_triggers_ticket_relogin():
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "/patient/kiosk_login/nouveau"}))
    client.window = _FakeWindow("http://127.0.0.1:5000/login")

    client.on_loaded()

    # Un ticket a été redemandé et la nouvelle URL signée naviguée.
    assert len(client.session.calls) == 1
    assert client.window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/nouveau"]


def test_kiosk_login_route_is_not_confused_with_login_page():
    """/patient/kiosk_login/<ticket> CRÉE la session : la détecter comme une
    page de connexion bouclerait des demandes de ticket."""
    client = _bare_client()
    client.window = _FakeWindow(
        "http://127.0.0.1:5000/patient/kiosk_login/ticket-en-cours")

    client.on_loaded()

    assert client.session.calls == []


def test_relogin_is_rate_limited():
    """Deux détections /login rapprochées = une seule demande de ticket."""
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "/patient/kiosk_login/t"}))
    client.window = _FakeWindow("http://127.0.0.1:5000/login")

    client.on_loaded()
    client.on_loaded()  # encore sur /login juste après

    assert len(client.session.calls) == 1


def test_relogin_retries_after_interval(monkeypatch):
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "/patient/kiosk_login/t"}))
    client.window = _FakeWindow("http://127.0.0.1:5000/login")

    client.on_loaded()
    # On simule l'écoulement de l'intervalle minimal, puis une NOUVELLE
    # redirection vers /login (la première renavigation a changé l'URL).
    client._last_relogin_attempt -= main.KIOSK_RELOGIN_MIN_INTERVAL + 1
    client.window._url = "http://127.0.0.1:5000/login"
    client.on_loaded()

    assert len(client.session.calls) == 2


def test_relogin_survives_ticket_failure():
    """Ticket refusé : pas de navigation, pas de plantage — la prochaine
    détection réessaiera (après l'intervalle)."""
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(500, {}))
    client.window = _FakeWindow("http://127.0.0.1:5000/login")

    client.on_loaded()  # ne doit pas lever

    assert client.window.loaded_urls == []


def test_page_watchdog_recovers_dead_page_with_fresh_ticket():
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client._patient_page_shown = True
    window = _FakeWindow("about:blank")
    client.window = window
    session = _FakeSession(_FakeResponse(
        200, {"login_url": "https://srv/secure?token=watchdog"}))
    client.session = session

    client._recover_patient_page()

    assert session.calls == [{
        "url": "http://127.0.0.1:5000/api/kiosk/session_ticket",
        "headers": {"X-App-Token": "token-appli"},
    }]
    assert window.loaded_urls == ["https://srv/secure?token=watchdog"]
    assert client._patient_page_shown is True
    assert client._patient_login_url == "https://srv/secure?token=watchdog"


def test_page_watchdog_keeps_server_page_and_login_untouched():
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    session = _FakeSession(_FakeResponse(200, {"login_url": "/patient/kiosk_login/t"}))
    client.session = session

    client.window = _FakeWindow("http://127.0.0.1:5000/patient")
    client._recover_patient_page()
    assert session.calls == []

    client.window = _FakeWindow("http://127.0.0.1:5000/login")
    client._recover_patient_page()
    assert session.calls == []  # _recover_kiosk_session gère /login


def test_page_watchdog_rate_limits_unrecoverable_session():
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client.window = _FakeWindow("chrome-error://webengine/")
    client.session = _FakeSession(_FakeResponse(500, {}))

    client._recover_patient_page()
    client._recover_patient_page()

    assert len(client.session.calls) == 1
    assert client.connected is False
    assert client.window.loaded_urls == []


def test_js_api_namespace_hides_members_from_pywebview_generation():
    """La CSP refuse le `new Function` de génération automatique : `dir()` doit
    rester vide, mais `js_bridge_call` résout toujours les membres via getattr."""
    def action():
        return {"ok": True}

    api = main._JSApiNamespace(printer=main._JSApiNamespace(print_ticket=action))

    assert dir(api) == []
    assert api.printer.print_ticket() == {"ok": True}
    assert getattr(api.printer, "set_print_callback", None) is None


def test_run_page_script_prefers_csp_safe_run_js():
    class Window:
        def __init__(self):
            self.scripts = []

        def run_js(self, script):
            self.scripts.append(("run_js", script))

        def evaluate_js(self, _script):
            raise AssertionError("evaluate_js utilise eval(), bloqué par la CSP")

    client = _bare_client()
    client.window = Window()

    client._run_page_script("window.x = 1")

    assert client.window.scripts == [("run_js", "window.x = 1")]


def test_run_page_script_falls_back_to_evaluate_js():
    class Window:
        def __init__(self):
            self.scripts = []

        def evaluate_js(self, script):
            self.scripts.append(script)

    client = _bare_client()
    client.window = Window()

    client._run_page_script("window.x = 1")

    assert client.window.scripts == ["window.x = 1"]


def test_run_uses_persistent_webview_storage(monkeypatch, tmp_path):
    """localStorage porte les acquittements d'impression : la WebView ne doit
    pas repartir en mode privé à chaque démarrage de la borne."""
    storage_path = tmp_path / "webview-profile"

    class _Settings:
        fullscreen = False
        debug = False

    class _Config:
        settings = _Settings()

        def webview_storage_path(self):
            return str(storage_path)

    starts = []
    monkeypatch.setattr(main, "Config", lambda: _Config())
    monkeypatch.setattr(
        main.webview, "start", lambda **kwargs: starts.append(kwargs), raising=False)

    client = _bare_client()
    client.create_window = lambda: None
    client._init_stop = threading.Event()
    client._token_refresh_stop = threading.Event()
    client._init_thread = None
    client._token_refresh_thread = None
    client.printer = None

    client.run()

    assert starts == [{
        "debug": False,
        "gui": "qt",
        "private_mode": False,
        "storage_path": str(storage_path),
    }]
