"""Code PIN de maintenance de la borne (logique pure, sans interface).

La touche de maintenance (F10, cf. ``main.MAINTENANCE_KEY``) ouvre l'éditeur
de configuration sans quitter l'application. Le patient n'a pas de clavier :
le raccourci suffit à le tenir à l'écart. Le code PIN est donc **optionnel** ;
il protège les sites où un clavier reste branché et accessible.

Le PIN n'est jamais stocké en clair : ``settings.json`` contient seulement
une empreinte PBKDF2-SHA256 salée (``Settings.maintenance_pin_hash``), au
format ``pbkdf2_sha256$<itérations>$<sel hex>$<empreinte hex>``. Une chaîne
vide signifie « aucun PIN ».

En cas d'oubli, ``--config`` (lancé depuis le système) ouvre l'éditeur sans
demander le PIN : qui accède au système contrôle déjà la borne.
"""

import hashlib
import hmac
import secrets

#: Touche qui ouvre la configuration depuis la borne en service. F11 est
#: déjà pris (bascule plein écran).
MAINTENANCE_KEY = "F10"

PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 12

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def pin_format_error(pin) -> str | None:
    """Message d'erreur si ``pin`` n'est pas un code acceptable, sinon None.

    Chiffres uniquement : la saisie se fait sur un pavé numérique ou une
    rangée de chiffres, sans ambiguïté de disposition de clavier."""
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit():
        return "Le code PIN ne doit contenir que des chiffres."
    if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
        return (f"Le code PIN doit comporter de {PIN_MIN_LENGTH} à "
                f"{PIN_MAX_LENGTH} chiffres.")
    return None


def _derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


def hash_pin(pin: str) -> str:
    """Empreinte salée de ``pin``, à stocker dans la configuration.

    Lève ``ValueError`` si le format est refusé (cf. ``pin_format_error``)."""
    error = pin_format_error(pin)
    if error:
        raise ValueError(error)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(pin, salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def _parse(stored):
    """(itérations, sel, empreinte) d'une empreinte stockée, ou None."""
    if not isinstance(stored, str):
        return None
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _ALGORITHM:
        return None
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        digest = bytes.fromhex(parts[3])
    except ValueError:
        return None
    if iterations < 1 or not salt or len(digest) != 32:
        return None
    return iterations, salt, digest


def is_valid_hash(stored) -> bool:
    """Vrai si ``stored`` est une empreinte lisible (et non vide)."""
    return _parse(stored) is not None


def pin_required(stored) -> bool:
    """Vrai si un PIN doit être demandé avant d'ouvrir la configuration.

    Une empreinte illisible ne bloque pas l'accès : la validation de la
    configuration la signale déjà, et la borne refuse alors de démarrer
    tant qu'elle n'est pas corrigée."""
    return is_valid_hash(stored)


def verify_pin(pin, stored) -> bool:
    """Vrai si ``pin`` correspond à l'empreinte ``stored``.

    Comparaison à temps constant ; toute entrée invalide renvoie False."""
    parsed = _parse(stored)
    if parsed is None or not isinstance(pin, str) or not pin:
        return False
    iterations, salt, digest = parsed
    return hmac.compare_digest(_derive(pin, salt, iterations), digest)


def resolve_pin_change(new_pin: str, confirm_pin: str, remove: bool,
                       current_hash: str) -> tuple[str, str | None]:
    """Empreinte à enregistrer d'après la saisie de l'éditeur.

    Renvoie ``(empreinte, erreur)`` ; si ``erreur`` n'est pas None, rien ne
    doit être enregistré.

    - nouveau PIN saisi : il doit être confirmé et bien formé ;
    - sinon, case « supprimer » cochée : plus de PIN (empreinte vide) ;
    - sinon : le PIN actuel est conservé tel quel."""
    new_pin = new_pin or ""
    confirm_pin = confirm_pin or ""
    if new_pin or confirm_pin:
        if new_pin != confirm_pin:
            return current_hash, ("Les deux saisies du code PIN de maintenance "
                                  "ne correspondent pas.")
        error = pin_format_error(new_pin)
        if error:
            return current_hash, error
        return hash_pin(new_pin), None
    if remove:
        return "", None
    return current_hash, None
