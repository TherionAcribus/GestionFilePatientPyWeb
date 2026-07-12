from escpos.printer import Usb
from escpos.exceptions import USBNotFoundError
from escpos.constants import RT_STATUS_PAPER
import base64
import threading
import requests
import queue
import time
import random
import socket
from datetime import datetime, timezone
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

# Backoff (secondes) pour les réessais d'envoi des statuts imprimante après un
# échec réseau/serveur. Croissance exponentielle bornée + jitter pour éviter que
# plusieurs bornes ne martèlent le serveur en cadence à sa remise en service.
STATUS_BACKOFF_START = 1.0
STATUS_BACKOFF_MAX = 30.0


# --- Contrôle des données d'impression ------------------------------------
# La charge d'impression (base64) provient de la page servie, transmise via le
# pont JavaScript. La borne est le DERNIER rempart avant l'imprimante physique :
# on valide donc STRICTEMENT la charge avant de l'envoyer, pour qu'un contenu
# malformé ou malveillant (serveur compromis, injection, MITM) ne puisse pas
# piloter l'imprimante avec des commandes arbitraires, la saturer, ni gaspiller
# le papier.

# Tailles maximales (garde-fous DoS / gaspillage). Un ticket de file est court ;
# ces bornes sont larges mais finies et cohérentes entre elles
# (encodé ≈ 4/3 × décodé).
MAX_ENCODED_LEN = 16384      # longueur max de la chaîne base64 (avant décodage)
MAX_DECODED_BYTES = 12288    # taille max des octets décodés
MAX_TICKET_CHARS = 6000      # longueur max du ticket (caractères)
MAX_TICKET_LINES = 150       # nombre max de lignes du ticket

# Séquences ESC/POS AUTORISÉES : exactement celles émises par le serveur
# (Serveur/utils.py convert_markdown_to_escpos). Toute autre séquence de
# contrôle débutant par ESC (0x1B) ou GS (0x1D) est refusée.
_ALLOWED_ESCPOS_SEQUENCES = (
    '\x1b\x61\x00', '\x1b\x61\x01',   # ESC a : alignement gauche / centré
    '\x1d\x21\x00', '\x1d\x21\x11',   # GS ! : taille normale / double
    '\x1b\x45\x00', '\x1b\x45\x01',   # ESC E : gras off / on
    '\x1b\x2d\x00', '\x1b\x2d\x01',   # ESC - : souligné off / on
)
# Caractères de contrôle « texte » tolérés en dehors des séquences ESC/POS.
_ALLOWED_CONTROL_CHARS = frozenset('\n\r\t')


def _validate_decoded_ticket(text):
    """Valide le TEXTE décodé d'un ticket. Lève ValueError (message SANS le
    contenu du ticket) si le ticket dépasse les limites de longueur ou contient
    un caractère / une commande de contrôle non autorisé(e).

    Seuls sont admis : le texte imprimable, les sauts de ligne / tabulations, et
    les séquences ESC/POS de mise en forme listées dans
    _ALLOWED_ESCPOS_SEQUENCES."""
    if len(text) > MAX_TICKET_CHARS:
        raise ValueError(
            f"ticket trop long ({len(text)} > {MAX_TICKET_CHARS} caractères)")
    if text.count('\n') > MAX_TICKET_LINES:
        raise ValueError(
            f"ticket comportant trop de lignes (> {MAX_TICKET_LINES})")

    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch in ('\x1b', '\x1d'):
            # ESC / GS : doit débuter une séquence de mise en forme autorisée.
            matched = next(
                (seq for seq in _ALLOWED_ESCPOS_SEQUENCES
                 if text.startswith(seq, i)),
                None,
            )
            if matched is None:
                raise ValueError(
                    f"commande de contrôle non autorisée à la position {i}")
            i += len(matched)
            continue
        code = ord(ch)
        if (code < 0x20 or code == 0x7f) and ch not in _ALLOWED_CONTROL_CHARS:
            raise ValueError(
                f"caractère de contrôle non autorisé (0x{code:02x}) "
                f"à la position {i}")
        i += 1


