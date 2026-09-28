"""Restrictions de navigation et journalisation sûre (lot 4).

Ces tests couvrent la politique d'URL partagée par la borne et l'éditeur :
- seules les URL HTTP(S)/WebSocket situées sous ``base_url`` sont autorisées ;
- les URL de session renvoyées par le serveur ne peuvent pas sortir du domaine ;
- les URL signées sont masquées dans les journaux ;
- le diagnostic éditeur ne suit pas une URL externe et ne révèle pas le ticket.
"""

import importlib.util
import logging
import os
import sys
import threading
import types

import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import config as config_mod
import logging_config
import main
from errors import SessionUnavailableError


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, timeout=None, data=None):
        self.calls.append(url)
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
    client = main.WebViewClient.__new__(main.WebViewClient)
    client.base_url = base_url
    client.app_token = "token-appli"
    client.session = None
    client.window = None
    client._patient_login_url = None
    client._last_relogin_attempt = None
    client._last_page_recovery_attempt = None
    client._patient_page_shown = False
    client.operational = False
    client.connected = False
    client._operational_lock = threading.Lock()
    client._window_ready = threading.Event()
    return client


# --- Politique d'URL ---------------------------------------------------------

def test_server_url_resolution_keeps_base_prefix():
    assert config_mod.resolve_server_url(
        "https://srv.example/app", "/patient/kiosk_login/ticket") == (
        "https://srv.example/app/patient/kiosk_login/ticket")
    assert config_mod.resolve_server_url(
        "https://srv.example/app", "patient") == (
        "https://srv.example/app/patient")


def test_server_url_resolution_accepts_absolute_same_origin():
    assert config_mod.resolve_server_url(
        "https://srv.example/app",
        "https://srv.example/app/patient/kiosk_login/ticket") == (
        "https://srv.example/app/patient/kiosk_login/ticket")


@pytest.mark.parametrize("url", [
    "https://evil.example/app/patient/kiosk_login/ticket",
    "http://srv.example/app/patient",
    "//evil.example/patient",
    "javascript:alert(1)",
    "file:///C:/Windows/System32/drivers/etc/hosts",
])
def test_server_url_resolution_rejects_external_or_dangerous_url(url):
    with pytest.raises(ValueError):
        config_mod.resolve_server_url("https://srv.example/app", url)


def test_url_allowlist_is_origin_and_path_prefix_based():
    base = "https://srv.example/app"
    assert config_mod.url_is_within_base("https://srv.example/app", base)
    assert config_mod.url_is_within_base("https://srv.example/app/patient", base)
    assert config_mod.url_is_within_base("wss://srv.example/app/socket.io/", base)
    assert not config_mod.url_is_within_base("https://srv.example/application", base)
    assert not config_mod.url_is_within_base("https://srv.example/other", base)
    assert not config_mod.url_is_within_base("https://evil.example/app", base)
    assert not config_mod.url_is_within_base("https://srv.example@evil.example/app", base)
    assert not config_mod.url_is_within_base("https://srv.example/app/../admin", base)


def test_fetch_login_url_rejects_absolute_url_outside_base():
    client = _bare_client("https://srv.example/app")
    client.session = _FakeSession(_FakeResponse(
        200, {"login_url": "https://evil.example/app/patient/kiosk_login/ticket"}))

    with pytest.raises(SessionUnavailableError):
        client._fetch_patient_login_url()


def test_navigation_guard_blocks_external_url_and_preserves_pywebview_events():
    class _Url:
        def __init__(self, value):
            self._value = value

        def toString(self):
            return self._value

    class _Info:
        def __init__(self, url):
            self._url = _Url(url)
            self.blocked = False
            self.forwarded = False

        def requestUrl(self):
            return self._url

        def requestMethod(self):
            return b"GET"

        def block(self, value):
            self.blocked = value

    class _BaseInterceptor:
        def __init__(self, window):
            self.window = window
            self.seen = []

        def interceptRequest(self, info):
            self.seen.append(info.requestUrl().toString())
            info.forwarded = True

    class _Profile:
        def __init__(self):
            self.interceptor = None

        def setUrlRequestInterceptor(self, interceptor):
            self.interceptor = interceptor

    client = _bare_client("https://srv.example/app")
    original = _BaseInterceptor(client.window)
    native = types.SimpleNamespace(
        request_interceptor=original,
        profile=_Profile(),
    )
    client.window = types.SimpleNamespace(native=native)

    client._install_navigation_guard()
    guarded = native.request_interceptor

    allowed = _Info("https://srv.example/app/patient")
    guarded.interceptRequest(allowed)
    assert allowed.forwarded is True
    assert guarded.seen == ["https://srv.example/app/patient"]

    blocked = _Info("https://evil.example/")
    guarded.interceptRequest(blocked)
    assert blocked.blocked is True
    assert blocked.forwarded is False
    assert guarded.seen == ["https://srv.example/app/patient"]


