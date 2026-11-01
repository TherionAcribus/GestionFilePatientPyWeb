"""Gardien de la borne : la relance si elle s'arrête anormalement.

Avant, le démarrage automatique lançait la borne une seule fois : un plantage
(moteur Qt, bibliothèque USB, processus tué faute de mémoire) laissait le
bureau visible des patients jusqu'à une relance à la main.

Avec ``--supervise`` (démarrage automatique et menu des applications), un
petit processus « gardien » lance la vraie borne et attend sa fin :

- sortie normale (code 0 : « Quitter l'application », « Éteindre le PC »,
  borne déjà en service) : le gardien s'arrête aussi ;
- relance demandée par la borne (configuration, fenêtre fermée par Alt+F4) :
  codes ``EXIT_RESTART*``, relance immédiate ;
- tout autre code, ou processus tué par un signal : relance après un délai
  qui double à chaque plantage rapproché (2 s, 4 s… plafonné à 5 min), pour
  ne pas tourner en boucle sur une panne qui persiste. La borne relancée
  reçoit la cause (``CRASH_ENV``) et la remonte au serveur.

Le gardien n'ouvre ni fenêtre, ni imprimante, ni réseau, et journalise dans
son propre fichier (``superviseur.log``) pour ne pas se disputer la rotation
de ``borne.log`` avec la borne.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import signal
import subprocess
import threading
import time

logger = logging.getLogger("borne.supervisor")

SUPERVISE_OPTION = "--supervise"
LOG_FILENAME = "superviseur.log"

#: Posée dans l'environnement de la borne lancée par le gardien.
SUPERVISED_ENV = "FLOWELIS_BORNE_SUPERVISED"
#: Cause du redémarrage, transmise à la borne relancée après un plantage.
CRASH_ENV = "FLOWELIS_BORNE_CRASH"

#: Codes de sortie par lesquels une borne supervisée demande sa relance (au
#: lieu de se relancer elle-même, ce qui la ferait échapper au gardien).
EXIT_RESTART = 75
EXIT_RESTART_MAINTENANCE = 76

#: Incident remonté au serveur par la borne relancée après un plantage.
SERVER_EVENT_CRASH_RESTART = "kiosk_crash_restart"

#: Plantages « rapprochés » : comptés sur cette fenêtre glissante.
CRASH_WINDOW_S = 600
RESTART_DELAY_BASE_S = 2.0
RESTART_DELAY_MAX_S = 300.0


def describe_exit(code) -> str:
    """Code de sortie lisible : « code 1 », « signal SIGSEGV »…"""
    if code is not None and code < 0:
        try:
            return f"signal {signal.Signals(-code).name}"
        except ValueError:
            return f"signal {-code}"
    return f"code {code}"


def restart_delay(recent_crashes: int) -> float:
    """Délai avant relance après le n-ième plantage rapproché."""
    n = max(1, recent_crashes)
    return min(RESTART_DELAY_MAX_S, RESTART_DELAY_BASE_S * 2 ** (n - 1))


class CrashHistory:
    """Plantages des ``window_s`` dernières secondes."""

    def __init__(self, window_s=CRASH_WINDOW_S):
        self.window_s = window_s
        self._times = collections.deque()

    def record(self, now) -> int:
        self._times.append(now)
        while self._times and now - self._times[0] > self.window_s:
            self._times.popleft()
        return len(self._times)


def crash_message(code, recent_crashes, window_s=CRASH_WINDOW_S) -> str:
    """Texte remonté au serveur par la borne relancée."""
    message = ("Borne relancée automatiquement après un arrêt anormal "
               f"({describe_exit(code)})")
    if recent_crashes > 1:
        message += (f", {recent_crashes} arrêts en {window_s // 60} min")
    return message + "."


class Supervisor:
    """Lance la borne et la relance selon sa façon de se terminer.

    ``command_for(extra)`` : ligne de commande de la borne, avec les options
    ``extra`` ; ``restart_codes`` : {code de sortie : options de relance}."""

    def __init__(self, command_for, restart_codes, base_env, popen=subprocess.Popen,
                 clock=time.monotonic, history=None):
        self._command_for = command_for
        self._restart_codes = dict(restart_codes)
        self._base_env = dict(base_env)
        self._popen = popen
        self._clock = clock
        self._history = history or CrashHistory()
        self._stop = threading.Event()
        self._child = None
        self.launches = 0

    def stop(self, signum=None):
        """Fin de session, arrêt du PC… : ne plus relancer, et arrêter la
        borne. Appelé depuis un gestionnaire de signal."""
        self._stop.set()
        child = self._child
        if child is not None:
            with contextlib.suppress(OSError):
                child.terminate()
        if signum is not None:
            logger.info("Arrêt du gardien demandé (signal %s).", signum)

    def run(self) -> int:
        extra = ()
        crash_note = None
        while not self._stop.is_set():
            env = dict(self._base_env)
            env[SUPERVISED_ENV] = "1"
            env.pop(CRASH_ENV, None)
            if crash_note:
                env[CRASH_ENV] = crash_note
            command = self._command_for(list(extra))
            try:
                self._child = self._popen(command, env=env)
            except OSError:
                logger.exception("Lancement de la borne impossible.")
                code = None
            else:
                self.launches += 1
                if self._stop.is_set():
                    # Arrêt demandé pendant le lancement : stop() n'a pas
                    # encore vu ce processus.
                    with contextlib.suppress(OSError):
                        self._child.terminate()
                code = self._child.wait()
            finally:
                self._child = None

            if self._stop.is_set():
                break
            if code == 0:
                logger.info("Borne arrêtée normalement : fin du gardien.")
                return 0
            if code in self._restart_codes:
                extra = self._restart_codes[code]
                crash_note = None
                logger.info("Relance de la borne demandée par elle-même.")
                continue

            recent = self._history.record(self._clock())
            delay = restart_delay(recent)
            crash_note = crash_message(code, recent)
            extra = ()
            logger.warning("Borne arrêtée anormalement (%s, %d arrêt(s) en "
                           "%d min) : relance dans %.0f s.",
                           "lancement impossible" if code is None
                           else describe_exit(code),
                           recent, CRASH_WINDOW_S // 60, delay)
            if self._stop.wait(delay):
                break
        logger.info("Gardien arrêté.")
        return 0
