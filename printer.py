from escpos.printer import Usb
from escpos.exceptions import USBNotFoundError
from escpos.constants import RT_STATUS_PAPER
import base64
import threading
import requests
import queue
import time
import usb.core  # pyusb : dépendance de python-escpos, fournit USBError
from config import Config
from array import array


class CustomUsb(Usb):
    def query_status(self, mode):
        """
        Surcharge de escpos.printer.Usb.query_status
        Version modifiée de query_status qui considère un tableau vide comme absence de papier
        Le problème est qu'à l'init de l'imprimante les status sont bien renvoyés, 
        mais en cours d'utilisation s'il n'y a plus de papier, le status renvoyé est vide. Or la lib escpos considère que cela correspond à la présence de papier.
        En fait, si status vide, c'est que impression occupée potentiellement parce qu'elle essaye d'imprimer sans papier.
        """
        self._raw(mode)
        time.sleep(0.1)  # Petit délai pour éviter laisser le temps à l'imprimante de répondre (mais bloque le process). Modif si besoin
        status = self._read()
        
        # Si le tableau est vide et qu'on vérifie le status papier
        if len(status) == 0 and mode == RT_STATUS_PAPER:
            # On retourne [126] qui correspond à l'absence de papier
            return array('B', [126])
        return status


class PrinterAPI:
    """API minimaliste pour PyWebView"""
    def __init__(self):
        self._print_callback = None

    def set_print_callback(self, callback):
        """Définit la fonction de callback pour l'impression"""
        self._print_callback = callback

    def print_ticket(self, print_data):
        """Méthode exposée à JavaScript pour l'impression.

        Retourne toujours un dictionnaire au format unique
        ``{'success': bool, 'code': str, 'message': str}``. Le callback
        (Printer.print) respecte déjà ce contrat ; on ne fait que
        garantir le même format pour les erreurs propres à l'API.
        """
        if self._print_callback:
            try:
                return self._print_callback(print_data)
            except Exception as e:
                return {
                    'success': False,
                    'code': 'error_exception',
                    'message': f'Erreur d\'impression : {str(e)}'
                }
        return {
            'success': False,
            'code': 'error_not_initialized',
            'message': 'Système d\'impression non initialisé'
        }


# Timeouts (connexion, lecture) en secondes pour les appels réseau de la borne.
# Sans timeout, un serveur injoignable ou lent bloque le thread appelant
# indéfiniment.
NETWORK_TIMEOUT = (5, 10)

# Intervalle (secondes) entre deux passages du gestionnaire de santé de
# l'imprimante : tant qu'elle n'est pas connectée (absente au démarrage ou
# débranchée en cours de route), il retente périodiquement l'ouverture USB.
HEALTH_CHECK_INTERVAL = 10


class PrinterStatusThread(threading.Thread):
    def __init__(self, url, headers, status_queue, session=None):
        super().__init__(daemon=True)
        self.url = url
        self._headers = dict(headers)
        self._headers_lock = threading.Lock()
        self.status_queue = status_queue
        self._stop_event = threading.Event()
        # Session persistante : réutilise la connexion TCP/TLS (keep-alive)
        # au lieu d'en rouvrir une à chaque envoi de statut.
        self.session = session or requests.Session()

    def update_headers(self, headers):
        """Met à jour les en-têtes (ex: nouveau token) de façon thread-safe."""
        with self._headers_lock:
            self._headers = dict(headers)

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                # Attente bornée : le thread se réveille régulièrement pour
                # vérifier _stop_event, sans boucle d'attente active sur le CPU.
                status_data = self.status_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._headers_lock:
                    headers = dict(self._headers)
                response = self.session.post(
                    self.url,
                    json=status_data,
                    headers=headers,
                    timeout=NETWORK_TIMEOUT
                )
                print(f"Status sent: {status_data}, Response: {response.status_code}")
            except Exception as e:
                print(f"Error sending printer status: {e}")


