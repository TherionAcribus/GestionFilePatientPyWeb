import webview
from config import Config
import requests
from requests.exceptions import RequestException
import time
from printer import Printer, PrinterAPI

class WebViewClient:
    def __init__(self):
        self.window = None
        self.username = Config.USERNAME
        self.password = Config.PASSWORD
        self.app_token = None
        self.printer = None
        self.connected = False
        self.base_url = Config.BASE_URL

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
        self.printer_api = PrinterAPI(self.printer)

        self.window = webview.create_window(
            title=Config.WINDOW_TITLE,
            url=Config.URL,
            width=Config.WINDOW_WIDTH,
            height=Config.WINDOW_HEIGHT,
            fullscreen=Config.FULLSCREEN,
            js_api=self.printer_api  # Exposer l'API à JavaScript
        )
        
        # Ajout des gestionnaires d'événements
        self.window.events.loaded += self.on_loaded

    def get_app_token(self, max_retries=3, retry_delay=2):
        """Obtient le token d'application avec système de retry"""
        url = f'{self.base_url}/api/get_app_token'
        data = {'app_secret': Config.APP_SECRET}
        
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
                Config.PRINTER_ID_VENDOR,
                Config.PRINTER_ID_PRODUCT,
                Config.PRINTER_MODEL,
                self.base_url,
                self.app_token
            )
        else:
            raise Exception("Tentative d'initialisation de l'imprimante sans token")


    def on_loaded(self):
        """Gestionnaire d'événement pour le chargement de la page"""
        current_url = self.window.get_current_url()
        print("Page loaded:", current_url)
        if "login" in current_url:
            self.inject_login_script()

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

    def run(self):
        """Lance l'application WebView"""
        self.create_window()
        webview.start(debug=Config.DEBUG)

if __name__ == '__main__':
    client = WebViewClient()
    client.run()