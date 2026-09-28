"""Tests du contrat de retour de l'impression.

Toutes les voies de sortie de ``Printer.print`` et de
``PrinterAPI.print_ticket`` doivent renvoyer un dictionnaire au format
unique ``{'success': bool, 'code': str, 'message': str}`` afin que le
front (patients.js) puisse fiablement lire ``result.success`` et
``result.message``.

Cas couverts : succès, absence de papier, imprimante absente, données
invalides, exception USB — plus les erreurs propres à l'API.
"""
import base64
import logging
import queue
import threading

import pytest

import printer as printer_module
from printer import (
    MAX_ENCODED_LEN,
    MAX_TICKET_CHARS,
    MAX_TICKET_LINES,
    Printer,
    PrinterAPI,
    decode_and_validate_print_payload,
)


def _b64(text):
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


# --- Doubles de test -------------------------------------------------------

class FakeDevice:
    """Imite l'objet imprimante escpos utilisé par Printer.print."""

    def __init__(self, text_exc=None, paper_status_value=2, open_exc=None,
                 paper_exc=None):
        self.text_exc = text_exc
        self.paper_status_value = paper_status_value
        self.open_exc = open_exc
        self.paper_exc = paper_exc
        self.open_calls = 0
        self.close_calls = 0
        self.paper_calls = 0
        self.text_calls = []
        self.cut_calls = 0

    def open(self):
        self.open_calls += 1
        if self.open_exc is not None:
            raise self.open_exc

    def text(self, data):
        self.text_calls.append(data)
        if self.text_exc is not None:
            raise self.text_exc

    def cut(self):
        self.cut_calls += 1

    def paper_status(self):
        self.paper_calls += 1
        if self.paper_exc is not None:
            raise self.paper_exc
        return self.paper_status_value

    def close(self):
        self.close_calls += 1


def make_printer(device=None, error=False, check_paper=False, monkeypatch=None,
                 device_factory=None):
    """Construit un Printer sans passer par __init__ (pas de matériel/thread).

    ``device_factory`` permet d'exercer le découplage matériel : la fabrique
    (idVendor, idProduct, model) -> périphérique est appelée par
    ``initialize_printer`` à la place du vrai USB."""
    p = Printer.__new__(Printer)
    p.p = device
    p.error = error
    p.encoding = 'utf-8'
    p.is_paper_ok = True
    p.status_queue = queue.Queue()
    p._status_lock = threading.Lock()
    # Verrou USB sérialisant les accès (ajouté avec la reconnexion USB) :
    # Printer.print l'acquiert, le helper doit donc le fournir.
    p._usb_lock = threading.RLock()
    # Identifiant de borne joint aux statuts (send_printer_status).
    p.borne_id = 'test-borne'
    # Métadonnées matériel + fabrique de périphérique injectée (découplage).
    p.idVendor = 0x04b8
    p.idProduct = 0x0202
    p.printer_model = 'TM-T88II'
    p._device_factory = device_factory

    # Neutralise la dépendance à Config().settings.check_paper : on force la
    # valeur au niveau du module pour éviter d'ouvrir le vrai fichier de config.
    class _Settings:
        pass

    settings = _Settings()
    settings.check_paper = check_paper

    class _Config:
        def __init__(self):
            self.settings = settings

    if monkeypatch is not None:
        monkeypatch.setattr(printer_module, 'Config', _Config)
    return p


VALID_PAYLOAD = base64.b64encode(b"Bonjour").decode('ascii')


# --- Tests Printer.print ---------------------------------------------------

def test_print_success(monkeypatch):
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result == {
        'success': True,
        'code': 'print_ok',
        'message': "Ticket imprimé.",
    }
    assert device.text_calls == ["Bonjour"]
    assert device.cut_calls == 1


