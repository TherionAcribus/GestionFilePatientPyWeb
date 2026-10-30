"""Extinction du PC de la borne (menu de maintenance, « Éteindre le PC »).

Linux uniquement, comme les raccourcis de la borne (``desktop_integration``) :
``systemctl poweroff`` passe par systemd-logind, qui l'autorise sans mot de
passe à l'utilisateur de la session graphique locale (règle polkit par
défaut des distributions de bureau). Ailleurs (Windows : poste de
développement), le bouton n'est pas proposé.

La commande rend la main aussitôt : l'arrêt du système se fait ensuite. Un
refus (droits, autre session ouverte…) est rapporté à l'appelant, qui
relance alors la borne plutôt que la laisser arrêtée.
"""

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger("borne.power")

#: Délai maximal accordé à la commande (elle ne fait que demander l'arrêt).
POWEROFF_TIMEOUT_S = 15


def poweroff_command(platform=None, which=shutil.which):
    """Commande d'extinction pour ``platform`` (``sys.platform`` par
    défaut), ou None si ce système n'est pas pris en charge."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return None
    systemctl = which("systemctl")
    if not systemctl:
        return None
    # --no-wall : pas de message diffusé aux terminaux ouverts.
    return [systemctl, "poweroff", "--no-wall"]


def supported(platform=None, which=shutil.which):
    """« Éteindre le PC » est-il proposable sur ce système ?"""
    return poweroff_command(platform, which) is not None


def power_off(run=subprocess.run, command=None):
    """Demande l'extinction du PC ; True si le système l'a acceptée."""
    command = command if command is not None else poweroff_command()
    if command is None:
        logger.error("Extinction du PC non prise en charge sur ce système.")
        return False
    logger.info("Extinction du PC demandée depuis le menu de maintenance.")
    try:
        # Commande construite par poweroff_command (chemin absolu de
        # systemctl, arguments fixes) : aucune donnée extérieure.
        result = run(command, capture_output=True, text=True,  # nosec B603
                     timeout=POWEROFF_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Extinction du PC impossible : %s", exc)  # noqa: TRY400
        return False
    if result.returncode != 0:
        logger.error("Extinction du PC refusée par le système (code %s) : %s",
                     result.returncode, (result.stderr or "").strip()[:300])
        return False
    return True
