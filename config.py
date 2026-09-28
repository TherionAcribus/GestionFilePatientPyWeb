# config.py
import contextlib
import json
import logging
import os
import platform
import posixpath
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import secret_store

logger = logging.getLogger("borne.config")

# --- Secret applicatif : AUCUNE valeur par défaut dans le code ---------------
# La borne ne s'authentifie PLUS que par son identité machine (``app_secret``
# -> jeton applicatif -> ticket de session patient, cf. main.py) : les champs
# ``username``/``password`` du compte technique ont disparu. Une borne non
# configurée refuse donc de démarrer (``validate()`` signale le secret vide)
# au lieu de tourner avec un accès trivial hérité du dépôt.
#
# La liste ci-dessous n'est PAS une valeur par défaut : c'est une DENYLIST
# servant à refuser une borne encore configurée avec les valeurs d'exemple
# historiques (installations existantes, tutoriels, copies de settings.json).
# Elle ne contient que des valeurs publiquement connues, donc sans valeur de
# secret.
INSECURE_APP_SECRETS = frozenset({"", "votre_secret_app", "changeme",
                                  "secret", "your_app_secret"})

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


def _normalized_url_path(path: str) -> str:
    """Chemin URL normalisé pour les comparaisons de préfixe.

    ``posixpath.normpath`` retire les segments ``.``/``..`` qui permettraient
    sinon d'échapper au préfixe du serveur (``/app/../admin``). ``unquote``
    couvre la variante encodée (``%2e%2e``, ``%2f``)."""
    try:
        decoded = unquote(path or "/")
    except Exception:
        decoded = path or "/"
    normalized = posixpath.normpath(decoded)
    if normalized == ".":
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _effective_port(parsed) -> int | None:
    """Port effectif d'une URL (port explicite ou port standard du schéma)."""
    try:
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        return None
    if parsed.scheme in ("https", "wss"):
        return 443
    if parsed.scheme in ("http", "ws"):
        return 80
    return None


def url_is_within_base(url: str, base_url: str) -> bool:
    """Vrai si ``url`` reste sous l'origine et le préfixe de ``base_url``.

    Utilisé à la fois pour la navigation WebView et pour les URL renvoyées par
    le serveur. Une comparaison naïve de chaîne accepterait des hôtes ambigus
    (``https://serveur@evil/``) ou des préfixes proches (``/app`` vs
    ``/application``) ; on compare donc schéma, hôte, port effectif et chemin
    normalisé. WebSocket est assimilé à son schéma HTTP correspondant."""
    try:
        candidate = urlparse(url)
        base = urlparse(base_url)
        if not candidate.scheme or not base.scheme:
            return False
        candidate_scheme = candidate.scheme.lower()
        base_scheme = base.scheme.lower()
        allowed_schemes = {base_scheme}
        if base_scheme == "http":
            allowed_schemes.add("ws")
        elif base_scheme == "https":
            allowed_schemes.add("wss")
        if candidate_scheme not in allowed_schemes:
            return False
        if candidate.username is not None or candidate.password is not None:
            return False
        if (candidate.hostname or "").lower() != (base.hostname or "").lower():
            return False
        if _effective_port(candidate) != _effective_port(base):
            return False
    except (TypeError, ValueError):
        return False

    base_path = _normalized_url_path(base.path)
    candidate_path = _normalized_url_path(candidate.path)
    if base_path == "/":
        return True
    return candidate_path == base_path or candidate_path.startswith(base_path + "/")


