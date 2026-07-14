"""Stockage sécurisé des secrets de la borne (``password`` et ``app_secret``).

Historique
----------
Ces secrets étaient écrits **en clair** dans ``settings.json`` (donc lisibles par
tout programme du poste). On les déplace vers le gestionnaire de secrets du
système via ``keyring`` : Gestionnaire d'identifiants Windows (DPAPI), Trousseau
macOS, Secret Service Linux.

Politique de repli (point 5)
----------------------------
On ne retombe **jamais silencieusement** sur un stockage en clair.
:func:`store_secrets` renvoie ``False`` lorsque le magasin sécurisé est
indisponible ; l'appelant (``config.Config.save_settings``) décide alors selon le
mode : **refus** en production (exception), **avertissement + clair** en
développement.

Aucune valeur secrète n'est jamais journalisée : seuls les noms de champ le sont.
"""

import logging

logger = logging.getLogger("borne.secret_store")

# Namespace de la borne dans le magasin d'identifiants du système.
SERVICE_NAME = "GestionFile-Borne"

# Champs de ``Settings`` traités comme secrets (jamais écrits en clair).
SECRET_FIELDS = ("password", "app_secret")


class SecretStoreUnavailableError(Exception):
    """Le magasin de secrets du système est indisponible et l'écriture en clair
    est refusée (mode production)."""


try:  # keyring est optionnel : la borne doit rester importable sans lui.
    import keyring
    from keyring.errors import KeyringError  # noqa: F401 (documentaire)
    _KEYRING_IMPORTED = True
except Exception as exc:  # pragma: no cover - dépend de l'environnement
    keyring = None
    _KEYRING_IMPORTED = False
    logger.warning("keyring indisponible (%s) : secrets non sécurisés.", exc)


def available() -> bool:
    """Vrai si un magasin de secrets réellement fonctionnel est disponible.

    Le backend « fail » de keyring (aucun magasin réel) est traité comme absent :
    il ne stocke rien, on ne doit donc pas le considérer comme sécurisé."""
    if not _KEYRING_IMPORTED:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:  # pragma: no cover - défensif
        return False
    module = (type(backend).__module__ or "").lower()
    return "fail" not in module


def get_secret(name) -> str:
    """Valeur du secret ``name`` dans le magasin, ou chaîne vide."""
    if not _KEYRING_IMPORTED:
        return ""
    try:
        return keyring.get_password(SERVICE_NAME, name) or ""
    except Exception as exc:  # pragma: no cover - dépend du backend
        logger.warning("Lecture keyring impossible pour %r (%s).", name, exc)
        return ""


def set_secret(name, value) -> bool:
    """Écrit ``value`` pour le secret ``name``. Renvoie ``True`` si réussi."""
    if not _KEYRING_IMPORTED:
        return False
    try:
        keyring.set_password(SERVICE_NAME, name, value or "")
        return True
    except Exception as exc:  # pragma: no cover - dépend du backend
        logger.warning("Écriture keyring impossible pour %r (%s).", name, exc)
        return False


def load_secrets() -> dict:
    """Secrets présents dans le magasin sécurisé ({champ: valeur}, vides omis)."""
    result = {}
    for name in SECRET_FIELDS:
        value = get_secret(name)
        if value:
            result[name] = value
    return result


def store_secrets(values) -> bool:
    """Écrit dans le magasin sécurisé les secrets présents dans ``values``.

    Renvoie ``True`` seulement si le magasin est disponible ET que tous les
    champs demandés ont été écrits ; ``False`` sinon. N'écrit jamais en clair
    (c'est à l'appelant de décider du repli)."""
    if not available():
        return False
    ok = True
    for name in SECRET_FIELDS:
        if name in values:
            if not set_secret(name, values[name]):
                ok = False
    return ok