# --- Journalisation ----------------------------------------------------------

def test_url_for_log_masks_ticket_and_query_parameters():
    url = ("https://srv.example/app/patient/kiosk_login/TICKET_SECRET"
           "?session=abc&token=abc123#fragment")
    safe = config_mod.redact_url_for_log(url)
    assert "TICKET_SECRET" not in safe
    assert "abc123" not in safe
    assert safe == "https://srv.example/app/patient/kiosk_login/***"


def test_redacting_filter_masks_registered_and_named_secrets():
    redactor = logging_config.RedactingFilter()
    redactor.register_secret("SECRET_APPLICATIF")
    redactor.register_secret("TICKET_SESSION")

    record = logging.makeLogRecord({
        "msg": ("secret=SECRET_APPLICATIF ticket=TICKET_SESSION "
                "X-App-Token: abcdef123456 password='motdepasse' "
                "Bearer jeton123456789 token=abcdefgh12345")
    })
    redactor.filter(record)
    rendered = record.getMessage()

    for sensitive in (
        "SECRET_APPLICATIF", "TICKET_SESSION", "abcdef123456",
        "motdepasse", "jeton123456789", "abcdefgh12345",
    ):
        assert sensitive not in rendered


def test_loaded_log_does_not_expose_signed_ticket(caplog):
    client = _bare_client()
    client.window = _FakeWindow(
        "http://127.0.0.1:5000/patient/kiosk_login/TICKET_SECRET?x=1")

    with caplog.at_level(logging.DEBUG, logger="borne.main"):
        client.on_loaded()

    assert "TICKET_SECRET" not in caplog.text
    assert "/patient/kiosk_login/***" in caplog.text


def test_run_disables_external_browser_and_file_urls(monkeypatch, tmp_path):
    class _Settings:
        fullscreen = False
        debug = False

    class _Config:
        settings = _Settings()

        def webview_storage_path(self):
            return str(tmp_path / "webview")

    settings = {
        "OPEN_EXTERNAL_LINKS_IN_BROWSER": True,
        "ALLOW_FILE_URLS": True,
    }
    monkeypatch.setattr(main.webview, "settings", settings, raising=False)
    monkeypatch.setattr(main.webview, "start", lambda **kwargs: None,
                        raising=False)
    monkeypatch.setattr(main, "Config", lambda: _Config())

    client = _bare_client()
    client.create_window = lambda: None
    client._init_stop = threading.Event()
    client._token_refresh_stop = threading.Event()
    client._page_watchdog_stop = threading.Event()
    client._init_thread = None
    client._token_refresh_thread = None
    client._page_watchdog_thread = None
    client.printer = None

    client.run()

    assert settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is False
    assert settings["ALLOW_FILE_URLS"] is False


# --- Diagnostic éditeur -------------------------------------------------------

def _load_config_editor():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, "config-editor.py"))
    spec = importlib.util.spec_from_file_location("config_editor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_editor_probe_rejects_external_login_url(monkeypatch):
    editor = _load_config_editor()

    def _post(url, **_kwargs):
        if url.endswith("/api/get_app_token"):
            return _FakeResponse(200, {"token": "app-token"})
        return _FakeResponse(200, {
            "login_url": "https://evil.example/patient/kiosk_login/TICKET"})

    def _get(_url, **_kwargs):
        raise AssertionError("une URL externe ne doit jamais être suivie")

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(requests, "get", _get)

    ok, message = editor.ConfigEditor._probe_server(
        None, "https://srv.example/app", "secret")

    assert ok is False
    assert "evil.example" not in message
    assert "TICKET" not in message


def test_editor_probe_resolves_relative_login_url_and_redacts_errors(monkeypatch):
    editor = _load_config_editor()
    requested = []

    def _post(url, **_kwargs):
        if url.endswith("/api/get_app_token"):
            return _FakeResponse(200, {"token": "app-token"})
        return _FakeResponse(200, {
            "login_url": "patient/kiosk_login/TICKET_SECRET"})

    def _get(url, **_kwargs):
        requested.append(url)
        raise requests.ConnectionError(f"cannot reach {url}")

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(requests, "get", _get)

    ok, message = editor.ConfigEditor._probe_server(
        None, "https://srv.example/app", "secret")

    assert requested == [
        "https://srv.example/app/patient/kiosk_login/TICKET_SECRET"]
    assert ok is False
    assert "TICKET_SECRET" not in message
    assert "kiosk_login" not in message
