"""Code PIN de maintenance de la borne (logique pure, sans interface).

La touche de maintenance (F10, cf. ``main.MAINTENANCE_KEY``) ouvre la
fenêtre de maintenance : ouvrir la configuration, reprendre le service ou
quitter l'application borne. Le patient n'a pas de clavier :
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
from dataclasses import dataclass

#: Touche qui ouvre la maintenance depuis la borne en service.
MAINTENANCE_KEY = "F10"
#: Touche qui bascule la borne entre plein écran et fenêtre (technicien).
FULLSCREEN_KEY = "F11"

#: Actions proposées par le menu de maintenance (maintenance_gate.
#: ActionDialog) après la touche de maintenance ; définies ici pour être
#: partagées avec ``main`` sans importer Qt. Toute autre issue (annulation,
#: inactivité, fenêtre en panne) laisse la borne en service.
MAINTENANCE_ACTION_CONFIGURE = "configure"
#: Reprendre le service : même effet qu'une annulation.
MAINTENANCE_ACTION_RESUME = "resume"
#: Fermer l'application borne sans la relancer. Le PC, lui, reste allumé.
MAINTENANCE_ACTION_QUIT = "quit"
#: Fermer l'application puis éteindre le PC.
MAINTENANCE_ACTION_POWEROFF = "poweroff"
#: Actions qui arrêtent la borne sans la relancer (confirmation demandée).
STOPPING_ACTIONS = (MAINTENANCE_ACTION_QUIT, MAINTENANCE_ACTION_POWEROFF)


@dataclass(frozen=True)
class MaintenanceChoice:
    """Un bouton de la fenêtre de maintenance."""
    action: str
    label: str
    description: str
    #: Action qui arrête la borne : affichée à part, en rouge, confirmée.
    danger: bool = False


#: Boutons de la fenêtre de maintenance, de haut en bas. Le choix par défaut
#: (Entrée) est en tête ; les actions qui arrêtent la borne sont en dernier,
#: séparées des autres, pour qu'un tap mal placé sur l'écran tactile ne tombe
#: pas dessus. « Éteindre le PC » n'est proposé que si le système sait le
#: faire (``maintenance_choices``).
MAINTENANCE_CHOICES = (
    MaintenanceChoice(
        MAINTENANCE_ACTION_CONFIGURE, "Ouvrir la configuration",
        "Serveur, imprimante, code PIN… La borne redémarre à la fermeture "
        "de la configuration."),
    MaintenanceChoice(
        MAINTENANCE_ACTION_RESUME, "Reprendre le service",
        "Ferme cette fenêtre : la borne reprend aussitôt, sans redémarrer."),
    MaintenanceChoice(
        MAINTENANCE_ACTION_QUIT, "Quitter l'application",
        "Ferme la borne sans la relancer. Le PC reste allumé.",
        danger=True),
    MaintenanceChoice(
        MAINTENANCE_ACTION_POWEROFF, "Éteindre le PC",
        "Ferme la borne puis éteint le PC. Pour la rallumer : bouton "
        "d'alimentation du PC.",
        danger=True),
)


def maintenance_choices(poweroff_supported: bool) -> tuple:
    """Boutons du menu ; sans extinction possible sur ce système, « Éteindre
    le PC » n'est pas proposé."""
    return tuple(c for c in MAINTENANCE_CHOICES
                 if poweroff_supported
                 or c.action != MAINTENANCE_ACTION_POWEROFF)


def poweroff_confirmation_text() -> str:
    """Texte de confirmation de « Éteindre le PC »."""
    return ("L'application borne va se fermer, puis le PC va s'éteindre.\n\n"
            "Pour rallumer la borne : appuyez sur le bouton d'alimentation du "
            "PC (elle se lance d'elle-même au démarrage).\n\n"
            "Éteindre le PC ?")


