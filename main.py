import logging
import os
import random
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests
import webview
from requests.exceptions import RequestException

import desktop_integration
import logging_config
import maintenance_pin
import ui_assets
from config import Config, redact_url_for_log, resolve_server_url, url_is_within_base
from errors import (
    BorneError,
    PrinterNotReadyError,
    SecretRejectedError,
    ServerResponseError,
    SessionUnavailableError,
    TokenUnavailableError,
)
from printer import NETWORK_TIMEOUT, Printer, PrinterAPI, join_with_timeout
from startup_diagnosis import DiagnosisTracker, diagnose

logger = logging.getLogger("borne.main")

# Renouvellement du token avant son expiration (24 h côté serveur). Marge d'1 h
# pour absorber d'éventuels échecs/réessais réseau.
TOKEN_REFRESH_INTERVAL = 23 * 3600

# Re-connexion de la borne (session patient expirée -> page /login détectée) :
# au plus une demande de ticket par intervalle, pour ne pas marteler le serveur
# si l'échec persiste.
KIOSK_RELOGIN_MIN_INTERVAL = 30

# Boucle d'initialisation persistante : au démarrage (et tant que la borne n'a
# pas obtenu son token), on réessaie avec un backoff exponentiel borné au lieu
# d'abandonner. Tant que la borne n'est pas opérationnelle, l'écran local
# « Borne hors ligne » (assets/offline.html, cf. ui_assets) est affiché et la
# page /patient n'est PAS chargée : aucune inscription (a fortiori nécessitant
# un ticket) ne peut donc aboutir.
INIT_BACKOFF_START = 5          # premier réessai après 5 s
INIT_BACKOFF_MAX = 60           # reprise en moins d'une minute après retour réseau
INIT_BACKOFF_JITTER_RATIO = 0.2 # décale les bornes de 0 à 20 %

# Surveillance de la page patient : pywebview n'expose pas d'évènement fiable
# « navigation échouée » ; une URL chrome-error/about:blank peut donc rester
# affichée après une coupure réseau. Ce délai borne les récupérations.
PATIENT_PAGE_RECOVERY_INTERVAL = 30

# Sonde DOM de la page patient : une WebView peut garder /patient affichée
# alors que le moteur est figé (plus de JS, plus de socket) — l'URL seule
# ne le voit pas. ``evaluate_js`` sur un moteur mort ne rend JAMAIS la
# main : la sonde part donc dans un thread jetable et, passé ce délai sans
# retour, la page est rechargée de force.
DOM_PROBE_TIMEOUT = 90

# Sonde de liaison réseau, INDÉPENDANTE du navigateur : une liaison Wi-Fi
# morte ne produit souvent aucune erreur immédiate (l'interface garde son
# IP, les paquets partent dans le vide) — le socket patient ne l'apprend
# qu'au timeout ping (~45 s) et les requêtes pendent 10 s. Cette sonde HTTP
# voit la coupure en ~15-20 s et pousse l'état directement à la page.
LINK_PROBE_PERIOD = 10       # s entre deux sondes
LINK_PROBE_TIMEOUT = 3       # s max par sonde
LINK_PROBE_MISSES = 2        # échecs consécutifs avant de déclarer la coupure

# Touche de maintenance (maintenance_pin.MAINTENANCE_KEY) : l'éditeur ouvert
# depuis une borne en service se ferme seul après ce délai sans clavier ni
# souris, pour que la borne ne reste pas hors service si on l'oublie.
MAINTENANCE_EDITOR_IDLE_TIMEOUT_S = 10 * 60


def _jittered_init_delay(delay):
    """Désynchronise les reconnexions sans dépasser le délai nominal.

    Le jitter est retranché (plutôt qu'ajouté) afin que le plafond de reprise
    reste strictement à ``INIT_BACKOFF_MAX`` secondes.
    """
    jitter = random.uniform(0, delay * INIT_BACKOFF_JITTER_RATIO)
    return max(0, delay - jitter)


