import webview
from config import Config
import requests
from requests.exceptions import RequestException
import time
import threading
import json
import html
import logging
import logging_config
from printer import Printer, PrinterAPI, NETWORK_TIMEOUT
import os

logger = logging.getLogger("borne.main")

# Renouvellement du token avant son expiration (24 h côté serveur). Marge d'1 h
# pour absorber d'éventuels échecs/réessais réseau.
TOKEN_REFRESH_INTERVAL = 23 * 3600

# Boucle d'initialisation persistante : au démarrage (et tant que la borne n'a
# pas obtenu son token), on réessaie avec un backoff exponentiel borné au lieu
# d'abandonner. Tant que la borne n'est pas opérationnelle, l'écran local
# « Borne hors ligne » ci-dessous est affiché et la page /patient n'est PAS
# chargée : aucune inscription (a fortiori nécessitant un ticket) ne peut donc
# aboutir.
INIT_BACKOFF_START = 5          # premier réessai après 5 s
INIT_BACKOFF_MAX = 300          # plafond : 5 min entre deux tentatives

# Écran affiché localement par l'application (donc visible même si le serveur
# est injoignable) tant que la borne n'est pas opérationnelle. Il masque le
# curseur et bloque le menu contextuel comme les pages kiosque servies.
OFFLINE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<style>
  html, body {
    margin: 0; height: 100%; width: 100%;
    background: #0f172a; color: #e2e8f0;
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    cursor: none; user-select: none;
  }
  .wrap {
    height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center; padding: 2rem;
  }
  .spinner {
    width: 84px; height: 84px; margin-bottom: 2.5rem;
    border: 8px solid rgba(226, 232, 240, 0.2);
    border-top-color: #38bdf8; border-radius: 50%;
    animation: spin 1s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h1 { font-size: 2.6rem; font-weight: 700; margin: 0 0 1rem; }
  p  { font-size: 1.4rem; margin: 0; color: #94a3b8; }
</style>
</head>
<body oncontextmenu="return false">
  <div class="wrap">
    <div class="spinner"></div>
    <h1>Borne hors ligne</h1>
    <p>Connexion au serveur en cours&hellip;<br>La borne sera disponible dès que possible.</p>
  </div>
</body>
</html>"""


# Écran affiché lorsque la borne refuse de démarrer pour cause de configuration
# invalide ou non sécurisée. La liste des problèmes détectés est injectée à la
# place de « __ERROR_ITEMS__ » (échappée) par build_config_error_html().
CONFIG_ERROR_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<style>
  html, body {
    margin: 0; height: 100%; width: 100%;
    background: #3f0d0d; color: #fee2e2;
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    cursor: none; user-select: none;
  }
  .wrap {
    height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center; padding: 2rem;
  }
  h1 { font-size: 2.4rem; font-weight: 700; margin: 0 0 1rem; }
  p  { font-size: 1.3rem; margin: 0.3rem 0; color: #fecaca; max-width: 44rem; }
  ul { text-align: left; font-size: 1.15rem; color: #fecaca; max-width: 44rem;
       margin: 1rem auto; line-height: 1.5; }
  li { margin: 0.35rem 0; }
</style>
</head>
<body oncontextmenu="return false">
  <div class="wrap">
    <h1>Configuration invalide</h1>
    <p>La borne refuse de démarrer pour les raisons suivantes :</p>
    <ul>__ERROR_ITEMS__</ul>
    <p>Ouvrez l'éditeur de configuration pour corriger ces points.</p>
  </div>
</body>
</html>"""


def build_config_error_html(errors):
    """Construit l'écran d'erreur de configuration en listant les problèmes.
    Chaque message est échappé (html.escape) avant insertion : une valeur de
    configuration ne peut donc pas injecter de balise dans l'écran."""
    if errors:
        items = "".join(f"<li>{html.escape(str(e))}</li>" for e in errors)
    else:
        items = "<li>Configuration invalide.</li>"
    return CONFIG_ERROR_HTML_TEMPLATE.replace("__ERROR_ITEMS__", items)


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
                return {
                    'success': False,
                    'message': f'Erreur de plein écran : {str(e)}'
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
        self.username = Config().settings.username
        self.password = Config().settings.password
        # Masque le mot de passe et le secret d'application dans TOUS les logs
        # (défense en profondeur : même si un message les contenait par erreur).
        logging_config.register_secret(self.password)
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

        # Validation stricte de la configuration AVANT démarrage. Une borne mal
        # configurée (fichier illisible, URL invalide, http distant en prod,
        # identifiants USB erronés, secret/identifiants vides, mauvais types)
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

        # Garde-fou sécurité : identifiants/secret par défaut (admin/admin). On
        # REFUSE en production (accès triviaux) ; simple avertissement en debug.
        if settings.has_insecure_default_credentials():
            if settings.is_production:
                self._config_errors.append(
                    "Identifiants ou secret d'application par défaut (admin/admin) "
                    "détectés en production : configurez des identifiants propres "
                    "à la borne.")
            else:
                logger.warning("Identifiants/secret par défaut (admin/admin). "
                               "Refusé en production ; corrigez avant déploiement.")

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
            content_kwargs = {'html': build_config_error_html(self._config_errors)}
        elif self.is_operational():
            content_kwargs = {'url': f"{self.base_url}/patient"}
            self._patient_page_shown = True
        else:
            content_kwargs = {'html': OFFLINE_HTML}

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
        gère le curseur.

        Multitouch : preventDefault UNIQUEMENT si plusieurs points de contact
        (pinch/zoom). Un tap simple laisse passer le clic synthétique, sinon des
        boutons deviennent inopérants selon le moteur WebView.

        Curseur : masqué par défaut (borne tactile en libre-service) MAIS
        réapparaît dès qu'une souris est utilisée (maintenance) puis se remasque
        au toucher suivant. Le réglage hide_cursor=False force l'affichage
        permanent. Les faux 'mousemove' générés par le tactile sont ignorés."""
        hide_cursor = "true" if Config().settings.hide_cursor else "false"
        js_code = """
        if (!window._contextMenuDisabled) {
            // Désactive le menu contextuel
            window.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                return false;
            }, true);

            // Ne bloque QUE le multitouch (pinch/zoom) : les taps simples
            // passent normalement (clic synthétique préservé).
            window.addEventListener('touchstart', function(e) {
                if (e.touches.length > 1) {
                    e.preventDefault();
                }
            }, {passive: false, capture: true});

            var hideCursor = %s;
            if (hideCursor) {
                // Curseur masqué tant que la classe 'using-mouse' est absente ;
                // une souris qui bouge la pose, un toucher la retire.
                var style = document.createElement('style');
                style.textContent = "html:not(.using-mouse) * { cursor: none !important; }";
                document.head.appendChild(style);

                var lastTouch = 0;
                window.addEventListener('touchstart', function() {
                    lastTouch = Date.now();
                    document.documentElement.classList.remove('using-mouse');
                }, true);
                window.addEventListener('mousemove', function() {
                    // Ignore les 'mousemove' synthétiques émis juste après un toucher.
                    if (Date.now() - lastTouch < 800) { return; }
                    document.documentElement.classList.add('using-mouse');
                }, true);
            }

            window._contextMenuDisabled = true;
        }
        """ % hide_cursor
        self.window.evaluate_js(js_code)

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
                else:
                    logger.warning("Échec de l'obtention du token (tentative %d/%d, HTTP %s).",
                                   attempt + 1, max_retries, response.status_code)
            except RequestException as e:
                logger.warning("Erreur réseau à l'obtention du token (tentative %d/%d): %s",
                               attempt + 1, max_retries, e)
            
            if attempt < max_retries - 1:  # Ne pas attendre après la dernière tentative
                time.sleep(retry_delay)
        
        raise Exception("Impossible d'obtenir le token après plusieurs tentatives")

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
                except Exception as e:
                    self._next_refresh_delay = 300  # réessai dans 5 min
                    logger.warning("Échec du renouvellement du token, réessai bientôt : %s", e)

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
                    self.start_token_refresh()
                    self.connected = True
                    self._set_operational(True)
                    logger.info("Borne opérationnelle.")
                    return
                except Exception as e:
                    self.connected = False
                    logger.warning("Initialisation impossible, nouvel essai dans %ss : %s", delay, e)
                    # Attente interruptible : réveil immédiat à la fermeture.
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
            self.window.load_url(f"{self.base_url}/patient")
        except Exception as e:
            with self._operational_lock:
                self._patient_page_shown = False
            logger.error("Erreur lors du chargement de /patient : %s", e)

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
            raise Exception("Tentative d'initialisation de l'imprimante sans token")

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
        except Exception as e:
            logger.warning("Échec du renouvellement du token (statut imprimante) : %s", e)
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

        if "login" in current_url:
            self.inject_login_script()

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
            return {
                'success': False,
                'message': f'Erreur lors du basculement du mode plein écran : {str(e)}'
            }
        
    def inject_keyboard_handler(self):
        """Injecte le gestionnaire de touches F11"""
        script = """
        document.addEventListener('keydown', function(event) {
            if (event.key === 'F11') {
                event.preventDefault();  // Empêche le comportement par défaut du navigateur
                window.pywebview.api.window.toggle_fullscreen();
            }
        });
        """
        self.window.evaluate_js(script)

    def inject_kiosk_protection(self):
        """Injecte les protections kiosque sur les pages servies (clic droit,
        zoom, sélection sur appui long).

        On NE bloque PLUS tous les touchstart : appeler preventDefault() sur
        chaque touchstart supprime, selon le moteur WebView, le clic synthétique
        et rend des boutons tactiles inopérants. On privilégie CSS touch-action
        (supprime le double-tap zoom et le délai de clic tactile SANS empêcher
        les taps) + user-select (empêche la sélection de texte sur appui long).
        Le pinch/zoom multitouch est neutralisé par
        disable_context_menu_and_cursor() (preventDefault UNIQUEMENT si
        plusieurs points de contact)."""
        protection_script = """
        if (!window._kioskProtected) {
            // Bloque le menu contextuel (clic droit / appui long)
            document.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                return false;
            }, false);

            // Approche CSS (préférée à un preventDefault global) :
            // - touch-action: manipulation -> désactive le double-tap zoom et le
            //   délai de 300 ms, mais laisse passer les taps -> clics OK.
            // - user-select/touch-callout: none -> pas de sélection ni de
            //   menu sur appui long. Les champs de saisie restent sélectionnables.
            var style = document.createElement('style');
            style.textContent =
                "html { touch-action: manipulation; } " +
                "* { -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; } " +
                "input, textarea { -webkit-user-select: text; user-select: text; }";
            document.head.appendChild(style);

            window._kioskProtected = true;
        }
        """
        self.window.evaluate_js(protection_script)
        self._protection_injected = True


    def inject_login_script(self):
        """Injecte et exécute le script de connexion automatique.

        Les identifiants sont sérialisés en JSON (json.dumps) AVANT insertion :
        un guillemet, un antislash, un saut de ligne ou toute séquence spéciale
        dans le mot de passe ne peut donc plus casser le script ni injecter de
        code. json.dumps produit un littéral chaîne JavaScript sûr (échappe
        guillemets/antislash/caractères de contrôle ; ensure_ascii encode les
        non-ASCII en \\uXXXX). On insère le résultat SANS guillemets autour :
        json.dumps les fournit déjà."""
        username_js = json.dumps(self.username)
        password_js = json.dumps(self.password)
        script = f"""
        function performLogin() {{
            console.log("Injecting login script");
            var usernameInput = document.querySelector('input[name="username"]');
            var passwordInput = document.querySelector('input[name="password"]');
            var rememberCheckbox = document.querySelector('input[name="remember"]');

            if (usernameInput) {{
                console.log("Found username input");
                usernameInput.value = {username_js};
            }} else {{
                console.log("Username input not found");
            }}

            if (passwordInput) {{
                console.log("Found password input");
                passwordInput.value = {password_js};
            }} else {{
                console.log("Password input not found");
            }}

            if (rememberCheckbox) {{
                console.log("Found remember me checkbox");
                rememberCheckbox.checked = true;
            }} else {{
                console.log("Remember me checkbox not found");
            }}

            var form = usernameInput ? usernameInput.closest('form') : null;
            if (form) {{
                console.log("Found form, submitting");
                form.submit();
            }} else {{
                console.log("Form not found");
            }}
        }}

        // Vérifie si le DOM est déjà chargé
        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', performLogin);
        }} else {{
            performLogin();
        }}
        """
        self.window.evaluate_js(script)

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
            self._init_stop.set()
            self._token_refresh_stop.set()
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