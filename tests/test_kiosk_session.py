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
import time

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

    def head(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}})
        if self.error:
            raise self.error
        return self.response


class _FakeWindow:
    def __init__(self, url):
        self._url = url
        self.loaded_urls = []
        self.loaded_html = []
        # Scripts injectés via evaluate_js (le repli de _run_page_script sans
        # run_js) : la sonde de liaison pousse __kioskLinkState par ce canal.
        self.js_scripts = []

    def get_current_url(self):
        return self._url

    def load_url(self, url):
        self._url = url
        self.loaded_urls.append(url)

    def load_html(self, html):
        self.loaded_html.append(html)

    def evaluate_js(self, script):
        self.js_scripts.append(script)


class _FakePrinterAPI:
    """Façade de remontée d'incidents borne : enregistre les appels à
    report_event au lieu d'enfiler un statut imprimante."""
    def __init__(self):
        self.events = []

    def report_event(self, code, message, is_error=False):
        self.events.append((code, message, is_error))


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
    client._offline_screen_shown = False
    client._dom_probe_pending = False
    client._dom_probe_sent_at = None
    client._dom_probe_error_logged = False
    client._evaluate_js_blocking = None
    client._link_alive = True
    client._link_misses = 0
    client._link_probe_stop = threading.Event()
    client._link_probe_thread = None
    client.printer_api = _FakePrinterAPI()
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
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/ticket"}))
    assert client._fetch_patient_login_url() == (
        "http://127.0.0.1:5000/patient/kiosk_login/ticket")


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
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/watchdog"}))
    client.session = session

    client._recover_patient_page()

    assert session.calls == [{
        "url": "http://127.0.0.1:5000/api/kiosk/session_ticket",
        "headers": {"X-App-Token": "token-appli"},
    }]
    assert window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/watchdog"]
    assert client._patient_page_shown is True
    assert client._patient_login_url == (
        "http://127.0.0.1:5000/patient/kiosk_login/watchdog")


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


def test_page_watchdog_shows_offline_screen_while_server_down():
    """Page morte + serveur injoignable : la WebView affiche l'écran local
    « Borne hors ligne » plutôt que la page d'erreur du navigateur, jusqu'à
    ce qu'un ticket de session redevienne disponible."""
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client._patient_page_shown = True
    window = _FakeWindow("chrome-error://webengine/")
    client.window = window
    client.session = _FakeSession(_FakeResponse(500, {}))

    client._recover_patient_page()

    assert window.loaded_html != []
    assert client.connected is False
    assert client._offline_screen_shown is True

    # Le serveur revient : le passage suivant du watchdog retente un ticket
    # et recharge la page patient à la place de l'écran hors ligne.
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/retour"}))
    client._last_page_recovery_attempt -= main.PATIENT_PAGE_RECOVERY_INTERVAL + 1
    client._recover_patient_page()

    assert window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/retour"]
    assert client.connected is True
    assert client._offline_screen_shown is False


def test_page_watchdog_forces_reload_when_dom_is_dead():
    """URL /patient valide mais moteur muet : après le timeout de la sonde
    DOM, la borne force une récupération complète (ticket + navigation)."""
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client._patient_page_shown = True
    window = _FakeWindow("http://127.0.0.1:5000/patient")
    client.window = window
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/regen"}))
    # Une sonde déjà envoyée, jamais revenue, plus vieille que le timeout.
    client._dom_probe_pending = True
    client._dom_probe_sent_at = (
        time.monotonic() - (main.DOM_PROBE_TIMEOUT + 1))

    client._recover_patient_page()

    assert window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/regen"]
    assert client.connected is True
    assert client._dom_probe_pending is False


def test_session_ticket_refusal_is_reported_as_kiosk_event():
    """Ticket refusé pendant la reconnexion /login : l'incident est remonté
    au serveur (journal « Bornes ») sans masquer l'état métier imprimante."""
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(500, {}))
    client.window = _FakeWindow("http://127.0.0.1:5000/login")

    client.on_loaded()

    assert [e[0] for e in client.printer_api.events] == ["kiosk_session_fail"]
    assert client.printer_api.events[0][2] is True


def test_page_watchdog_reports_lost_page_as_kiosk_event():
    """Page servie disparue : l'incident de récupération est journalisé
    (kiosk_page_lost) — non bloquant, donc sans drapeau d'erreur."""
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client._patient_page_shown = True
    client.window = _FakeWindow("chrome-error://webengine/")
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/r"}))

    client._recover_patient_page()

    assert [e[0] for e in client.printer_api.events] == ["kiosk_page_lost"]
    assert client.printer_api.events[0][2] is False