class _JSApiNamespace:
    """Espace de noms d'API exposé à JavaScript.

    ``dir()`` renvoie volontairement une liste vide : la génération automatique
    de pywebview 6 utilise ``new Function`` pour créer les fonctions publiques,
    ce qui est bloqué par la CSP stricte des pages serveur. Les appels réels
    restent résolus par ``getattr`` depuis ``js_bridge_call``, puis exposés côté
    page par ``assets/pywebview_bridge.js`` sans ``eval`` ni ``Function``.
    """
    def __init__(self, **members):
        object.__setattr__(self, '_members', members)

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, '_members')[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __dir__(self):
        return []


class WindowControlAPI:
    """API pour la gestion des contrôles de la fenêtre"""
    def __init__(self):
        self._fullscreen_callback = None

    def set_fullscreen_callback(self, callback):
        """Définit la fonction de callback pour le plein écran"""
        self._fullscreen_callback = callback

    def toggle_fullscreen(self):
        """Méthode exposée à JavaScript pour basculer le plein écran"""
        if self._fullscreen_callback:
            try:
                return self._fullscreen_callback()
            except Exception as e:
                # FRONTIÈRE (pont JavaScript) : la page attend toujours un
                # dictionnaire ; on journalise la trace et on renvoie l'échec.
                logger.exception("Erreur inattendue au basculement plein écran.")
                return {
                    'success': False,
                    'message': f'Erreur de plein écran : {e!s}'
                }
        return {
            'success': False,
            'message': 'Gestion du plein écran non initialisée'
        }


class WebViewClient:
    def __init__(self):
        self.window = None
        self.app_token = None
        self.printer = None
        self.connected = False
        config = Config()
        # La borne ne connaît QUE son identité machine : le secret applicatif
        # (jeton) suffit — plus de compte utilisateur ni de mot de passe à
        # injecter dans la page de connexion.
        # Masque le secret d'application dans TOUS les logs (défense en
        # profondeur : même si un message le contenait par erreur).
        logging_config.register_secret(config.settings.app_secret)
        # URL normalisée par un parseur (schéma/hôte en minuscules, sans slash
        # final). On NE force PLUS silencieusement http -> https : le schéma
        # configuré est respecté, et la validation ci-dessous n'autorise http
        # que pour localhost ou en mode développement. Une URL http vers un
        # serveur distant en production est donc REFUSÉE au démarrage plutôt que
        # réécrite en douce (ce qui masquait les erreurs de configuration).
        self.base_url = config.settings.normalized_base_url()
        self.is_fullscreen = config.settings.fullscreen

        # Ajout des configurations d'optimisation
        self.webview_settings = {
            'text_select': False,
            'localization': False,
            'on_top': True,
        }


        # Session HTTP persistante (keep-alive) pour les appels de la borne :
        # évite de rouvrir une connexion TCP/TLS à chaque requête.
        self.session = requests.Session()
        self._next_refresh_delay = TOKEN_REFRESH_INTERVAL
        self._token_refresh_stop = threading.Event()
        self._token_refresh_thread = None

        # Création des APIs
        self.printer_api = PrinterAPI()
        self.window_api = WindowControlAPI()

        # État opérationnel de la borne. Tant qu'il est faux, l'écran local
        # « Borne hors ligne » est affiché et /patient n'est pas chargée.
        self.operational = False
        self._operational_lock = threading.Lock()
        self._window_ready = threading.Event()
        self._patient_page_shown = False
        # L'écran local « Borne hors ligne » est-il déjà affiché à la place
        # d'une page d'erreur ? Évite de le réinjecter à chaque passage du
        # watchdog.
        self._offline_screen_shown = False
        self._dom_probe_pending = False
        self._dom_probe_sent_at = None
        # evaluate_js « bloquant » conservé avant le reroutage CSP : run_js
        # (fire-and-forget) ne prouve pas que le moteur répond.
        self._evaluate_js_blocking = None
        self._init_stop = threading.Event()
        self._init_thread = None
        # URL de connexion signée (ticket borne) obtenue auprès du serveur ;
        # naviguée par la WebView pour poser le cookie de session patient.
        self._patient_login_url = None
        self._last_relogin_attempt = None
        self._last_page_recovery_attempt = None
        self._page_watchdog_stop = threading.Event()
        self._page_watchdog_thread = None
        # Sonde de liaison réseau (voir _check_server_link).
        self._link_probe_stop = threading.Event()
        self._link_probe_thread = None
        self._link_alive = True
        self._link_misses = 0
        # Touche de maintenance pressée : la fenêtre se ferme et __main__
        # enchaîne sur la configuration puis relance la borne.
        self.maintenance_requested = False
        # Cause probable d'une initialisation qui échoue, affichée au
        # technicien sur l'écran « Borne hors ligne » (startup_diagnosis).
        self._diagnosis_tracker = DiagnosisTracker()
        self._displayed_diagnosis = None

        # Validation stricte de la configuration AVANT démarrage. Une borne mal
        # configurée (fichier illisible, URL invalide, http distant en prod,
        # identifiants USB erronés, secret vide, mauvais types)
        # REFUSE de démarrer et affiche la liste des problèmes, au lieu de
        # tourner avec une configuration partielle ou des valeurs par défaut
        # appliquées en douce.
        self._config_error = False
        self._config_errors = []
        settings = config.settings

        # Fichier de configuration illisible (JSON invalide, etc.) : signalé par
        # Config au lieu d'un repli silencieux sur les valeurs par défaut.
        if config.load_error:
            self._config_errors.append(config.load_error)

        # Un secret en clair non migrable est toléré seulement en développement :
        # en production, la borne refuse de tourner avec une copie qui resterait
        # lisible dans settings.json.
        if config.secret_store_error:
            if settings.is_production:
                self._config_errors.append(config.secret_store_error)
            else:
                logger.warning("%s", config.secret_store_error)

        # Validation de forme (URL/parseur, IDs USB, modèle, secrets, types).
        self._config_errors.extend(settings.validate())

        # Garde-fou sécurité : secret applicatif trivial (vide ou repris de
        # l'exemple). Le code ne fournit AUCUN secret par défaut ; celui
        # détecté ici vient donc de la configuration du poste. On REFUSE en
        # production (accès trivial) ; simple avertissement en debug.
        insecure = settings.insecure_credentials_reasons()
        if insecure:
            if settings.is_production:
                self._config_errors.extend(
                    f"{reason} Configurez un secret propre à cette borne "
                    "(config_editor.py)." for reason in insecure)
            else:
                for reason in insecure:
                    logger.warning("%s Refusé en production ; corrigez avant "
                                   "déploiement.", reason)

        if self._config_errors:
            self._config_error = True
            logger.error("Refus de démarrage, configuration invalide : %s",
                         " ; ".join(self._config_errors))

        # Initialisation non bloquante : une boucle d'état persistante réessaie
        # l'obtention du token avec backoff, initialise l'imprimante dès qu'il
        # est disponible, puis bascule la borne en mode opérationnel. Le
        # démarrage ne dépend donc plus de la réussite immédiate de la connexion
        # (récupération automatique après un démarrage hors ligne). On ne la
        # lance PAS si la configuration est refusée.
        if not self._config_error:
            self.start_initialization()

    def create_window(self):
        """Crée et configure la fenêtre WebView"""
        self.window_api.set_fullscreen_callback(self.toggle_fullscreen)

        js_api = _JSApiNamespace(
            printer=_JSApiNamespace(
                print_ticket=self.printer_api.print_ticket),
            window=_JSApiNamespace(
                toggle_fullscreen=self.window_api.toggle_fullscreen),
            kiosk=_JSApiNamespace(
                borne_id=self.printer_api.borne_id),
        )

        # Tant que la borne n'est pas opérationnelle, on affiche l'écran local
        # « Borne hors ligne » (indépendant du serveur) plutôt que /patient :
        # aucune inscription ne peut donc être tentée hors ligne. /patient sera
        # chargée par _maybe_show_patient_page dès que la borne le devient.
        if self._config_error:
            # Configuration refusée : on n'affiche NI /patient ni l'écran hors
            # ligne, mais un écran d'erreur listant les problèmes détectés, et la
            # borne reste non opérationnelle.
            content_kwargs = {'html': ui_assets.build_config_error_html(self._config_errors)}
        elif self.is_operational():
            # Fenêtre créée APRÈS l'init (ex. init très rapide) : même chemin
            # que _maybe_show_patient_page — l'URL signée du ticket, pas
            # /patient directement (sinon redirection vers /login).
            content_kwargs = {'url': self._patient_login_url
                              or f"{self.base_url}/patient"}
            self._patient_page_shown = True
        else:
            diagnosis = self._diagnosis_tracker.visible(time.monotonic())
            self._displayed_diagnosis = diagnosis
            content_kwargs = {'html': ui_assets.offline_html(
                diagnosis.screen_text() if diagnosis else None)}

        self.window = webview.create_window(
            title="Flowelis Borne",
            fullscreen=Config().settings.fullscreen,
            js_api=js_api,
            background_color='#FFFFFF',
            **content_kwargs,
            **self.webview_settings  # Applique les configurations d'optimisation
        )
        self._install_csp_safe_js_callbacks()

        # Ajout des gestionnaires d'événements
        self.window.events.loaded += self.on_loaded
        self.window.events.loaded += lambda: self.disable_context_menu_and_cursor()
        # Installé avant l'évènement « shown » : le profil Qt existe alors, et
        # toutes les navigations/redirections suivantes passent par l'allowlist.
        self.window.events.before_show += self._install_navigation_guard
        # Même évènement (exécuté dans le fil Qt) : raccourci de maintenance.
        self.window.events.before_show += self._install_maintenance_shortcut
        # La fenêtre est prête : on peut désormais naviguer vers /patient si la
        # borne est (ou devient) opérationnelle.
        self.window.events.shown += self._on_window_shown
        self._start_page_watchdog()

    def _install_csp_safe_js_callbacks(self):
        """Redirige les retours du pont JS vers ``run_js`` quand il existe.

        pywebview 6 exécute ces retours via ``evaluate_js``, qui encapsule le
        code dans ``eval()`` et échoue sous la CSP ``script-src 'self'`` du
        serveur. ``run_js`` passe directement par ``runJavaScript`` : même
        action, sans exiger ``unsafe-eval``. Sur les anciennes versions sans
        ``run_js``, ``evaluate_js`` reste le repli.
        """
        run_js = getattr(self.window, 'run_js', None)
        if callable(run_js):
            # Conservé pour la sonde DOM du watchdog : run_js ne renvoie
            # rien et ne prouve pas que le moteur répond.
            self._evaluate_js_blocking = self.window.evaluate_js
            self.window.evaluate_js = run_js

    def _run_page_script(self, script):
        """Exécute un script injecté sans ``eval`` quand le backend le permet."""
        if not script:
            return
        run_js = getattr(self.window, 'run_js', None)
        if callable(run_js):
            run_js(script)
        else:
            self.window.evaluate_js(script)

    def inject_pywebview_bridge(self):
        """Expose les fonctions Python attendues par les pages servies."""
        self._run_page_script(ui_assets.pywebview_bridge_script())

    # Injection de code JS pour désactiver le menu contextuel et gérer le curseur
    def disable_context_menu_and_cursor(self):
        """Désactive le menu contextuel, bloque le pinch-zoom (multitouch) et
        gère le curseur. Le script vit dans ``assets/kiosk_input.js`` (voir
        ui_assets) ; seul le réglage ``hide_cursor`` y est injecté."""
        self._run_page_script(
            ui_assets.kiosk_input_script(Config().settings.hide_cursor))

    def get_app_token(self, max_retries=3, retry_delay=2):
        """Obtient le token d'application avec système de retry"""
        url = f'{self.base_url}/api/get_app_token'
        data = {'app_secret': Config().settings.app_secret}

        # Dernière cause d'échec, typée : le serveur qui RÉPOND mal (secret
        # refusé, URL qui n'est pas un serveur Flowelis) se distingue ainsi du
        # serveur injoignable dans le diagnostic affiché (startup_diagnosis).
        failure = None
        for attempt in range(max_retries):
            try:
                # timeout : sans lui, une borne face à un serveur injoignable
                # resterait bloquée indéfiniment sur cet appel.
                response = self.session.post(url, data=data, timeout=NETWORK_TIMEOUT)
                if response.status_code == 200:
                    try:
                        token = response.json()['token']
                    except (ValueError, KeyError, TypeError):
                        token = None
                    if not token or not isinstance(token, str):
                        failure = ServerResponseError(
                            "réponse du serveur sans jeton d'application",
                            status_code=200)
                        logger.warning("Réponse sans jeton à l'obtention du "
                                       "token (tentative %d/%d).",
                                       attempt + 1, max_retries)
                    else:
                        self.app_token = token
                        # Enregistre le jeton pour qu'il soit masqué s'il
                        # apparaît un jour dans un message de log (défense en
                        # profondeur).
                        logging_config.register_secret(self.app_token)
                        logger.info("Token d'application obtenu (connexion serveur OK).")
                        return True
                else:
                    if response.status_code in (401, 403):
                        failure = SecretRejectedError(response.status_code)
                    else:
                        failure = ServerResponseError(
                            f"réponse inattendue du serveur (HTTP "
                            f"{response.status_code})",
                            status_code=response.status_code)
                    logger.warning("Échec de l'obtention du token (tentative %d/%d, HTTP %s).",
                                   attempt + 1, max_retries, response.status_code)
            except RequestException as e:
                failure = None
                logger.warning("Erreur réseau à l'obtention du token (tentative %d/%d): %s",
                               attempt + 1, max_retries, e)

            if attempt < max_retries - 1:  # Ne pas attendre après la dernière tentative
                time.sleep(retry_delay)

        if failure is not None:
            raise failure
        raise TokenUnavailableError(
            f"Impossible d'obtenir le token d'application après {max_retries} "
            "tentative(s)")

    def start_token_refresh(self):
        """Renouvelle le token avant son expiration (24 h côté serveur).

        Sur une borne qui tourne en continu, un token expiré ferait échouer en
        401 toutes les requêtes authentifiées (notamment l'envoi du statut
        imprimante). On le renouvelle donc de façon proactive et, en cas
        d'échec réseau, on réessaie rapidement au lieu d'attendre l'intervalle
        complet.
        """
        def _loop():
            while not self._token_refresh_stop.wait(self._next_refresh_delay):
                try:
                    self.get_app_token()
                    if self.printer:
                        self.printer.update_token(self.app_token)
                    self._next_refresh_delay = TOKEN_REFRESH_INTERVAL
                    logger.info("Token d'application renouvelé.")
                except TokenUnavailableError as e:
                    # Panne ATTENDUE (serveur injoignable) : réessai rapproché.
                    self._next_refresh_delay = 300  # réessai dans 5 min
                    logger.warning("Échec du renouvellement du token, réessai bientôt : %s", e)
                except Exception:
                    # Inattendu : trace complète, mais le thread doit survivre —
                    # sans lui le token expirerait et TOUS les appels
                    # authentifiés tomberaient en 401.
                    self._next_refresh_delay = 300
                    logger.exception("Erreur inattendue au renouvellement du token.")

        self._token_refresh_thread = threading.Thread(target=_loop, daemon=True)
        self._token_refresh_thread.start()

    def start_initialization(self):
        """Boucle d'initialisation persistante (gère le démarrage hors ligne).

        Réessaie l'obtention du token avec un backoff exponentiel borné. Dès
        que le token est obtenu, initialise l'imprimante, démarre le
        renouvellement périodique et bascule la borne en mode opérationnel — ce
        qui déclenche le chargement de /patient à la place de l'écran « Borne
        hors ligne ». Tourne dans un thread démon pour ne pas bloquer
        l'ouverture de la fenêtre.
        """
        def _supervise():
            delay = INIT_BACKOFF_START
            while not self._init_stop.is_set():
                try:
                    # Une seule tentative par itération : le backoff est géré
                    # ici, get_app_token n'ajoute donc pas sa propre attente.
                    self.get_app_token(max_retries=1)
                    self.initialize_printer()
                    # Ticket de session borne : requis pour que la WebView
                    # ouvre /patient quand SECURITY_LOGIN_PATIENT est actif.
                    self._patient_login_url = self._fetch_patient_login_url()
                    self.start_token_refresh()
                    self.connected = True
                    self._set_operational(True)
                    self._diagnosis_tracker.record_success()
                    logger.info("Borne opérationnelle.")
                    return
                except BorneError as e:
                    # Pannes ATTENDUES : serveur injoignable (token) ou
                    # imprimante pas prête. On réessaie, écran hors ligne affiché.
                    self.connected = False
                    wait_delay = _jittered_init_delay(delay)
                    logger.warning("Initialisation impossible, nouvel essai "
                                   "dans %.1fs : %s", wait_delay, e)
                    self._diagnosis_tracker.record_failure(
                        diagnose(e, self.base_url), time.monotonic())
                    self._refresh_startup_diagnosis()
                except Exception:
                    # Inattendu : trace complète, mais la boucle continue (une
                    # borne ne doit jamais rester bloquée sur l'écran hors ligne
                    # à cause d'une erreur ponctuelle).
                    self.connected = False
                    wait_delay = _jittered_init_delay(delay)
                    logger.exception("Erreur inattendue à l'initialisation, "
                                     "nouvel essai dans %.1fs.", wait_delay)

                # Attente commune à TOUS les échecs, attendus ou non : avant,
                # elle ne figurait que dans la branche « inattendu », si bien
                # qu'une panne prévue (token, imprimante) annonçait un délai
                # mais repartait immédiatement — la borne martelait
                # /api/get_app_token et l'USB en boucle serrée. Interruptible
                # (réveil immédiat à la fermeture), puis backoff borné.
                if self._init_stop.wait(wait_delay):
                    return
                delay = min(delay * 2, INIT_BACKOFF_MAX)

        self._init_thread = threading.Thread(target=_supervise, daemon=True)
        self._init_thread.start()

    def _refresh_startup_diagnosis(self):
        """Met à jour la ligne « Diagnostic technicien » de l'écran hors
        ligne quand la panne à signaler change.

        Seulement pendant le démarrage (borne pas encore opérationnelle,
        configuration acceptée) et une fois la fenêtre prête. L'écran n'est
        rechargé que si le diagnostic visible change, pas à chaque échec."""
        if self._config_error or self.is_operational():
            return
        if not (self.window and self._window_ready.is_set()):
            return
        diagnosis = self._diagnosis_tracker.visible(time.monotonic())
        if diagnosis == self._displayed_diagnosis:
            return
        load_html = getattr(self.window, 'load_html', None)
        if not callable(load_html):
            return
        try:
            load_html(ui_assets.offline_html(
                diagnosis.screen_text() if diagnosis else None))
        except Exception:
            # Frontière Qt/pywebview : l'écran garde l'ancien texte, on
            # réessaiera au prochain échec.
            logger.exception("Mise à jour du diagnostic à l'écran impossible.")
            return
        self._displayed_diagnosis = diagnosis
        if diagnosis:
            logger.warning("Diagnostic affiché à l'écran : %s", diagnosis.message)

    def is_operational(self):
        with self._operational_lock:
            return self.operational

    def _set_operational(self, value):
        with self._operational_lock:
            self.operational = value
        if value:
            self._maybe_show_patient_page()

    def _maybe_show_patient_page(self):
        """Charge /patient dès que la borne est opérationnelle ET la fenêtre
        prête. Appelé à la fois par la boucle d'init et par l'évènement
        d'affichage de la fenêtre (l'ordre des deux n'est pas garanti). Le
        contrôle-et-marquage est atomique pour éviter un double chargement
        quand les deux threads arrivent en même temps."""
        with self._operational_lock:
            if self._patient_page_shown or not self.operational:
                return
            if not (self._window_ready.is_set() and self.window):
                return
            self._patient_page_shown = True
        try:
            # On navigue sur l'URL de connexion signée (ticket) plutôt que
            # /patient directement : le serveur y pose le cookie de session
            # borne puis redirige vers /patient. Si la sécurité patient est
            # inactive, le mécanisme reste transparent (le ticket est accepté
            # aussi).
            if not self._patient_login_url:
                raise SessionUnavailableError("aucun ticket de session obtenu")
            self.window.load_url(self._patient_login_url)
            # La page patient remplace l'écran hors ligne s'il était affiché.
            self._offline_screen_shown = False
        except Exception:
            # Frontière pywebview (le moteur peut lever selon le backend Qt) :
            # on annule le marquage pour qu'une tentative ultérieure soit
            # possible, avec la trace complète pour diagnostiquer.
            with self._operational_lock:
                self._patient_page_shown = False
            logger.exception("Erreur lors du chargement de /patient.")

    def _fetch_patient_login_url(self):
        """Ticket de session borne : échange le jeton applicatif contre une URL
        de connexion signée à courte durée de vie (/patient/kiosk_login/...).

        Remplace l'ancienne connexion par formulaire : plus de compte
        utilisateur ni de mot de passe injecté dans le DOM — seul le jeton
        applicatif (déjà requis pour l'imprimante) sert d'identité machine.
        Lève SessionUnavailableError (panne attendue) sur tout échec."""
        try:
            response = self.session.post(
                f"{self.base_url}/api/kiosk/session_ticket",
                headers={"X-App-Token": self.app_token},
                timeout=NETWORK_TIMEOUT)
        except RequestException as e:
            raise SessionUnavailableError(
                f"ticket de session injoignable : {e}") from e
        if response.status_code != 200:
            raise SessionUnavailableError(
                f"ticket de session refusé (HTTP {response.status_code})")
        try:
            login_url = response.json()["login_url"]
        except (ValueError, KeyError) as e:
            raise SessionUnavailableError(
                "ticket de session : réponse inattendue") from e
        try:
            resolved_url = resolve_server_url(self.base_url, login_url)
        except ValueError as e:
            raise SessionUnavailableError(
                "ticket de session hors du domaine applicatif autorisé") from e
        # Défense en profondeur : si cette URL ou son ticket apparaissent un
        # jour dans une exception ou un message non prévu, ils seront remplacés
        # par *** (les journaux normaux utilisent déjà redact_url_for_log).
        logging_config.register_secret(resolved_url)
        logging_config.register_secret(
            urlparse(resolved_url).path.rsplit('/', 1)[-1])
        return resolved_url

    def _recover_kiosk_session(self):
        """La WebView a été redirigée vers /login : la session borne est
        expirée ou invalidée (redémarrage serveur, rotation de clé…). On
        redemande un ticket et on renavigue — borné à une tentative par
        KIOSK_RELOGIN_MIN_INTERVAL pour ne pas marteler le serveur si l'échec
        persiste."""
        now = time.monotonic()
        if (self._last_relogin_attempt is not None
                and now - self._last_relogin_attempt < KIOSK_RELOGIN_MIN_INTERVAL):
            logger.warning("Page de connexion affichée : ticket déjà redemandé "
                           "il y a peu, on attend avant de réessayer.")
            return
        self._last_relogin_attempt = now
        try:
            self._patient_login_url = self._fetch_patient_login_url()
        except SessionUnavailableError as e:
            logger.warning("Impossible de renouveler la session borne : %s", e)
            self.printer_api.report_event(
                "kiosk_session_fail",
                f"Ticket de session indisponible : {e}", is_error=True)
            # Le serveur ne délivre plus de ticket : /login n'est pas utile
            # au patient, on lui substitue l'écran hors ligne. Le watchdog de
            # page poursuivra les tentatives (l'URL n'est plus dans base_url).
            self._show_offline_screen()
            return
        self.window.load_url(self._patient_login_url)

    def _start_page_watchdog(self):
        """Supervise la page affichée après l'initialisation.

        Une navigation peut échouer sans exception exploitable (page
        chrome-error, URL vide ou locale restée affichée). Ce thread vérifie
        périodiquement que la borne opérationnelle affiche bien une page du
        serveur ; sinon il redemande un ticket de session frais avant de
        recharger, car l'URL signée précédente peut avoir expiré pendant la
        panne.
        """
        if self._page_watchdog_thread and self._page_watchdog_thread.is_alive():
            return
        self._page_watchdog_stop.clear()
        self._page_watchdog_thread = threading.Thread(
            target=self._page_watchdog_loop,
            daemon=True)
        self._page_watchdog_thread.start()
        # Sonde de liaison : lancée au même endroit que la surveillance de
        # page (fenêtre créée) ; indépendante pour garder son propre rythme
        # (10 s ≠ 30 s).
        if not (self._link_probe_thread and self._link_probe_thread.is_alive()):
            self._link_probe_stop.clear()
            self._link_probe_thread = threading.Thread(
                target=self._link_probe_loop, daemon=True)
            self._link_probe_thread.start()

    def _page_watchdog_loop(self):
        while not self._page_watchdog_stop.wait(PATIENT_PAGE_RECOVERY_INTERVAL):
            try:
                self._recover_patient_page()
            except Exception:
                # Frontière Qt/pywebview : le superviseur est la sécurité de
                # dernier recours ; il ne doit jamais mourir sur une erreur
                # ponctuelle du moteur ou d'une lecture d'URL.
                logger.exception("Erreur inattendue dans la surveillance de page.")

    def _check_server_link(self):
        """Une itération de la sonde : HEAD /patient, délai court.

        La transition up->down exige ``LINK_PROBE_MISSES`` échecs
        consécutifs (un paquet perdu ne fait pas clignoter l'écran) ; le
        retour à la normale est immédiat (plus d'attente inutile)."""
        session = getattr(self, 'session', None)
        if session is None or not self.base_url:
            return
        try:
            session.head(f"{self.base_url}/patient",
                         timeout=LINK_PROBE_TIMEOUT)
            alive = True
        except Exception:
            alive = False
        if alive:
            self._link_misses = 0
        else:
            self._link_misses += 1
        effective = alive or self._link_misses < LINK_PROBE_MISSES
        if effective != self._link_alive:
            self._link_alive = effective
            self._on_link_state_changed(effective)

    def _on_link_state_changed(self, alive):
        """Transition de liaison : journal serveur (rejoué au retour du
        réseau) + état poussé à la page pour l'overlay immédiat."""
        if alive:
            self.printer_api.report_event(
                "kiosk_link_restored", "Liaison réseau rétablie")
        else:
            logger.warning("Liaison serveur perdue (sonde borne).")
            self.printer_api.report_event(
                "kiosk_link_lost",
                "Liaison réseau coupée côté borne", is_error=True)
        self._push_link_state(alive)

    def _push_link_state(self, alive):
        """Transmet l'état au JS de la page : l'overlay patient s'affiche ou
        se ferme sans attendre le socket. La garde JS rend l'appel sans
        effet sur offline.html (qui n'a pas le hook)."""
        try:
            self._run_page_script(
                "window.__kioskLinkState "
                f"&& window.__kioskLinkState({str(alive).lower()})")
        except Exception:
            # Page morte/absente : le watchdog de page reprendra la main.
            logger.debug("État de liaison non transmis à la page.")

    def _link_probe_loop(self):
        while not self._link_probe_stop.wait(LINK_PROBE_PERIOD):
            try:
                self._check_server_link()
            except Exception:
                # La sonde est un supplément, jamais une sécurité critique :
                # elle ne doit pas mourir ni casser le reste de la borne.
                logger.exception("Sonde de liaison impossible.")

    def _show_offline_screen(self):
        """Substitue l'écran local « Borne hors ligne » à la page affichée.

        La WebView est restée sur une page d'erreur du navigateur (ou sur une
        page de connexion alors que le serveur est injoignable) : le patient
        y retrouve le même écran Flowelis qu'au démarrage hors ligne, pendant
        que les superviseurs continuent de retenter la connexion. Une seule
        injection par panne — le watchdog ne doit pas la recharger en boucle.
        """
        if getattr(self, '_offline_screen_shown', False) or not self.window:
            return
        load_html = getattr(self.window, 'load_html', None)
        if not callable(load_html):
            return
        try:
            load_html(ui_assets.offline_html())
            self._offline_screen_shown = True
        except Exception:
            # Frontière Qt/pywebview : sans écran local, la page d'erreur
            # reste affichée — le watchdog réessaiera au prochain passage.
            logger.exception("Écran hors ligne non affichable.")

    def _check_dom_probe(self):
        """Sonde asynchrone du DOM (page figée sur URL valide).

        Déclenchée quand l'URL courante est bien dans ``base_url`` : le
        garde-URL ne distingue pas une page vivante d'une page morte. La
        sonde n'attend pas de valeur — seul le RETOUR de l'appel prouve
        que le moteur répond. Une sonde sans retour au-delà de
        ``DOM_PROBE_TIMEOUT`` force la récupération complète."""
        now = time.monotonic()
        if getattr(self, '_dom_probe_pending', False):
            if (self._dom_probe_sent_at is not None
                    and now - self._dom_probe_sent_at > DOM_PROBE_TIMEOUT):
                logger.warning("Page patient muette (DOM sans réponse), "
                               "récupération forcée.")
                self.printer_api.report_event(
                    "kiosk_page_dead",
                    "Page patient figée : DOM sans réponse, rechargement "
                    "forcé",
                    is_error=True)
                self._dom_probe_pending = False
                self._force_patient_reload()
            return
        evaluate = (getattr(self, '_evaluate_js_blocking', None)
                    or getattr(self.window, 'evaluate_js', None))
        if not callable(evaluate) or not self.window:
            return
        self._dom_probe_pending = True
        self._dom_probe_sent_at = now

        def _probe():
            try:
                evaluate("void 0")
            except Exception:
                logger.exception("Sonde DOM de la page patient impossible.")
            # Une sonde qui revient (même en erreur) prouve que le moteur
            # répond ; seule l'absence de retour déclenche le timeout.
            self._dom_probe_pending = False

        threading.Thread(target=_probe, daemon=True).start()

    def _force_patient_reload(self):
        """Recharge /patient quand la page est figée : nouveau ticket (le
        précédent a pu expirer) puis navigation — même fin que le
        rattrapage d'URL, avec l'écran hors ligne si le serveur refuse."""
        try:
            self._patient_login_url = self._fetch_patient_login_url()
        except SessionUnavailableError as e:
            self.connected = False
            logger.warning("Session borne non récupérable : %s", e)
            self.printer_api.report_event(
                "kiosk_session_fail",
                f"Ticket de session indisponible : {e}", is_error=True)
            self._show_offline_screen()
            return
        self.connected = True
        with self._operational_lock:
            self._patient_page_shown = False
        self._maybe_show_patient_page()

    def _recover_patient_page(self):
        """Recharge /patient quand la fenêtre n'affiche plus le serveur.

        Ne touche pas à /login : ``_recover_kiosk_session`` gère ce cas avec son
        propre throttle. Ne redemande un ticket qu'une fois par intervalle afin
        de ne pas marteler le serveur pendant une panne prolongée.
        """
        if not (self.is_operational() and self.window and self._window_ready.is_set()):
            return
        try:
            current_url = self.window.get_current_url()
        except Exception:
            logger.exception("Lecture de l'URL WebView impossible.")
            current_url = None
        if current_url and url_is_within_base(current_url, self.base_url):
            # URL valide ≠ page vivante : un moteur figé garde l'URL sans
            # répondre — la sonde DOM le détecte et force la récupération.
            self._check_dom_probe()
            return

        # La page servie a disparu (chrome-error, about:blank…) : on affiche
        # tout de suite l'écran local « Borne hors ligne » plutôt que de
        # laisser la page d'erreur du navigateur pendant la récupération.
        # Journal serveur : la remontée est dédupliquée par signature, un
        # seul enregistrement par épisode de panne.
        self.printer_api.report_event(
            "kiosk_page_lost",
            f"Page patient perdue ({redact_url_for_log(current_url)}), "
            "récupération")
        self._show_offline_screen()

        now = time.monotonic()
        if (self._last_page_recovery_attempt is not None
                and now - self._last_page_recovery_attempt < PATIENT_PAGE_RECOVERY_INTERVAL):
            return
        self._last_page_recovery_attempt = now
        logger.warning("Page patient absente (%s), récupération de session.",
                       redact_url_for_log(current_url))
        try:
            self._patient_login_url = self._fetch_patient_login_url()
        except SessionUnavailableError as e:
            self.connected = False
            logger.warning("Session borne non récupérable pour l'instant : %s", e)
            return
        # Une navigation précédente peut être marquée réussie alors que la
        # WebView est restée sur une page d'erreur : on autorise explicitement
        # une nouvelle tentative avec le ticket frais.
        with self._operational_lock:
            self._patient_page_shown = False
        self.connected = True
        self._maybe_show_patient_page()

    def initialize_printer(self):
        """Initialise ou réutilise le gestionnaire d'imprimante."""
        if not self.app_token:
            raise PrinterNotReadyError(
                "Tentative d'initialisation de l'imprimante sans token "
                "d'application")
        if self.printer is None:
            self.printer = Printer(
                Config().settings.printer_id_vendor,
                Config().settings.printer_id_product,
                Config().settings.printer_model,
                self.base_url,
                self.app_token,
                token_refresh_callback=self._refresh_app_token_for_printer
            )
        else:
            # Une nouvelle tentative après un échec ultérieur (ticket de
            # session, par exemple) ne doit pas empiler des gestionnaires et
            # leurs threads de santé/statut : le même Printer reste propriétaire
            # du périphérique et ne reçoit que le token renouvelé.
            self.printer.update_token(self.app_token)
        # Une fois l'imprimante initialisée, on la passe à l'API
        self.printer_api.set_print_callback(self.printer.print)
        # Canal de remontée des incidents borne (report_event) : il emprunte
        # la file de statuts du Printer — seul détenteur du thread d'envoi,
        # qui rejoue la file bornée au retour du réseau.
        self.printer_api.set_event_reporter(self.printer.report_event)

    def _refresh_app_token_for_printer(self):
        """Renouvelle le token à la demande du thread de statut imprimante (ex:
        401 sur l'envoi d'un statut) et le renvoie, ou None en cas d'échec. On
        propage aussi le nouveau token à l'imprimante pour garder les en-têtes
        cohérents avec le reste des appels."""
        try:
            self.get_app_token()
            if self.printer:
                self.printer.update_token(self.app_token)
            return self.app_token
        except TokenUnavailableError as e:
            logger.warning("Échec du renouvellement du token (statut imprimante) : %s", e)
            return None
        except Exception:
            # Appelé depuis le thread de statut imprimante : ne jamais propager.
            logger.exception("Erreur inattendue au renouvellement du token "
                             "(statut imprimante).")
            return None


    def _navigation_request_allowed(self, url):
        """Politique de navigation réseau de la WebView.

        Toutes les requêtes HTTP(S)/WebSocket doivent rester dans l'origine et
        le préfixe de ``base_url``. Les schémas locaux nécessaires à la page
        hors ligne (``about:blank``) ou aux ressources inline (``data:``,
        ``blob:``) restent acceptés ; ``file:``, ``javascript:`` et les autres
        schémas sont bloqués."""
        try:
            parsed = urlparse(url)
        except (TypeError, ValueError):
            return False
        scheme = (parsed.scheme or "").lower()
        if scheme in ("http", "https", "ws", "wss"):
            return url_is_within_base(url, self.base_url)
        if (url or "").lower().startswith("about:blank"):
            return True
        return scheme in ("data", "blob")

    def _install_navigation_guard(self):
        """Installe le garde-navigation sur le profil Qt WebEngine.

        pywebview expose les requêtes à l'application, mais ne fournit pas de
        mécanisme d'autorisation. On encapsule donc son intercepteur Qt : les
        requêtes hors ``base_url`` sont bloquées, tandis que les requêtes
        autorisées continuent d'appeler l'implémentation d'origine (évènement
        ``request_sent`` et éventuels ajustements d'en-têtes)."""
        try:
            native = getattr(self.window, "native", None)
            interceptor = getattr(native, "request_interceptor", None)
            profile = getattr(native, "profile", None)
        except Exception:
            return
        if interceptor is None or profile is None:
            return

        client = self

        class GuardedRequestInterceptor(type(interceptor)):
            def interceptRequest(self, info):
                try:
                    url = info.requestUrl().toString()
                except Exception:
                    return super().interceptRequest(info)
                if not client._navigation_request_allowed(url):
                    logger.warning(
                        "Navigation hors domaine bloquée : %s",
                        redact_url_for_log(url))
                    try:
                        info.block(True)
                    except Exception:
                        logger.exception(
                            "Blocage d'une navigation externe impossible.")
                    return None
                return super().interceptRequest(info)

        try:
            guarded = GuardedRequestInterceptor(self.window)
            profile.setUrlRequestInterceptor(guarded)
            # Conserver une référence Python comme le fait pywebview : sinon
            # l'objet peut être collecté alors que Qt en détient le pointeur.
            native.request_interceptor = guarded
        except Exception:
            logger.exception("Installation du garde-navigation impossible.")

    def _install_maintenance_shortcut(self):
        """Pose le raccourci clavier de maintenance (F10) sur la fenêtre Qt.

        Raccourci Qt natif plutôt que script injecté : il fonctionne sur TOUS
        les écrans (page patient, « Borne hors ligne », « Configuration
        invalide »), même si la page ne répond plus. Appelé par l'évènement
        ``before_show``, que pywebview exécute dans le fil Qt — condition pour
        créer un objet Qt rattaché à la fenêtre."""
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QKeySequence, QShortcut
        except ImportError:
            logger.warning("Raccourci de maintenance indisponible (Qt absent).")
            return
        native = getattr(self.window, "native", None)
        if native is None:
            logger.warning("Raccourci de maintenance indisponible (fenêtre "
                           "native introuvable).")
            return
        try:
            shortcut = QShortcut(QKeySequence(maintenance_pin.MAINTENANCE_KEY),
                                 native)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self.request_maintenance)
            # Référence Python conservée : sans elle, le raccourci peut être
            # collecté et cesser de répondre.
            native.maintenance_shortcut = shortcut
        except Exception:
            # FRONTIÈRE Qt : sans raccourci, la borne doit quand même tourner.
            logger.exception("Installation du raccourci de maintenance "
                             "impossible.")
            return
        logger.info("Touche de maintenance : %s.", maintenance_pin.MAINTENANCE_KEY)

    def request_maintenance(self):
        """Touche de maintenance pressée : ferme la fenêtre kiosque.

        La suite (code PIN, choix maintenance, éditeur, relance ou
        extinction) se fait APRÈS la boucle Qt, dans ``__main__`` : Tk et
        Qt ne cohabitent pas dans une même boucle, et une borne relancée
        repart d'une configuration relue proprement."""
        if self.maintenance_requested:
            return
        self.maintenance_requested = True
        logger.info("Touche de maintenance pressée : fermeture de la borne "
                    "pour configuration.")
        try:
            self.window.destroy()
        except Exception:
            logger.exception("Fermeture de la fenêtre pour maintenance "
                             "impossible.")
            self.maintenance_requested = False

    def _on_window_shown(self):
        """La fenêtre est affichée (boucle GUI démarrée) : on marque la fenêtre
        prête et on charge /patient si la borne est déjà opérationnelle."""
        self._window_ready.set()
        self._maybe_show_patient_page()
        self._refresh_startup_diagnosis()

    def on_loaded(self):
        """Gestionnaire d'événement pour le chargement de la page"""
        current_url = self.window.get_current_url()
        logger.debug("Page chargée : %s", redact_url_for_log(current_url))

        # L'écran local « Borne hors ligne » (chargé via html=) gère lui-même sa
        # présentation ; les injections kiosque ne concernent que les pages
        # servies par le serveur. On les saute donc tant qu'on n'est pas dans le
        # périmètre strict de base_url (origine + port + préfixe, pas simple
        # préfixe de chaîne). Les scripts sont idempotents : ils sont réinjectés
        # à chaque navigation, car chaque chargement repart d'un contexte JS neuf.
        if not url_is_within_base(current_url or "", self.base_url):
            return

        self.inject_pywebview_bridge()
        self.inject_kiosk_protection()

        # Injecte le gestionnaire de touches
        self.inject_keyboard_handler()

        # Page /login atteinte : la session borne est absente ou expirée (la
        # route /patient/kiosk_login — qui CRÉE la session — ne doit PAS être
        # confondue avec la page de connexion ; on compare le chemin exact).
        if urlparse(current_url).path.rstrip('/') == '/login':
            self._recover_kiosk_session()
            return

        # Si l'utilisateur est redirigé vers la racine après authentification,
        # on recharge explicitement la page /patient
        if current_url.rstrip('/') == self.base_url.rstrip('/'):
            logger.info("Redirection inattendue vers la racine détectée, chargement de /patient")
            self.window.load_url(f"{self.base_url}/patient")

    def toggle_fullscreen(self):
        """Gère le basculement du mode plein écran"""
        try:
            self.is_fullscreen = not self.is_fullscreen
            self.window.toggle_fullscreen()
            return {
                'success': True,
                'message': 'Mode plein écran basculé avec succès',
                'is_fullscreen': self.is_fullscreen
            }
        except Exception as e:
            # Frontière pywebview : renvoyer un résultat au lieu de propager
            # dans le pont JavaScript.
            logger.exception("Basculement du mode plein écran impossible.")
            return {
                'success': False,
                'message': f'Erreur lors du basculement du mode plein écran : {e}'
            }

    def inject_keyboard_handler(self):
        """Injecte le gestionnaire de touches F11 (assets/keyboard.js)."""
        self._run_page_script(ui_assets.keyboard_script())

    def inject_kiosk_protection(self):
        """Injecte les protections kiosque sur les pages servies (clic droit,
        zoom, sélection sur appui long). Le script vit dans
        ``assets/kiosk_protection.js`` (voir ui_assets)."""
        self._run_page_script(ui_assets.kiosk_protection_script())


    def run(self):
        """Lance l'application"""
        try:
            os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '1'
            os.environ['WEBKIT_FORCE_ACCELERATED_COMPOSITING'] = '1'
            self.create_window()
            config = Config()
            logger.info("Fenêtre créée, démarrage de l'interface (fullscreen=%s).",
                        config.settings.fullscreen)
            # Mode kiosque : ne jamais sortir vers le navigateur système ni
            # autoriser une page servie à atteindre le système de fichiers local.
            # Les clés existent dans pywebview 6 ; les ignorer silencieusement si
            # une version future les renomme.
            for key, value in (
                ("OPEN_EXTERNAL_LINKS_IN_BROWSER", False),
                ("ALLOW_FILE_URLS", False),
                # debug=True ouvre sinon une seconde fenêtre « Web Inspector »
                # (BrowserView 'web_inspector') : tant qu'elle reste ouverte,
                # closeEvent n'appelle pas _app.exit() et le processus ne se
                # termine jamais après la fermeture de la fenêtre principale.
                ("OPEN_DEVTOOLS_IN_DEBUG", False),
            ):
                try:
                    webview.settings[key] = value
                except (AttributeError, KeyError, TypeError):
                    logger.debug("Réglage WebView indisponible : %s", key)
            webview.start(
                debug=config.settings.debug,
                gui="qt",
                private_mode=False,
                storage_path=config.webview_storage_path())
        finally:
            logger.info("Arrêt de la borne.")
            # Réveil immédiat des boucles de fond, puis attente BORNÉE de leur
            # terminaison (jamais de join() infini : une boucle bloquée sur un
            # appel réseau ne doit pas figer la fermeture de la borne).
            self._init_stop.set()
            self._token_refresh_stop.set()
            page_watchdog_stop = getattr(self, '_page_watchdog_stop', None)
            if page_watchdog_stop:
                page_watchdog_stop.set()
            link_probe_stop = getattr(self, '_link_probe_stop', None)
            if link_probe_stop:
                link_probe_stop.set()
            join_with_timeout(self._init_thread, "initialisation borne")
            join_with_timeout(self._token_refresh_thread, "renouvellement du token")
            join_with_timeout(
                getattr(self, '_page_watchdog_thread', None),
                "surveillance de page")
            join_with_timeout(
                getattr(self, '_link_probe_thread', None), "sonde de liaison")
            if self.printer:
                self.printer.cleanup()