def poweroff_failed_text() -> str:
    """Message affiché si le système refuse l'extinction."""
    return ("Le système a refusé d'éteindre le PC (droits insuffisants ?). "
            "La borne va redémarrer.\n\n"
            "Pour l'éteindre : menu du système, ou bouton d'alimentation du "
            "PC.")


def quit_confirmation_text(shortcuts_supported: bool) -> str:
    """Texte de confirmation de « Quitter l'application ».

    Dit clairement que seule l'application s'arrête (pas le PC) et comment
    la relancer. ``shortcuts_supported`` : la borne crée ses raccourcis
    système (Linux : menu des applications + démarrage à l'ouverture de
    session, cf. ``desktop_integration``)."""
    if shortcuts_supported:
        relaunch = ("Pour la relancer : « Flowelis Borne » dans le menu des "
                    "applications, ou redémarrez le PC (elle se lance "
                    "d'elle-même à l'ouverture de session).")
    else:
        relaunch = ("Pour la relancer : ouvrez à nouveau l'application "
                    "Flowelis Borne, ou redémarrez le PC.")
    return ("L'application borne va se fermer et ne se relancera pas "
            "d'elle-même. Le PC reste allumé.\n\n"
            f"{relaunch}\n\n"
            "Quitter l'application ?")


def maintenance_intro_text(timeout_s: int) -> str:
    """Phrase d'en-tête du menu de maintenance."""
    return ("La borne est en pause tant que cette fenêtre est ouverte. Sans "
            f"réponse d'ici {timeout_s} s, elle reprend son service "
            "d'elle-même.")


#: Codes envoyés au serveur quand la borne s'arrête volontairement : sans
#: eux, elle y passerait pour injoignable (alerte au comptoir). Le serveur
#: connaît ces deux codes (``models.KIOSK_MAINTENANCE_CODE`` et
#: ``KIOSK_STOPPED_CODE``).
SERVER_EVENT_MAINTENANCE = "kiosk_maintenance"
SERVER_EVENT_STOPPED = "kiosk_stopped"
#: Code d'alerte : trop de codes PIN incorrects.
SERVER_EVENT_PIN_LOCKOUT = "kiosk_pin_lockout"


def shutdown_server_event(action) -> tuple[str, str]:
    """``(code, message)`` annoncé au serveur quand la fenêtre de la borne
    se ferme pour la maintenance, selon l'action choisie (None : fenêtre
    fermée autrement, la borne redémarre sur le menu de maintenance)."""
    if action == MAINTENANCE_ACTION_QUIT:
        return (SERVER_EVENT_STOPPED,
                "Application de la borne quittée depuis le menu de "
                "maintenance.")
    if action == MAINTENANCE_ACTION_POWEROFF:
        return (SERVER_EVENT_STOPPED,
                "Borne éteinte (PC arrêté) depuis le menu de maintenance.")
    if action == MAINTENANCE_ACTION_CONFIGURE:
        return (SERVER_EVENT_MAINTENANCE,
                "Borne arrêtée pour configuration (menu de maintenance).")
    return (SERVER_EVENT_MAINTENANCE,
            "Fenêtre de la borne fermée : redémarrage sur le menu de "
            "maintenance.")


def pin_lockout_message(failures: int, wait_text: str) -> str:
    """Message d'alerte envoyé au serveur lors d'une attente imposée."""
    return (f"Code PIN de maintenance incorrect {failures} fois de suite sur "
            f"la borne : nouvel essai possible dans {wait_text}.")


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


def fullscreen_toggle_allowed(currently_fullscreen: bool, stored) -> bool:
    """La touche plein écran (F11) peut-elle agir ?

    Revenir en plein écran est toujours permis. En sortir ne l'est que sans
    code PIN : la fenêtre obtenue donnerait accès au bureau et à la croix de
    fermeture, ce qui contournerait le PIN de la touche de maintenance."""
    return not currently_fullscreen or not pin_required(stored)


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
