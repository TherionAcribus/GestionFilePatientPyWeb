"""Une seule borne à la fois sur un poste (single_instance).

Régression : rien n'empêchait un second lancement (menu des applications,
raccourci) — deux fenêtres plein écran, une seule avec l'imprimante, et des
statuts contradictoires envoyés au serveur sous le même identifiant.
"""

import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import main
import single_instance
from single_instance import InstanceLock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# Processus enfant qui prend le verrou puis attend (pour être tué).
_HOLDER = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import single_instance
lock = single_instance.InstanceLock(sys.argv[2])
assert lock.acquire()
print("ready", os.getpid(), flush=True)
time.sleep(60)
"""


@pytest.fixture()
def lock_path(tmp_path):
    return tmp_path / "cfg" / single_instance.LOCK_FILENAME


@pytest.fixture()
def holder(lock_path):
    """Une AUTRE borne (processus séparé) détient le verrou."""
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, ROOT, str(lock_path)],
        stdout=subprocess.PIPE, text=True)
    try:
        word, pid = child.stdout.readline().split()
        assert word == "ready"
        # Vrai PID de l'interpréteur (sous Windows, le python d'un venv est
        # un lanceur qui démarre un autre processus).
        child.real_pid = int(pid)
        yield child
    finally:
        child.kill()
        child.wait(10)


# --- Verrou ----------------------------------------------------------------------

def test_second_verrou_refuse_puis_libre_apres_release(lock_path):
    first = InstanceLock(lock_path)
    assert first.acquire() and first.held
    assert InstanceLock(lock_path).acquire() is False
    first.release()
    second = InstanceLock(lock_path)
    assert second.acquire()
    second.release()


def test_autre_processus_detecte_avec_son_pid(holder, lock_path):
    assert single_instance.kiosk_running(lock_path) is True
    assert InstanceLock(lock_path).holder_pid() == holder.real_pid


def test_verrou_repris_apres_plantage_de_la_borne(holder, lock_path):
    """Processus tué (plantage) : le système libère le verrou, aucun
    fichier orphelin ne bloque la borne suivante."""
    holder.kill()
    holder.wait(10)
    lock = InstanceLock(lock_path)
    assert lock.acquire()
    lock.release()
    assert single_instance.kiosk_running(lock_path) is False


def test_attente_pendant_une_relance(lock_path):
    """Relance volontaire : l'ancienne borne libère le verrou un peu après
    le démarrage de la nouvelle, qui attend au lieu de renoncer."""
    old = InstanceLock(lock_path)
    assert old.acquire()
    threading.Timer(0.3, old.release).start()
    new = InstanceLock(lock_path)
    assert new.acquire(wait_s=5, poll_s=0.05)
    new.release()


def test_attente_bornee(lock_path):
    old = InstanceLock(lock_path)
    assert old.acquire()
    started = time.monotonic()
    assert InstanceLock(lock_path).acquire(wait_s=0.3, poll_s=0.05) is False
    assert time.monotonic() - started < 3
    old.release()


def test_dossier_inaccessible_la_borne_demarre_quand_meme(tmp_path):
    """Mieux vaut un risque de doublon qu'une borne qui ne démarre plus."""
    blocker = tmp_path / "fichier"
    blocker.write_text("x")
    assert InstanceLock(blocker / "sous" / "borne.lock").acquire() is True


# --- Configuration / assistant lancés seuls ------------------------------------------

def test_edition_refusee_pendant_le_service(holder, lock_path):
    shown = []
    assert single_instance.editing_blocked(
        lock_path.parent, lambda *a: shown.append(a), "Configuration") is True
    assert shown == [("Configuration", single_instance.EDIT_WHILE_RUNNING_TEXT)]
    assert "F10" in single_instance.EDIT_WHILE_RUNNING_TEXT


def test_edition_permise_borne_arretee(lock_path):
    shown = []
    assert single_instance.editing_blocked(
        lock_path.parent, lambda *a: shown.append(a), "Configuration") is False
    assert shown == []


def test_edition_refusee_meme_sans_affichage(holder, lock_path):
    def no_tk(*_args):
        raise RuntimeError("pas d'affichage")
    assert single_instance.editing_blocked(
        lock_path.parent, no_tk, "Configuration") is True


# --- Démarrage de la borne (main) ------------------------------------------------------

@pytest.fixture()
def kiosk(monkeypatch, lock_path):
    notices = []
    monkeypatch.setattr(main, "_instance_config_path", lambda: lock_path.parent)
    monkeypatch.setattr(main, "_show_notice", lambda *a: notices.append(a))
    return notices


def test_second_lancement_annule_avec_message(kiosk, holder):
    assert main._acquire_kiosk_lock(environ={}) is None
    assert kiosk == [(single_instance.ALREADY_RUNNING_TITLE,
                      single_instance.ALREADY_RUNNING_TEXT)]


def test_premier_lancement_garde_le_verrou(kiosk, lock_path):
    lock = main._acquire_kiosk_lock(environ={})
    try:
        assert lock is not None and lock.held
        # Tant qu'elle tourne, toute autre prise du verrou est refusée.
        assert InstanceLock(lock_path).acquire() is False
        assert kiosk == []
    finally:
        lock.release()


def test_borne_relancee_attend_l_ancienne(kiosk, lock_path, monkeypatch):
    old = InstanceLock(lock_path)
    assert old.acquire()
    threading.Timer(0.3, old.release).start()
    environ = {main.RESTART_ENV: "1"}

    lock = main._acquire_kiosk_lock(environ=environ)

    assert lock is not None
    lock.release()
    assert main.RESTART_ENV not in environ   # pas transmis plus loin
    assert kiosk == []


def test_lancement_manuel_n_attend_pas(kiosk, holder):
    started = time.monotonic()
    assert main._acquire_kiosk_lock(environ={}) is None
    assert time.monotonic() - started < 2


def test_relance_marque_l_environnement(monkeypatch):
    calls = []
    monkeypatch.setattr(main.logging, "shutdown", lambda: None)
    monkeypatch.setattr(main.os, "execve",
                        lambda exe, cmd, env: calls.append(env), raising=False)
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda cmd, env, close_fds: calls.append(env))
    with pytest.raises(SystemExit):
        main._restart_kiosk()
    assert calls and calls[0][main.RESTART_ENV] == "1"


def _main_block(filename):
    with open(os.path.join(ROOT, filename), encoding="utf-8") as handle:
        return handle.read().split("if __name__ == ", 1)[1]


def test_verrou_pris_avant_raccourcis_et_fenetre():
    block = _main_block("main.py")
    lock_at = block.index("_acquire_kiosk_lock()")
    assert lock_at < block.index("_sync_shortcuts()")
    assert lock_at < block.index("_prepare_client()")
    # Les options qui ne lancent pas la borne passent avant (pas de verrou).
    assert block.index("'--selfcheck'") < lock_at
    assert block.count("editing_blocked(") == 2      # --config et --setup


@pytest.mark.parametrize("filename", ["config_editor.py", "setup_wizard.py"])
def test_editeur_et_assistant_seuls_controlent_la_borne(filename):
    block = _main_block(filename)
    assert block.index("editing_blocked(") < block.index("mainloop()" if
                                                          filename == "config_editor.py"
                                                          else "run_setup_wizard()")