def _launch_config_editor(**editor_options):
    """Ouvre l'éditeur graphique de configuration (bloquant jusqu'à fermeture).

    Import paresseux de ``config_editor`` (et donc de tkinter) : la borne ne
    l'utilise que pour se configurer, et une absence de Tk doit seulement
    priver la borne de l'éditeur — pas l'empêcher de démarrer.
    ``editor_options`` est transmis à ``ConfigEditor`` (``kiosk_follows``,
    ``idle_timeout_s``). Retourne True si l'éditeur s'est exécuté jusqu'au
    bout."""
    try:
        import config_editor
    except ImportError:
        logger.exception("Éditeur de configuration indisponible (tkinter ?).")
        return False
    try:
        config_editor.ConfigEditor(**editor_options).mainloop()
    except Exception:
        # FRONTIÈRE : une fenêtre Tk qui plante ne doit pas emporter la borne.
        logger.exception("L'éditeur de configuration a planté.")
        return False
    return True


def _launch_setup_wizard(**wizard_options):
    """Ouvre l'assistant de premier lancement (bloquant jusqu'à fermeture).

    Renvoie ``done``, ``advanced`` (l'utilisateur veut l'éditeur complet)
    ou ``cancelled`` ; None si l'assistant n'a pas pu s'afficher (Tk absent
    ou en panne) — la borne retombe alors sur l'éditeur ou son écran
    d'erreur, comme avant l'assistant."""
    try:
        import setup_wizard
    except ImportError:
        logger.exception("Assistant d'installation indisponible (tkinter ?).")
        return None
    try:
        return setup_wizard.run_setup_wizard(**wizard_options)
    except Exception:
        # FRONTIÈRE : une fenêtre Tk qui plante ne doit pas emporter la borne.
        logger.exception("L'assistant d'installation a planté.")
        return None


