# config.py
import json
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path
import platform

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
    app_secret: str = "votre_secret_app"
    check_paper: bool = True
    # Identifiant de la borne joint aux statuts imprimante. Vide => le hostname
    # de la machine est utilisé par défaut (voir Printer.__init__).
    borne_id: str = ""

    @property
    def url(self) -> str:
        return f"{self.base_url}/patient"

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
            or self.app_secret in ("", "votre_secret_app")
        )

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
        """Charge les paramètres depuis le fichier JSON"""
        config_file = self.config_path / "settings.json"
        
        try:
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Ignore les clés inconnues (ex : options retirées d'une
                    # version antérieure comme websocket_enabled/websocket_debug).
                    # Sans ce filtrage, Settings(**data) lèverait une TypeError et
                    # le repli sur les valeurs par défaut réinitialiserait TOUTE
                    # la configuration d'une borne déjà déployée (URL, identifiants,
                    # imprimante...).
                    known = {f.name for f in fields(Settings)}
                    ignored = set(data) - known
                    if ignored:
                        print(f"Clés de configuration ignorées (inconnues): "
                              f"{', '.join(sorted(ignored))}")
                    filtered = {k: v for k, v in data.items() if k in known}
                    # Crée une nouvelle instance de Settings avec les données du fichier
                    self.settings = Settings(**filtered)
            else:
                # Utilise les valeurs par défaut
                self.settings = Settings()
                # Sauvegarde les valeurs par défaut
                self.save_settings()
        except Exception as e:
            print(f"Erreur lors du chargement des paramètres: {e}")
            self.settings = Settings()

    def save_settings(self):
        """Sauvegarde les paramètres dans le fichier JSON"""
        config_file = self.config_path / "settings.json"

        try:
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.settings), f, indent=4)
            # Le fichier contient mot de passe + secret d'application : on
            # restreint les permissions au seul propriétaire (lecture/écriture).
            # Sans effet notable sous Windows, déterminant sous Linux (borne).
            try:
                os.chmod(config_file, 0o600)
            except OSError as e:
                print(f"Impossible de restreindre les permissions de {config_file}: {e}")
        except Exception as e:
            print(f"Erreur lors de la sauvegarde des paramètres: {e}")