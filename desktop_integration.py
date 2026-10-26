"""Raccourcis système de la borne sous Linux (fichiers .desktop XDG).

Le binaire de release ne passe pas par ``install.sh`` : il crée donc
lui-même, au lancement, ce que le script posait —

- le **démarrage automatique** à l'ouverture de session ;
- l'**icône** Flowelis (thème hicolor) ;
- un raccourci **« Configuration »** sur le bureau ;

et en plus trois entrées dans le **menu des applications** : la borne,
sa configuration (``--config``) et l'assistant d'installation
(``--setup``). Les noms de fichiers sont ceux d'``install.sh`` : pas de
doublon sur une borne installée par le script.

Règles :

- **automatique seulement pour le binaire** (``sys.frozen``) : lancée depuis
  les sources (IDE), la borne ne touche pas au poste de développement ;
- un fichier qu'on a **créé** est mis à jour si besoin (binaire déplacé ou
  renommé à chaque version) ; un démarrage automatique **désactivé** par
  l'utilisateur le reste ;
- un fichier **supprimé** par l'utilisateur n'est pas recréé ;
- un fichier **qui n'est pas de nous** (écrit à la main, ou par
  ``install.sh``) n'est jamais écrasé ;
- ``--install-shortcuts`` force la (re)création, ``--remove-shortcuts``
  retire nos fichiers.

Les fonctions de planification sont pures (chemins et contenus) ; seules
``install_shortcuts`` / ``remove_shortcuts`` touchent au disque.
"""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("borne.desktop")

#: Marqueur des fichiers créés par la borne : seuls ceux-là sont mis à jour
#: ou supprimés automatiquement.
MANAGED_KEY = "X-Flowelis-Managed"
ICON_NAME = "flowelis"
STATE_FILENAME = "desktop_integration.json"

_AUTOSTART_ENABLED = "X-GNOME-Autostart-enabled"


@dataclass(frozen=True)
class Entry:
    """Un fichier .desktop à poser."""

    path: Path
    content: str
    #: Raccourci de bureau : exécutable + « de confiance » (GNOME).
    on_desktop: bool = False


# ---------------------------------------------------------------------------
# Planification (pur)
# ---------------------------------------------------------------------------

def _exec_quote(arg: str) -> str:
    """Argument de la clé Exec, quoté selon la spécification XDG si besoin.

    « % » introduit un code de champ (%f, %u…) : il est doublé partout."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = (arg.replace("\\", "\\\\").replace('"', '\\"')
               .replace("`", "\\`").replace("$", "\\$"))
    # Dans un fichier .desktop, la barre oblique inverse est elle-même
    # échappée une seconde fois (valeur de type string).
    return '"' + escaped.replace("\\", "\\\\") + '"'


def exec_line(command) -> str:
    """Valeur de la clé Exec pour ``command`` (liste d'arguments)."""
    return " ".join(_exec_quote(str(a)) for a in command)


def app_command(executable=None, frozen=None, script=None) -> list:
    """Commande qui lance la borne : le binaire, ou l'interpréteur sur
    main.py quand on tourne depuis les sources."""
    executable = sys.executable if executable is None else executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return [executable]
    script = script or str(Path(__file__).resolve().parent / "main.py")
    return [executable, script]


def _desktop_file(name, comment, command, categories=None, extra=()):
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={name}",
        f"Comment={comment}",
        f"Exec={exec_line(command)}",
        f"Icon={ICON_NAME}",
        "Terminal=false",
    ]
    if categories:
        lines.append(f"Categories={categories}")
    lines.extend(extra)
    lines.append(f"{MANAGED_KEY}=true")
    return "\n".join(lines) + "\n"


def plan_entries(command, config_home: Path, data_home: Path,
                 desktop_dir: Path | None) -> list:
    """Fichiers .desktop à poser pour la commande ``command``."""
    applications = data_home / "applications"
    entries = [
        Entry(config_home / "autostart" / "flowelis-borne.desktop",
              _desktop_file("Flowelis Borne",
                            "Borne patient Flowelis (démarrage automatique)",
                            command,
                            extra=[f"{_AUTOSTART_ENABLED}=true"])),
        Entry(applications / "flowelis-borne.desktop",
              _desktop_file("Flowelis Borne", "Borne patient Flowelis",
                            command, categories="Office;")),
        Entry(applications / "flowelis-borne-configuration.desktop",
              _desktop_file("Flowelis Borne — Configuration",
                            "Serveur, secret, imprimante et options de la borne",
                            [*command, "--config"], categories="Settings;")),
        Entry(applications / "flowelis-borne-assistant.desktop",
              _desktop_file("Flowelis Borne — Assistant d'installation",
                            "Configurer la borne pas à pas",
                            [*command, "--setup"], categories="Settings;")),
    ]
    if desktop_dir is not None:
        entries.append(Entry(
            desktop_dir / "flowelis-configuration.desktop",
            _desktop_file("Configuration Flowelis Borne",
                          "Modifier l'URL du serveur, le secret et l'imprimante",
                          [*command, "--config"]),
            on_desktop=True))
    return entries


def is_managed(content: str) -> bool:
    return f"{MANAGED_KEY}=true" in content.splitlines()


def keep_user_choices(existing: str, desired: str) -> str:
    """Contenu à écrire en conservant un démarrage automatique désactivé
    par l'utilisateur (``X-GNOME-Autostart-enabled=false``)."""
    disabled = f"{_AUTOSTART_ENABLED}=false"
    if disabled in existing.splitlines():
        return desired.replace(f"{_AUTOSTART_ENABLED}=true", disabled)
    return desired


