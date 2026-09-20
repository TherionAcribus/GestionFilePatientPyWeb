import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests
import webview
from requests.exceptions import RequestException

import logging_config
import ui_assets
from config import Config
from errors import BorneError, PrinterNotReadyError, SessionUnavailableError, TokenUnavailableError
from printer import NETWORK_TIMEOUT, Printer, PrinterAPI, join_with_timeout

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
INIT_BACKOFF_MAX = 300          # plafond : 5 min entre deux tentatives


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
        # La borne ne connaît QUE son identité machine : le secret applicatif
        # (jeton) suffit — plus de compte utilisateur ni de mot de passe à
        # injecter dans la page de connexion.
        # Masque le secret d'application dans TOUS les logs (défense en
        # profondeur : même si un message le contenait par erreur).
        logging_config.register_secret(Config().settings.app_secret)
        # URL normalisée par un parseur (schéma/hôte en minuscules, sans slash
        # final). On NE force PLUS silencieusement http -> https : le schéma
        # configuré est respecté, et la validation ci-dessous n'autorise http
        # que pour localhost ou en mode développement. Une URL http vers un
        # serveur distant en production est donc REFUSÉE au démarrage plutôt que
        # réécrite en douce (ce qui masquait les erreurs de configuration).
        self.base_url = Config().settings.normalized_base_url()
        self.is_fullscreen = Config().settings.fullscreen

        # Ajout des configurations d'optimisation
        self.webview_settings = {
            'text_select': False,
            'localization': False,
            'on_top': True,
        }


        self._protection_injected = False

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
        self._init_stop = threading.Event()
        self._init_thread = None
        # URL de connexion signée (ticket borne) obtenue auprès du serveur ;
        # naviguée par la WebView pour poser le cookie de session patient.
        self._patient_login_url = None
        self._last_relogin_attempt = None

        # Validation stricte de la configuration AVANT démarrage. Une borne mal
        # configurée (fichier illisible, URL invalide, http distant en prod,
        # identifiants USB erronés, secret vide, mauvais types)
        # REFUSE de démarrer et affiche la liste des problèmes, au lieu de
        # tourner avec une configuration partielle ou des valeurs par défaut
        # appliquées en douce.
        self._config_error = False
        self._config_errors = []
        settings = Config().settings

        # Fichier de configuration illisible (JSON invalide, etc.) : signalé par
        # Config au lieu d'un repli silencieux sur les valeurs par défaut.
        if Config().load_error:
            self._config_errors.append(Config().load_error)

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
                    "(config-editor.py)." for reason in insecure)
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

        class CombinedAPI:
            def __init__(self, printer_api, window_api):
                self.printer = printer_api
                self.window = window_api

        combined_api = CombinedAPI(self.printer_api, self.window_api)

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
            content_kwargs = {'html': ui_assets.offline_html()}

        self.window = webview.create_window(
            title="PharmaFile",
            fullscreen=Config().settings.fullscreen,
            js_api=combined_api,
            background_color='#FFFFFF',
            **content_kwargs,
            **self.webview_settings  # Applique les configurations d'optimisation
        )

        # Ajout des gestionnaires d'événements
        self.window.events.loaded += self.on_loaded
        self.window.events.loaded += lambda: self.disable_context_menu_and_cursor()
        # La fenêtre est prête : on peut désormais naviguer vers /patient si la
        # borne est (ou devient) opérationnelle.
        self.window.events.shown += self._on_window_shown

    # Injection de code JS pour désactiver le menu contextuel et gérer le curseur
    def disable_context_menu_and_cursor(self):
        """Désactive le menu contextuel, bloque le pinch-zoom (multitouch) et
        gère le curseur. Le script vit dans ``assets/kiosk_input.js`` (voir
        ui_assets) ; seul le réglage ``hide_cursor`` y est injecté."""
        self.window.evaluate_js(
            ui_assets.kiosk_input_script(Config().settings.hide_cursor))

    def get_app_token(self, max_retries=3, retry_delay=2):
        """Obtient le token d'application avec système de retry"""
        url = f'{self.base_url}/api/get_app_token'
        data = {'app_secret': Config().settings.app_secret}

        for attempt in range(max_retries):
            try:
                # timeout : sans lui, une borne face à un serveur injoignable
                # resterait bloquée indéfiniment sur cet appel.
                response = self.session.post(url, data=data, timeout=NETWORK_TIMEOUT)
                if response.status_code == 200:
                    self.app_token = response.json()['token']
                    # Enregistre le jeton pour qu'il soit masqué s'il apparaît
                    # un jour dans un message de log (défense en profondeur).
                    logging_config.register_secret(self.app_token)
                    logger.info("Token d'application obtenu (connexion serveur OK).")
                    return True
                logger.warning("Échec de l'obtention du token (tentative %d/%d, HTTP %s).",
                               attempt + 1, max_retries, response.status_code)
            except RequestException as e:
                logger.warning("Erreur réseau à l'obtention du token (tentative %d/%d): %s",
                               attempt + 1, max_retries, e)

            if attempt < max_retries - 1:  # Ne pas attendre après la dernière tentative
                time.sleep(retry_delay)

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
                    logger.info("Borne opérationnelle.")
                    return
                except BorneError as e:
                    # Pannes ATTENDUES : serveur injoignable (token) ou
                    # imprimante pas prête. On réessaie, écran hors ligne affiché.
                    self.connected = False
                    logger.warning("Initialisation impossible, nouvel essai "
                                   "dans %ss : %s", delay, e)
                except Exception:
                    # Inattendu : trace complète, mais la boucle continue (une
                    # borne ne doit jamais rester bloquée sur l'écran hors ligne
                    # à cause d'une erreur ponctuelle).
                    self.connected = False
                    logger.exception("Erreur inattendue à l'initialisation, "
                                     "nouvel essai dans %ss.", delay)

                # Attente commune à TOUS les échecs, attendus ou non : avant,
                # elle ne figurait que dans la branche « inattendu », si bien
                # qu'une panne prévue (token, imprimante) annonçait un délai
                # mais repartait immédiatement — la borne martelait
                # /api/get_app_token et l'USB en boucle serrée. Interruptible
                # (réveil immédiat à la fermeture), puis backoff borné.
                if self._init_stop.wait(delay):
                    return
                delay = min(delay * 2, INIT_BACKOFF_MAX)

        self._init_thread = threading.Thread(target=_supervise, daemon=True)
        self._init_thread.start()

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
        return f"{self.base_url}{login_url}"

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
            return
        self.window.load_url(self._patient_login_url)

    def initialize_printer(self):
        """Initialise l'imprimante une fois le token obtenu"""
        if self.app_token:
            self.printer = Printer(
                Config().settings.printer_id_vendor,
                Config().settings.printer_id_product,
                Config().settings.printer_model,
                self.base_url,
                self.app_token,
                token_refresh_callback=self._refresh_app_token_for_printer
            )
            # Une fois l'imprimante initialisée, on la passe à l'API
            self.printer_api.set_print_callback(self.printer.print)
        else:
            raise PrinterNotReadyError(
                "Tentative d'initialisation de l'imprimante sans token "
                "d'application")

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


    def _on_window_shown(self):
        """La fenêtre est affichée (boucle GUI démarrée) : on marque la fenêtre
        prête et on charge /patient si la borne est déjà opérationnelle."""
        self._window_ready.set()
        self._maybe_show_patient_page()

    def on_loaded(self):
        """Gestionnaire d'événement pour le chargement de la page"""
        current_url = self.window.get_current_url()
        logger.debug("Page chargée : %s", current_url)

        # L'écran local « Borne hors ligne » (chargé via html=) gère lui-même sa
        # présentation ; les injections kiosque ne concernent que les pages
        # servies par le serveur. On les saute donc tant qu'on n'est pas sur une
        # URL du serveur, ce qui évite aussi de consommer prématurément le
        # verrou _protection_injected.
        if not (current_url or '').startswith(self.base_url):
            return

        if not self._protection_injected:
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
        self.window.evaluate_js(ui_assets.keyboard_script())

    def inject_kiosk_protection(self):
        """Injecte les protections kiosque sur les pages servies (clic droit,
        zoom, sélection sur appui long). Le script vit dans
        ``assets/kiosk_protection.js`` (voir ui_assets)."""
        self.window.evaluate_js(ui_assets.kiosk_protection_script())
        self._protection_injected = True


    def run(self):
        """Lance l'application"""
        try:
            os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '1'
            os.environ['WEBKIT_FORCE_ACCELERATED_COMPOSITING'] = '1'
            os.environ["PYWEBVIEW_GUI"] = "qt"
            self.create_window()
            logger.info("Fenêtre créée, démarrage de l'interface (fullscreen=%s).",
                        Config().settings.fullscreen)
            webview.start(debug=Config().settings.debug)
        finally:
            logger.info("Arrêt de la borne.")
            # Réveil immédiat des boucles de fond, puis attente BORNÉE de leur
            # terminaison (jamais de join() infini : une boucle bloquée sur un
            # appel réseau ne doit pas figer la fermeture de la borne).
            self._init_stop.set()
            self._token_refresh_stop.set()
            join_with_timeout(self._init_thread, "initialisation borne")
            join_with_timeout(self._token_refresh_thread, "renouvellement du token")
            if self.printer:
                self.printer.cleanup()

if __name__ == '__main__':
    # Journalisation en tout premier, pour capturer même les messages émis
    # pendant le chargement de la configuration. Le niveau est ajusté ensuite
    # selon le réglage debug.
    logging_config.setup_logging()
    logger.info("Démarrage de la borne PharmaFile.")
    client = WebViewClient()
    logging_config.set_level(logging.DEBUG if Config().settings.debug else logging.INFO)
    client.run()