def test_dom_probe_timeout_reports_kiosk_page_dead():
    """Moteur figé sur URL valide : le timeout de la sonde DOM remonte
    l'incident kiosk_page_dead (en erreur) avant le rechargement forcé."""
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client._patient_page_shown = True
    client.window = _FakeWindow("http://127.0.0.1:5000/patient")
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/r"}))
    client._dom_probe_pending = True
    client._dom_probe_sent_at = (
        time.monotonic() - (main.DOM_PROBE_TIMEOUT + 1))

    client._recover_patient_page()

    assert [e[0] for e in client.printer_api.events] == ["kiosk_page_dead"]
    assert client.printer_api.events[0][2] is True


def test_force_reload_session_failure_reports_kiosk_event():
    """Rechargement forcé sans ticket disponible : kiosk_session_fail est
    remonté en erreur, puis l'écran hors ligne s'affiche."""
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    client._patient_page_shown = True
    client.window = _FakeWindow("http://127.0.0.1:5000/patient")
    client.session = _FakeSession(_FakeResponse(500, {}))

    client._force_patient_reload()

    assert [e[0] for e in client.printer_api.events] == ["kiosk_session_fail"]
    assert client.printer_api.events[0][2] is True
    assert client.connected is False


def test_page_watchdog_starts_dom_probe_on_live_url():
    """Page valide affichée : le watchdog arme une sonde DOM (thread jetable)
    sans toucher au réseau."""
    client = _bare_client()
    client.operational = True
    client.connected = True
    client._window_ready.set()
    window = _FakeWindow("http://127.0.0.1:5000/patient")
    client.window = window
    client.session = _FakeSession()

    client._recover_patient_page()

    assert client.session.calls == []
    # La sonde finit (evaluate_js de la doublure rend la main) : pending
    # repasse à False rapidement — on ne l'exige pas ici, seulement
    # l'absence d'effet sur la navigation.
    assert window.loaded_urls == []


def test_link_probe_detects_cut_and_recovery():
    """La sonde déclare la coupure au 2e échec (pas au 1er : pas de
    clignotement), pousse l'état à la page et journalise l'incident."""
    client = _bare_client()
    client.window = _FakeWindow("http://127.0.0.1:5000/patient")
    # Une coupure réseau LÈVE (ConnectionError) : un code HTTP quelconque ne
    # fait pas échouer session.head.
    client.session = _FakeSession(error=ReqConnectionError("down"))

    client._check_server_link()
    assert client._link_alive is True          # un seul échec ne suffit pas
    client._check_server_link()
    assert client._link_alive is False
    assert [e[0] for e in client.printer_api.events] == ["kiosk_link_lost"]
    assert client.printer_api.events[0][2] is True
    # La page reçoit l'état via le pont JS.
    assert any("__kioskLinkState(false)" in s
               for s in client.window.js_scripts)

    client.session = _FakeSession(_FakeResponse(200, {}))
    client._check_server_link()
    assert client._link_alive is True
    assert [e[0] for e in client.printer_api.events][-1] == "kiosk_link_restored"
    assert any("__kioskLinkState(true)" in s
               for s in client.window.js_scripts)



def test_link_probe_vise_healthz():
    """La sonde interroge /healthz (réponse fixe) et non /patient, que le
    serveur rendait en entier toutes les 10 s par borne."""
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(200, {}))

    client._check_server_link()

    assert [c["url"] for c in client.session.calls] == [
        "http://127.0.0.1:5000/healthz"]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_link_probe_proxy_devant_un_serveur_arrete(status):
    """Le proxy répond, pas le serveur : c'est une coupure."""
    client = _bare_client()
    client.window = _FakeWindow("http://127.0.0.1:5000/patient")
    client.session = _FakeSession(_FakeResponse(status, {}))

    client._check_server_link()
    client._check_server_link()

    assert client._link_alive is False
    assert [e[0] for e in client.printer_api.events] == ["kiosk_link_lost"]


@pytest.mark.parametrize("status", [200, 404, 405, 401])
def test_link_probe_serveur_qui_repond_reste_joignable(status):
    """404/405 : serveur ancien sans /healthz, mais qui répond."""
    client = _bare_client()
    client.session = _FakeSession(_FakeResponse(status, {}))

    for _ in range(3):
        client._check_server_link()

    assert client._link_alive is True
    assert client.printer_api.events == []

