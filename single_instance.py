"""Une seule borne à la fois sur un poste.

Deux bornes lancées ensemble (démarrage automatique + menu des applications,
par exemple) ouvraient deux fenêtres plein écran : une seule pouvait ouvrir
l'imprimante (le ticket d'un patient tombé sur l'autre ne sortait pas), et les
deux envoyaient leurs statuts au serveur sous le même identifiant (état de
l'imprimante qui alternait « OK » / « en erreur » côté admin et comptoirs).

Mécanisme : un verrou de fichier du SYSTÈME (``flock`` sous Linux,
``msvcrt.locking`` sous Windows) sur ``borne.lock``, dans le dossier de
configuration. Le système le libère seul quand le processus se termine, même
sur un plantage : jamais de verrou orphelin à nettoyer. Le fichier est ouvert
non héritable : une relance par ``execve`` (même PID) le libère aussi, et la
nouvelle image le reprend.

Si le verrou ne peut pas être créé (dossier inaccessible), la borne démarre
quand même : mieux vaut un risque de doublon qu'une borne qui ne démarre plus.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("borne.single_instance")

LOCK_FILENAME = "borne.lock"

#: Relance volontaire (configuration, menu de maintenance) : sous Windows la
#: nouvelle borne démarre AVANT la fin de l'ancienne ; elle attend donc que
#: celle-ci libère le verrou.
RESTART_WAIT_S = 15.0

if os.name == "nt":
    import msvcrt

    def _try_lock(fd) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

# Contenu du fichier : un octet (le seul verrouillé sous Windows, où une zone
# verrouillée ne peut plus être lue par les autres) puis le PID du détenteur,
# lisible pour le journal.
_PID_OFFSET = 1


class InstanceLock:
    """Verrou « borne en service » sur ``path``."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, wait_s=0.0, poll_s=0.2, clock=time.monotonic,
                sleep=time.sleep) -> bool:
        """Prend le verrou ; True si c'est fait (ou s'il ne peut pas
        exister), False si une autre borne le détient encore après
        ``wait_s`` secondes."""
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            logger.exception("Verrou d'instance impossible à créer (%s) : la "
                             "borne démarre sans protection contre un double "
                             "lancement.", self.path)
            return True
        deadline = clock() + max(0.0, wait_s)
        while not _try_lock(fd):
            if clock() >= deadline:
                os.close(fd)
                return False
            sleep(poll_s)
        self._fd = fd
        self._write_pid()
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            _unlock(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _write_pid(self) -> None:
        # Information de journal seulement : un échec ne compte pas.
        with contextlib.suppress(OSError):
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, b" " + str(os.getpid()).encode("ascii"))
            os.ftruncate(self._fd, os.lseek(self._fd, 0, os.SEEK_CUR))

    def holder_pid(self) -> int | None:
        """PID de la borne qui détient le verrou, si lisible."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(_PID_OFFSET)
                return int(handle.read(32).strip() or b"0") or None
        except (OSError, ValueError):
            return None


def kiosk_running(path) -> bool:
    """Une borne est-elle en service (verrou détenu par un autre
    processus) ? Prend puis rend aussitôt le verrou s'il est libre."""
    probe = InstanceLock(path)
    if not probe.acquire():
        return True
    probe.release()
    return False


#: Message affiché au technicien qui relance une borne déjà en service.
ALREADY_RUNNING_TITLE = "Borne déjà en service"
ALREADY_RUNNING_TEXT = (
    "La borne Flowelis est déjà en service sur ce poste : ce second "
    "lancement est annulé.\n\n"
    "Pour la configurer ou la quitter : touche F10 sur la borne, puis "
    "« Ouvrir la configuration » ou « Quitter l'application ».")

#: Configuration ou assistant ouverts pendant que la borne tourne.
EDIT_WHILE_RUNNING_TEXT = (
    "La borne Flowelis est en service sur ce poste : la configuration ne "
    "peut pas être modifiée en parallèle (elle ne serait pas prise en "
    "compte, et le test d'imprimante entrerait en conflit avec la borne).\n\n"
    "Utilisez la touche F10 sur la borne, puis « Ouvrir la configuration ».")


def editing_blocked(config_path, notify, title) -> bool:
    """Configuration ou assistant lancés SEULS (``--config``, ``--setup``,
    raccourcis du bureau) pendant que la borne tourne : refusés, avec un
    message au technicien (``notify(title, text)``). True si refusé.

    Ouverts par la borne elle-même (F10, premier lancement), ils tournent
    dans son processus, qui détient le verrou : ce contrôle ne les concerne
    pas."""
    if not kiosk_running(Path(config_path) / LOCK_FILENAME):
        return False
    logger.warning("%s refusée : la borne est en service sur ce poste.", title)
    try:
        notify(title, EDIT_WHILE_RUNNING_TEXT)
    except Exception:
        # FRONTIÈRE Tk : sans fenêtre, le refus reste au moins journalisé.
        logger.exception("Message « %s » impossible à afficher.", title)
    return True