def _run_setup(kiosk_follows):
    """Assistant, puis éditeur complet si l'utilisateur l'a demandé."""
    outcome = _launch_setup_wizard(kiosk_follows=kiosk_follows)
    if outcome == "advanced":
        _launch_config_editor(kiosk_follows=kiosk_follows)
    return outcome


def _maintenance_pin_granted(stored_hash):
    """Demande le code PIN de maintenance s'il y en a un.

    Sans PIN défini, l'accès est direct (le clavier suffit à écarter les
    patients). Si la fenêtre du PIN ne peut pas s'afficher, l'accès est
    refusé : la borne repart simplement en service."""
    if not maintenance_pin.pin_required(stored_hash):
        return True
    try:
        import config_editor
        return config_editor.ask_maintenance_pin(stored_hash)
    except Exception:
        # FRONTIÈRE : Tk absent ou en panne — on refuse plutôt que d'ouvrir.
        logger.exception("Demande du code PIN de maintenance impossible.")
        return False


def _ask_maintenance_action():
    """Demande la suite à donner (configuration, extinction ou reprise).

    Renvoie la constante d'action choisie, ou None si la fenêtre n'a pas pu
    s'afficher ou a été abandonnée — la borne reprend alors son service."""
    try:
        import config_editor
        return config_editor.ask_maintenance_action()
    except Exception:
        # FRONTIÈRE : Tk absent ou en panne — on reprend plutôt que laisser
        # la borne arrêtée sans écran.
        logger.exception("Fenêtre de choix de maintenance indisponible.")
        return None