def test_print_no_paper(monkeypatch):
    # paper_status == 0 => plus de papier ; check_paper activé.
    device = FakeDevice(paper_status_value=0)
    p = make_printer(device=device, check_paper=True, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'no_paper'
    assert 'papier' in result['message'].lower()
    # Rien n'a été imprimé.
    assert device.text_calls == []
    assert device.cut_calls == 0


def test_print_printer_absent(monkeypatch):
    # Imprimante non initialisée (self.p is None).
    p = make_printer(device=None, check_paper=False, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'error_init'
    assert isinstance(result['message'], str) and result['message']


def test_print_invalid_data(monkeypatch):
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)

    # Base64 invalide => échec de décodage, pas d'erreur matérielle.
    result = p.print("ceci n'est pas du base64 !!!@@@")

    assert result['success'] is False
    assert result['code'] == 'invalid_data'
    # On n'a pas tenté d'imprimer.
    assert device.text_calls == []


def test_print_usb_exception(monkeypatch):
    # L'écriture sur le périphérique lève une erreur type USBError.
    class FakeUSBError(Exception):
        pass

    device = FakeDevice(text_exc=FakeUSBError("USB pipe error"))
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'error_print'
    assert 'USB pipe error' in result['message']
    # L'exception a pu survenir après des octets déjà envoyés : le serveur ne
    # doit pas l'interpréter comme « aucun ticket ».
    assert result['maybe_printed'] is True


def test_print_unexpected_error_invalidates_handle(monkeypatch):
    """Une exception non-USBError peut tout de même laisser un handle mort :
    on le ferme pour permettre la reconnexion par le gestionnaire de santé."""
    device = FakeDevice(text_exc=RuntimeError("backend bloqué"))
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'error_print'
    assert result['maybe_printed'] is True
    assert device.close_calls == 1
    assert p.p is None
    assert p.error is True


def test_print_times_out_instead_of_waiting_for_stuck_usb(monkeypatch):
    """Un accès USB qui ne rend pas le verrou ne doit plus figer l'API
    d'impression : l'appel échoue de façon bornée et contractuelle."""
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)
    locked = threading.Event()
    release = threading.Event()

    def hold_usb_lock():
        p._usb_lock.acquire()
        locked.set()
        release.wait(2)
        p._usb_lock.release()

    holder = threading.Thread(target=hold_usb_lock, daemon=True)
    holder.start()
    assert locked.wait(1)
    monkeypatch.setattr(printer_module, 'USB_LOCK_TIMEOUT', 0.05)
    try:
        result = p.print(VALID_PAYLOAD)
    finally:
        release.set()
        holder.join(timeout=1)

    assert result['success'] is False
    assert result['code'] == 'error_print'
    assert result['attempted'] is False
    assert result['maybe_printed'] is True
    assert device.text_calls == []


def test_print_usb_langid_permission(monkeypatch):
    # ValueError contenant "langid" => problème de permissions USB.
    device = FakeDevice(text_exc=ValueError("The device has no langid"))
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'error_grant'


def test_print_always_returns_dict_contract(monkeypatch):
    """Toute sortie expose bien les clés success/code/message."""
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)
    result = p.print(VALID_PAYLOAD)
    assert {'success', 'code', 'message'}.issubset(result.keys())
    assert isinstance(result['success'], bool)


def test_print_journalise_le_print_job_id_serveur(monkeypatch, caplog):
    """Le print_job_id serveur devient le « job » des journaux : les lignes
    d'une impression restent corrélables avec l'inscription et son
    acquittement /patient/confirm_print."""
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)
    with caplog.at_level(logging.INFO, logger="borne.printer"):
        p.print(VALID_PAYLOAD, job_id="srv-job-123")
    assert any(getattr(r, "job_id", None) == "srv-job-123"
               for r in caplog.records)


def test_print_sans_job_id_genere_un_identifiant_local(monkeypatch, caplog):
    """Tirage hors parcours (test admin) : un identifiant local est généré
    comme avant — les lignes du travail restent corrélées entre elles."""
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)
    with caplog.at_level(logging.INFO, logger="borne.printer"):
        p.print(VALID_PAYLOAD)
    job_ids = {getattr(r, "job_id", None) for r in caplog.records}
    assert None not in job_ids and len(job_ids) == 1


# --- Tests PrinterAPI.print_ticket ----------------------------------------

def test_api_forwards_callback_result():
    api = PrinterAPI()
    expected = {'success': True, 'code': 'print_ok', 'message': 'Ticket imprimé.'}
    api.set_print_callback(lambda data, job_id=None: dict(expected))

    result = api.print_ticket("payload")
    for key, value in expected.items():
        assert result[key] == value