# --- Sonde DOM : journal non inondé ----------------------------------------

class _FakeJavascriptException(Exception):
    """Doublure de ``webview.JavascriptException`` (pywebview absent en CI)."""


def _csp_refusal(_script):
    # Ce que fait réellement pywebview sur une page du serveur : eval()
    # refusé par la CSP « script-src 'self' » (vérifié sous Qt WebEngine).
    raise _FakeJavascriptException({"name": "EvalError"})


def _probe_errors(caplog):
    return [r for r in caplog.records
            if r.name == "borne.main" and r.levelno >= 30]


def test_dom_probe_csp_refusal_is_proof_of_life_without_log(monkeypatch, caplog):
    """Régression : evaluate_js est toujours refusé par la CSP du serveur ;
    chaque passage (toutes les 30 s) écrivait une trace complète et le
    journal (6 Mo) était écrasé en un ou deux jours."""
    monkeypatch.setattr(main.webview, "JavascriptException",
                        _FakeJavascriptException, raising=False)
    client = _bare_client()
    caplog.set_level("DEBUG", logger="borne.main")

    for _ in range(50):
        client._dom_probe_pending = True
        client._run_dom_probe(_csp_refusal)
        # Le moteur a répondu : pas de rechargement forcé à venir.
        assert client._dom_probe_pending is False

    assert _probe_errors(caplog) == []


def test_dom_probe_unexpected_error_traced_once_per_episode(monkeypatch, caplog):
    """Une erreur inattendue reste tracée en entier, mais une seule fois
    tant qu'elle dure ; après une sonde réussie, un nouvel épisode l'est à
    nouveau."""
    monkeypatch.setattr(main.webview, "JavascriptException",
                        _FakeJavascriptException, raising=False)
    client = _bare_client()
    caplog.set_level("DEBUG", logger="borne.main")

    def broken(_script):
        raise RuntimeError("fenêtre détruite")

    for _ in range(10):
        client._run_dom_probe(broken)
    errors = _probe_errors(caplog)
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert client._dom_probe_pending is False

    client._run_dom_probe(lambda _script: None)   # le moteur répond à nouveau
    client._run_dom_probe(broken)
    assert len(_probe_errors(caplog)) == 2


def test_dom_probe_thread_runs_the_probe_body():
    """La sonde armée par le watchdog exécute bien ce corps dans son
    thread : le drapeau « en attente » retombe dès la réponse."""
    client = _bare_client()
    client._evaluate_js_blocking = _csp_refusal
    client.window = _FakeWindow("http://127.0.0.1:5000/patient")

    client._check_dom_probe()

    deadline = time.monotonic() + 2
    while client._dom_probe_pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client._dom_probe_pending is False


# --- Moteur d'affichage arrêté (renderProcessTerminated) ---------------------

class _FakeStatus:
    def __init__(self, name):
        self.name = name


def _renderer_client(operational=True):
    client = _bare_client()
    client.operational = operational
    client.connected = operational
    client._window_ready.set()
    client._patient_page_shown = True
    client._page_watchdog_stop = threading.Event()
    client._last_renderer_recovery = None
    client._config_error = False
    client._config_errors = []
    client._diagnosis_tracker = main.DiagnosisTracker()
    client._displayed_diagnosis = None
    client.window = _FakeWindow("http://127.0.0.1:5000/patient")
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/neuf"}))
    return client


def test_moteur_plante_page_patient_rechargee_avec_ticket_neuf():
    client = _renderer_client()
    client._dom_probe_pending = True     # sonde partie vers le moteur mort

    client._recover_after_renderer_crash("plantage, code -11")

    assert client.window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/neuf"]
    assert client._dom_probe_pending is False
    code, message, is_error = client.printer_api.events[0]
    assert code == "kiosk_renderer_crash" and is_error is True
    assert "plantage, code -11" in message


def test_moteur_plante_ecran_hors_ligne_recharge():
    client = _renderer_client(operational=False)

    client._recover_after_renderer_crash("processus tué, code 1")

    assert client.window.loaded_urls == []
    assert len(client.window.loaded_html) == 1
    assert "hors ligne" in client.window.loaded_html[0].lower()