class Printer:
    def __init__(self, idVendor, idProduct, printer_model, web_url, app_token):
        self.idVendor = int(idVendor, 16)
        self.idProduct = int(idProduct, 16)
        self.printer_model = printer_model
        self.web_url = web_url
        self.app_token = app_token
        self.p = None
        self.error = None
        self.encoding = 'utf-8'
        # File bornée : ne conserve que le dernier état (voir send_printer_status).
        self.status_queue = queue.Queue()
        self._status_lock = threading.Lock()
        self.is_paper_ok = True

        # Verrou SÉRIALISANT tous les accès USB (ouverture, impression, contrôle
        # papier, fermeture). Réentrant car print() appelle check_paper_status()
        # qui le reprend. Garantit qu'une impression JavaScript et une impression
        # WebSocket ne peuvent pas s'exécuter en même temps sur le même handle.
        self._usb_lock = threading.RLock()
        # Signalé à la fermeture pour arrêter le thread de santé.
        self._closing = threading.Event()
        self._health_thread = None
        
        # Démarrage du thread de status
        self.status_thread = PrinterStatusThread(
            f'{self.web_url}/api/printer/status',
            {
                'X-App-Token': self.app_token,
                'Content-Type': 'application/json'
            },
            self.status_queue
        )
        self.status_thread.start()
        
        # Initialisation de l'imprimante
        try:
            self.initialize_printer()
        except ValueError as e:
            if "langid" in str(e):
                error_msg = ('Erreur de permissions USB. Pour résoudre :\n'
                           '1. Ajoutez une règle udev :\n'
                           f'echo \'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{idVendor}", '
                           f'ATTRS{{idProduct}}=="{idProduct}", MODE="0666", GROUP="dialout"\' '
                           '| sudo tee /etc/udev/rules.d/99-printer.rules\n'
                           '2. Rechargez les règles :\n'
                           'sudo udevadm control --reload-rules && sudo udevadm trigger\n'
                           '3. Ajoutez votre utilisateur au groupe dialout :\n'
                           'sudo usermod -a -G dialout $USER\n'
                           '4. Déconnectez-vous et reconnectez-vous')
                print(error_msg)
                self.send_printer_status('error_grant', error_msg)
            else:
                self.send_printer_status('error_init', f"Erreur d'initialisation : {str(e)}")

        # Gestionnaire de santé : surveille la connexion et rouvre l'USB dès que
        # possible. Démarré même si l'initialisation ci-dessus a échoué (borne
        # démarrée imprimante débranchée) pour permettre la reconnexion.
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def initialize_printer(self):
        # Ouverture USB sérialisée : jamais concurrente d'une impression ou d'une
        # tentative de reconnexion par le thread de santé.
        with self._usb_lock:
            # Repart d'un état propre : si un ancien handle traîne (reconnexion),
            # on le ferme avant d'en ouvrir un nouveau.
            self._close_printer()
            try:
                self.p = CustomUsb(self.idVendor, self.idProduct, profile=self.printer_model)
                print("printer", self.p)
                self.send_printer_status('init_ok', "Imprimante USB initialisée avec succès.")
                self.error = False
                print("Imprimante initialisée avec succès.")
            except USBNotFoundError:
                print("Avertissement : Imprimante USB non trouvée.")
                self.p = None
                self.error = True
                self.send_printer_status('error_not_found', "Imprimante USB non trouvée.")
            except ValueError as e:
                if "langid" in str(e):
                    raise  # Remonter l'erreur pour une gestion spéciale
                print(f"Erreur lors de l'initialisation : {e}")
                self.p = None
                self.error = True
                self.send_printer_status('error_init', f"Erreur lors de l'initialisation : {e}")
            except Exception as e:
                print(f"Erreur lors de l'initialisation : {e}")
                self.p = None
                self.error = True
                self.send_printer_status('error_init', f"Erreur lors de l'initialisation : {e}")
            # vérification du papier
            if Config().settings.check_paper:
                self.check_paper_status()

    def _close_printer(self):
        """Ferme proprement le handle USB courant (le cas échéant) et repart de
        None. Tolérant aux erreurs : un handle déjà invalide ne doit pas empêcher
        la suite. À appeler en détenant self._usb_lock."""
        if self.p is not None:
            try:
                self.p.close()
            except Exception as e:
                print(f"Fermeture du handle imprimante: {e}")
            finally:
                self.p = None

    def _reset_connection(self):
        """Après une erreur USB matérielle (débranchement, pipe cassé...), ferme
        le handle et marque l'imprimante en erreur. Le thread de santé se
        chargera de rouvrir la connexion au prochain passage."""
        with self._usb_lock:
            self._close_printer()
            self.error = True

    def _health_loop(self):
        """Surveillance périodique : tant que l'imprimante n'est pas connectée,
        retente l'ouverture USB. Permet à une borne démarrée sans imprimante, ou
        dont l'imprimante a été débranchée puis rebranchée, de se rétablir seule
        sans redémarrage de l'application."""
        while not self._closing.wait(HEALTH_CHECK_INTERVAL):
            try:
                self._try_reconnect()
            except Exception as e:
                print(f"Surveillance imprimante: {e}")

    def _try_reconnect(self):
        """Réessaie d'ouvrir l'imprimante si elle n'est pas connectée. Ne fait
        rien tant qu'un handle valide existe."""
        with self._usb_lock:
            if self.p is not None:
                return
            try:
                self.initialize_printer()
            except ValueError as e:
                # langid = permissions USB insuffisantes : un réessai ne corrige
                # rien, on évite de spammer les statuts serveur.
                if "langid" in str(e):
                    print("Réessai imprimante: permissions USB toujours insuffisantes")
                else:
                    print(f"Réessai imprimante échoué: {e}")
            except Exception as e:
                print(f"Réessai imprimante échoué: {e}")

    def print(self, data):
        # Tout le chemin d'impression est sérialisé : deux appels concurrents
        # (JavaScript via PrinterAPI et WebSocket via handle_websocket_print) ne
        # peuvent pas écrire en même temps sur le handle USB. Le second attend le
        # premier au lieu d'entrelacer octets et découpes.
        with self._usb_lock:
            # si on voulait verifier le papier avant chaque impression
            if Config().settings.check_paper:
                paper_code = self.check_paper_status()
            # sinon c'est toujours bon
            else:
                paper_code = 'paper_ok'

            if self.p is None:
                print("Erreur : L'imprimante n'est pas initialisée correctement.")
                self.error = True
                self.send_printer_status('error_init', "Imprimante non initialisée correctement.")
                return {
                    'success': False,
                    'code': 'error_init',
                    'message': "Imprimante non initialisée correctement."
                }

            if paper_code == 'no_paper':
                return {
                    'success': False,
                    'code': 'no_paper',
                    'message': "Plus de papier dans l'imprimante."
                }

            # Décodage des données à imprimer (base64 -> texte). Isolé du reste
            # pour distinguer une charge utile invalide d'une véritable erreur
            # matérielle.
            try:
                decoded = base64.b64decode(data).decode(self.encoding)
            except Exception as e:
                print(f"Données d'impression invalides : {e}")
                self.send_printer_status('invalid_data', f"Données d'impression invalides : {e}")
                return {
                    'success': False,
                    'code': 'invalid_data',
                    'message': "Données d'impression invalides."
                }

            try:
                print("Impression du message :", decoded)
                self.p.text(decoded)
                self.p.cut()
                # on renvoie un message pour indiquer que tout va bien si l'imprimante était précédemment en erreur
                if self.error:
                    self.error = False
                    self.send_printer_status('print_ok', "Impression réussie.")
                return {
                    'success': True,
                    'code': 'print_ok',
                    'message': "Ticket imprimé."
                }

            except usb.core.USBError as e:
                # Erreur USB matérielle (débranchement, pipe cassé, périphérique
                # occupé...) : le handle est probablement mort. On le ferme pour
                # que le thread de santé le rouvre proprement.
                print(f"Erreur USB lors de l'impression : {e}")
                self._reset_connection()
                self.send_printer_status('error_print', f"Erreur USB lors de l'impression : {e}")
                return {
                    'success': False,
                    'code': 'error_print',
                    'message': f"Erreur USB lors de l'impression : {e}"
                }

            except ValueError as e:
                if "langid" in str(e):
                    # langid pendant l'impression = communication USB rompue :
                    # on réinitialise le handle en plus de signaler l'erreur.
                    self._reset_connection()
                    self.send_printer_status('error_grant', "Erreur de permissions USB. Vérifiez les droits d'accès.")
                    return {
                        'success': False,
                        'code': 'error_grant',
                        'message': "Erreur de permissions USB. Vérifiez les droits d'accès."
                    }
                self.send_printer_status('error_print', f"Erreur lors de l'impression : {e}")
                return {
                    'success': False,
                    'code': 'error_print',
                    'message': f"Erreur lors de l'impression : {e}"
                }

            except Exception as e:
                print(f"Erreur lors de l'impression : {e}")
                self.send_printer_status('error_print', f"Erreur lors de l'impression : {e}")
                return {
                    'success': False,
                    'code': 'error_print',
                    'message': f"Erreur lors de l'impression : {e}"
                }
        

    def send_printer_status(self, error, error_message):
        item = {'error': error, 'message': error_message}
        # File bornée qui ne conserve que le DERNIER état : si un statut est
        # encore en attente (réseau lent/bloqué), on le remplace au lieu
        # d'empiler un backlog de statuts périmés. Le serveur n'a besoin que de
        # l'état courant de l'imprimante.
        with self._status_lock:
            try:
                while True:
                    self.status_queue.get_nowait()
            except queue.Empty:
                pass
            self.status_queue.put(item)

    def update_token(self, new_token):
        """Met à jour le token utilisé pour l'envoi des statuts (renouvellement
        avant l'expiration des 24 h côté serveur)."""
        self.app_token = new_token
        self.status_thread.update_headers({
            'X-App-Token': new_token,
            'Content-Type': 'application/json'
        })

    def cleanup(self):
        """À appeler lors de la fermeture de l'application"""
        # Arrêt du gestionnaire de santé (réveil immédiat via l'événement).
        self._closing.set()
        if self._health_thread:
            self._health_thread.join(timeout=2)
        # Fermeture propre du handle USB.
        with self._usb_lock:
            self._close_printer()
        if self.status_thread:
            self.status_thread.stop()
            self.status_thread.join()


    def check_paper_status(self):
        """
        Vérifie l'état du papier en utilisant les méthodes python-escpos
        """
        print("verification du papier")
        # Accès USB sérialisé (verrou réentrant : ok si déjà détenu par print()
        # ou initialize_printer()).
        with self._usb_lock:
            if self.p is None:
                self.send_printer_status("error_init", "Imprimante non initialisée")
                return

            try:
                paper_status = self.p.paper_status()

                if paper_status == 0:
                    self.send_printer_status("no_paper", "Plus de papier dans l'imprimante")
                    self.is_paper_ok = False
                    return 'no_paper'
                elif paper_status == 1:
                    self.send_printer_status("low_paper", "Il ne reste pas beaucoup de papier dans l'imprimante")
                    self.is_paper_ok = False
                    return 'low_paper'
                # on envoie un message si le papier est ok uniquement si ce n'était pas le cas avant
                else:
                    if not self.is_paper_ok:
                        self.send_printer_status("paper_ok", "Papier remis dans l'imprimante")
                        self.is_paper_ok = True
                    return 'paper_ok'

            except Exception as e:
                print(f"Erreur lors de la vérification papier: {str(e)}")
                self.send_printer_status("error_paper_check", f"Erreur lors de la vérification papier: {str(e)}")
                return 'paper_check_error'
