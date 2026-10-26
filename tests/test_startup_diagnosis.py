"""Diagnostic de démarrage : une borne bien configurée en apparence mais qui
n'arrive pas à joindre son serveur dit au technicien pourquoi, au lieu de
rester indéfiniment sur « Borne hors ligne »."""

import threading
import types

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

import main
import ui_assets
from errors import (
    SecretRejectedError,
    ServerResponseError,
    SessionUnavailableError,
    TokenUnavailableError,
)
from startup_diagnosis import DIAGNOSIS_DELAY_S, DiagnosisTracker, diagnose

BASE = "https://serveur.example"


# --- Classement des pannes ------------------------------------------------------

def test_secret_refuse_signale_immediatement():
    d = diagnose(SecretRejectedError(401), BASE)
    assert d.code == "secret_refused" and d.immediate
    assert "401" in d.message and "secret" in d.message


def test_page_introuvable_signale_une_mauvaise_url():
    d = diagnose(ServerResponseError("x", status_code=404), BASE)
    assert d.code == "not_flowelis" and d.immediate
    assert BASE in d.message and "URL" in d.message


def test_erreur_serveur_peut_etre_passagere():
    d = diagnose(ServerResponseError("réponse inattendue (HTTP 502)",
                                     status_code=502), BASE)
    assert d.code == "server_error" and not d.immediate


def test_serveur_injoignable_peut_etre_passager():
    d = diagnose(TokenUnavailableError("réseau"), BASE)
    assert d.code == "unreachable" and not d.immediate
    assert BASE in d.message


def test_session_refusee():
    d = diagnose(SessionUnavailableError("ticket refusé (HTTP 500)"), BASE)
    assert d.code == "session_refused" and not d.immediate


def test_panne_sans_diagnostic():
    assert diagnose(RuntimeError("bogue"), BASE) is None


def test_le_texte_ecran_indique_la_touche_de_maintenance():
    text = diagnose(SecretRejectedError(403), BASE).screen_text()
    assert "F10" in text


# --- Délai d'affichage ------------------------------------------------------------

def test_panne_de_configuration_visible_tout_de_suite():
    tracker = DiagnosisTracker()
    tracker.record_failure(diagnose(SecretRejectedError(401), BASE), now=0)
    assert tracker.visible(now=0).code == "secret_refused"


def test_panne_passagere_visible_seulement_si_elle_dure():
    tracker = DiagnosisTracker()
    unreachable = diagnose(TokenUnavailableError("réseau"), BASE)
    tracker.record_failure(unreachable, now=0)
    tracker.record_failure(unreachable, now=DIAGNOSIS_DELAY_S - 1)
    assert tracker.visible(now=DIAGNOSIS_DELAY_S - 1) is None
    tracker.record_failure(unreachable, now=DIAGNOSIS_DELAY_S)
    assert tracker.visible(now=DIAGNOSIS_DELAY_S) == unreachable


def test_changement_de_panne_relance_le_delai():
    tracker = DiagnosisTracker(delay_s=100)
    tracker.record_failure(diagnose(TokenUnavailableError("r"), BASE), now=0)
    tracker.record_failure(
        diagnose(ServerResponseError("HTTP 502", status_code=502), BASE), now=90)
    assert tracker.visible(now=150) is None
    assert tracker.visible(now=190).code == "server_error"


def test_succes_efface_le_diagnostic():
    tracker = DiagnosisTracker()
    tracker.record_failure(diagnose(SecretRejectedError(401), BASE), now=0)
    tracker.record_success()
    assert tracker.visible(now=1000) is None


def test_panne_sans_diagnostic_ignoree():
    tracker = DiagnosisTracker()
    tracker.record_failure(None, now=0)
    assert tracker.visible(now=1000) is None


# --- Erreurs typées à l'obtention du jeton ---------------------------------------------

class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("pas du JSON")
        return self._payload