def _run_maintenance():
    """Touche de maintenance : code PIN éventuel, puis action choisie.

    Appelé après la fermeture de la fenêtre kiosque. Renvoie True si la
    borne doit être relancée — partout sauf après un choix explicite
    « Éteindre la borne »."""
    if not _maintenance_pin_granted(Config().settings.maintenance_pin_hash):
        logger.info("Accès maintenance non accordé : relance de la borne.")
        return True
    action = _ask_maintenance_action()
    if action == maintenance_pin.MAINTENANCE_ACTION_SHUTDOWN:
        logger.info("Extinction de la borne demandée (touche de maintenance).")
        return False
    if action == maintenance_pin.MAINTENANCE_ACTION_CONFIGURE:
        logger.info("Accès maintenance accordé : ouverture de la configuration.")
        _launch_config_editor(kiosk_follows=True,
                              idle_timeout_s=MAINTENANCE_EDITOR_IDLE_TIMEOUT_S)
    # Annulation, inactivité ou fenêtre en panne : la borne reprend.
    return True


#: Options qui font une action puis quittent, sans lancer la borne.
_ONE_SHOT_OPTIONS = ("--config", "--setup", "--selfcheck",
                     "--install-shortcuts", "--remove-shortcuts")


def _shortcuts_state_path():
    return Config().config_path / desktop_integration.STATE_FILENAME


