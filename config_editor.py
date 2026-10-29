# config_editor.py
import contextlib
import logging
import queue
import threading
import time
import tkinter as tk
from dataclasses import fields
from tkinter import messagebox, ttk

import desktop_integration
import editor_logic
import logging_config
import maintenance_pin
from config import Config, Settings, resolve_server_url
from maintenance_pin import MAINTENANCE_KEY

logger = logging.getLogger("borne.editor")

# Couleurs des messages de mise en évidence (mode debug, identifiants par
# défaut) : orange = avertissement, rouge = refus à l'enregistrement.
_WARN_COLOR = "#b35900"
_DANGER_COLOR = "#b00020"

# Timeout (connexion, lecture) du test de joignabilité du serveur.
_TEST_TIMEOUT = (5, 10)

# Fenêtre du code PIN : nombre d'essais, puis abandon (la borne repart).
PIN_MAX_ATTEMPTS = 3
# Sans saisie pendant ce délai, la fenêtre du PIN se ferme : une touche de
# maintenance pressée par erreur ne laisse pas la borne hors service.
PIN_DIALOG_TIMEOUT_S = 60
# Même garde pour la fenêtre de choix de maintenance : sans réponse, la
# borne reprend son service d'elle-même.
ACTION_DIALOG_TIMEOUT_S = 60

# Période de vérification de l'inactivité de l'éditeur.
_IDLE_CHECK_MS = 5000