def decode_and_validate_print_payload(data, encoding='utf-8'):
    """Décode et valide STRICTEMENT une charge d'impression base64.

    Renvoie le texte décodé prêt à imprimer, ou lève ValueError avec un message
    ne contenant JAMAIS le contenu du ticket (journaux sûrs). Contrôles
    successifs : type / vacuité, taille encodée, base64 strict (validate=True),
    taille décodée, décodage dans l'encodage attendu, puis validation du contenu
    (_validate_decoded_ticket)."""
    if not isinstance(data, str):
        raise ValueError("charge d'impression non textuelle")
    if not data:
        raise ValueError("charge d'impression vide")
    if len(data) > MAX_ENCODED_LEN:
        raise ValueError(
            f"charge encodée trop volumineuse ({len(data)} > {MAX_ENCODED_LEN})")
    # validate=True : refuse tout caractère hors alphabet base64 au lieu de
    # l'ignorer silencieusement (décodage laxiste par défaut).
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise ValueError("base64 invalide")
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError(
            f"charge décodée trop volumineuse "
            f"({len(raw)} > {MAX_DECODED_BYTES} octets)")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        raise ValueError(f"contenu non décodable en {encoding}")
    _validate_decoded_ticket(text)
    return text


class PrinterStatusThread(threading.Thread):
    def __init__(self, url, headers, status_queue, session=None, token_refresh_callback=None):
        super().__init__(daemon=True)
        self.url = url
        self._headers = dict(headers)
        self._headers_lock = threading.Lock()
        self.status_queue = status_queue
        self._stop_event = threading.Event()
        # Session persistante : réutilise la connexion TCP/TLS (keep-alive)
        # au lieu d'en rouvrir une à chaque envoi de statut.
        self.session = session or requests.Session()
        # On ne ferme la session à l'arrêt que si on l'a créée nous-mêmes.
        self._owns_session = session is None
        # Callback (optionnel) invoqué sur 401 pour renouveler le token ;
        # renvoie le nouveau token (str) ou None en cas d'échec.
        self._token_refresh_callback = token_refresh_callback

    def update_headers(self, headers):
        """Met à jour les en-têtes (ex: nouveau token) de façon thread-safe."""
        with self._headers_lock:
            self._headers = dict(headers)

    def stop(self):
        self._stop_event.set()

    def _drain_latest(self):
        """Vide la file et renvoie le statut le plus récent (ou None). Un statut
        plus récent supersède celui en attente : le serveur n'a besoin que de
        l'état courant de l'imprimante."""
        latest = None
        while True:
            try:
                latest = self.status_queue.get_nowait()
            except queue.Empty:
                break
        return latest

    def _try_send(self, status_data):
        """Tente un envoi. Renvoie 'ok' (2xx), 'unauthorized' (401) ou 'fail'
        (erreur réseau ou tout autre code non-2xx)."""
        with self._headers_lock:
            headers = dict(self._headers)
        try:
            response = self.session.post(
                self.url,
                json=status_data,
                headers=headers,
                timeout=NETWORK_TIMEOUT
            )
        except Exception as e:
            print(f"Error sending printer status: {e}")
            return 'fail'

        if 200 <= response.status_code < 300:
            print(f"Status sent: {status_data}, Response: {response.status_code}")
            return 'ok'
        if response.status_code == 401:
            print("Statut imprimante: 401 (token expiré ?)")
            return 'unauthorized'
        # Tout code non-2xx est un échec (avant : un 500 était considéré envoyé).
        print(f"Statut imprimante rejeté par le serveur (HTTP {response.status_code})")
        return 'fail'

    def run(self):
        # Statut en cours d'envoi, CONSERVÉ tant qu'il n'est pas acquitté (2xx) :
        # une erreur réseau ne le fait plus disparaître.
        pending = None
        backoff = STATUS_BACKOFF_START
        token_retries = 0  # limite les renouvellements de token immédiats
        try:
            while not self._stop_event.is_set():
                if pending is None:
                    # Pas de statut en attente : on en récupère un (attente
                    # bornée pour rester réactif à l'arrêt).
                    try:
                        pending = self.status_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                else:
                    # On a un statut non acquitté : s'il en est arrivé un plus
                    # récent entretemps, il le remplace.
                    newer = self._drain_latest()
                    if newer is not None:
                        pending = newer

                result = self._try_send(pending)

                if result == 'ok':
                    pending = None
                    backoff = STATUS_BACKOFF_START
                    token_retries = 0
                    continue

                if (result == 'unauthorized' and self._token_refresh_callback
                        and token_retries < 1):
                    # Renouvellement du token puis réessai immédiat (une fois).
                    token_retries += 1
                    new_token = self._token_refresh_callback()
                    if new_token:
                        self.update_headers({
                            'X-App-Token': new_token,
                            'Content-Type': 'application/json'
                        })
                        continue  # réessai sans attendre avec le nouveau token

                # Échec (réseau, non-2xx, ou 401 non renouvelable) : on GARDE
                # pending et on attend avant de réessayer (backoff + jitter),
                # de façon interruptible à l'arrêt.
                token_retries = 0
                delay = min(backoff, STATUS_BACKOFF_MAX)
                delay += random.uniform(0, delay * 0.5)  # jitter
                if self._stop_event.wait(delay):
                    break
                backoff = min(backoff * 2, STATUS_BACKOFF_MAX)
        finally:
            # Fermeture de la session HTTP à l'arrêt (si on en est propriétaire).
            if self._owns_session:
                try:
                    self.session.close()
                except Exception:
                    pass


