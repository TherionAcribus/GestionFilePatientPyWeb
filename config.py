# config.py
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from urllib.parse import urlparse, urlunparse
import platform

import secret_store

logger = logging.getLogger("borne.config")

# Secret d'application par défaut (livré dans l'exemple) : à refuser en
# production, cf. has_insecure_default_credentials / main.py.
DEFAULT_APP_SECRET = "votre_secret_app"

# Hôtes considérés comme « locaux » : seuls ceux-ci (ou le mode développement)
# autorisent le HTTP en clair. Tout le reste doit passer en HTTPS.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

# Identifiant USB : hexadécimal 16 bits, avec ou sans préfixe 0x (ex. 0x04b8).
_USB_ID_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{1,4}$")


def _host_is_local(host: str) -> bool:
    """Vrai si l'hôte désigne la machine locale (boucle locale). Utilisé pour
    n'autoriser le HTTP en clair que localement."""
    if not host:
        return False
    host = host.lower()
    if host in _LOCAL_HOSTNAMES:
        return True
    # Toute la plage de bouclage 127.0.0.0/8.
    return host.startswith("127.")


@dataclass
class Settings:
    """Structure des paramètres de l'application"""
    base_url: str = "http://localhost:5000"
    fullscreen: bool = False
    debug: bool = True
    # Masquer le curseur (mode kiosque tactile). Mettre à False pour un poste de
    # maintenance à la souris. Même à True, le curseur réapparaît dès qu'une
    # souris est utilisée et se remasque au toucher suivant (cf. main.py).
    hide_cursor: bool = True
    username: str = "admin"
    password: str = "admin"
    printer_id_vendor: str = "0x04b8"
    printer_id_product: str = "0x0202"
    printer_model: str = "TM-T88II"
    app_secret: str = DEFAULT_APP_SECRET
    check_paper: bool = True
    # Identifiant de la borne joint aux statuts imprimante. Vide => le hostname
    # de la machine est utilisé par défaut (voir Printer.__init__).
    borne_id: str = ""

    @property
    def url(self) -> str:
        return f"{self.normalized_base_url()}/patient"

    @property
    def is_production(self) -> bool:
        """Production = mode debug désactivé."""
        return not self.debug

    def has_insecure_default_credentials(self) -> bool:
        """Vrai si des identifiants par défaut (admin/admin) ou le secret
        d'application par défaut sont encore en place. À refuser en production
        (cf. main.py) pour ne pas exposer une borne avec des accès triviaux."""
        return (
            (self.username == "admin" and self.password == "admin")
            or self.app_secret in ("", DEFAULT_APP_SECRET)
        )

    # ------------------------------------------------------------------
    # Validation / normalisation
    # ------------------------------------------------------------------
    def normalized_base_url(self) -> str:
        """URL racine normalisée par un parseur : schéma et hôte en minuscules,
        sans slash final. La borne (main.py) utilise cette valeur telle quelle.

        Best-effort : si l'URL est invalide, ``validate()`` l'aura déjà signalée
        et le démarrage sera refusé ; on renvoie ici la meilleure normalisation
        possible sans lever."""
        raw = (self.base_url or "").strip()
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        if not scheme or not netloc:
            # URL non conforme (pas de schéma/hôte) : renvoyer la saisie nettoyée
            # de son slash final, la validation refusera le démarrage.
            return raw.rstrip("/")
        return urlunparse((scheme, netloc, path, "", "", ""))

    def base_url_errors(self) -> list:
        """Erreurs de l'URL du serveur (liste de messages, vide si valide).

        Le HTTP en clair n'est autorisé que pour un hôte local OU en mode
        développement (debug) ; un serveur distant en production doit être en
        HTTPS. On ne réécrit plus silencieusement http -> https : une URL non
        conforme est signalée."""
        if not isinstance(self.base_url, str):
            return []  # l'erreur de type est signalée par ailleurs
        raw = self.base_url.strip()
        if not raw:
            return ["L'URL du serveur ne peut pas être vide."]
        try:
            parsed = urlparse(raw)
        except Exception:
            return [f"L'URL du serveur est invalide : {self.base_url!r}."]
        if parsed.scheme not in ("http", "https"):
            return ["L'URL du serveur doit commencer par http:// ou https://."]
        if not parsed.hostname:
            return ["L'URL du serveur ne contient pas de nom d'hôte valide."]
        try:
            parsed.port  # lève ValueError si le port n'est pas numérique
        except ValueError:
            return ["Le port indiqué dans l'URL du serveur est invalide."]
        if parsed.scheme == "http" and not (
            _host_is_local(parsed.hostname) or self.debug is True
        ):
            return [
                "HTTP n'est autorisé que pour localhost ou en mode développement "
                "(debug). Utilisez https:// pour un serveur distant."
            ]
        return []

    def usb_id_errors(self, field_name: str, value) -> list:
        """Erreurs d'un identifiant USB (vendeur/produit) : format hexadécimal
        16 bits (0x0000..0xFFFF)."""
        if not isinstance(value, str):
            return []  # l'erreur de type est signalée par ailleurs
        raw = value.strip()
        if not _USB_ID_RE.match(raw):
            return [
                f"L'identifiant USB « {field_name} » doit être hexadécimal "
                f"(ex. 0x04b8) ; valeur reçue : {value!r}."
            ]
        try:
            number = int(raw, 16)
        except ValueError:
            return [f"L'identifiant USB « {field_name} » est invalide : {value!r}."]
        if not (0 <= number <= 0xFFFF):
            return [
                f"L'identifiant USB « {field_name} » doit être compris entre "
                "0x0000 et 0xFFFF."
            ]
        return []

    def validate(self) -> list:
        """Valide la configuration AVANT démarrage. Renvoie la liste des
        problèmes (vide = configuration valide). Vérifie les types, l'URL
        (parseur + règle HTTP/HTTPS), les identifiants USB, le modèle, les
        identifiants d'authentification et le secret d'application."""
        errors = []

        # Types : une valeur JSON du mauvais type ne doit pas passer en douce.
        for name in ("fullscreen", "debug", "hide_cursor", "check_paper"):
            if not isinstance(getattr(self, name), bool):
                errors.append(f"Le champ « {name} » doit être un booléen (vrai/faux).")
        for name in (
            "base_url", "username", "password", "printer_id_vendor",
            "printer_id_product", "printer_model", "app_secret", "borne_id",
        ):
            if not isinstance(getattr(self, name), str):
                errors.append(
                    f"Le champ « {name} » doit être une chaîne de caractères."
                )

        # URL du serveur.
        errors.extend(self.base_url_errors())

        # Authentification borne.
        if isinstance(self.username, str) and not self.username.strip():
            errors.append("Le nom d'utilisateur ne peut pas être vide.")
        if isinstance(self.password, str) and self.password == "":
            errors.append("Le mot de passe ne peut pas être vide.")
        if isinstance(self.app_secret, str) and not self.app_secret.strip():
            errors.append("Le secret d'application ne peut pas être vide.")

        # Imprimante.
        errors.extend(self.usb_id_errors("printer_id_vendor", self.printer_id_vendor))
        errors.extend(self.usb_id_errors("printer_id_product", self.printer_id_product))
        if isinstance(self.printer_model, str) and not self.printer_model.strip():
            errors.append("Le modèle d'imprimante ne peut pas être vide.")

        return errors