class ConfigEditor(tk.Tk):
    def __init__(self, kiosk_follows=False, idle_timeout_s=None):
        """``kiosk_follows`` : la borne démarre (ou redémarre) d'elle-même à
        la fermeture de l'éditeur — ouverture automatique au démarrage ou
        touche de maintenance. Les textes l'indiquent au lieu de demander un
        redémarrage manuel.

        ``idle_timeout_s`` : si renseigné, l'éditeur se ferme sans
        enregistrer après ce délai sans clavier ni souris. Utilisé par la
        touche de maintenance, pour qu'une borne en service ne reste pas
        bloquée sur la configuration si on l'oublie ouverte."""
        super().__init__()

        self.title("Configuration — Flowelis Borne")
        self.config = Config()
        self._kiosk_follows = kiosk_follows

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

        # La borne vient de quitter le plein écran : l'éditeur doit passer
        # devant, clavier prêt.
        _bring_to_front(self)

        if idle_timeout_s:
            self._idle_timeout_s = idle_timeout_s
            self._last_activity = time.monotonic()
            for sequence in ("<KeyPress>", "<ButtonPress>", "<Motion>",
                             "<MouseWheel>"):
                self.bind_all(sequence, self._note_activity, add="+")
            self.after(_IDLE_CHECK_MS, self._check_idle)

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
        secret_fields = {"app_secret"}

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
        # prennent donc effet qu'après un redémarrage de l'application borne
        # — automatique quand la borne attend la fermeture de l'éditeur.
        if self._kiosk_follows:
            banner = ("ℹ La borne démarrera avec cette configuration à la "
                      "fermeture de cette fenêtre.")
        else:
            banner = ("ℹ Les modifications ne prennent effet qu'après "
                      "redémarrage de la borne (aucun rechargement à chaud).")
        tk.Label(
            scrollable_frame,
            text=banner,
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

                if field_type is bool:
                    self.variables[field_name] = tk.BooleanVar()
                    widget = ttk.Checkbutton(scrollable_frame, variable=self.variables[field_name])
                    widget.grid(row=row, column=1, padx=5, pady=2, sticky="w")
                elif field_name in secret_fields:
                    self.variables[field_name] = tk.StringVar()
                    entry = ttk.Entry(scrollable_frame, show="*",
                                      textvariable=self.variables[field_name])
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

        row = self._create_maintenance_section(scrollable_frame, row)

        # Met en évidence le mode debug et le secret par défaut dès que l'un
        # des champs concernés change (et une première fois au chargement).
        for name in ("debug", "app_secret"):
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
        ttk.Button(button_frame, text="Enregistrer",
                   command=self.save_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Annuler", command=self._on_close).pack(side=tk.LEFT)
        row += 1

        # Journal des diagnostics : chaque étape des tests serveur/imprimante y
        # apparaît horodatée, en temps réel (les workers postent via after()).
        # Sans lui, l'utilisateur ne saurait pas si le test tourne encore.
        log_frame = ttk.LabelFrame(scrollable_frame, text="Journal des tests")
        log_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        self._test_log = tk.Text(log_frame, height=7, state="disabled",
                                 wrap="word", relief="flat")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self._test_log.yview)
        self._test_log.configure(yscrollcommand=log_scroll.set)
        self._test_log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        row += 1

        # Pack final
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def destroy(self):
        _cancel_pending_callbacks(self)
        super().destroy()

    def _create_maintenance_section(self, parent, row):
        """Section « Maintenance » : touche d'accès et code PIN optionnel.

        Le PIN n'est jamais réaffiché : on n'indique que sa présence. Les
        champs de saisie ne sont pas des champs de ``Settings`` (seule
        l'empreinte l'est) et restent hors de ``self.variables``."""
        ttk.Label(parent, text="Maintenance", font=('Helvetica', 10, 'bold')).grid(
            row=row, column=0, columnspan=2, pady=(10, 5), sticky="w")
        row += 1
        tk.Label(
            parent,
            text=(f"Touche {MAINTENANCE_KEY} sur un clavier branché à la "
                  "borne : propose d'ouvrir cette configuration ou de "
                  "quitter l'application. Code PIN facultatif (chiffres "
                  f"uniquement, {maintenance_pin.PIN_MIN_LENGTH} à "
                  f"{maintenance_pin.PIN_MAX_LENGTH}) ; laissez les champs "
                  "vides pour ne rien changer."),
            wraplength=540, justify="left",
        ).grid(row=row, column=0, columnspan=3, padx=5, pady=(0, 4), sticky="w")
        row += 1

        ttk.Label(parent, text="Code PIN actuel:").grid(
            row=row, column=0, padx=5, pady=2, sticky="e")
        self._pin_status_label = ttk.Label(parent, text="")
        self._pin_status_label.grid(row=row, column=1, columnspan=2, padx=5,
                                    pady=2, sticky="w")
        row += 1

        self._pin_new = tk.StringVar()
        self._pin_confirm = tk.StringVar()
        self._pin_remove = tk.BooleanVar(value=False)
        for var, label in ((self._pin_new, "Nouveau code PIN:"),
                           (self._pin_confirm, "Confirmer le code PIN:")):
            ttk.Label(parent, text=label).grid(
                row=row, column=0, padx=5, pady=2, sticky="e")
            ttk.Entry(parent, show="*", textvariable=var).grid(
                row=row, column=1, padx=5, pady=2, sticky="w")
            row += 1

        self._pin_remove_check = ttk.Checkbutton(
            parent, text="Supprimer le code PIN actuel",
            variable=self._pin_remove)
        self._pin_remove_check.grid(row=row, column=1, columnspan=2, padx=5,
                                    pady=2, sticky="w")
        row += 1
        return row

    def _refresh_pin_section(self):
        """Vide les saisies du PIN et affiche l'état du PIN enregistré."""
        self._pin_new.set("")
        self._pin_confirm.set("")
        self._pin_remove.set(False)
        stored = self.config.settings.maintenance_pin_hash
        if maintenance_pin.is_valid_hash(stored):
            status = "défini"
        elif stored:
            status = "illisible — à redéfinir ou à supprimer"
        else:
            status = (f"aucun ({MAINTENANCE_KEY} ouvre directement la "
                      "maintenance)")
        self._pin_status_label.config(text=status)
        self._pin_remove_check.config(state="normal" if stored else "disabled")

    def _pin_change_pending(self) -> bool:
        """Vrai si une modification du PIN est saisie (non enregistrée)."""
        return bool(self._pin_new.get() or self._pin_confirm.get()
                    or self._pin_remove.get())

    def load_config(self):
        """Charge la configuration dans les champs et mémorise l'état de départ
        (pour détecter les modifications non enregistrées)."""
        for field_name, var in self.variables.items():
            value = getattr(self.config.settings, field_name)
            var.set(value)
        self._loaded_values = self._current_form_values()
        self._refresh_pin_section()
        self._refresh_security_highlights()

    def _current_form_values(self) -> dict:
        """Valeurs courantes du formulaire (par nom de champ)."""
        return {name: var.get() for name, var in self.variables.items()}

    def _is_dirty(self) -> bool:
        """Vrai si le formulaire diffère de l'état chargé/enregistré."""
        return (editor_logic.values_differ(self._loaded_values,
                                           self._current_form_values())
                or self._pin_change_pending())

    # ------------------------------------------------------------------
    # Inactivité (touche de maintenance)
    # ------------------------------------------------------------------
    def _note_activity(self, _event=None):
        self._last_activity = time.monotonic()

    def _check_idle(self):
        """Ferme l'éditeur sans enregistrer après une trop longue inactivité :
        la borne en service reprend alors d'elle-même."""
        idle = time.monotonic() - self._last_activity
        if idle >= self._idle_timeout_s:
            logger.warning(
                "Configuration fermée après %d min d'inactivité%s.",
                self._idle_timeout_s // 60,
                " (modifications non enregistrées abandonnées)"
                if self._is_dirty() else "")
            self.destroy()
            return
        self.after(_IDLE_CHECK_MS, self._check_idle)

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
        """Affiche/masque le bandeau mettant en évidence le mode debug et le
        secret par défaut, en fonction de la saisie courante."""
        # Le label n'existe pas encore pendant la construction initiale.
        if not hasattr(self, "_security_label"):
            return
        try:
            settings = self._settings_from_form()
        except (TypeError, ValueError, tk.TclError):
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
                "Le secret d'application sera VIDÉ : le code ne fournit aucun "
                "secret par défaut, vous devrez le ressaisir.\n\n"
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
        if self._is_dirty() and not messagebox.askyesno(
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
        except (TypeError, ValueError, tk.TclError) as e:
            messagebox.showerror("Erreur", f"Valeurs invalides : {e}")
            return

        # Code PIN de maintenance : appliqué AVANT la validation, pour qu'une
        # empreinte illisible puisse être remplacée ou supprimée ici.
        pin_hash, pin_error = maintenance_pin.resolve_pin_change(
            self._pin_new.get(), self._pin_confirm.get(),
            self._pin_remove.get(), settings.maintenance_pin_hash)
        if pin_error:
            messagebox.showerror("Code PIN de maintenance", pin_error)
            return
        settings.maintenance_pin_hash = pin_hash
        # Configuration valide enregistrée par une personne : la borne est
        # configurée, l'assistant de premier lancement n'a plus à s'ouvrir.
        settings.setup_completed = True

        errors = settings.validate()
        if errors:
            messagebox.showerror(
                "Configuration invalide",
                "Corrigez les points suivants avant d'enregistrer :\n\n- "
                + "\n- ".join(errors))
            return

        # Garde-fou sécurité : refuse le secret d'application par défaut hors
        # mode développement explicitement activé (case « Mode debug »), en
        # cohérence avec le refus de démarrage (main.py).
        cred_error = editor_logic.default_credentials_error(settings)
        if cred_error:
            messagebox.showerror("Identifiants par défaut refusés", cred_error)
            return

        try:
            # save_settings applique la nouvelle configuration de façon atomique
            # et restaure l'ancienne en mémoire si l'écriture échoue (point 10).
            self.config.save_settings(settings)
        except Exception as e:
            # FRONTIÈRE : échec d'écriture (droits, disque, magasin de secrets
            # indisponible en production...) -> remonté tel quel à l'utilisateur.
            messagebox.showerror("Erreur", f"Erreur lors de la sauvegarde : {e}")
            return

        # État enregistré : plus rien à signaler comme « non enregistré ».
        self._loaded_values = self._current_form_values()
        self._refresh_pin_section()
        if self._kiosk_follows:
            message = ("Configuration enregistrée avec succès.\nLa borne "
                       "va démarrer avec cette configuration.")
        else:
            message = ("Configuration enregistrée avec succès.\nRedémarrez "
                       "l'application principale pour appliquer les "
                       "changements.")
        messagebox.showinfo("Succès", message)
        self.destroy()

    # ------------------------------------------------------------------
    # Tests de diagnostic
    # ------------------------------------------------------------------
    def _log_test_step(self, message):
        """Ajoute une ligne horodatée au journal des tests.

        Appelé uniquement depuis le thread UI (via ``after()``) : les widgets
        Tk ne sont pas thread-safe."""
        self._test_log.configure(state="normal")
        self._test_log.insert("end",
                              f"{time.strftime('%H:%M:%S')}  {message}\n")
        self._test_log.see("end")
        self._test_log.configure(state="disabled")

    def _run_test(self, button, busy_text, idle_text, worker):
        """Exécute ``worker(report)`` (renvoyant (ok: bool, message: str)) dans
        un thread pour ne pas figer l'interface, puis affiche le résultat. Le
        bouton est désactivé pendant le test. ``worker`` reçoit un callable
        ``report(msg)`` qui journalise sa progression en temps réel."""
        button.config(state="disabled", text=busy_text)
        self._log_test_step(f"— {busy_text}")

        # Le worker ne touche JAMAIS Tk : appeler after() depuis un thread
        # secondaire bloque avec Tcl/Tk 9 (interpréteur lié au thread UI). On
        # passe donc par une file que le thread UI dépollue périodiquement.
        results = queue.Queue()

        def report(message):
            results.put(("step", message))

        def task():
            try:
                ok, message = worker(report)
            except Exception as e:
                # FRONTIÈRE (thread de test) : une exception qui remonterait ici
                # tuerait le thread sans rien afficher à l'utilisateur.
                logger.exception("Test de diagnostic en échec.")
                ok, message = False, f"Erreur inattendue : {e}"
            results.put(("finish", ok, message))

        threading.Thread(target=task, daemon=True).start()
        self.after(50, self._poll_test_results, results, button, idle_text)

    def _poll_test_results(self, results, button, idle_text):
        """Dépile les messages du thread de test (thread UI uniquement) et se
        replanifie tant que le test n'est pas terminé."""
        done = False
        while True:
            try:
                item = results.get_nowait()
            except queue.Empty:
                break
            if item[0] == "step":
                self._log_test_step(item[1])
            else:
                _, ok, message = item
                done = True
                button.config(state="normal", text=idle_text)
                self._log_test_step(("✔ " if ok else "✘ ") + message)
                if ok:
                    messagebox.showinfo("Résultat du test", message)
                else:
                    messagebox.showerror("Résultat du test", message)
        if not done:
            self.after(50, self._poll_test_results, results, button, idle_text)

    def test_server(self):
        """Teste la joignabilité du serveur, la validité du secret d'application
        ET le chemin de connexion de la borne (ticket de session -> redirection
        signée vers /patient), avec l'URL/secret saisis. Une seule tentative
        bornée par étape — pas de boucle de réessai."""
        try:
            settings = self._settings_from_form()
        except (TypeError, ValueError, tk.TclError) as e:
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
                       lambda report: self._probe_server(base_url, app_secret, report))

    def _probe_server(self, base_url, app_secret, report=None):
        return probe_server(base_url, app_secret, report)

    def test_printer(self):
        """Teste l'ouverture de l'imprimante USB avec les identifiants/modèle
        saisis (ne fait qu'ouvrir et refermer le périphérique, sans imprimer)."""
        try:
            settings = self._settings_from_form()
        except (TypeError, ValueError, tk.TclError) as e:
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
                       lambda report: self._probe_printer(id_vendor, id_product, model,
                                                        report))

    def _probe_printer(self, id_vendor, id_product, model, report=None):
        return probe_printer(id_vendor, id_product, model, report)


