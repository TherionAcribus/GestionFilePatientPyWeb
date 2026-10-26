"""Assistant de premier lancement de la borne (fenêtre tkinter).

Quatre étapes — serveur, imprimante, borne, récapitulatif — au lieu de
l'éditeur complet : seul l'essentiel est demandé, chaque étape est vérifiée
avant de passer à la suivante, et les tests serveur/imprimante sont à portée
de main. S'ouvre automatiquement tant que la borne n'a jamais été configurée
(``Settings.setup_completed``), ou à la demande : ``--setup`` (binaire ou
main.py) ou ``python setup_wizard.py``.

La logique (validation, configuration finale, récapitulatif, tri USB) est
dans ``setup_logic`` ; ce module ne fait que l'interface.
"""

import logging
import queue
import socket
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import logging_config
import setup_logic
from config import Config
from config_editor import (
    _DANGER_COLOR,
    _bring_to_front,
    _cancel_pending_callbacks,
    probe_printer,
    probe_server,
)
from maintenance_pin import MAINTENANCE_KEY, PIN_MAX_LENGTH, PIN_MIN_LENGTH
from setup_logic import STEPS, TEST_FAILED, TEST_NOT_RUN, TEST_OK

logger = logging.getLogger("borne.setup")

_OK_COLOR = "#1e7b34"
_BUSY_COLOR = "#555555"
_WARN_COLOR = "#b35900"

_STEP_TITLES = {
    "server": "Serveur Flowelis",
    "printer": "Imprimante ticket",
    "borne": "Borne",
    "summary": "Récapitulatif",
}

#: Résultats possibles de l'assistant.
RESULT_DONE, RESULT_ADVANCED, RESULT_CANCELLED = "done", "advanced", "cancelled"


def read_usb_devices():
    """Périphériques USB visibles, au format attendu par
    ``setup_logic.usb_candidates``. Lève une exception si la pile USB est
    indisponible (pas de libusb, pas de pilote) : l'appelant l'affiche."""
    import usb.core

    devices = []
    for dev in usb.core.find(find_all=True) or []:
        classes = set()
        try:
            for configuration in dev:
                for interface in configuration:
                    classes.add(interface.bInterfaceClass)
        except Exception:
            # Descripteurs illisibles (droits, pilote) : on garde l'appareil,
            # classé d'après son seul fabricant.
            logger.debug("Interfaces USB illisibles pour %04x:%04x.",
                         dev.idVendor, dev.idProduct)
        devices.append({"vendor": dev.idVendor, "product": dev.idProduct,
                        "device_class": dev.bDeviceClass,
                        "interface_classes": classes})
    return devices