def test_moteur_plante_ecran_configuration_invalide_recharge():
    client = _renderer_client(operational=False)
    client._config_error = True
    client._config_errors = ["URL du serveur invalide"]

    client._recover_after_renderer_crash("plantage, code 5")

    assert "URL du serveur invalide" in client.window.loaded_html[0]
    assert client.window.loaded_urls == []


def test_plantages_en_serie_espaces():
    client = _renderer_client()
    now = [1000.0]
    waits = []
    client._page_watchdog_stop = type("Stop", (), {
        "wait": lambda _self, s: waits.append(round(s, 1)) or False})()

    client._recover_after_renderer_crash("plantage", clock=lambda: now[0])
    now[0] += 3
    client._recover_after_renderer_crash("plantage", clock=lambda: now[0])

    assert waits == [main.RENDERER_RECOVERY_MIN_INTERVAL - 3]
    assert len(client.window.loaded_urls) == 2


def test_arret_de_la_borne_pendant_l_attente_annule_la_reprise():
    client = _renderer_client()
    client._last_renderer_recovery = time.monotonic()
    client._page_watchdog_stop.set()

    client._recover_after_renderer_crash("plantage")

    assert client.window.loaded_urls == []
    assert client.printer_api.events == []


def test_signal_qt_lance_la_reprise_hors_du_fil_qt(monkeypatch):
    client = _renderer_client()
    done = threading.Event()
    seen = []

    def recover(description):
        seen.append((description, threading.current_thread().name))
        done.set()

    monkeypatch.setattr(client, "_recover_after_renderer_crash", recover)
    client._on_render_process_terminated(_FakeStatus("CrashedTerminationStatus"), -11)

    assert done.wait(2)
    assert seen == [("plantage, code -11", "reprise-affichage")]


_REAL_RENDERER_CRASH = r"""
import sys, threading, types
sys.path.insert(0, sys.argv[1])
from PySide6.QtWebEngineWidgets import QWebEngineView  # avant QApplication
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication
import main

app = QApplication([])
view = QWebEngineView()
client = main.WebViewClient.__new__(main.WebViewClient)
client.window = types.SimpleNamespace(native=types.SimpleNamespace(webview=view))
recovered = []
def recover(description):          # appelé hors du fil Qt
    recovered.append(description)
client._recover_after_renderer_crash = recover
poll = QTimer()
poll.timeout.connect(lambda: recovered and app.quit())
poll.start(100)
client._install_renderer_watch()
view.setHtml("<p>borne</p>", QUrl("https://kiosk.example/"))
QTimer.singleShot(1500, lambda: view.load(QUrl("chrome://crash")))
QTimer.singleShot(20000, app.quit)
app.exec()
print("RECOVERED:" + "|".join(recovered), flush=True)
"""


def test_vrai_moteur_qt_plante_reprise_declenchee():
    """Bout en bout avec le vrai Qt WebEngine : chrome://crash fait tomber
    le processus de rendu, le signal branché par _install_renderer_watch
    déclenche la reprise. Processus séparé : Qt WebEngine doit être chargé
    avant toute QApplication (les autres tests Qt en créent une)."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    import subprocess
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run(
        [sys.executable, "-c", _REAL_RENDERER_CRASH, root],
        capture_output=True, text=True, timeout=60, env=env, check=False)
    line = [ln for ln in result.stdout.splitlines() if ln.startswith("RECOVERED:")]
    assert line, result.stdout + result.stderr
    assert line[0].startswith("RECOVERED:plantage, code ")


def test_redirection_vers_la_racine_repasse_par_un_ticket():
    """Racine du site affichée : retour à /patient par un ticket neuf.
    Avant : /patient chargé directement, donc /login quand la connexion
    patient est exigée, puis jusqu'à 30 s d'attente avant un ticket."""
    client = _bare_client()
    client.operational = True
    client._window_ready.set()
    client._patient_page_shown = True
    client.window = _FakeWindow("http://127.0.0.1:5000/")
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "http://127.0.0.1:5000/patient/kiosk_login/neuf"}))

    client.on_loaded()

    assert client.window.loaded_urls == [
        "http://127.0.0.1:5000/patient/kiosk_login/neuf"]


def test_plus_de_reglage_webkit():
    """Variables du moteur WebKitGTK, sans effet avec Qt (gui="qt")."""
    with open(os.path.join(os.path.dirname(main.__file__), "main.py"),
              encoding="utf-8") as handle:
        assert "WEBKIT_" not in handle.read()