def probe_server(base_url, app_secret, report=None):
    """Teste le serveur saisi : jeton, ticket de session, redirection vers
    /patient. Renvoie ``(ok, message)`` ; ``report(msg)`` suit la progression.
    Partagé avec l'assistant de premier lancement."""

    # report(msg) journalise la progression dans la fenêtre ; None pour les
    # appels hors UI (tests unitaires).
    def emit(message):
        if report:
            report(message)

    # Import local : l'éditeur doit rester ouvrable même sans `requests`
    # installé (le test de serveur est alors le seul indisponible).
    import requests
    from requests.exceptions import RequestException
    emit(f"Étape 1/3 : jeton applicatif auprès de {base_url}…")
    try:
        response = requests.post(
            f"{base_url}/api/get_app_token",
            data={'app_secret': app_secret},
            timeout=_TEST_TIMEOUT,
        )
    except RequestException as e:
        return False, f"Serveur injoignable :\n{e}"

    if response.status_code in (401, 403):
        return False, (f"Serveur joignable mais secret d'application refusé "
                       f"(HTTP {response.status_code}).")
    if response.status_code != 200:
        return False, (f"Serveur joignable mais réponse inattendue "
                       f"(HTTP {response.status_code}).")

    try:
        token = response.json().get('token')
    except ValueError:
        token = None
    if not token:
        return False, "Serveur joignable mais réponse inattendue (aucun token)."
    emit("Jeton applicatif obtenu.")

    # Étape 2 : échange du jeton contre un ticket de session borne — c'est
    # le nouveau chemin de connexion (plus de compte utilisateur).
    emit("Étape 2/3 : échange du jeton contre un ticket de session…")
    try:
        ticket_resp = requests.post(
            f"{base_url}/api/kiosk/session_ticket",
            headers={"X-App-Token": token},
            timeout=_TEST_TIMEOUT,
        )
    except RequestException as e:
        return False, (f"Secret valide mais ticket de session injoignable :"
                       f"\n{e}")
    if ticket_resp.status_code != 200:
        return False, (f"Secret valide mais ticket de session refusé "
                       f"(HTTP {ticket_resp.status_code}).")
    try:
        login_url = ticket_resp.json().get('login_url')
    except ValueError:
        login_url = None
    if not login_url:
        return False, ("Ticket de session : réponse inattendue "
                       "(aucune URL de connexion).")
    emit("Ticket de session obtenu.")
    try:
        login_target = resolve_server_url(base_url, login_url)
    except ValueError:
        return False, ("Ticket obtenu mais le serveur a renvoyé une URL de "
                       "connexion hors du domaine applicatif autorisé.")

    # Étape 3 : la route de connexion doit rediriger vers /patient (cookie
    # de session posé). Une seule tentative, sans suivre la redirection.
    emit("Étape 3/3 : vérification de la redirection vers /patient…")
    try:
        login_resp = requests.get(
            login_target, timeout=_TEST_TIMEOUT,
            allow_redirects=False)
    except RequestException:
        # Ne pas reprendre le texte de l'exception : requests peut y inclure
        # l'URL signée, donc le ticket de session.
        return False, ("Ticket obtenu mais connexion borne injoignable "
                       "(vérifiez le réseau et le serveur).")
    location = login_resp.headers.get('Location', '')
    if login_resp.status_code in (301, 302, 303, 307, 308) and \
            location.rstrip('/').endswith('/patient'):
        return True, (f"Serveur joignable ({base_url}), secret valide et "
                      "connexion borne fonctionnelle (ticket -> /patient).")
    return False, (f"Ticket obtenu mais la connexion borne ne redirige pas "
                   f"vers /patient (HTTP {login_resp.status_code}).")


