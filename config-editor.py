# config_editor.py
import tkinter as tk
from tkinter import ttk, messagebox
from config import Config, Settings

class ConfigEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        
        self.title("Configuration File d'attente")
        self.config = Config()
        
        # Configuration de la fenêtre
        self.geometry("600x800")
        self.resizable(True, True)
        
        # Création du formulaire
        self.create_widgets()
        self.load_config()
        
    def create_widgets(self):
        # Frame principal avec scrollbar
        main_frame = ttk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Canvas et scrollbar
        canvas = tk.Canvas(main_frame)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Variables
        self.variables = {}
        
        # Création des champs
        fields = [
            ("Serveur", [
                ("base_url", "URL racine:", str),
            ]),
            ("Fenêtre", [
                ("fullscreen", "Plein écran:", bool),
                ("debug", "Mode debug:", bool),
            ]),
            ("Authentification", [
                ("username", "Nom d'utilisateur:", str),
                ("password", "Mot de passe:", str),
                ("app_secret", "Secret de l'application:", str),
            ]),
            ("Imprimante", [
                ("printer_id_vendor", "ID Vendeur:", str),
                ("printer_id_product", "ID Produit:", str),
                ("printer_model", "Modèle:", str),
            ]),
        ]

        # Création des sections
        row = 0
        for section_title, section_fields in fields:
            # Titre de section
            ttk.Label(scrollable_frame, text=section_title, font=('Helvetica', 10, 'bold')).grid(
                row=row, column=0, columnspan=2, pady=(10, 5), sticky="w"
            )
            row += 1
            
            # Champs de la section
            for field_name, field_label, field_type in section_fields:
                ttk.Label(scrollable_frame, text=field_label).grid(
                    row=row, column=0, padx=5, pady=2, sticky="e"
                )
                
                if field_type == bool:
                    self.variables[field_name] = tk.BooleanVar()
                    widget = ttk.Checkbutton(scrollable_frame, variable=self.variables[field_name])
                else:
                    self.variables[field_name] = tk.StringVar()
                    widget = ttk.Entry(scrollable_frame, textvariable=self.variables[field_name])
                
                widget.grid(row=row, column=1, padx=5, pady=2, sticky="w")
                row += 1

        # Boutons
        button_frame = ttk.Frame(scrollable_frame)
        button_frame.grid(row=row, column=0, columnspan=2, pady=20)
        
        ttk.Button(button_frame, text="Enregistrer", command=self.save_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Annuler", command=self.quit).pack(side=tk.LEFT)

        # Pack final
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def load_config(self):
        """Charge la configuration dans les champs"""
        for field_name, var in self.variables.items():
            value = getattr(self.config.settings, field_name)
            var.set(value)

    def save_config(self):
        """Sauvegarde la configuration"""
        try:
            # Création d'un dictionnaire avec les nouvelles valeurs
            new_values = {}
            for field_name, var in self.variables.items():
                new_values[field_name] = var.get()

            # Mise à jour des paramètres
            self.config.settings = Settings(**new_values)
            self.config.save_settings()
            
            messagebox.showinfo(
                "Succès",
                "Configuration enregistrée avec succès.\nRedémarrez l'application principale pour appliquer les changements."
            )
            self.quit()
        except Exception as e:
            messagebox.showerror("Erreur", f"Erreur lors de la sauvegarde : {str(e)}")

if __name__ == "__main__":
    app = ConfigEditor()
    app.mainloop()