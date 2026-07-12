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
import queue
import threading

import pytest

import printer as printer_module
from printer import (
    Printer,
    PrinterAPI,
    decode_and_validate_print_payload,
    MAX_ENCODED_LEN,
    MAX_DECODED_BYTES,
    MAX_TICKET_CHARS,
    MAX_TICKET_LINES,
)


def _b64(text):
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


# --- Doubles de test -------------------------------------------------------

class FakeDevice:
    """Imite l'objet imprimante escpos utilisé par Printer.print."""

    def __init__(self, text_exc=None, paper_status_value=2):
        self.text_exc = text_exc
        self.paper_status_value = paper_status_value
        self.text_calls = []
        self.cut_calls = 0

    def text(self, data):
        self.text_calls.append(data)
        if self.text_exc is not None:
            raise self.text_exc

    def cut(self):
        self.cut_calls += 1

    def paper_status(self):
        return self.paper_status_value


def make_printer(device=None, error=False, check_paper=False, monkeypatch=None):
    """Construit un Printer sans passer par __init__ (pas de matériel/thread)."""
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


VALID_PAYLOAD = base64.b64encode("Bonjour".encode('utf-8')).decode('ascii')


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
    assert set(['success', 'code', 'message']).issubset(result.keys())
    assert isinstance(result['success'], bool)


# --- Tests PrinterAPI.print_ticket ----------------------------------------

def test_api_forwards_callback_result():
    api = PrinterAPI()
    expected = {'success': True, 'code': 'print_ok', 'message': 'Ticket imprimé.'}
    api.set_print_callback(lambda data: expected)

    assert api.print_ticket("payload") == expected


def test_api_not_initialized():
    api = PrinterAPI()  # aucun callback défini

    result = api.print_ticket("payload")

    assert result['success'] is False
    assert result['code'] == 'error_not_initialized'
    assert isinstance(result['message'], str) and result['message']


def test_api_callback_raises():
    api = PrinterAPI()

    def boom(data):
        raise RuntimeError("boom")

    api.set_print_callback(boom)

    result = api.print_ticket("payload")

    assert result['success'] is False
    assert result['code'] == 'error_exception'
    assert 'boom' in result['message']


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
        assert False, "aurait dû être refusé"
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
