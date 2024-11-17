# config.py
import json
import os
from dataclasses import dataclass, asdict

@dataclass
class Settings:
    """Structure des paramètres de l'application"""
    base_url: str = "http://localhost:5000"
    #window_width: int = 1024
    #window_height: int = 768
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

    @property
    def url(self) -> str:
        return f"{self.base_url}/patient"

class Config:
    """Gestionnaire de configuration"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
            cls._instance.config_file = "settings.json"
            cls._instance.settings = None
            cls._instance.load_settings()
        return cls._instance

    def load_settings(self):
        """Charge les paramètres depuis le fichier JSON"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
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
        try:
            with open(self.config_file, 'w') as f:
                json.dump(asdict(self.settings), f, indent=4)
        except Exception as e:
            print(f"Erreur lors de la sauvegarde des paramètres: {e}")