def test_api_transmet_le_print_job_id_au_callback():
    """Le print_job_id serveur (clé de /patient/confirm_print) doit atteindre
    le callback : c'est lui qui corrèle les journaux d'impression avec
    l'inscription du patient."""
    api = PrinterAPI()
    seen = {}
    api.set_print_callback(
        lambda data, job_id=None: seen.update(job=job_id)
        or {'success': True, 'code': 'print_ok', 'message': 'ok'})

    api.print_ticket("payload", "job-serveur-123")
    assert seen['job'] == "job-serveur-123"


def test_api_sans_job_id_transmet_none():
    """Tirage de test / appel historique : le callback reçoit None, pas
    d'exception d'arité."""
    api = PrinterAPI()
    seen = {}
    api.set_print_callback(
        lambda data, job_id=None: seen.update(job=job_id)
        or {'success': True, 'code': 'print_ok', 'message': 'ok'})

    api.print_ticket("payload")
    assert seen['job'] is None


def test_api_result_inclut_borne_id():
    """L'acquittement des tirages de test admin identifie la borne
    répondante : print_ticket joint toujours borne_id (settings ou nom
    d'hôte), y compris sur les chemins d'erreur de l'API."""
    api = PrinterAPI()
    api.set_print_callback(lambda data, job_id=None: {'success': True, 'code': 'print_ok',
                                         'message': 'ok'})

    assert api.print_ticket("payload")['borne_id']
    # Sans callback (imprimante non initialisée) : borne_id quand même.
    assert PrinterAPI().print_ticket("payload")['borne_id']


def test_api_not_initialized():
    api = PrinterAPI()  # aucun callback défini

    result = api.print_ticket("payload")

    assert result['success'] is False
    assert result['code'] == 'error_not_initialized'
    assert isinstance(result['message'], str) and result['message']


def test_api_callback_raises():
    api = PrinterAPI()

    def boom(data, job_id=None):
        raise RuntimeError("boom")

    api.set_print_callback(boom)

    result = api.print_ticket("payload")

    assert result['success'] is False
    assert result['code'] == 'error_exception'
    assert 'boom' in result['message']
    assert result['maybe_printed'] is True


def test_api_resultat_non_dict_devient_incertain():
    """Un callback qui renvoie autre chose qu'un dict a pu imprimer avant :
    le résultat exposé au serveur reste ambigu, jamais un échec « sûr »."""
    api = PrinterAPI()
    api.set_print_callback(lambda data, job_id=None: None)

    result = api.print_ticket("payload")

    assert result['success'] is False
    assert result['code'] == 'invalid_result'
    assert result['maybe_printed'] is True


# --- Tests validation stricte des données d'impression ---------------------

# Ticket légitime : texte + toutes les séquences ESC/POS émises par le serveur
# (alignement, gras, taille double, souligné, séparateur).
_LEGIT_TICKET = (
    "\x1b\x61\x01Pharmacie\x1b\x61\x00\n"       # centré
    "\x1b\x45\x01Ticket\x1b\x45\x00\n"          # gras
    "\x1d\x21\x11A12\x1d\x21\x00\n"             # double taille
    + "-" * 42 + "\n"                            # séparateur
    "\x1b\x2d\x01Merci\x1b\x2d\x00\n"           # souligné
)


def test_payload_accepts_plain_text():
    assert decode_and_validate_print_payload(_b64("Bonjour")) == "Bonjour"


def test_payload_accepts_utf8_accents():
    text = "Numéro d'appel : 42\nMerci de patienter"
    assert decode_and_validate_print_payload(_b64(text)) == text


def test_payload_accepts_legit_escpos_sequences():
    assert decode_and_validate_print_payload(_b64(_LEGIT_TICKET)) == _LEGIT_TICKET


@pytest.mark.parametrize("payload", [
    "pas du base64 !!!@@@",          # hors alphabet base64
    _b64("ok")[:-1] + "*",           # caractère interdit inséré
])
def test_payload_rejects_invalid_base64(payload):
    with pytest.raises(ValueError):
        decode_and_validate_print_payload(payload)


def test_payload_rejects_non_string():
    with pytest.raises(ValueError):
        decode_and_validate_print_payload(12345)


def test_payload_rejects_empty():
    with pytest.raises(ValueError):
        decode_and_validate_print_payload("")


