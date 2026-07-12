# logging_config.py
"""Configuration centralisée de la journalisation de la borne.

Remplace les ``print()`` par ``logging`` avec :
- niveaux (DEBUG/INFO/WARNING/ERROR) ;
- horodatage, niveau, composant (nom du logger) et identifiant de travail
  (``job_id``) dans chaque ligne ;
- masquage des secrets/jetons/mots de passe (défense en profondeur) ;
- rotation locale des fichiers de log.

Utilisation :
    import logging
    logger = logging.getLogger("borne.<composant>")

    # au démarrage du processus (main.py / config-editor.py) :
    import logging_config
    logging_config.setup_logging()
    logging_config.register_secret(app_secret)   # masquage runtime

Convention des noms de logger : ``borne.main``, ``borne.printer``,
``borne.config``, ``borne.status``, ``borne.editor``.
"""
import logging
import logging.handlers
import os
import platform
import re
import threading
from pathlib import Path

APP_NAME = "FileAttente"  # cohérent avec config.Config.app_name
LOG_FILENAME = "borne.log"

# Rotation locale : ~5 Mo au total (1 Mo × 5 sauvegardes). Suffisant pour une
# borne en libre-service sans saturer le disque.
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 5

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [job=%(job_id)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Longueur minimale d'une valeur pour être masquée telle quelle (évite de
# masquer des fragments trop courts et fréquents).
_MIN_SECRET_LEN = 4

_configured = False
_setup_lock = threading.Lock()


def default_log_dir() -> Path:
    """Répertoire de logs, dans l'espace utilisateur (même base que la config).

    Calculé indépendamment de ``config.Config`` pour pouvoir initialiser la
    journalisation AVANT le chargement de la configuration (dont les messages
    doivent déjà être capturés)."""
    system = platform.system()
    if system == "Windows":
        base = os.path.join(os.environ.get("LOCALAPPDATA", str(Path.home())), APP_NAME)
    elif system == "Linux":
        base = os.path.join(str(Path.home()), ".config", APP_NAME)
    else:
        # Repli raisonnable sur les autres systèmes (dev).
        base = os.path.join(str(Path.home()), f".{APP_NAME}")
    return Path(base) / "logs"


class _DefaultFieldsFilter(logging.Filter):
    """Garantit la présence des champs personnalisés du format (``job_id``) pour
    les enregistrements qui ne les fournissent pas, afin que le formateur ne
    lève pas de KeyError."""

    def filter(self, record):
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        return True


class RedactingFilter(logging.Filter):
    """Masque les secrets/jetons/mots de passe dans les messages de log.

    Double protection :
    - valeurs exactes enregistrées à l'exécution (``register_secret``) : secret
      d'application, mot de passe, jetons (remplacés à chaque renouvellement) ;
    - motifs génériques (clé=valeur) pour les cas non anticipés.
    """

    _PATTERNS = [
        re.compile(r"(x-app-token['\"\s:=]+)\S+", re.IGNORECASE),
        re.compile(r"(app[_-]?secret['\"\s:=]+)\S+", re.IGNORECASE),
        re.compile(r"(password['\"\s:=]+)\S+", re.IGNORECASE),
        re.compile(r"(bearer\s+)\S+", re.IGNORECASE),
        re.compile(r"(['\"]?token['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    ]
    _MASK = "***"

    def __init__(self):
        super().__init__()
        self._secrets = set()
        self._lock = threading.Lock()

    def register_secret(self, value):
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
            with self._lock:
                self._secrets.add(value)

    def _redact(self, text):
        with self._lock:
            secrets = list(self._secrets)
        # Masque d'abord les valeurs exactes connues (les plus longues d'abord
        # pour éviter les masquages partiels).
        for secret in sorted(secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, self._MASK)
        for pattern in self._PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + self._MASK, text)
        return text

    def filter(self, record):
        try:
            message = record.getMessage()
            redacted = self._redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            # La journalisation ne doit jamais casser le flux applicatif.
            pass
        return True


# Filtre de masquage partagé (permet register_secret depuis l'extérieur).
_redacting_filter = RedactingFilter()


def register_secret(value):
    """Enregistre une valeur sensible à masquer dans TOUS les logs (secret
    d'application, mot de passe, jeton). À rappeler à chaque renouvellement de
    jeton."""
    _redacting_filter.register_secret(value)


def setup_logging(level=logging.INFO, log_dir=None, console=True):
    """Configure la journalisation du processus (idempotent).

    - ``level`` : niveau initial (ajustable ensuite via ``set_level``).
    - fichier tournant dans ``log_dir`` (défaut : ``default_log_dir()``) ;
    - handler console optionnel (utile en développement).

    Si l'écriture du fichier échoue (droits, disque), on continue avec la seule
    sortie console : la journalisation ne doit pas empêcher la borne de démarrer.
    """
    global _configured
    with _setup_lock:
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)  # les handlers filtrent le niveau effectif

        # Évite d'empiler les handlers si appelé plusieurs fois.
        if _configured:
            return

        formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
        default_fields = _DefaultFieldsFilter()

        handlers = []

        if console:
            stream = logging.StreamHandler()
            stream.setLevel(level)
            handlers.append(stream)

        log_directory = Path(log_dir) if log_dir else default_log_dir()
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            # Restreint l'accès au répertoire de logs (déterminant sous Linux).
            try:
                os.chmod(log_directory, 0o700)
            except OSError:
                pass
            file_handler = logging.handlers.RotatingFileHandler(
                log_directory / LOG_FILENAME,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            handlers.append(file_handler)
        except Exception as e:
            # Pas de fichier de log possible : on garde au moins la console.
            logging.getLogger("borne.logging").warning(
                "Journalisation fichier indisponible (%s), sortie console seule.", e)

        for handler in handlers:
            handler.setFormatter(formatter)
            handler.addFilter(default_fields)
            handler.addFilter(_redacting_filter)
            root.addHandler(handler)

        _configured = True


def set_level(level):
    """Ajuste le niveau effectif de tous les handlers (ex. DEBUG si la config
    ``debug`` est active, INFO sinon)."""
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)