def plan_actions(entries, existing: dict, created_before: set,
                 force: bool = False) -> list:
    """Décide quoi écrire : ``[(Entry, contenu, raison)]``.

    ``existing`` : contenu actuel des fichiers présents (chemin -> texte).
    ``created_before`` : fichiers que la borne a déjà créés (état)."""
    actions = []
    for entry in entries:
        current = existing.get(entry.path)
        if current is None:
            if entry.path in created_before and not force:
                continue            # supprimé par l'utilisateur : respecté
            actions.append((entry, entry.content, "création"))
            continue
        if not is_managed(current) and not force:
            continue                # pas de nous (manuel, install.sh)
        content = keep_user_choices(current, entry.content)
        if content != current:
            actions.append((entry, content, "mise à jour"))
    return actions


# ---------------------------------------------------------------------------
# Disque
# ---------------------------------------------------------------------------

def _xdg_dir(variable: str, default: Path) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else default


def _desktop_dir(home: Path) -> Path | None:
    """Bureau de l'utilisateur (xdg-user-dir), s'il existe et n'est pas le
    dossier personnel lui-même."""
    path = None
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                             text=True, timeout=5, check=False)
        if out.returncode == 0 and out.stdout.strip():
            path = Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        path = None
    path = path or home / "Desktop"
    if path.is_dir() and path.resolve() != home.resolve():
        return path
    return None


def _read_state(state_path: Path) -> set:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return {Path(p) for p in data.get("created", [])}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def _write_state(state_path: Path, created: set) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"created": sorted(str(p) for p in created)}, indent=2),
            encoding="utf-8")
    except OSError:
        logger.warning("État des raccourcis non enregistré (%s).", state_path)


def _install_icon(icon_source: Path, data_home: Path) -> None:
    target = data_home / "icons" / "hicolor" / "256x256" / "apps" / f"{ICON_NAME}.png"
    try:
        if target.is_file() and target.read_bytes() == icon_source.read_bytes():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon_source, target)
    except OSError:
        logger.warning("Icône Flowelis non installée.", exc_info=True)
        return
    # Outil absent : l'icône sera trouvée au prochain rafraîchissement.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["gtk-update-icon-cache", "-q",
                        str(data_home / "icons" / "hicolor")],
                       capture_output=True, timeout=10, check=False)


def _trust_desktop_file(path: Path) -> None:
    """Rend un raccourci de bureau lançable (exécutable + « de confiance »
    pour GNOME). Sans effet bloquant si l'outil manque."""
    try:
        path.chmod(0o755)
    except OSError:
        logger.warning("Raccourci %s non rendu exécutable.", path)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["gio", "set", str(path), "metadata::trusted", "true"],
                       capture_output=True, timeout=5, check=False)


def _locations(home=None):
    home = Path.home() if home is None else home
    config_home = _xdg_dir("XDG_CONFIG_HOME", home / ".config")
    data_home = _xdg_dir("XDG_DATA_HOME", home / ".local" / "share")
    return home, config_home, data_home


def supported() -> bool:
    """Les raccourcis XDG n'ont de sens que sous Linux."""
    return sys.platform.startswith("linux")


def install_shortcuts(state_path: Path, icon_source: Path, force=False,
                      command=None, home=None, desktop_dir="auto") -> list:
    """Crée ou met à jour les raccourcis ; renvoie la liste des fichiers
    écrits. Ne lève pas : un échec de raccourci ne doit jamais empêcher la
    borne de démarrer."""
    home, config_home, data_home = _locations(home)
    if desktop_dir == "auto":
        desktop_dir = _desktop_dir(home)
    entries = plan_entries(command or app_command(), config_home, data_home,
                           desktop_dir)
    existing = {}
    for entry in entries:
        try:
            if entry.path.is_file():
                existing[entry.path] = entry.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing[entry.path] = ""     # illisible : traité comme non géré
    created = _read_state(state_path)

    written = []
    for entry, content, reason in plan_actions(entries, existing, created, force):
        try:
            entry.path.parent.mkdir(parents=True, exist_ok=True)
            entry.path.write_text(content, encoding="utf-8")
        except OSError:
            logger.warning("Raccourci non écrit : %s", entry.path, exc_info=True)
            continue
        if entry.on_desktop:
            _trust_desktop_file(entry.path)
        created.add(entry.path)
        written.append(entry.path)
        logger.info("Raccourci (%s) : %s", reason, entry.path)

    if written and icon_source.is_file():
        _install_icon(icon_source, data_home)
    _write_state(state_path, created)
    return written


def remove_shortcuts(state_path: Path, home=None, desktop_dir="auto") -> list:
    """Supprime les raccourcis créés par la borne (jamais ceux d'autrui) ;
    renvoie la liste des fichiers supprimés."""
    home, config_home, data_home = _locations(home)
    if desktop_dir == "auto":
        desktop_dir = _desktop_dir(home)
    removed = []
    for entry in plan_entries(app_command(), config_home, data_home, desktop_dir):
        try:
            if entry.path.is_file() and is_managed(
                    entry.path.read_text(encoding="utf-8")):
                entry.path.unlink()
                removed.append(entry.path)
                logger.info("Raccourci supprimé : %s", entry.path)
        except (OSError, UnicodeDecodeError):
            logger.warning("Raccourci non supprimé : %s", entry.path)
    # On repart de zéro : une réinstallation explicite recréera tout.
    _write_state(state_path, set())
    return removed
