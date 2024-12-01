from escpos.printer import Usb
from escpos.exceptions import USBNotFoundError
from escpos.constants import RT_STATUS_PAPER
import base64
import threading
import requests
import queue
import time
from config import Config
from array import array


class CustomUsb(Usb):
    def query_status(self, mode):
        """
        Surcharge de escpos.printer.Usb.query_status
        Version modifiée de query_status qui considère un tableau vide comme absence de papier
        Le problème est qu'à l'init de l'imprimante les status sont bien renvoyés, 
        mais en cours d'utilisation s'il n'y a plus de papier, le status renvoyé est vide. Or la lib escpos considère que cela correspond à la présence de papier
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
        """Méthode exposée à JavaScript pour l'impression"""
        if self._print_callback:
            try:
                return self._print_callback(print_data)
            except Exception as e:
                return {
                    'success': False,
                    'message': f'Erreur d\'impression : {str(e)}'
                }
        return {
            'success': False,
            'message': 'Système d\'impression non initialisé'
        }


class PrinterStatusThread(threading.Thread):
    def __init__(self, url, headers, status_queue):
        super().__init__(daemon=True)
        self.url = url
        self.headers = headers
        self.status_queue = status_queue
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                if not self.status_queue.empty():
                    status_data = self.status_queue.get()
                    response = requests.post(
                        self.url,
                        json=status_data,
                        headers=self.headers
                    )
                    print(f"Status sent: {status_data}, Response: {response.status_code}")
            except Exception as e:
                print(f"Error sending printer status: {e}")
            time.sleep(0.1)  # Petit délai pour éviter de surcharger le CPU


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
        self.status_queue = queue.Queue()
        self.is_paper_ok = True
        
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

    def initialize_printer(self):
        try:
            self.p = Usb(self.idVendor, self.idProduct, profile=self.printer_model)
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

    def print(self, data):
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
            return False

        try:
            if paper_code != 'no_paper':
                data = base64.b64decode(data).decode(self.encoding)
                print("Impression du message :", data)
                self.p.text(data)
                self.p.cut()
                # on renvoie un message pour indiquer que tout va bien si l'imprimante était précédemment en erreur
                if self.error:
                    self.error = False
                    self.send_printer_status('print_ok', "Impression réussie.")
                # on vérifie l'état du papier après chaque impression.            
                return True
            return False
        
        except ValueError as e:
            if "langid" in str(e):
                self.send_printer_status('error_grant', "Erreur de permissions USB. Vérifiez les droits d'accès.")
            else:
                self.send_printer_status('error_print', f"Erreur lors de l'impression : {e}")
            return False
        
        except Exception as e:
            print(f"Erreur lors de l'impression : {e}")
            self.send_printer_status('error_print', f"Erreur lors de l'impression : {e}")
            return False
        

    def send_printer_status(self, error, error_message):
        self.status_queue.put({
            'error': error,
            'message': error_message
        })

    def cleanup(self):
        """À appeler lors de la fermeture de l'application"""
        if self.status_thread:
            self.status_thread.stop()
            self.status_thread.join()


    def check_paper_status(self):
        """
        Vérifie l'état du papier en utilisant les méthodes python-escpos
        """
        print("verification du papier")
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
            return False, f"Erreur lors de la vérification papier: {str(e)}"
