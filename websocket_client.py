import socketio
import threading
import time
import json
from typing import Callable

class WebSocketClient(threading.Thread):
    def __init__(self, web_url: str, print_callback: Callable[[str], None], debug: bool = False):
        super().__init__(daemon=True)  # Thread daemon pour arrêt automatique
        # Configuration de l'URL
        if "https" in web_url:
            self.web_url = web_url.replace("https", "wss")
        else:
            self.web_url = web_url.replace("http", "ws")

        self._should_run = True
        self._is_connected = False
        self._print_callback = print_callback
        
        # Configuration du client Socket.IO
        self.sio = socketio.Client(
            logger=debug,
            engineio_logger=debug,
            reconnection=False
        )

        # Configuration des événements
        self.sio.on('connect', self.on_connect, namespace='/socket_app_patient')
        self.sio.on('disconnect', self.on_disconnect, namespace='/socket_app_patient')
        self.sio.on('update', self.on_update, namespace='/socket_app_patient')

    def run(self):
        """Boucle principale du client WebSocket"""
        while self._should_run:
            try:
                if not self._is_connected:
                    self.sio.connect(
                        self.web_url,
                        namespaces=['/socket_app_patient'],
                        wait_timeout=10,
                        transports=['websocket']
                    )
                    self._is_connected = True
                
                while self._is_connected and self._should_run:
                    time.sleep(1)
                
                if not self._should_run:
                    break

            except socketio.exceptions.ConnectionError as e:
                print(f"Erreur de connexion WebSocket: {e}")
                if not self._should_run:
                    break
                time.sleep(5)
        
        self._cleanup()

    def stop(self):
        """Arrête proprement le client WebSocket"""
        print("Arrêt du client WebSocket...")
        self._should_run = False
        self._is_connected = False
        self._cleanup()
        self.join()  # Attend la fin du thread
        print("Client WebSocket arrêté")

    def _cleanup(self):
        """Nettoyage des ressources"""
        try:
            if hasattr(self, 'sio') and self.sio.connected:
                self.sio.disconnect()
        except Exception as e:
            print(f"Erreur lors du nettoyage WebSocket: {e}")

    def on_connect(self):
        print('WebSocket connecté')
        self._is_connected = True

    def on_disconnect(self):
        print('WebSocket déconnecté')
        self._is_connected = False

    def on_update(self, data):
        """Gestion des mises à jour reçues"""
        try:
            if isinstance(data, str):
                data = json.loads(data)
            print(f"Mise à jour WebSocket reçue: {data}")
            if data.get('flag') == 'print' and self._print_callback:
                self._print_callback(data['data'])
        except json.JSONDecodeError as e:
            print(f"Erreur de décodage JSON WebSocket: {e}")