@pytest.mark.parametrize("dangerous", [
    "X\x1b\x70\x00\x19\xfaY",   # ESC p : tiroir-caisse (commande non autorisée)
    "X\x1d\x56\x00Y",           # GS V : découpe brute (commande non autorisée)
    "A\x07B",                    # BEL : caractère de contrôle brut
    "A\x1b",                     # séquence ESC incomplète
    "A\x00B",                    # NUL
])
def test_payload_rejects_unauthorized_control(dangerous):
    with pytest.raises(ValueError):
        decode_and_validate_print_payload(_b64(dangerous))


def test_payload_rejects_non_utf8():
    # Octets non décodables en UTF-8 (0xFF isolé), base64 pourtant valide.
    payload = base64.b64encode(b"\xff\xfe\xfd").decode('ascii')
    with pytest.raises(ValueError):
        decode_and_validate_print_payload(payload)


def test_payload_rejects_too_many_chars():
    with pytest.raises(ValueError):
        decode_and_validate_print_payload(_b64("a" * (MAX_TICKET_CHARS + 1)))


def test_payload_rejects_too_many_lines():
    with pytest.raises(ValueError):
        decode_and_validate_print_payload(_b64("x\n" * (MAX_TICKET_LINES + 1)))


def test_payload_rejects_oversized_encoded():
    with pytest.raises(ValueError):
        decode_and_validate_print_payload("A" * (MAX_ENCODED_LEN + 1))


def test_payload_error_never_leaks_content():
    # Le message d'erreur ne doit pas contenir le contenu (secret) du ticket.
    secret = "SECRET-TOKEN-12345\x07"
    try:
        decode_and_validate_print_payload(_b64(secret))
        raise AssertionError('aurait dû être refusé')
    except ValueError as e:
        assert "SECRET-TOKEN" not in str(e)


def test_print_rejects_dangerous_payload_without_printing(monkeypatch):
    # Une charge avec commande non autorisée => invalid_data, rien n'est imprimé.
    device = FakeDevice()
    p = make_printer(device=device, check_paper=False, monkeypatch=monkeypatch)

    result = p.print(_b64("X\x1b\x70\x00Y"))  # ESC p (tiroir-caisse)

    assert result['success'] is False
    assert result['code'] == 'invalid_data'
    assert device.text_calls == []
    assert device.cut_calls == 0


# --- Tests découplage matériel (fabrique de périphérique injectée) ----------

def test_initialize_printer_uses_injected_factory(monkeypatch):
    """initialize_printer crée le périphérique via la fabrique injectée : on
    peut donc utiliser une fausse imprimante SANS matériel USB."""
    fake = FakeDevice()
    calls = []

    def factory(id_vendor, id_product, model):
        calls.append((id_vendor, id_product, model))
        return fake

    p = make_printer(device=None, check_paper=False, monkeypatch=monkeypatch,
                     device_factory=factory)

    p.initialize_printer()

    assert p.p is fake
    assert p.error is False
    assert fake.open_calls == 1
    # La fabrique a reçu les identifiants/modèle du Printer.
    assert calls == [(0x04b8, 0x0202, 'TM-T88II')]


def test_initialize_printer_factory_failure_sets_error(monkeypatch):
    """Si la fabrique échoue (matériel absent), l'imprimante passe en erreur
    sans lever, et un ticket ne sera pas imprimé."""
    def factory(id_vendor, id_product, model):
        raise RuntimeError("USB indisponible")

    p = make_printer(device=None, check_paper=False, monkeypatch=monkeypatch,
                     device_factory=factory)

    assert p.initialize_printer() is False

    assert p.p is None
    assert p.error is True


def test_initialize_printer_requires_real_open(monkeypatch):
    """Un objet retourné par la fabrique ne suffit pas : ``open()`` doit
    réussir avant que ``p`` soit considéré connecté."""
    fake = FakeDevice(open_exc=printer_module.DeviceNotFoundError("absent"))
    p = make_printer(device=None, check_paper=False, monkeypatch=monkeypatch,
                     device_factory=lambda v, pr, m: fake)

    assert p.initialize_printer() is False

    assert fake.open_calls == 1
    assert fake.close_calls == 1
    assert p.p is None
    assert p.error is True
    assert p.status_queue.get_nowait()['error'] == 'error_not_found'