def probe_printer(id_vendor, id_product, model, report=None):
    """Ouvre puis referme l'imprimante USB (sans imprimer). Renvoie
    ``(ok, message)``. Partagé avec l'assistant de premier lancement."""

    def emit(message):
        if report:
            report(message)

    try:
        from printer import USB_IO_TIMEOUT_MS, CustomUsb
    except ImportError as e:
        return False, ("Module d'impression (python-escpos) indisponible dans "
                       f"cet éditeur :\n{e}")
    emit(f"Ouverture du périphérique USB {id_vendor}/{id_product} "
         f"(profil {model})…")
    printer = None
    try:
        printer = CustomUsb(
            int(id_vendor, 16), int(id_product, 16), profile=model,
            timeout=USB_IO_TIMEOUT_MS)
        # Créer l'objet ne suffit pas : open() localise puis configure le
        # périphérique. La surcharge CustomUsb rend l'échec fatal au lieu
        # de le limiter à un log.
        printer.open()
    except Exception as e:
        if printer is not None:
            with contextlib.suppress(Exception):
                printer.close()
        # FRONTIÈRE MATÉRIELLE : la pile USB remonte des types très variés.
        return False, (f"Imprimante non disponible :\n{e}\n\n"
                       "Vérifiez qu'elle est branchée, sous tension, et que "
                       "l'application principale ne l'utilise pas déjà.")
    emit("Périphérique ouvert — fermeture.")
    with contextlib.suppress(Exception):
        printer.close()
    return True, f"Imprimante détectée et ouverte avec succès (modèle {model})."