def resolve_server_url(base_url: str, url: str) -> str:
    """Résout une URL renvoyée par le serveur contre ``base_url``.

    Les chemins relatifs sont rattachés au préfixe configuré ; les URL absolues
    ne sont acceptées que si elles restent dans le même périmètre. Toute autre
    valeur (domaine externe, ``javascript:``, ``file:``, référence
    protocole-relative ``//host``) est refusée par ``ValueError``."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL de connexion vide ou invalide")
    candidate = url.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError as e:
        raise ValueError("URL de connexion invalide") from e

    if parsed.scheme or parsed.netloc:
        if url_is_within_base(candidate, base_url):
            return candidate
        raise ValueError("URL de connexion hors du domaine applicatif autorisé")
    if candidate.startswith("//"):
        raise ValueError("URL de connexion externe refusée")

    resolved = f"{base_url.rstrip('/')}/{candidate.lstrip('/')}"
    if not url_is_within_base(resolved, base_url):
        raise ValueError("URL de connexion hors du domaine applicatif autorisé")
    return resolved


def redact_url_for_log(url: str) -> str:
    """Version journalisable d'une URL : hôte et chemin utiles, jamais de
    ticket, de requête ni de fragment.

    Les tickets de session sont portés par ``/patient/kiosk_login/<ticket>`` ;
    ils sont remplacés par ``***``. Les paramètres de requête sont toujours
    omis car ils peuvent contenir des jetons."""
    if not isinstance(url, str) or not url:
        return "<aucune URL>"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<URL invalide>"

    scheme = parsed.scheme.lower()
    path = parsed.path or ""
    marker = "/kiosk_login/"
    if marker in path:
        prefix = path.split(marker, 1)[0]
        path = f"{prefix}{marker}***"
    elif path.endswith("/kiosk_login"):
        path = f"{path}/***"

    if scheme in ("http", "https", "ws", "wss"):
        return urlunparse((scheme, parsed.netloc, path, "", "", ""))
    if scheme == "about":
        return f"about:{path or 'blank'}"
    if scheme:
        return f"{scheme}:***"
    return "<URL non absolue>"


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
    # Secret applicatif de la borne : VIDE par défaut (aucun secret livré dans
    # le code). À renseigner via config-editor.py ; la validation refuse le
    # démarrage tant qu'il ne l'est pas.
    printer_id_vendor: str = "0x04b8"
    printer_id_product: str = "0x0202"
    printer_model: str = "TM-T88II"
    app_secret: str = ""
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

    def insecure_credentials_reasons(self) -> list:
        """Liste des secrets triviaux détectés (vide = rien à signaler).

        Compare la configuration à la DENYLIST de valeurs publiquement connues
        (valeurs d'exemple, secrets usuels) plutôt qu'aux « valeurs par défaut
        du code », qui n'existent pas. La comparaison est insensible à la casse
        et aux espaces de bordure."""
        def _norm(value):
            # Les valeurs de mauvais type sont signalées par validate() ; ici on
            # les ramène à la chaîne vide (elle-même dans la denylist).
            return value.strip().lower() if isinstance(value, str) else ""

        reasons = []
        app_secret = _norm(self.app_secret)

        if app_secret in INSECURE_APP_SECRETS:
            reasons.append(
                "Secret d'application vide ou repris de l'exemple de "
                "configuration.")
        return reasons

    def has_insecure_default_credentials(self) -> bool:
        """Vrai si le secret d'application est trivial (vide ou repris de
        l'exemple). À refuser en production (cf. main.py) pour ne pas exposer
        une borne avec un accès trivial."""
        return bool(self.insecure_credentials_reasons())

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
        except ValueError:
            return [f"L'URL du serveur est invalide : {self.base_url!r}."]
        if parsed.scheme not in ("http", "https"):
            return ["L'URL du serveur doit commencer par http:// ou https://."]
        if not parsed.hostname:
            return ["L'URL du serveur ne contient pas de nom d'hôte valide."]
        try:
            # L'ACCÈS à .port déclenche l'analyse du port : c'est lui qui lève
            # ValueError si la valeur n'est pas numérique.
            _ = parsed.port
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
            "base_url", "printer_id_vendor",
            "printer_id_product", "printer_model", "app_secret", "borne_id",
        ):
            if not isinstance(getattr(self, name), str):
                errors.append(
                    f"Le champ « {name} » doit être une chaîne de caractères."
                )

        # URL du serveur.
        errors.extend(self.base_url_errors())

        # Authentification borne : l'identité machine seule (secret applicatif).
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
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Initialise les attributs de l'instance sans laisser une exception de
        chemin rendre l'objet partiellement utilisable.

        Si le répertoire applicatif ne peut pas être déterminé ou créé, on
        conserve des valeurs par défaut en mémoire et on remplit ``load_error`` :
        la borne refusera de démarrer proprement au lieu de planter avant même
        l'affichage de l'écran de diagnostic."""
        self.app_name = "FileAttente"
        self.settings = Settings()
        self.load_error = None
        self.secret_store_error = None
        try:
            self.config_path = self._get_config_path()
            self._ensure_config_dir()
        except Exception as e:
            logger.exception("Répertoire de configuration indisponible.")
            self.config_path = Path(tempfile.gettempdir()) / self.app_name
            with contextlib.suppress(Exception):
                self._ensure_config_dir()
            self.load_error = (
                "Répertoire de configuration indisponible : "
                f"{e}")
            return
        self.load_settings()

    def _get_config_path(self) -> Path:
        """Détermine le chemin de configuration selon le système d'exploitation"""
        system = platform.system()

        if system == "Windows":
            # LOCALAPPDATA peut être absent dans certains contextes de service ;
            # retomber alors explicitement sur le profil utilisateur.
            local_appdata = os.environ.get(
                "LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            base_path = os.path.join(local_appdata, self.app_name)
        elif system == "Linux":
            # Sur Linux, utilise ~/.config
            base_path = os.path.join(str(Path.home()), ".config", self.app_name)
        else:
            raise OSError(f"Système d'exploitation non supporté: {system}")

        return Path(base_path)

    def _ensure_config_dir(self):
        """Crée le répertoire de configuration s'il n'existe pas"""
        self.config_path.mkdir(parents=True, exist_ok=True)

    def webview_storage_path(self) -> str:
        """Répertoire persistant du profil WebView (cookies et localStorage)."""
        storage_path = self.config_path / "webview"
        storage_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(storage_path, 0o700)
        except OSError as e:
            logger.warning(
                "Impossible de restreindre les permissions de %s: %s",
                storage_path, e)
        return str(storage_path)

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
            except Exception:
                # Non fatal : les valeurs par défaut restent en mémoire. Trace
                # complète, c'est le seul indice si le poste refuse l'écriture.
                logger.exception("Impossible d'écrire la configuration par défaut.")
            return

        try:
            with open(config_file, encoding='utf-8') as f:
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
            # Le secret (app_secret) provient désormais du magasin sécurisé du
            # système ; on migre au besoin une valeur héritée en clair puis on
            # réécrit le fichier sans elle.
            if self._apply_secret_store(data):
                try:
                    self.save_settings()
                except Exception:
                    logger.exception(
                        "Réécriture après migration des secrets impossible.")
        except Exception as e:
            # Config illisible : on NE bascule PAS en douce sur les défauts. On
            # signale l'erreur (main.py refusera de démarrer) tout en gardant un
            # objet utilisable.
            logger.exception("Erreur lors du chargement des paramètres.")
            self.load_error = f"Fichier de configuration illisible : {e}"
            self.settings = Settings()

    def _apply_secret_store(self, raw_data) -> bool:
        """Renseigne les secrets de ``self.settings`` depuis le magasin sécurisé.

        - Si une valeur est présente dans le magasin, elle prime.
        - Sinon, une valeur héritée en clair dans ``raw_data`` (fichier JSON) est
          migrée vers le magasin lorsque c'est possible.
        - Toute copie en clair obsolète (``app_secret`` ou ancien ``password``)
          doit déclencher une réécriture filtrée du fichier, même si le magasin
          contient déjà la valeur retenue.

        Renvoie ``True`` si le fichier doit être réécrit. Ne journalise jamais
        de valeur. Si la migration d'un secret en clair échoue, la valeur reste
        disponible pour l'éditeur mais ``secret_store_error`` est rempli afin
        que la borne refuse de démarrer en production."""
        self.secret_store_error = None
        raw = raw_data if isinstance(raw_data, dict) else {}
        needs_rewrite = bool(raw.get("password"))
        for name in secret_store.SECRET_FIELDS:
            stored = secret_store.get_secret(name)
            legacy = raw.get(name) or ""
            if stored:
                setattr(self.settings, name, stored)
                if legacy:
                    needs_rewrite = True
                    logger.info(
                        "Copie en clair de « %s » supprimée au profit du "
                        "magasin sécurisé.", name)
                continue
            if legacy:
                if secret_store.set_secret(name, legacy):
                    logger.info(
                        "Secret « %s » migré du fichier vers le magasin sécurisé.",
                        name)
                else:
                    self.secret_store_error = (
                        "Le secret d'application est présent en clair dans "
                        "settings.json et n'a pas pu être déplacé vers le "
                        "magasin sécurisé du système.")
                    logger.warning(
                        "Secret « %s » en clair non migrable : magasin "
                        "sécurisé indisponible ou écriture refusée.", name)
                # Réécriture nécessaire dans tous les cas : elle filtrera aussi
                # les anciennes clés héritées lorsqu'elle est possible.
                needs_rewrite = True
                # Valeur conservée en mémoire pour la session en cours, que la
                # migration ait réussi ou non.
                setattr(self.settings, name, legacy)
        return needs_rewrite

    def save_settings(self, new_settings=None):
        """Sauvegarde les paramètres dans le fichier JSON, de façon **atomique**.

        Si ``new_settings`` est fourni, il devient la configuration courante
        (``self.settings``) le temps de l'écriture. **En cas d'échec d'écriture,
        l'ancien objet en mémoire est restauré** (point 10) : le fichier sur
        disque n'ayant pas été remplacé (``os.replace`` n'a pas eu lieu), mémoire
        et disque restent cohérents. Sans cet argument, on écrit ``self.settings``
        tel quel (usage interne : premier démarrage, migration des secrets).

        Le secret (``app_secret``) n'est **jamais** écrit en
        clair : il est déplacé vers le magasin de secrets du système. On ne
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