def _token_client(monkeypatch, outcome):
    monkeypatch.setattr(main, "Config", lambda: types.SimpleNamespace(
        settings=types.SimpleNamespace(app_secret="s3cr3t")))
    client = main.WebViewClient.__new__(main.WebViewClient)
    client.base_url = BASE
    client.app_token = None

    def post(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client.session = types.SimpleNamespace(post=post)
    return client


@pytest.mark.parametrize("status", [401, 403])
def test_secret_refuse_leve_une_erreur_dediee(monkeypatch, status):
    client = _token_client(monkeypatch, _Response(status, {"error": "x"}))
    with pytest.raises(SecretRejectedError) as exc:
        client.get_app_token(max_retries=1)
    assert exc.value.status_code == status


def test_page_introuvable_leve_une_erreur_de_reponse(monkeypatch):
    client = _token_client(monkeypatch, _Response(404))
    with pytest.raises(ServerResponseError) as exc:
        client.get_app_token(max_retries=1)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("payload", [None, {}, {"token": ""}, {"token": 3}])
def test_reponse_sans_jeton(monkeypatch, payload):
    client = _token_client(monkeypatch, _Response(200, payload))
    with pytest.raises(ServerResponseError):
        client.get_app_token(max_retries=1)
    assert client.app_token is None


def test_erreur_reseau_reste_une_panne_generique(monkeypatch):
    client = _token_client(monkeypatch, RequestsConnectionError("refusé"))
    with pytest.raises(TokenUnavailableError) as exc:
        client.get_app_token(max_retries=1)
    assert type(exc.value) is TokenUnavailableError


def test_jeton_obtenu(monkeypatch):
    client = _token_client(monkeypatch, _Response(200, {"token": "jeton-ok"}))
    assert client.get_app_token(max_retries=1) is True
    assert client.app_token == "jeton-ok"


# --- Écran hors ligne ----------------------------------------------------------------

def test_ecran_sans_diagnostic():
    html = ui_assets.offline_html()
    assert "Borne hors ligne" in html
    assert "Diagnostic technicien" not in html


def test_ecran_avec_diagnostic_echappe():
    html = ui_assets.offline_html("URL <script>x</script> refusée")
    assert "Diagnostic technicien" in html
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_repli_minimal_garde_le_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_assets, "ASSETS_DIR", tmp_path)
    html = ui_assets.offline_html("Secret refusé")
    assert "Secret refusé" in html


# --- Mise à jour de l'écran par la borne --------------------------------------------------

class _Window:
    def __init__(self):
        self.loaded = []

    def load_html(self, content):
        self.loaded.append(content)


def _screen_client():
    client = main.WebViewClient.__new__(main.WebViewClient)
    client.base_url = BASE
    client._config_error = False
    client.operational = False
    client._operational_lock = threading.Lock()
    client._window_ready = threading.Event()
    client._window_ready.set()
    client.window = _Window()
    client._diagnosis_tracker = DiagnosisTracker()
    client._displayed_diagnosis = None
    return client


def _fail(client, error):
    client._diagnosis_tracker.record_failure(diagnose(error, BASE),
                                             main.time.monotonic())
    client._refresh_startup_diagnosis()


def test_ecran_recharge_une_seule_fois_par_diagnostic():
    client = _screen_client()
    _fail(client, SecretRejectedError(401))
    _fail(client, SecretRejectedError(401))
    assert len(client.window.loaded) == 1
    assert "Diagnostic technicien" in client.window.loaded[0]
    assert "HTTP 401" in client.window.loaded[0]


def test_panne_passagere_ne_recharge_pas_l_ecran():
    client = _screen_client()
    _fail(client, TokenUnavailableError("réseau"))
    assert client.window.loaded == []


def test_nouvelle_cause_remplace_l_ancienne():
    client = _screen_client()
    _fail(client, SecretRejectedError(401))
    _fail(client, ServerResponseError("x", status_code=404))
    assert len(client.window.loaded) == 2
    assert "pas un serveur" in client.window.loaded[1]


def test_rien_tant_que_la_fenetre_n_est_pas_prete():
    client = _screen_client()
    client._window_ready.clear()
    _fail(client, SecretRejectedError(401))
    assert client.window.loaded == []
    # Fenêtre prête : le diagnostic en attente s'affiche.
    client._window_ready.set()
    client._refresh_startup_diagnosis()
    assert len(client.window.loaded) == 1


@pytest.mark.parametrize("state", ["operational", "config_error"])
def test_pas_de_diagnostic_hors_demarrage(state):
    client = _screen_client()
    if state == "operational":
        client.operational = True
    else:
        client._config_error = True
    _fail(client, SecretRejectedError(401))
    assert client.window.loaded == []


def test_boucle_de_demarrage_affiche_le_secret_refuse(monkeypatch):
    client = _screen_client()
    client._init_stop = threading.Event()
    client._init_thread = None
    client.connected = False

    def refuse(**_kwargs):
        client._init_stop.set()      # une seule itération
        raise SecretRejectedError(401)

    client.get_app_token = refuse
    client.start_initialization()
    client._init_thread.join(timeout=5)
    assert len(client.window.loaded) == 1
    assert "secret" in client.window.loaded[0]

