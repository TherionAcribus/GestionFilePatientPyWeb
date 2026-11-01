"""Écran de la borne toujours allumé, jamais verrouillé (screen_awake).

Régression : rien n'empêchait le bureau d'éteindre l'écran puis de verrouiller
la session (Lubuntu : xscreensaver) après quelques minutes sans patient.
Les commandes système sont simulées : ces tests vérifient CE QUI est demandé
au système, pas son effet réel (à valider sur la borne).
"""

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import screen_awake
from screen_awake import ScreenAwake

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

XSET_Q = """Keyboard Control:
  auto repeat:  on    key click percent:  0    LED mask:  00000000
Screen Saver:
  prefer blanking:  yes    allow exposures:  yes
  timeout:  600    cycle:  600
DPMS (Energy Star):
  Standby: 600    Suspend: 600    Off: 600
  DPMS is Enabled
  Monitor is On
"""

LUBUNTU = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "LXQt"}
ALL_TOOLS = {"xset", "xscreensaver-command", "dbus-send"}


class _System:
    """Doublure de subprocess.run + shutil.which."""
    def __init__(self, tools=ALL_TOOLS, fail=(), raise_on=()):
        self.tools = set(tools)
        self.fail = set(fail)
        self.raise_on = set(raise_on)
        self.commands = []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, command):
        self.commands.append(command)
        if command[0] in self.raise_on:
            raise subprocess.TimeoutExpired(command, 5)
        code = 1 if command[0] in self.fail else 0
        stdout = XSET_Q if command[:2] == ["xset", "q"] else ""
        return subprocess.CompletedProcess(command, code, stdout, "")


def _awake(system, environ=LUBUNTU, platform="linux", period_s=3600):
    return ScreenAwake(run=system.run, which=system.which, environ=environ,
                       platform=platform, period_s=period_s)


# --- Lecture / restauration des réglages X --------------------------------------------

def test_lecture_des_reglages_xset():
    assert screen_awake.parse_xset_query(XSET_Q) == {
        "timeout": 600, "cycle": 600, "blanking": True, "dpms": True}
    assert screen_awake.parse_xset_query("rien") is None


def test_commande_de_restauration():
    assert screen_awake.restore_command(
        {"timeout": 600, "cycle": 600, "blanking": True, "dpms": True}) == [
        "xset", "s", "600", "600", "s", "blank", "+dpms"]
    assert screen_awake.restore_command(
        {"timeout": 0, "cycle": 0, "blanking": False, "dpms": False})[-2:] == [
        "noblank", "-dpms"]


# --- Démarrage ------------------------------------------------------------------------------

def test_veille_x_coupee_et_compteurs_remis_a_zero():
    system = _System()
    awake = _awake(system)
    try:
        assert awake.start() is True
        assert ["xset", "s", "off", "s", "noblank", "-dpms"] in system.commands
        # Premier rappel d'activité immédiat : xset, xscreensaver (verrou
        # de Lubuntu), puis économiseurs D-Bus (freedesktop, Cinnamon).
        assert ["xset", "s", "reset"] in system.commands
        assert ["xscreensaver-command", "-deactivate"] in system.commands
        destinations = [c[3] for c in system.commands if c[0] == "dbus-send"]
        assert destinations == ["--dest=org.freedesktop.ScreenSaver",
                                "--dest=org.cinnamon.ScreenSaver"]
    finally:
        awake.stop()


@pytest.mark.parametrize("environ,platform", [
    (LUBUNTU, "win32"),
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
      "DISPLAY": ":0"}, "linux"),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux"),
    ({}, "linux"),
])
def test_rien_hors_session_x11(environ, platform):
    system = _System()
    awake = _awake(system, environ=environ, platform=platform)
    assert awake.start() is False
    awake.stop()
    assert system.commands == []


def test_outils_absents_jamais_bloquant():
    """Poste sans xscreensaver ni dbus-send : seul xset est utilisé."""
    system = _System(tools={"xset"})
    awake = _awake(system)
    try:
        assert awake.start() is True
        assert {c[0] for c in system.commands} == {"xset"}
    finally:
        awake.stop()


def test_echecs_et_delais_jamais_bloquants():
    system = _System(fail={"xscreensaver-command"}, raise_on={"dbus-send"})
    awake = _awake(system)
    try:
        assert awake.start() is True
        awake.poke()
    finally:
        awake.stop()


def test_rappel_periodique():
    system = _System()
    awake = _awake(system, period_s=0.05)
    try:
        awake.start()
        deadline = time.monotonic() + 3
        while (system.commands.count(["xset", "s", "reset"]) < 3
               and time.monotonic() < deadline):
            time.sleep(0.02)
    finally:
        awake.stop()
    assert system.commands.count(["xset", "s", "reset"]) >= 3


# --- Arrêt ------------------------------------------------------------------------------------

def test_reglages_d_origine_retablis_a_l_arret():
    system = _System()
    awake = _awake(system)
    awake.start()
    awake.stop()
    assert system.commands[-1] == ["xset", "s", "600", "600", "s", "blank", "+dpms"]
    count = len(system.commands)
    awake.stop()                                 # second arrêt : sans effet
    assert len(system.commands) == count


def test_plus_de_rappel_apres_l_arret():
    system = _System()
    awake = _awake(system, period_s=0.02)
    awake.start()
    awake.stop()
    count = len(system.commands)
    time.sleep(0.1)
    assert len(system.commands) == count


def test_reglages_illisibles_rien_a_retablir():
    system = _System(fail={"xset"})
    awake = _awake(system)
    awake.start()
    count = len(system.commands)
    awake.stop()
    assert len(system.commands) == count


# --- Câblage ---------------------------------------------------------------------------------

def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_branche_au_demarrage_de_la_borne_seulement():
    block = _read("main.py").split("if __name__ == ", 1)[1]
    start_at = block.index("_screen.start()")
    assert block.index("_acquire_kiosk_lock()") < start_at   # pas pour un doublon
    assert block.index("_run_supervisor()") < start_at        # ni pour le gardien
    assert start_at < block.index("_prepare_client()")
    assert "atexit.register(_screen.stop)" in block


def test_relance_par_execve_retablit_d_abord_l_ecran():
    restart = _read("main.py").split("def _restart_kiosk", 1)[1].split("\ndef ", 1)[0]
    assert restart.index("_screen.stop()") < restart.index("os.execve")


def test_install_sh_connexion_automatique_et_xset():
    script = _read("install.sh")
    assert "x11-xserver-utils" in script
    assert "/etc/sddm.conf.d/50-flowelis-autologin.conf" in script      # Lubuntu
    assert "/etc/lightdm/lightdm.conf.d/50-flowelis-autologin.conf" in script  # Mint
    assert "FLOWELIS_AUTOLOGIN" in script and "read -r -p" in script    # demandé
