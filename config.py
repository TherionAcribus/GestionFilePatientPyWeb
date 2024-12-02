# config.py
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
import platform

@dataclass
class Settings:
    """Structure des paramètres de l'application"""
    base_url: str = "http://localhost:5000"
    fullscreen: bool = False
    debug: bool = True
    username: str = "admin"
    password: str = "admin"
    printer_id_vendor: str = "0x04b8"
    printer_id_product: str = "0x0202"
    printer_model: str = "TM-T88II"
    app_secret: str = "votre_secret_app"
    websocket_enabled: bool = False  
    websocket_debug: bool = False
    check_paper: bool = True  

    @property
    def url(self) -> str:
        return f"{self.base_url}/patient"

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
                    # Crée une nouvelle instance de Settings avec les données du fichier
                    self.settings = Settings(**data)
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
        except Exception as e:
            print(f"Erreur lors de la sauvegarde des paramètres: {e}")