def test_try_reconnect_reopens_until_success(monkeypatch):
    """La boucle de santé retrouve bien un handle OUVERT après un échec initial."""
    devices = [FakeDevice(open_exc=printer_module.DeviceNotFoundError("absent")),
               FakeDevice()]
    calls = []

    def factory(id_vendor, id_product, model):
        device = devices[len(calls)]
        calls.append(device)
        return device

    p = make_printer(device=None, check_paper=False, monkeypatch=monkeypatch,
                     device_factory=factory)

    assert p.initialize_printer() is False
    p._try_reconnect()

    assert [d.open_calls for d in devices] == [1, 1]
    assert devices[0].close_calls == 1
    assert p.p is devices[1]
    assert p.error is False


def test_paper_check_error_closes_handle_and_blocks_print(monkeypatch):
    """Une sonde USB en échec ne doit pas mener à une impression sur un état
    inconnu : le handle est fermé puis rouvert par le gestionnaire de santé."""
    fake = FakeDevice(paper_exc=RuntimeError("lecture impossible"))
    p = make_printer(device=fake, check_paper=True, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'error_paper_check'
    assert fake.text_calls == []
    assert fake.close_calls == 1
    assert p.p is None
    assert p.error is True


def test_unknown_paper_status_blocks_print(monkeypatch):
    """Un code papier hors contrat n'est pas transformé en succès."""
    fake = FakeDevice(paper_status_value=99)
    p = make_printer(device=fake, check_paper=True, monkeypatch=monkeypatch)

    result = p.print(VALID_PAYLOAD)

    assert result['success'] is False
    assert result['code'] == 'error_paper_check'
    assert fake.text_calls == []


def test_default_factory_bounds_usb_io_timeout(monkeypatch):
    """Le vrai périphérique reçoit un timeout : les appels pyusb ne doivent pas
    être créés avec le timeout=0 (infini) par défaut de python-escpos."""
    created = {}

    class Device:
        def __init__(self, *args, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(printer_module, 'CustomUsb', Device)

    printer_module._default_device_factory(0x04b8, 0x0202, 'TM-T88II')

    assert created['timeout'] == printer_module.USB_IO_TIMEOUT_MS


def test_custom_usb_read_uses_timeout():
    """Usb._read d'origine ignore self.timeout : la surcharge le transmet."""
    class Device:
        def __init__(self):
            self.calls = []

        def read(self, endpoint, size, timeout):
            self.calls.append((endpoint, size, timeout))
            return b'ready'

    custom = printer_module.CustomUsb.__new__(printer_module.CustomUsb)
    custom.device = Device()
    custom.in_ep = 0x82
    custom.timeout = 1234

    assert custom._read() == b'ready'
    assert custom.device.calls == [(0x82, 16, 1234)]


def test_custom_usb_requires_set_configuration():
    """La surcharge CustomUsb rend l'échec de configuration USB fatal."""
    class Device:
        def set_configuration(self):
            raise printer_module.usb.core.USBError("configuration refusée")

        def reset(self):
            raise AssertionError("ne doit pas être appelé")

    custom = printer_module.CustomUsb.__new__(printer_module.CustomUsb)
    custom.device = Device()

    with pytest.raises(printer_module.usb.core.USBError):
        custom._configure_usb()


def test_custom_usb_tolerates_reset_failure_after_configuration():
    """La preuve minimale est ``set_configuration`` ; un reset non supporté ne
    doit pas rejeter une imprimante par ailleurs correctement ouverte."""
    class Device:
        def __init__(self):
            self.calls = []

        def set_configuration(self):
            self.calls.append('configure')

        def reset(self):
            self.calls.append('reset')
            raise printer_module.usb.core.USBError("reset non supporté")

    custom = printer_module.CustomUsb.__new__(printer_module.CustomUsb)
    custom.device = Device()

    custom._configure_usb()

    assert custom.device.calls == ['configure', 'reset']


def test_print_end_to_end_with_fake_printer(monkeypatch):
    """Impression complète (init via fabrique + print) avec une fausse
    imprimante : démontre l'exécution des tests sans dépendance matérielle."""
    fake = FakeDevice()
    p = make_printer(device=None, check_paper=False, monkeypatch=monkeypatch,
                     device_factory=lambda v, pr, m: fake)

    p.initialize_printer()
    result = p.print(_b64("Bonjour"))

    assert result['success'] is True
    assert fake.text_calls == ["Bonjour"]
    assert fake.cut_calls == 1
