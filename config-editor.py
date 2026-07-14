# config_editor.py
import logging
import threading
import tkinter as tk
from dataclasses import fields
from tkinter import ttk, messagebox
import logging_config
import editor_logic
from config import Config, Settings

logger = logging.getLogger("borne.editor")

# Couleurs des messages de mise en évidence (mode debug, identifiants par
# défaut) : orange = avertissement, rouge = refus à l'enregistrement.
_WARN_COLOR = "#b35900"
_DANGER_COLOR = "#b00020"

# Timeout (connexion, lecture) du test de joignabilité du serveur.
_TEST_TIMEOUT = (5, 10)


class ConfigEditor(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Configuration File d'attente")
        self.config = Config()

        # Configuration de la fenêtre
        self.geometry("600x800")
        self.resizable(True, True)

        # Instantané des valeurs chargées, pour détecter les modifications non
        # enregistrées (avertissement avant fermeture).
        self._loaded_values = {}

        # Création du formulaire
        self.create_widgets()
        self.load_config()

        # Avertir avant de fermer la fenêtre si des changements ne sont pas
        # enregistrés (croix de la fenêtre + bouton « Annuler »).
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

        # Champs sensibles : masqués par défaut (points), avec une case
        # « Afficher » pour les révéler ponctuellement.
        secret_fields = {"password", "app_secret"}

        # Création des champs
        fields = [
            ("Serveur", [
                ("base_url", "URL racine:", str),
            ]),
            ("Borne", [
                ("borne_id", "Identifiant de la borne:", str),
            ]),
            ("Fenêtre", [
                ("fullscreen", "Plein écran:", bool),
                ("debug", "Mode debug:", bool),
                ("hide_cursor", "Masquer le curseur (mode kiosque):", bool),
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
                ("check_paper", "Vérifier le papier avant les impressions:", bool),
            ]),
        ]

        # Création des sections
        row = 0

        # Bandeau : toute la configuration de la borne est lue au démarrage et
        # n'est jamais rechargée à chaud. Les modifications enregistrées ici ne
        # prennent donc effet qu'après un redémarrage de l'application borne.
        tk.Label(
            scrollable_frame,
            text=("ℹ Les modifications ne prennent effet qu'après redémarrage "
                  "de la borne (aucun rechargement à chaud)."),
            fg="#0b5394", wraplength=540, justify="left",
        ).grid(row=row, column=0, columnspan=3, padx=5, pady=(0, 8), sticky="w")
        row += 1

        # Mise en évidence dynamique du mode debug et des identifiants par
        # défaut (mis à jour à chaque modification des champs concernés). Masqué
        # tant qu'il n'y a rien à signaler.
        self._security_label = tk.Label(
            scrollable_frame, text="", fg=_WARN_COLOR, wraplength=540,
            justify="left", font=('Helvetica', 9, 'bold'))
        self._security_label.grid(row=row, column=0, columnspan=3, padx=5,
                                  pady=(0, 8), sticky="w")
        self._security_label.grid_remove()
        row += 1

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
                    widget.grid(row=row, column=1, padx=5, pady=2, sticky="w")
                elif field_name in secret_fields:
                    self.variables[field_name] = tk.StringVar()
                    entry = ttk.Entry(scrollable_frame, textvariable=self.variables[field_name], show="*")
                    entry.grid(row=row, column=1, padx=5, pady=2, sticky="w")
                    # Case « Afficher » : révèle/masque la valeur de CE champ.
                    reveal_var = tk.BooleanVar(value=False)
                    def _toggle(e=entry, v=reveal_var):
                        e.config(show="" if v.get() else "*")
                    ttk.Checkbutton(scrollable_frame, text="Afficher",
                                    variable=reveal_var, command=_toggle).grid(
                        row=row, column=2, padx=5, pady=2, sticky="w")
                else:
                    self.variables[field_name] = tk.StringVar()
                    widget = ttk.Entry(scrollable_frame, textvariable=self.variables[field_name])
                    widget.grid(row=row, column=1, padx=5, pady=2, sticky="w")

                row += 1

        # Met en évidence le mode debug et les identifiants par défaut dès que
        # l'un des champs concernés change (et une première fois au chargement).
        for name in ("debug", "username", "password", "app_secret"):
            self.variables[name].trace_add(
                "write", lambda *a: self._refresh_security_highlights())

        # Boutons
        button_frame = ttk.Frame(scrollable_frame)
        button_frame.grid(row=row, column=0, columnspan=3, pady=20)

        # Tests de diagnostic (n'enregistrent rien) : ils valident et éprouvent
        # la configuration SAISIE (non encore sauvegardée).
        self._server_button = ttk.Button(button_frame, text="Tester le serveur",
                                          command=self.test_server)
        self._server_button.pack(side=tk.LEFT, padx=5)
        self._printer_button = ttk.Button(button_frame, text="Tester l'imprimante",
                                           command=self.test_printer)
        self._printer_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Restaurer les valeurs par défaut",
                   command=self.restore_defaults).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Enregistrer", command=self.save_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Annuler", command=self._on_close).pack(side=tk.LEFT)

        # Pack final
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def load_config(self):
        """Charge la configuration dans les champs et mémorise l'état de départ
        (pour détecter les modifications non enregistrées)."""
        for field_name, var in self.variables.items():
            value = getattr(self.config.settings, field_name)
            var.set(value)
        self._loaded_values = self._current_form_values()
        self._refresh_security_highlights()

    def _current_form_values(self) -> dict:
        """Valeurs courantes du formulaire (par nom de champ)."""
        return {name: var.get() for name, var in self.variables.items()}

    def _is_dirty(self) -> bool:
        """Vrai si le formulaire diffère de l'état chargé/enregistré."""
        return editor_logic.values_differ(self._loaded_values,
                                          self._current_form_values())

    def _settings_from_form(self):
        """Construit un objet Settings à partir des champs SAISIS (sans
        sauvegarder). Tout champ non exposé dans l'éditeur garde sa valeur
        courante (robuste à l'ajout futur de champs au dataclass)."""
        new_values = self._current_form_values()
        known = {f.name for f in fields(Settings)}
        base = {f: getattr(self.config.settings, f)
                for f in known if f not in new_values
                and hasattr(self.config.settings, f)}
        base.update(new_values)
        return Settings(**base)

    # ------------------------------------------------------------------
    # Mise en évidence / fermeture / restauration
    # ------------------------------------------------------------------
    def _refresh_security_highlights(self):
        """Affiche/masque le bandeau mettant en évidence le mode debug et les
        identifiants par défaut, en fonction de la saisie courante."""
        # Le label n'existe pas encore pendant la construction initiale.
        if not hasattr(self, "_security_label"):
            return
        try:
            settings = self._settings_from_form()
        except Exception:
            return

        msgs = []
        color = _WARN_COLOR
        if settings.debug:
            msgs.append("Mode debug ACTIVÉ (développement) — à désactiver "
                        "avant un déploiement en production.")
        cred_warning = editor_logic.default_credentials_warning(settings)
        if cred_warning:
            msgs.append(cred_warning)
            if settings.is_production:
                # Refus à l'enregistrement : signalé en rouge.
                color = _DANGER_COLOR

        if msgs:
            self._security_label.config(text="⚠ " + "\n⚠ ".join(msgs), fg=color)
            self._security_label.grid()
        else:
            self._security_label.config(text="")
            self._security_label.grid_remove()

    def restore_defaults(self):
        """Remplit le formulaire avec les valeurs par défaut (après
        confirmation). N'enregistre rien : l'utilisateur doit toujours cliquer
        sur « Enregistrer » pour appliquer."""
        if not messagebox.askyesno(
                "Restaurer les valeurs par défaut",
                "Remplacer tous les champs par les valeurs par défaut ?\n\n"
                "Les modifications non enregistrées seront perdues. "
                "L'enregistrement reste nécessaire pour appliquer les "
                "changements."):
            return
        defaults = Settings()
        for name, var in self.variables.items():
            var.set(getattr(defaults, name))
        self._refresh_security_highlights()

    def _on_close(self):
        """Ferme la fenêtre en prévenant si des modifications ne sont pas
        enregistrées."""
        if self._is_dirty():
            if not messagebox.askyesno(
                    "Modifications non enregistrées",
                    "Des modifications n'ont pas été enregistrées.\n\n"
                    "Quitter sans enregistrer ?"):
                return
        self.destroy()

    def save_config(self):
        """Valide puis sauvegarde la configuration.

        - Refuse d'enregistrer une configuration invalide (validation stricte).
        - Remonte à l'interface toute erreur de sauvegarde (plus de faux
          « succès » : save_settings ne ravale plus les exceptions)."""
        try:
            settings = self._settings_from_form()
        except Exception as e:
            messagebox.showerror("Erreur", f"Valeurs invalides : {e}")
            return

        errors = settings.validate()
        if errors:
            messagebox.showerror(
                "Configuration invalide",
                "Corrigez les points suivants avant d'enregistrer :\n\n- "
                + "\n- ".join(errors))
            return

        # Garde-fou sécurité : refuse les identifiants/secret par défaut
        # (admin/admin) hors mode développement explicitement activé (case
        # « Mode debug »), en cohérence avec le refus de démarrage (main.py).
        cred_error = editor_logic.default_credentials_error(settings)
        if cred_error:
            messagebox.showerror("Identifiants par défaut refusés", cred_error)
            return

        try:
            # save_settings applique la nouvelle configuration de façon atomique
            # et restaure l'ancienne en mémoire si l'écriture échoue (point 10).
            self.config.save_settings(settings)
        except Exception as e:
            messagebox.showerror("Erreur", f"Erreur lors de la sauvegarde : {e}")
            return

        # État enregistré : plus rien à signaler comme « non enregistré ».
        self._loaded_values = self._current_form_values()
        messagebox.showinfo(
            "Succès",
            "Configuration enregistrée avec succès.\nRedémarrez l'application principale pour appliquer les changements."
        )
        self.destroy()

    # ------------------------------------------------------------------
    # Tests de diagnostic
    # ------------------------------------------------------------------
    def _run_test(self, button, busy_text, idle_text, worker):
        """Exécute ``worker`` (renvoyant (ok: bool, message: str)) dans un thread
        pour ne pas figer l'interface, puis affiche le résultat. Le bouton est
        désactivé pendant le test."""
        button.config(state="disabled", text=busy_text)

        def task():
            try:
                ok, message = worker()
            except Exception as e:
                ok, message = False, f"Erreur inattendue : {e}"

            def finish():
                button.config(state="normal", text=idle_text)
                if ok:
                    messagebox.showinfo("Résultat du test", message)
                else:
                    messagebox.showerror("Résultat du test", message)

            # Retour sur le thread principal Tk pour manipuler l'interface.
            self.after(0, finish)

        threading.Thread(target=task, daemon=True).start()

    def test_server(self):
        """Teste la joignabilité du serveur ET la validité du secret
        d'application (obtention d'un token) avec l'URL/secret saisis."""
        try:
            settings = self._settings_from_form()
        except Exception as e:
            messagebox.showerror("Erreur", f"Valeurs invalides : {e}")
            return

        url_errors = settings.base_url_errors()
        if url_errors:
            messagebox.showerror("URL invalide", "\n".join(url_errors))
            return
        if not settings.app_secret.strip():
            messagebox.showerror(
                "Secret manquant",
                "Renseignez le secret d'application pour tester le serveur.")
            return

        base_url = settings.normalized_base_url()
        app_secret = settings.app_secret
        self._run_test(self._server_button, "Test du serveur…", "Tester le serveur",
                       lambda: self._probe_server(base_url, app_secret))

    def _probe_server(self, base_url, app_secret):
        import requests
        try:
            response = requests.post(
                f"{base_url}/api/get_app_token",
                data={'app_secret': app_secret},
                timeout=_TEST_TIMEOUT,
            )
        except Exception as e:
            return False, f"Serveur injoignable :\n{e}"

        if response.status_code == 200:
            try:
                token = response.json().get('token')
            except ValueError:
                token = None
            if token:
                return True, (f"Serveur joignable ({base_url}) et secret "
                              "d'application valide.")
            return False, "Serveur joignable mais réponse inattendue (aucun token)."
        if response.status_code in (401, 403):
            return False, (f"Serveur joignable mais secret d'application refusé "
                           f"(HTTP {response.status_code}).")
        return False, (f"Serveur joignable mais réponse inattendue "
                       f"(HTTP {response.status_code}).")

    def test_printer(self):
        """Teste l'ouverture de l'imprimante USB avec les identifiants/modèle
        saisis (ne fait qu'ouvrir et refermer le périphérique, sans imprimer)."""
        try:
            settings = self._settings_from_form()
        except Exception as e:
            messagebox.showerror("Erreur", f"Valeurs invalides : {e}")
            return

        errors = (settings.usb_id_errors("printer_id_vendor", settings.printer_id_vendor)
                  + settings.usb_id_errors("printer_id_product", settings.printer_id_product))
        if not settings.printer_model.strip():
            errors.append("Le modèle d'imprimante ne peut pas être vide.")
        if errors:
            messagebox.showerror("Imprimante invalide", "\n".join(errors))
            return

        id_vendor = settings.printer_id_vendor
        id_product = settings.printer_id_product
        model = settings.printer_model
        self._run_test(self._printer_button, "Test de l'imprimante…", "Tester l'imprimante",
                       lambda: self._probe_printer(id_vendor, id_product, model))

    def _probe_printer(self, id_vendor, id_product, model):
        try:
            from escpos.printer import Usb
        except Exception as e:
            return False, ("Module d'impression (python-escpos) indisponible dans "
                           f"cet éditeur :\n{e}")
        try:
            printer = Usb(int(id_vendor, 16), int(id_product, 16), profile=model)
        except Exception as e:
            return False, (f"Imprimante non disponible :\n{e}\n\n"
                           "Vérifiez qu'elle est branchée, sous tension, et que "
                           "l'application principale ne l'utilise pas déjà.")
        try:
            printer.close()
        except Exception:
            pass
        return True, f"Imprimante détectée et ouverte avec succès (modèle {model})."


if __name__ == "__main__":
    logging_config.setup_logging()
    logger.info("Ouverture de l'éditeur de configuration.")
    app = ConfigEditor()
    app.mainloop()