def _sync_shortcuts(force=False):
    """Crée/met à jour les raccourcis système (Linux). Jamais bloquant."""
    if not desktop_integration.supported():
        logger.info("Raccourcis système : non pris en charge sur cette "
                    "plateforme (Linux uniquement).")
        return []
    try:
        return desktop_integration.install_shortcuts(
            _shortcuts_state_path(), ui_assets.ASSETS_DIR / "flowelis.png",
            force=force)
    except Exception:
        # FRONTIÈRE : un raccourci raté ne doit jamais empêcher la borne
        # de démarrer.
        logger.exception("Création des raccourcis système impossible.")
        return []


def _restart_command(argv=None, executable=None, frozen=None):
    """Ligne de commande qui relance la borne à l'identique.

    Binaire PyInstaller : le binaire lui-même. Depuis les sources : le même
    interpréteur sur ce fichier. Les options de lancement sont conservées,
    sauf celles qui ne mènent pas à la borne (``_ONE_SHOT_OPTIONS``)."""
    argv = sys.argv if argv is None else argv
    executable = sys.executable if executable is None else executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    options = [a for a in argv[1:] if a not in _ONE_SHOT_OPTIONS]
    if frozen:
        return [executable, *options]
    return [executable, os.path.abspath(__file__), *options]