class Printer:
    def __init__(self, idVendor, idProduct, printer_model, web_url, app_token,
                 token_refresh_callback=None):
        self.idVendor = int(idVendor, 16)
        self.idProduct = int(idProduct, 16)
        self.printer_model = printer_model
        self.web_url = web_url
        self.app_token = app_token
        # Identifiant de la borne joint à chaque statut (repli sur le hostname si
        # non configuré) pour distinguer les bornes côté serveur.
        self.borne_id = Config().settings.borne_id or socket.gethostname()
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
            self.status_queue,
            token_refresh_callback=token_refresh_callback
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
        # Tout le chemin d'impression est sérialisé : une impression déclenchée
        # via le pont JavaScript (PrinterAPI) et un accès concurrent du thread de
        # statut/santé imprimante (vérification papier, reconnexion USB) ne
        # peuvent pas toucher en même temps le handle USB. Le second attend le
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

            # Décodage ET validation stricte de la charge (base64 -> texte).
            # Isolé du reste pour distinguer une charge utile invalide/refusée
            # d'une véritable erreur matérielle. Le message d'erreur ne contient
            # JAMAIS le contenu du ticket (journaux sûrs).
            try:
                decoded = decode_and_validate_print_payload(data, self.encoding)
            except ValueError as e:
                print(f"Données d'impression refusées : {e}")
                self.send_printer_status('invalid_data', f"Données d'impression invalides : {e}")
                return {
                    'success': False,
                    'code': 'invalid_data',
                    'message': "Données d'impression invalides."
                }

            try:
                # On NE journalise PAS le contenu du ticket : seulement sa taille.
                print(f"Impression d'un ticket ({len(decoded)} caractères)")
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
        # borne_id : pour distinguer les bornes côté serveur.
        # timestamp : instant de GÉNÉRATION du statut (et non d'envoi), pour
        # rester exploitable même si l'envoi n'aboutit qu'après des réessais.
        item = {
            'error': error,
            'message': error_message,
            'borne_id': self.borne_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
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
