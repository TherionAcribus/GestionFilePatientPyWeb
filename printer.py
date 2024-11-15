from escpos.printer import Usb
from escpos.exceptions import USBNotFoundError
import base64
import threading
import requests
from requests.exceptions import RequestException
import queue
import time


class PrinterAPI:
    def __init__(self, printer):
        self.printer = printer

    def print_ticket(self, print_data):
        """Méthode exposée à JavaScript pour l'impression"""
        print("Données reçues pour impression:", print_data)
        try:
            print("Données reçues pour impression:", print_data)
            success = self.printer.print(print_data)
            return {
                'success': success,
                'message': 'Impression réussie' if success else 'Échec de l\'impression'
            }
        except Exception as e:
            print(f"Erreur lors de l'impression: {e}")
            return {
                'success': False,
                'message': f'Erreur: {str(e)}'
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
        self.initialize_printer()

    def initialize_printer(self):
        try:
            self.p = Usb(self.idVendor, self.idProduct, profile=self.printer_model)
            print("printer", self.p)
            self.send_printer_status(False, "Imprimante USB initialisée avec succès.")
            self.error = False
            print("Imprimante initialisée avec succès.")
        except USBNotFoundError:
            print("Avertissement : Imprimante USB non trouvée.")
            self.p = None
            self.error = True
            self.send_printer_status(True, "Imprimante USB non trouvée.")
        except Exception as e:
            print(f"Erreur lors de l'initialisation : {e}")
            self.p = None
            self.error = True
            self.send_printer_status(True, f"Erreur lors de l'initialisation : {e}")

    def print(self, data):
        if self.p is None:
            print("Erreur : L'imprimante n'est pas initialisée correctement.")
            self.error = True
            self.send_printer_status(True, "Imprimante non initialisée correctement.")
            return False

        try:
            data = base64.b64decode(data).decode(self.encoding)
            print("Impression du message :", data)
            self.p.text(data)
            self.p.cut()
            if self.error:
                self.error = False
                self.send_printer_status(False, "Impression réussie.")
            return True
        except Exception as e:
            print(f"Erreur lors de l'impression : {e}")
            self.send_printer_status(True, f"Erreur lors de l'impression : {e}")
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