def _restart_kiosk():
    """Relance la borne dans un processus neuf (configuration relue, Qt
    réinitialisé) — plus sûr que de redémarrer Qt WebEngine dans ce processus.

    Linux : ``execv`` garde le même PID (service systemd, autostart).
    Windows : ``execv`` y crée de toute façon un autre processus ; on lance
    donc le nouveau puis on sort. Binaire PyInstaller « onefile » :
    ``PYINSTALLER_RESET_ENVIRONMENT`` fait repartir le binaire comme un
    lancement neuf au lieu de réutiliser le dossier extrait de celui-ci."""
    command = _restart_command()
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    logger.info("Relance de la borne.")
    logging.shutdown()   # vide les fichiers journaux avant de quitter
    try:
        if os.name == "posix":
            os.execve(command[0], command, env)
        # Commande construite par _restart_command : notre propre exécutable.
        subprocess.Popen(command, env=env, close_fds=True)
    except OSError:
        logging_config.setup_logging()
        logger.exception("Relance de la borne impossible.")
        # Code non nul : un superviseur (systemd Restart=on-failure) relance.
        sys.exit(1)
    sys.exit(0)


def _prepare_client():
    """Assistant de premier lancement / éditeur si nécessaire, puis borne.

    Ordre : l'assistant (borne jamais configurée) passe AVANT la création de
    la borne, qui lance déjà la connexion au serveur ; l'éditeur ne s'ouvre
    ensuite que si la configuration reste refusée."""
    # Premier lancement : l'assistant s'ouvre AVANT de créer la borne (dont
    # la création lance déjà la connexion au serveur). Abandonné, il se
    # rouvrira au prochain démarrage ; la borne démarre quand même si la
    # configuration existante est valide.
    setup_shown = False
    if not Config().settings.setup_completed:
        logger.info("Borne jamais configurée : ouverture de l'assistant.")
        setup_shown = _run_setup(kiosk_follows=True) is not None

    client = WebViewClient()

    # Première utilisation ou configuration refusée : l'éditeur graphique
    # s'ouvre automatiquement AVANT l'interface kiosque — la borne peut ainsi
    # être configurée sans connaître l'emplacement du fichier. En cas
    # d'abandon, l'écran d'erreur local reste affiché comme auparavant.
    # (Sauf si l'assistant vient d'être fermé : on n'enchaîne pas une
    # seconde fenêtre ; l'écran d'erreur rappelle la touche F10.)
    if client._config_error and not setup_shown:
        logger.info("Configuration invalide : ouverture de l'éditeur.")
        if _launch_config_editor(kiosk_follows=True):
            client = WebViewClient()   # Config singleton relue après édition
    return client