class SetupWizard(tk.Tk):
    def __init__(self, kiosk_follows=False):
        """``kiosk_follows`` : la borne démarre à la fermeture (premier
        lancement) — le dernier bouton l'annonce."""
        super().__init__()
        self.title("Installation — Flowelis Borne")
        self.geometry("700x600")
        self.minsize(620, 540)

        self.config_store = Config()
        self.result = RESULT_CANCELLED
        self._kiosk_follows = kiosk_follows
        self._step = 0
        self._tests = {"server": TEST_NOT_RUN, "printer": TEST_NOT_RUN}
        self._candidates = []

        initial = setup_logic.initial_values(self.config_store.settings)
        self._vars = {}
        for name, value in initial.items():
            var = tk.BooleanVar(value=value) if isinstance(value, bool) \
                else tk.StringVar(value=value)
            self._vars[name] = var
        self._initial_values = self._values()

        self._build()
        self._show_step(0)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _bring_to_front(self)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self):
        header = ttk.Frame(self, padding=(20, 16, 20, 4))
        header.pack(fill="x")
        ttk.Label(header, text="Installation de la borne",
                  font=("Helvetica", 15, "bold")).pack(anchor="w")
        self._step_label = ttk.Label(header, font=("Helvetica", 11))
        self._step_label.pack(anchor="w", pady=(2, 0))
        ttk.Separator(self).pack(fill="x", padx=20, pady=(8, 0))

        body = ttk.Frame(self, padding=(20, 12))
        body.pack(fill="both", expand=True)
        self._pages = {
            "server": self._build_server(body),
            "printer": self._build_printer(body),
            "borne": self._build_borne(body),
            "summary": self._build_summary(body),
        }

        ttk.Separator(self).pack(fill="x", padx=20)
        footer = ttk.Frame(self, padding=(20, 10, 20, 16))
        footer.pack(fill="x")
        ttk.Button(footer, text="Configuration avancée…",
                   command=self._advanced).pack(side=tk.LEFT)
        self._next_button = ttk.Button(footer, command=self._next)
        self._next_button.pack(side=tk.RIGHT)
        self._back_button = ttk.Button(footer, text="◀ Précédent",
                                       command=self._back)
        self._back_button.pack(side=tk.RIGHT, padx=(0, 8))

    def _page(self, parent, intro):
        frame = ttk.Frame(parent)
        frame.columnconfigure(1, weight=1)
        tk.Label(frame, text=intro, justify="left", wraplength=620,
                 font=("Helvetica", 10)).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        return frame

    def _entry_row(self, frame, row, label, name, show=None, hint=None):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="e",
                                          padx=(0, 8), pady=4)
        entry = ttk.Entry(frame, textvariable=self._vars[name], show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        if hint:
            ttk.Label(frame, text=hint, foreground=_BUSY_COLOR).grid(
                row=row + 1, column=1, sticky="w")
        return entry

    def _check_row(self, frame, row, text, name):
        ttk.Checkbutton(frame, text=text, variable=self._vars[name]).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=2)

    def _build_server(self, parent):
        frame = self._page(parent, (
            "Bienvenue ! Cet assistant prépare la borne en quatre étapes.\n"
            "Munissez-vous de l'adresse du serveur Flowelis et du secret "
            "d'application, fournis par l'administrateur Flowelis."))
        self._server_entry = self._entry_row(
            frame, 1, "Adresse du serveur :", "base_url",
            hint="ex. https://flowelis.ma-pharmacie.fr")
        secret = self._entry_row(frame, 3, "Secret d'application :",
                                 "app_secret", show="*")
        reveal = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Afficher", variable=reveal,
                        command=lambda: secret.config(
                            show="" if reveal.get() else "*")).grid(
            row=3, column=2, padx=(8, 0))
        self._server_button = ttk.Button(frame, text="Tester la connexion",
                                         command=self._test_server)
        self._server_button.grid(row=4, column=1, sticky="w", pady=(14, 4))
        self._server_status = tk.Label(frame, text="", justify="left",
                                       wraplength=480)
        self._server_status.grid(row=5, column=1, columnspan=2, sticky="w")
        for name in ("base_url", "app_secret"):
            self._vars[name].trace_add(
                "write", lambda *_a: self._set_test("server", TEST_NOT_RUN, ""))
        return frame

    def _build_printer(self, parent):
        frame = self._page(parent, (
            "Branchez et allumez l'imprimante ticket, puis cliquez sur "
            "« Détecter ». Vous pouvez aussi saisir ses identifiants USB."))
        self._detect_button = ttk.Button(frame, text="Détecter les imprimantes USB",
                                         command=self._detect)
        self._detect_button.grid(row=1, column=0, columnspan=2, sticky="w")
        self._detect_choice = ttk.Combobox(frame, state="disabled")
        self._detect_choice.grid(row=2, column=0, columnspan=3, sticky="ew",
                                 pady=(6, 2))
        self._detect_choice.bind("<<ComboboxSelected>>", self._use_detected)
        self._detect_status = tk.Label(frame, text="", justify="left",
                                       wraplength=600)
        self._detect_status.grid(row=3, column=0, columnspan=3, sticky="w",
                                 pady=(0, 8))
        self._entry_row(frame, 4, "ID vendeur :", "printer_id_vendor")
        self._entry_row(frame, 5, "ID produit :", "printer_id_product")
        self._entry_row(frame, 6, "Modèle :", "printer_model",
                        hint="profil python-escpos, ex. TM-T88II")
        self._check_row(frame, 8, "Vérifier le papier avant chaque impression",
                        "check_paper")
        self._printer_button = ttk.Button(frame, text="Tester l'imprimante",
                                          command=self._test_printer)
        self._printer_button.grid(row=9, column=1, sticky="w", pady=(12, 4))
        self._printer_status = tk.Label(frame, text="", justify="left",
                                        wraplength=480)
        self._printer_status.grid(row=10, column=1, columnspan=2, sticky="w")
        for name in ("printer_id_vendor", "printer_id_product", "printer_model"):
            self._vars[name].trace_add(
                "write", lambda *_a: self._set_test("printer", TEST_NOT_RUN, ""))
        return frame

    def _build_borne(self, parent):
        frame = self._page(parent, (
            "Options de la borne. Les valeurs proposées conviennent à une "
            "borne en service."))
        self._entry_row(frame, 1, "Identifiant :", "borne_id",
                        hint=f"facultatif — laissé vide, l'identifiant de la "
                             f"machine est généré automatiquement "
                             f"({socket.gethostname()})")
        self._check_row(frame, 3, "Plein écran (mode kiosque)", "fullscreen")
        self._check_row(frame, 4, "Masquer le curseur (écran tactile)",
                        "hide_cursor")
        self._check_row(frame, 5, ("Mode développement — autorise un serveur "
                                   "en http:// hors de cette machine ; "
                                   "déconseillé en service"), "debug")

        ttk.Label(frame, text="Code PIN de maintenance",
                  font=("Helvetica", 10, "bold")).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(16, 2))
        tk.Label(frame, justify="left", wraplength=600, text=(
            f"La touche {MAINTENANCE_KEY} d'un clavier branché ouvre la "
            "configuration de la borne en service. Code facultatif "
            f"({PIN_MIN_LENGTH} à {PIN_MAX_LENGTH} chiffres) : laissez vide "
            "si personne d'autre n'a accès au clavier.")).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self._entry_row(frame, 8, "Code PIN :", "pin_new", show="*")
        self._entry_row(frame, 9, "Confirmation :", "pin_confirm", show="*")
        if self.config_store.settings.maintenance_pin_hash:
            self._check_row(frame, 10, "Supprimer le code PIN actuel",
                            "pin_remove")
        return frame

    def _build_summary(self, parent):
        frame = self._page(parent, "Vérifiez les réglages avant de terminer.")
        self._summary = tk.Label(frame, justify="left", anchor="w",
                                 font=("Helvetica", 10))
        self._summary.grid(row=1, column=0, columnspan=3, sticky="w")
        self._summary_errors = tk.Label(frame, justify="left", wraplength=620,
                                        fg=_DANGER_COLOR)
        self._summary_errors.grid(row=2, column=0, columnspan=3, sticky="w",
                                  pady=(12, 0))
        self._summary_warning = tk.Label(frame, justify="left", wraplength=620,
                                         fg=_WARN_COLOR)
        self._summary_warning.grid(row=3, column=0, columnspan=3, sticky="w",
                                   pady=(8, 0))
        return frame

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _values(self) -> dict:
        return {name: var.get() for name, var in self._vars.items()}

    def _show_step(self, index):
        self._step = index
        step = STEPS[index]
        for name, page in self._pages.items():
            if name == step:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        self._step_label.config(
            text=f"Étape {index + 1} sur {len(STEPS)} — {_STEP_TITLES[step]}")
        self._back_button.config(state="normal" if index else "disabled")
        if step == "summary":
            self._refresh_summary()
            self._next_button.config(
                text="Terminer et démarrer la borne" if self._kiosk_follows
                else "Terminer")
        else:
            self._next_button.config(text="Suivant ▶")
        if step == "server":
            self.after(50, self._server_entry.focus_set)

    def _next(self):
        step = STEPS[self._step]
        if step == "summary":
            self._finish()
            return
        errors = setup_logic.step_errors(step, self._values())
        if errors:
            messagebox.showerror("Étape incomplète", "\n".join(errors),
                                 parent=self)
            return
        self._show_step(self._step + 1)

    def _back(self):
        if self._step:
            self._show_step(self._step - 1)

    def _refresh_summary(self):
        lines = setup_logic.summary_lines(
            self._values(), self._tests["server"], self._tests["printer"],
            pin_already_set=bool(self.config_store.settings.maintenance_pin_hash))
        self._summary.config(text="\n".join(f"•  {line}" for line in lines))
        # Points qui bloqueront « Terminer » (http distant en production…) :
        # annoncés dès le récapitulatif plutôt qu'au clic.
        _settings, errors = setup_logic.build_settings(
            self.config_store.settings, self._values())
        self._summary_errors.config(
            text="\n".join(f"✘ {e}" for e in errors))
        warnings = []
        if self._tests["server"] != TEST_OK:
            warnings.append("Le serveur n'a pas été testé avec succès : la "
                            "borne affichera « Borne hors ligne » tant qu'il "
                            "ne répond pas.")
        if self._tests["printer"] != TEST_OK:
            warnings.append("L'imprimante n'a pas été testée avec succès : "
                            "vérifiez-la avant la mise en service.")
        self._summary_warning.config(
            text="\n".join(f"⚠ {w}" for w in warnings))

    def _finish(self):
        settings, errors = setup_logic.build_settings(
            self.config_store.settings, self._values())
        if errors:
            messagebox.showerror(
                "Configuration incomplète",
                "Corrigez les points suivants :\n\n- " + "\n- ".join(errors),
                parent=self)
            return
        try:
            self.config_store.save_settings(settings)
        except Exception as e:
            # FRONTIÈRE : droits, disque, magasin de secrets indisponible…
            logger.exception("Enregistrement de la configuration impossible.")
            messagebox.showerror("Erreur",
                                 f"Erreur lors de l'enregistrement : {e}",
                                 parent=self)
            return
        logger.info("Assistant de premier lancement terminé.")
        self.result = RESULT_DONE
        self.destroy()

    def _changed(self) -> bool:
        return self._values() != self._initial_values

    def _on_close(self):
        if not messagebox.askyesno(
                "Quitter l'assistant",
                "La configuration n'est pas terminée : rien n'a été "
                "enregistré, et l'assistant se rouvrira au prochain démarrage "
                "de la borne.\n\nQuitter quand même ?", parent=self):
            return
        logger.info("Assistant de premier lancement abandonné.")
        self.result = RESULT_CANCELLED
        self.destroy()

    def _advanced(self):
        if self._changed() and not messagebox.askyesno(
                "Configuration avancée",
                "Ouvrir l'éditeur complet ? Les valeurs saisies dans "
                "l'assistant ne seront pas enregistrées.", parent=self):
            return
        self.result = RESULT_ADVANCED
        self.destroy()

    def destroy(self):
        _cancel_pending_callbacks(self)
        super().destroy()

    # ------------------------------------------------------------------
    # Tests et détection (en arrière-plan : l'interface reste réactive)
    # ------------------------------------------------------------------
    def _run_background(self, worker, on_done):
        """``worker()`` dans un thread ; ``on_done(résultat)`` dans le fil Tk.
        Un worker qui lève transmet l'exception comme résultat."""
        results = queue.Queue()

        def task():
            try:
                outcome = worker()
            except Exception as e:
                # FRONTIÈRE (thread) : toujours rendre la main à l'interface.
                logger.exception("Opération de l'assistant en échec.")
                outcome = e
            results.put(outcome)

        def poll():
            try:
                outcome = results.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            on_done(outcome)

        threading.Thread(target=task, daemon=True).start()
        self.after(100, poll)

    def _set_test(self, which, state, message):
        self._tests[which] = state
        label = self._server_status if which == "server" else self._printer_status
        color = {TEST_OK: _OK_COLOR, TEST_FAILED: _DANGER_COLOR}.get(
            state, _BUSY_COLOR)
        prefix = {TEST_OK: "✔ ", TEST_FAILED: "✘ "}.get(state, "")
        label.config(text=prefix + message if message else "", fg=color)

    def _run_test(self, which, button, fields, worker):
        """Lance un test ; son résultat n'est retenu que si les champs testés
        n'ont pas changé entre-temps."""
        snapshot = {name: self._vars[name].get() for name in fields}
        idle_text = button.cget("text")
        button.config(state="disabled", text="Test en cours…")
        self._set_test(which, TEST_NOT_RUN, "Test en cours…")

        def done(outcome):
            button.config(state="normal", text=idle_text)
            if {name: self._vars[name].get() for name in fields} != snapshot:
                return  # saisie modifiée pendant le test : résultat périmé
            if isinstance(outcome, Exception):
                self._set_test(which, TEST_FAILED, f"Erreur inattendue : {outcome}")
                return
            ok, message = outcome
            self._set_test(which, TEST_OK if ok else TEST_FAILED, message)

        self._run_background(worker, done)

    def _test_server(self):
        values = self._values()
        errors = setup_logic.step_errors("server", values)
        if errors:
            messagebox.showerror("Test impossible", "\n".join(errors), parent=self)
            return
        url = values["base_url"].strip().rstrip("/")
        secret = values["app_secret"].strip()
        self._run_test("server", self._server_button, ("base_url", "app_secret"),
                       lambda: probe_server(url, secret))

    def _test_printer(self):
        values = self._values()
        errors = setup_logic.step_errors("printer", values)
        if errors:
            messagebox.showerror("Test impossible", "\n".join(errors), parent=self)
            return
        vendor = values["printer_id_vendor"].strip()
        product = values["printer_id_product"].strip()
        model = values["printer_model"].strip()
        self._run_test("printer", self._printer_button,
                       ("printer_id_vendor", "printer_id_product", "printer_model"),
                       lambda: probe_printer(vendor, product, model))

    def _detect(self):
        self._detect_button.config(state="disabled")
        self._detect_status.config(text="Recherche des périphériques USB…",
                                   fg=_BUSY_COLOR)

        def done(outcome):
            self._detect_button.config(state="normal")
            if isinstance(outcome, Exception):
                self._detect_status.config(
                    fg=_DANGER_COLOR,
                    text=("Détection USB indisponible sur ce poste "
                          f"({outcome}). Saisissez les identifiants "
                          "ci-dessous (commande lsusb sous Linux)."))
                return
            self._candidates = setup_logic.usb_candidates(outcome)
            if not self._candidates:
                self._detect_choice.config(values=[], state="disabled")
                self._detect_status.config(
                    fg=_DANGER_COLOR,
                    text="Aucun périphérique USB trouvé : vérifiez que "
                         "l'imprimante est branchée et allumée.")
                return
            labels = [label for _v, _p, label in self._candidates]
            self._detect_choice.config(values=labels, state="readonly")
            self._detect_choice.current(0)
            self._use_detected()
            self._detect_status.config(
                fg=_OK_COLOR,
                text=f"{len(labels)} périphérique(s) trouvé(s) — le plus "
                     "probable est sélectionné.")

        self._run_background(read_usb_devices, done)

    def _use_detected(self, _event=None):
        index = self._detect_choice.current()
        if 0 <= index < len(self._candidates):
            vendor, product, _label = self._candidates[index]
            self._vars["printer_id_vendor"].set(vendor)
            self._vars["printer_id_product"].set(product)


def run_setup_wizard(kiosk_follows=False) -> str:
    """Affiche l'assistant ; renvoie ``done``, ``advanced`` ou ``cancelled``."""
    wizard = SetupWizard(kiosk_follows=kiosk_follows)
    wizard.mainloop()
    return wizard.result


if __name__ == "__main__":
    logging_config.setup_logging()
    logger.info("Ouverture de l'assistant d'installation.")
    if run_setup_wizard() == RESULT_ADVANCED:
        import config_editor
        config_editor.ConfigEditor().mainloop()
