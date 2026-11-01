"""Écran de la borne toujours allumé, jamais verrouillé (Linux, X11).

Sans précaution, le bureau éteint l'écran après quelques minutes sans
activité, et peut verrouiller la session : le patient suivant trouvait un
écran noir, voire une demande de mot de passe. Une borne voit souvent passer
de longues minutes sans patient.

Tant que la borne tourne, et SANS modifier durablement les réglages du bureau
(rien n'est écrit sur disque) :

- le serveur d'affichage X ne met plus l'écran en veille
  (``xset s off s noblank -dpms``), réglages d'origine rétablis à l'arrêt ;
- toutes les ``KEEPALIVE_PERIOD_S`` secondes, le compteur d'inactivité est
  remis à zéro, comme après un contact :
  - ``xset s reset`` (serveur X : sert aussi aux bureaux qui mesurent
    l'inactivité par X, dont la gestion d'énergie LXQt de Lubuntu) ;
  - ``xscreensaver-command -deactivate`` si xscreensaver tourne (Lubuntu :
    c'est lui qui noircit et VERROUILLE l'écran) ;
  - ``SimulateUserActivity`` par D-Bus (économiseurs freedesktop et
    Cinnamon / Linux Mint), sans effet là où il n'existe pas.

Chaque mécanisme est facultatif : un outil absent ou en échec est
journalisé, jamais bloquant. Session Wayland, Windows : rien n'est fait (le
journal le dit). La connexion automatique au démarrage du PC relève du
système (``install.sh``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger("borne.screen")

#: Rappel d'activité : plus court que le plus petit délai de veille réglable
#: (1 min sous xscreensaver comme sous LXQt et Cinnamon).
KEEPALIVE_PERIOD_S = 50
COMMAND_TIMEOUT_S = 5

_DBUS_SCREENSAVERS = (
    ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
     "org.freedesktop.ScreenSaver.SimulateUserActivity"),
    ("org.cinnamon.ScreenSaver", "/org/cinnamon/ScreenSaver",
     "org.cinnamon.ScreenSaver.SimulateUserActivity"),
)


def _default_run(command):
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=COMMAND_TIMEOUT_S, check=False)


def parse_xset_query(text):
    """Réglages de veille X lus dans ``xset q`` (None si illisible)."""
    timeout = re.search(r"timeout:\s*(\d+)\s+cycle:\s*(\d+)", text or "")
    if not timeout:
        return None
    blanking = re.search(r"prefer blanking:\s*(yes|no)", text)
    dpms = re.search(r"DPMS is (Enabled|Disabled)", text)
    return {
        "timeout": int(timeout.group(1)),
        "cycle": int(timeout.group(2)),
        "blanking": blanking is None or blanking.group(1) == "yes",
        "dpms": dpms is not None and dpms.group(1) == "Enabled",
    }


def restore_command(state):
    """Commande ``xset`` qui rétablit ``state`` (cf. parse_xset_query)."""
    return ["xset", "s", str(state["timeout"]), str(state["cycle"]),
            "s", "blank" if state["blanking"] else "noblank",
            "+dpms" if state["dpms"] else "-dpms"]


class ScreenAwake:
    """Garde l'écran allumé tant que ``start()`` … ``stop()``."""

    def __init__(self, run=None, which=shutil.which, environ=None,
                 platform=None, period_s=None):
        self._run_command = run or _default_run
        self._which = which
        self._environ = os.environ if environ is None else environ
        self._platform = sys.platform if platform is None else platform
        self._period_s = KEEPALIVE_PERIOD_S if period_s is None else period_s
        self._stop = threading.Event()
        self._thread = None
        self._saved = None
        self.active = False

    # --- Outils -----------------------------------------------------------

    def _run(self, command):
        """Lance ``command`` ; renvoie le résultat, ou None si l'outil est
        absent ou en échec (jamais d'exception)."""
        if self._which(command[0]) is None:
            return None
        try:
            result = self._run_command(command)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Commande « %s » impossible : %s", command[0], exc)
            return None
        if result.returncode != 0:
            logger.debug("Commande « %s » en échec (code %s).",
                         " ".join(command[:2]), result.returncode)
            return None
        return result

    def unsupported_reason(self):
        if not self._platform.startswith("linux"):
            return "système autre que Linux"
        if (self._environ.get("XDG_SESSION_TYPE") == "wayland"
                or (self._environ.get("WAYLAND_DISPLAY")
                    and not self._environ.get("DISPLAY"))):
            return "session Wayland"
        if not self._environ.get("DISPLAY"):
            return "pas d'affichage X (DISPLAY absent)"
        return None

    # --- Cycle de vie ---------------------------------------------------------

    def start(self):
        reason = self.unsupported_reason()
        if reason:
            logger.info("Écran toujours allumé : non géré (%s).", reason)
            return False
        query = self._run(["xset", "q"])
        self._saved = parse_xset_query(query.stdout) if query else None
        applied = self._run(["xset", "s", "off", "s", "noblank", "-dpms"]) is not None
        xscreensaver = self._which("xscreensaver-command") is not None
        logger.info(
            "Écran toujours allumé (bureau : %s) : veille X %s ; xscreensaver "
            "%s ; rappel d'activité toutes les %d s.",
            self._environ.get("XDG_CURRENT_DESKTOP") or "inconnu",
            "désactivée" if applied else "NON désactivée (xset absent ?)",
            "présent" if xscreensaver else "absent", self._period_s)
        self.active = True
        self.poke()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ecran-allume",
                                        daemon=True)
        self._thread.start()
        return True

    def poke(self):
        """Remet à zéro les compteurs d'inactivité (comme un contact)."""
        self._run(["xset", "s", "reset"])
        self._run(["xscreensaver-command", "-deactivate"])
        for destination, path, method in _DBUS_SCREENSAVERS:
            self._run(["dbus-send", "--session", "--type=method_call",
                       f"--dest={destination}", path, method])

    def _loop(self):
        while not self._stop.wait(self._period_s):
            try:
                self.poke()
            except Exception:
                # Le rappel ne doit jamais mourir : sans lui, l'écran s'éteint.
                logger.exception("Rappel d'activité de l'écran impossible.")

    def stop(self):
        """Arrêt de la borne : plus de rappel, réglages X d'origine."""
        if not self.active:
            return
        self.active = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=COMMAND_TIMEOUT_S + 1)
        if self._saved is not None:
            self._run(restore_command(self._saved))
            logger.info("Réglages de veille de l'écran rétablis.")