def _cancel_pending_callbacks(window):
    """Annule les rappels ``after`` encore planifiés par ``window``.

    La fenêtre du PIN et l'éditeur se succèdent dans le même processus : un
    rappel resté en attente se déclencherait sinon dans la boucle suivante,
    sur une fenêtre détruite (« invalid command name »)."""
    with contextlib.suppress(tk.TclError):
        for after_id in window.tk.splitlist(window.tk.call("after", "info")):
            window.after_cancel(after_id)


def _bring_to_front(window):
    """Fait passer ``window`` au premier plan avec le focus clavier.

    Le « toujours au-dessus » est levé juste après : il ne sert qu'à passer
    devant le bureau au moment où la borne quitte le plein écran, sans
    ensuite masquer les boîtes de dialogue de l'éditeur."""
    window.lift()
    window.attributes("-topmost", True)
    window.after(300, lambda: window.attributes("-topmost", False))
    window.after(50, window.focus_force)


class MaintenancePinDialog(tk.Tk):
    """Demande le code PIN de maintenance avant d'ouvrir la configuration.

    ``granted`` passe à True si le code est bon. Fermeture, Échap, essais
    épuisés ou inactivité : ``granted`` reste False et la borne repart."""

    def __init__(self, stored_hash, max_attempts=PIN_MAX_ATTEMPTS,
                 timeout_s=PIN_DIALOG_TIMEOUT_S):
        super().__init__()
        self.title("Maintenance — Flowelis Borne")
        self.resizable(False, False)
        self.granted = False
        self._stored_hash = stored_hash
        self._attempts_left = max_attempts
        self._timeout_s = timeout_s
        self._last_activity = time.monotonic()

        frame = ttk.Frame(self, padding=24)
        frame.pack()
        ttk.Label(frame, text="Code PIN de maintenance",
                  font=('Helvetica', 12, 'bold')).pack(anchor="w")
        self._pin = tk.StringVar()
        self._entry = ttk.Entry(frame, show="*", textvariable=self._pin,
                                width=16, font=('Helvetica', 16))
        self._entry.pack(pady=(10, 6), fill="x")
        self._message = tk.Label(frame, text="", fg=_DANGER_COLOR)
        self._message.pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(pady=(10, 0), fill="x")
        ttk.Button(buttons, text="Annuler", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Valider", command=self._submit).pack(
            side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._submit())
        self.bind("<KP_Enter>", lambda _e: self._submit())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind_all("<KeyPress>", self._note_activity, add="+")

        # Centré à l'écran (la borne vient de fermer sa fenêtre plein écran).
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
        y = (self.winfo_screenheight() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        _bring_to_front(self)
        self.after(100, self._entry.focus_force)
        self.after(1000, self._check_timeout)

    def destroy(self):
        _cancel_pending_callbacks(self)
        super().destroy()

    def _note_activity(self, _event=None):
        self._last_activity = time.monotonic()

    def _check_timeout(self):
        if time.monotonic() - self._last_activity >= self._timeout_s:
            logger.info("Saisie du code PIN de maintenance abandonnée "
                        "(inactivité).")
            self.destroy()
            return
        self.after(1000, self._check_timeout)

    def _submit(self):
        if maintenance_pin.verify_pin(self._pin.get(), self._stored_hash):
            self.granted = True
            self.destroy()
            return
        self._attempts_left -= 1
        self._pin.set("")
        if self._attempts_left <= 0:
            logger.warning("Accès maintenance refusé : code PIN incorrect, "
                           "essais épuisés.")
            self.destroy()
            return
        logger.warning("Code PIN de maintenance incorrect.")
        self._message.config(
            text=f"Code incorrect — {self._attempts_left} essai(s) restant(s).")


def ask_maintenance_pin(stored_hash) -> bool:
    """Affiche la demande de code PIN ; True si le bon code a été saisi."""
    dialog = MaintenancePinDialog(stored_hash)
    dialog.mainloop()
    return dialog.granted


class MaintenanceActionDialog(tk.Tk):
    """Suite à donner après la touche de maintenance.

    ``action`` vaut ``maintenance_pin.MAINTENANCE_ACTION_CONFIGURE`` ou
    ``MAINTENANCE_ACTION_QUIT`` selon le bouton choisi ; il reste None
    sur annulation, Échap ou inactivité — la borne repart alors en service.
    « Quitter l'application » demande confirmation : un tap de trop sur
    l'écran tactile ne doit pas arrêter la borne pour la journée. Seule
    l'application se ferme : le PC reste allumé."""

    def __init__(self, timeout_s=ACTION_DIALOG_TIMEOUT_S):
        super().__init__()
        self.title("Maintenance — Flowelis Borne")
        self.resizable(False, False)
        self.action = None
        self._timeout_s = timeout_s
        self._last_activity = time.monotonic()

        frame = ttk.Frame(self, padding=24)
        frame.pack()
        ttk.Label(frame, text="Maintenance de la borne",
                  font=('Helvetica', 12, 'bold')).pack(anchor="w")
        tk.Label(
            frame, justify="left", wraplength=400,
            text=("La borne est arrêtée. Choisissez la suite — sans "
                  "réponse, elle reprend son service d'elle-même."),
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(pady=(14, 0), fill="x")
        ttk.Button(buttons, text="Reprendre le service",
                   command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Quitter l'application",
                   command=self._quit_app).pack(side=tk.RIGHT, padx=(0, 6))
        self._config_button = ttk.Button(
            buttons, text="Ouvrir la configuration",
            command=self._configure)
        self._config_button.pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._configure())
        self.bind("<KP_Enter>", lambda _e: self._configure())
        self.bind("<Escape>", lambda _e: self.destroy())
        for sequence in ("<KeyPress>", "<ButtonPress>", "<Motion>",
                         "<MouseWheel>"):
            self.bind_all(sequence, self._note_activity, add="+")

        # Centré à l'écran (la borne vient de fermer sa fenêtre plein écran).
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
        y = (self.winfo_screenheight() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        _bring_to_front(self)
        self.after(100, self._config_button.focus_force)
        self.after(1000, self._check_timeout)

    def destroy(self):
        _cancel_pending_callbacks(self)
        super().destroy()

    def _note_activity(self, _event=None):
        self._last_activity = time.monotonic()

    def _check_timeout(self):
        if time.monotonic() - self._last_activity >= self._timeout_s:
            logger.info("Choix de maintenance abandonné (inactivité) : "
                        "la borne reprend son service.")
            self.destroy()
            return
        self.after(1000, self._check_timeout)

    def _configure(self):
        self.action = maintenance_pin.MAINTENANCE_ACTION_CONFIGURE
        self.destroy()

    def _quit_app(self):
        if not messagebox.askyesno(
                "Quitter l'application",
                maintenance_pin.quit_confirmation_text(
                    desktop_integration.supported()),
                parent=self):
            return
        self.action = maintenance_pin.MAINTENANCE_ACTION_QUIT
        self.destroy()


def ask_maintenance_action():
    """Affiche le choix de maintenance ; renvoie l'action choisie ou None."""
    dialog = MaintenanceActionDialog()
    dialog.mainloop()
    return dialog.action


if __name__ == "__main__":
    logging_config.setup_logging()
    logger.info("Ouverture de l'éditeur de configuration.")
    app = ConfigEditor()
    app.mainloop()
