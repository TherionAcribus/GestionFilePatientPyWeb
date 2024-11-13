# config.py
class Config:
    """Configuration centralisée de l'application"""
    HOST = "localhost"
    PORT = 5000
    URL = f"http://{HOST}:{PORT}/patient"
    BASE_URL = f"http://{HOST}:{PORT}"
    WINDOW_TITLE = "File d'attente"
    WINDOW_WIDTH = 1024
    WINDOW_HEIGHT = 768
    FULLSCREEN = False  # À mettre à True pour la production sur Raspberry
    DEBUG = True

    # Credentials
    USERNAME = "admin"
    PASSWORD = "admin"

    PRINTER_ID_VENDOR = "0x04b8"  # Exemple, à adapter
    PRINTER_ID_PRODUCT = "0x0202"  # Exemple, à adapter
    PRINTER_MODEL = "TM-T88II"  # Exemple, à adapter

    # Application Secret
    APP_SECRET = "votre_secret_app"