class Config:
    """Gestionnaire de configuration"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Initialise les attributs de l'instance"""
        self.app_name = "FileAttente"
        self.config_path = self._get_config_path()
        self._ensure_config_dir()
        self.settings = None
        # Renseigné si le fichier de configuration existe mais est illisible :
        # la borne REFUSE alors de démarrer (main.py) au lieu de tourner en
        # douce avec les valeurs par défaut (cf. load_settings).
        self.load_error = None
        self.load_settings()

    def _get_config_path(self) -> Path:
        """Détermine le chemin de configuration selon le système d'exploitation"""
        system = platform.system()

        if system == "Windows":
            # Sur Windows, utilise AppData/Local
            base_path = os.path.join(os.environ["LOCALAPPDATA"], self.app_name)
        elif system == "Linux":
            # Sur Linux, utilise ~/.config
            base_path = os.path.join(str(Path.home()), ".config", self.app_name)
        else:
            raise OSError(f"Système d'exploitation non supporté: {system}")

        return Path(base_path)

    def _ensure_config_dir(self):
        """Crée le répertoire de configuration s'il n'existe pas"""
        self.config_path.mkdir(parents=True, exist_ok=True)

    def load_settings(self):
        """Charge les paramètres depuis le fichier JSON.

        Si le fichier existe mais est illisible (JSON invalide, contenu qui
        n'est pas un objet...), on NE remplace PLUS silencieusement la
        configuration par les valeurs par défaut : on mémorise l'erreur dans
        ``self.load_error`` pour que la borne refuse de démarrer et l'affiche.
        Les valeurs par défaut sont tout de même chargées en mémoire pour que
        l'objet reste utilisable (éditeur de configuration)."""
        config_file = self.config_path / "settings.json"
        self.load_error = None

        if not config_file.exists():
            # Premier démarrage : on écrit les valeurs par défaut. Un échec
            # d'écriture n'est pas fatal (on garde les défauts en mémoire).
            self.settings = Settings()
            # Si un magasin sécurisé contient déjà des secrets (fichier supprimé
            # mais keyring conservé), on les reprend au lieu d'écraser avec les
            # valeurs par défaut.
            self._apply_secret_store({})
            try:
                self.save_settings()
            except Exception as e:
                logger.error("Impossible d'écrire la configuration par défaut: %s", e)
            return

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("le contenu n'est pas un objet JSON")
            # Ignore les clés inconnues (ex : options retirées d'une version
            # antérieure comme websocket_enabled/websocket_debug). Sans ce
            # filtrage, Settings(**data) lèverait une TypeError.
            known = {f.name for f in fields(Settings)}
            ignored = set(data) - known
            if ignored:
                logger.warning("Clés de configuration ignorées (inconnues): %s",
                               ', '.join(sorted(ignored)))
            filtered = {k: v for k, v in data.items() if k in known}
            self.settings = Settings(**filtered)
            # Les secrets (password, app_secret) proviennent désormais du
            # magasin sécurisé du système ; on migre au besoin une valeur
            # héritée en clair puis on réécrit le fichier sans elle.
            if self._apply_secret_store(data):
                try:
                    self.save_settings()
                except Exception as e:
                    logger.error(
                        "Réécriture après migration des secrets impossible: %s", e)
        except Exception as e:
            # Config illisible : on NE bascule PAS en douce sur les défauts. On
            # signale l'erreur (main.py refusera de démarrer) tout en gardant un
            # objet utilisable.
            logger.error("Erreur lors du chargement des paramètres: %s", e)
            self.load_error = f"Fichier de configuration illisible : {e}"
            self.settings = Settings()

    def _apply_secret_store(self, raw_data) -> bool:
        """Renseigne les secrets de ``self.settings`` depuis le magasin sécurisé.

        - Si une valeur est présente dans le magasin, elle prime.
        - Sinon, une valeur héritée en clair dans ``raw_data`` (fichier JSON) est
          migrée vers le magasin lorsque c'est possible.

        Renvoie ``True`` si une migration a eu lieu (le fichier doit alors être
        réécrit pour effacer la copie en clair). Ne journalise jamais de valeur."""
        migrated = False
        raw = raw_data if isinstance(raw_data, dict) else {}
        for name in secret_store.SECRET_FIELDS:
            stored = secret_store.get_secret(name)
            if stored:
                setattr(self.settings, name, stored)
                continue
            legacy = raw.get(name) or ""
            if legacy:
                if secret_store.set_secret(name, legacy):
                    migrated = True
                    logger.info(
                        "Secret « %s » migré du fichier vers le magasin sécurisé.",
                        name)
                # Valeur conservée en mémoire pour la session en cours, que la
                # migration ait réussi ou non.
                setattr(self.settings, name, legacy)
        return migrated

    def save_settings(self, new_settings=None):
        """Sauvegarde les paramètres dans le fichier JSON, de façon **atomique**.

        Si ``new_settings`` est fourni, il devient la configuration courante
        (``self.settings``) le temps de l'écriture. **En cas d'échec d'écriture,
        l'ancien objet en mémoire est restauré** (point 10) : le fichier sur
        disque n'ayant pas été remplacé (``os.replace`` n'a pas eu lieu), mémoire
        et disque restent cohérents. Sans cet argument, on écrit ``self.settings``
        tel quel (usage interne : premier démarrage, migration des secrets).

        Les secrets (``password``, ``app_secret``) ne sont **jamais** écrits en
        clair : ils sont déplacés vers le magasin de secrets du système. On ne
        retombe PAS silencieusement sur un stockage en clair (point 5) :
        - magasin disponible  -> secrets dans le magasin, champs vidés du JSON ;
        - indisponible, **production** -> ``SecretStoreUnavailableError`` (refus) ;
        - indisponible, **développement** -> repli en clair mais AVERTISSEMENT.

        Les erreurs d'écriture NE sont PLUS avalées : elles se propagent à
        l'appelant (éditeur de configuration) pour être remontées à l'interface
        au lieu d'afficher un faux « succès »."""
        previous = self.settings
        if new_settings is not None:
            self.settings = new_settings
        try:
            self._write_settings_file()
        except Exception:
            # Écriture atomique échouée : le fichier n'a pas été remplacé. On
            # restaure l'objet en mémoire précédent pour ne pas laisser la borne
            # avec une configuration qui n'est pas celle réellement persistée.
            self.settings = previous
            raise

    def _write_settings_file(self):
        """Écrit ``self.settings`` dans ``settings.json`` de manière atomique.

        Procédé (point 10) : sérialisation dans un fichier temporaire situé dans
        le **même dossier** (donc le même système de fichiers, condition d'un
        ``os.replace`` atomique), permissions restreintes appliquées **avant** d'y
        écrire d'éventuels secrets, ``flush`` + ``fsync`` pour forcer l'écriture
        physique, copie ``.bak`` de l'ancienne version, puis remplacement atomique.
        Un lecteur ne voit jamais un fichier à moitié écrit : soit l'ancien
        contenu complet, soit le nouveau."""
        config_file = self.config_path / "settings.json"

        data = asdict(self.settings)

        secret_values = {k: data.get(k, "") for k in secret_store.SECRET_FIELDS}
        if secret_store.store_secrets(secret_values):
            # Stockés de façon sécurisée : ne rien laisser en clair dans le JSON.
            for k in secret_store.SECRET_FIELDS:
                data[k] = ""
        elif self.settings.is_production:
            # Production : refuser catégoriquement l'écriture en clair.
            raise secret_store.SecretStoreUnavailableError(
                "Le gestionnaire de secrets du système est indisponible : les "
                "secrets ne peuvent pas être enregistrés de façon sécurisée et "
                "l'écriture en clair est refusée en production. Activez un "
                "magasin de secrets (Gestionnaire d'identifiants Windows, "
                "Trousseau, Secret Service) puis réessayez.")
        else:
            # Développement : repli en clair TOLÉRÉ mais jamais silencieux.
            logger.warning(
                "Secrets de la borne stockés EN CLAIR dans %s (magasin sécurisé "
                "indisponible, mode développement).", config_file)
            # ``data`` conserve les valeurs en clair.

        # Fichier temporaire dans le même dossier. ``mkstemp`` le crée d'emblée
        # en 0600 (lisible/inscriptible par le seul propriétaire) : les
        # permissions restrictives sont donc en place AVANT toute écriture de
        # secret. Déterminant sous Linux (borne), sans effet notable sous Windows.
        fd, tmp_name = tempfile.mkstemp(
            prefix="settings-", suffix=".tmp", dir=str(self.config_path))
        tmp_path = Path(tmp_name)
        try:
            try:
                os.chmod(tmp_path, 0o600)
            except OSError as e:
                logger.warning(
                    "Impossible de restreindre les permissions de %s: %s",
                    tmp_path, e)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            # Copie de sécurité de l'ancienne version avant remplacement. Un
            # échec de cette copie n'empêche pas la sauvegarde elle-même.
            if config_file.exists():
                try:
                    shutil.copy2(config_file, config_file.parent / (config_file.name + ".bak"))
                except OSError as e:
                    logger.warning("Copie de sauvegarde .bak impossible: %s", e)
            # Remplacement atomique : os.replace est atomique sur le même système
            # de fichiers (POSIX et Windows).
            os.replace(tmp_path, config_file)
        except Exception:
            # Le remplacement n'a pas eu lieu : supprimer le fichier temporaire
            # pour ne pas laisser de résidu.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
        # Confirme les permissions finales (redondant après mkstemp+replace,
        # mais sans risque). Un échec de chmod n'invalide pas la sauvegarde.
        try:
            os.chmod(config_file, 0o600)
        except OSError as e:
            logger.warning("Impossible de restreindre les permissions de %s: %s",
                           config_file, e)
