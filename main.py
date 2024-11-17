import webview
from config import Config
import requests
from requests.exceptions import RequestException
import time
from printer import Printer, PrinterAPI
from websocket_client import WebSocketClient
import os


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
        self.base_url = Config().settings.base_url
        self.is_fullscreen = Config().settings.fullscreen

        # Ajout des configurations d'optimisation
        self.webview_settings = {
            'text_select': False,
            'localization': False,
            'gui': 'cef'
        }


        self._protection_injected = False

        self.socket_client = None
        if Config().settings.websocket_enabled:
            self.start_websocket_client()

        # Création des APIs
        self.printer_api = PrinterAPI()
        self.window_api = WindowControlAPI()

        # Tentative d'obtention du token au démarrage
        try:
            self.get_app_token()
            self.connected = True
            print("Connexion établie avec succès")
            self.initialize_printer()
        except Exception as e:
            print("Erreur lors de l'initialisation :", e)
            self.connected = False

    def create_window(self):
        """Crée et configure la fenêtre WebView"""
        self.window_api.set_fullscreen_callback(self.toggle_fullscreen)

        class CombinedAPI:
            def __init__(self, printer_api, window_api):
                self.printer = printer_api
                self.window = window_api

        combined_api = CombinedAPI(self.printer_api, self.window_api)

        self.window = webview.create_window(
            title="PharmaFile",
            url=f"{self.base_url}/patient",
            fullscreen=Config().settings.fullscreen,
            js_api=combined_api,
            background_color='#FFFFFF',
            **self.webview_settings  # Applique les configurations d'optimisation
        )
        
        # Ajout des gestionnaires d'événements
        self.window.events.loaded += self.on_loaded
        self.window.events.loaded += lambda: disable_context_menu()

        # Injection de code JS pour désactiver le menu contextuel
        def disable_context_menu():
            js_code = """
            if (!window._contextMenuDisabled) {
                window.addEventListener('contextmenu', function(e) {
                    e.preventDefault();
                    return false;
                }, true);
                
                window.addEventListener('touchstart', function(e) {
                    if (e.touches.length > 1) {
                        e.preventDefault();
                        return false;
                    }
                }, true);
                
                window._contextMenuDisabled = true;
            }
            """
            self.window.evaluate_js(js_code)

    def get_app_token(self, max_retries=3, retry_delay=2):
        """Obtient le token d'application avec système de retry"""
        url = f'{self.base_url}/api/get_app_token'
        data = {'app_secret': Config().settings.app_secret}
        
        for attempt in range(max_retries):
            try:
                response = requests.post(url, data=data)
                if response.status_code == 200:
                    self.app_token = response.json()['token']
                    print("Token obtenu avec succès")
                    return True
                else:
                    print(f"Échec de l'obtention du token (tentative {attempt + 1}/{max_retries})")
            except RequestException as e:
                print(f"Erreur réseau (tentative {attempt + 1}/{max_retries}): {e}")
            
            if attempt < max_retries - 1:  # Ne pas attendre après la dernière tentative
                time.sleep(retry_delay)
        
        raise Exception("Impossible d'obtenir le token après plusieurs tentatives")
    
    def initialize_printer(self):
        """Initialise l'imprimante une fois le token obtenu"""
        if self.app_token:
            self.printer = Printer(
                Config().settings.printer_id_vendor,
                Config().settings.printer_id_product,
                Config().settings.printer_model,
                self.base_url,
                self.app_token
            )
            # Une fois l'imprimante initialisée, on la passe à l'API
            self.printer_api.set_print_callback(self.printer.print)
        else:
            raise Exception("Tentative d'initialisation de l'imprimante sans token")


    def on_loaded(self):
        """Gestionnaire d'événement pour le chargement de la page"""
        current_url = self.window.get_current_url()
        print("Page loaded:", current_url)

        if not self._protection_injected:
            self.inject_kiosk_protection()

        # Injecte le gestionnaire de touches
        self.inject_keyboard_handler()

        if "login" in current_url:
            self.inject_login_script()

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
        """Injecte les protections basiques pour le mode kiosque (clic droit et appui long)"""
        protection_script = """
        // Bloque le menu contextuel (clic droit)
        document.addEventListener('contextmenu', function(e) {
            e.preventDefault();
            return false;
        }, false);
        
        // Bloque l'appui long sur écran tactile
        document.addEventListener('touchstart', function(e) {
            e.preventDefault();
            return false;
        }, {passive: false});
        """
        self.window.evaluate_js(protection_script)
        self._protection_injected = True


    def inject_login_script(self):
        """Injecte et exécute le script de connexion automatique"""
        script = f"""
        function performLogin() {{
            console.log("Injecting login script");
            var usernameInput = document.querySelector('input[name="username"]');
            var passwordInput = document.querySelector('input[name="password"]');
            var rememberCheckbox = document.querySelector('input[name="remember"]');
            
            if (usernameInput) {{
                console.log("Found username input");
                usernameInput.value = "{self.username}";
            }} else {{
                console.log("Username input not found");
            }}

            if (passwordInput) {{
                console.log("Found password input");
                passwordInput.value = "{self.password}";
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

    def start_websocket_client(self):
        """Démarre le client WebSocket si activé"""
        try:
            if not self.socket_client:
                print("Démarrage du client WebSocket...")
                self.socket_client = WebSocketClient(
                    web_url=self.base_url,
                    print_callback=self.handle_websocket_print,
                    debug=Config().settings.websocket_debug
                )
                self.socket_client.start()
        except Exception as e:
            print(f"Erreur lors du démarrage du WebSocket: {e}")

    def handle_websocket_print(self, data):
        """Gère l'impression via WebSocket"""
        try:
            if self.printer:
                self.printer.print(data)
            else:
                print("Impression WebSocket impossible: imprimante non initialisée")
        except Exception as e:
            print(f"Erreur lors de l'impression WebSocket: {e}")

    def run(self):
        """Lance l'application"""
        try:
            os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '1'
            os.environ['WEBKIT_FORCE_ACCELERATED_COMPOSITING'] = '1'
            self.create_window()
            webview.start(debug=Config().settings.debug)
        finally:
            if self.socket_client:
                self.socket_client.stop()

if __name__ == '__main__':
    client = WebViewClient()
    client.run()