def _selfcheck():
    """Vérifie que le binaire embarque tout le nécessaire, SANS affichage ni
    matériel (``--selfcheck``, utilisé par le workflow de release).

    Les modules importés dynamiquement — backend Qt de pywebview
    (``gui="qt"``), backends keyring — sont les seuls que PyInstaller puisse
    manquer : les imports statiques auraient déjà fait échouer le démarrage.
    Retourne la liste des problèmes détectés (vide si tout va bien).
    """
    import importlib

    problems = []
    for module in ('webview.platforms.qt', 'PySide6.QtWebEngineWidgets',
                   'escpos.printer', 'usb.core',
                   'keyring.backends.SecretService', 'keyring.backends.chainer',
                   'tkinter', 'config_editor', 'setup_wizard'):
        try:
            importlib.import_module(module)
        except Exception as exc:  # tout rapporter : c'est un diagnostic
            problems.append(f"{module}: {exc}")

    # Données embarquées : JS interne de pywebview et écrans locaux de la borne.
    if not os.path.isdir(os.path.join(os.path.dirname(webview.__file__), 'lib')):
        problems.append("webview/lib absent du bundle")
    for asset in ('offline.html', 'pywebview_bridge.js', 'config_error.html'):
        if not (ui_assets.ASSETS_DIR / asset).is_file():
            problems.append(f"asset manquant : {asset}")
    return problems


if __name__ == '__main__':
    if '--selfcheck' in sys.argv:
        problems = _selfcheck()
        if problems:
            print("selfcheck ÉCHEC : " + " | ".join(problems))
            sys.exit(1)
        print("selfcheck OK")
        sys.exit(0)

    # Journalisation en tout premier, pour capturer même les messages émis
    # pendant le chargement de la configuration. Le niveau est ajusté ensuite
    # selon le réglage debug.
    logging_config.setup_logging()

    if '--config' in sys.argv:
        logger.info("Ouverture de l'éditeur de configuration (--config).")
        sys.exit(0 if _launch_config_editor() else 1)

    if '--setup' in sys.argv:
        logger.info("Ouverture de l'assistant d'installation (--setup).")
        sys.exit(0 if _run_setup(kiosk_follows=False) is not None else 1)

    if '--install-shortcuts' in sys.argv:
        written = _sync_shortcuts(force=True)
        print("\n".join(f"Raccourci : {path}" for path in written)
              or "Aucun raccourci créé (voir le journal).")
        sys.exit(0 if desktop_integration.supported() else 1)

    if '--remove-shortcuts' in sys.argv:
        removed = (desktop_integration.remove_shortcuts(_shortcuts_state_path())
                   if desktop_integration.supported() else [])
        print("\n".join(f"Supprimé : {path}" for path in removed)
              or "Aucun raccourci de la borne à supprimer.")
        sys.exit(0)

    logger.info("Démarrage de la borne Flowelis.")

    # Binaire de release : il crée lui-même ses raccourcis (démarrage
    # automatique, menu, bureau) — install.sh n'est pas passé par là. Pas
    # depuis les sources : un lancement depuis l'IDE ne doit pas toucher au
    # poste de développement.
    if getattr(sys, "frozen", False):
        _sync_shortcuts()

    client = _prepare_client()
    logging_config.set_level(logging.DEBUG if Config().settings.debug else logging.INFO)
    client.run()

    # Touche de maintenance : la fenêtre s'est fermée pour laisser place à la
    # maintenance ; la borne est relancée sauf si l'extinction a été choisie
    # (_restart_kiosk ne revient pas — il termine le processus).
    if client.maintenance_requested and _run_maintenance():
        _restart_kiosk()
