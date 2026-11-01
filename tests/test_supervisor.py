"""Gardien de la borne (``--supervise``) : relance après un arrêt anormal.

Régression : le démarrage automatique lançait la borne une seule fois ; un
plantage laissait le bureau visible des patients jusqu'à une relance à la main.
"""

import os
import signal
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import main
import supervisor
from supervisor import EXIT_RESTART, EXIT_RESTART_MAINTENANCE, Supervisor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def fast_delays(monkeypatch):
    monkeypatch.setattr(supervisor, "RESTART_DELAY_BASE_S", 0.01)
    monkeypatch.setattr(supervisor, "RESTART_DELAY_MAX_S", 0.05)


# --- Politique pure ----------------------------------------------------------------

def test_description_des_sorties():
    assert supervisor.describe_exit(1) == "code 1"
    assert supervisor.describe_exit(-signal.SIGTERM) == "signal SIGTERM"
    assert supervisor.describe_exit(-999) == "signal 999"


def test_delai_double_puis_plafonne(monkeypatch):
    monkeypatch.setattr(supervisor, "RESTART_DELAY_BASE_S", 2.0)
    monkeypatch.setattr(supervisor, "RESTART_DELAY_MAX_S", 300.0)
    assert [supervisor.restart_delay(n) for n in (1, 2, 3, 4)] == [2, 4, 8, 16]
    assert supervisor.restart_delay(20) == 300


def test_plantages_comptes_sur_10_minutes():
    history = supervisor.CrashHistory(window_s=600)
    assert history.record(0) == 1
    assert history.record(100) == 2
    assert history.record(650) == 2      # celui de t=0 est sorti de la fenêtre
    assert history.record(5000) == 1


def test_message_au_serveur():
    assert supervisor.crash_message(-11, 1) == (
        "Borne relancée automatiquement après un arrêt anormal "
        "(signal SIGSEGV).")
    assert "3 arrêts en 10 min" in supervisor.crash_message(1, 3)


# --- Gardien (processus simulé) --------------------------------------------------------

class _FakeChild:
    def __init__(self, code, on_wait=None):
        self.code = code
        self.terminated = False
        self.on_wait = on_wait

    def wait(self):
        if self.on_wait:
            self.on_wait()
        return self.code

    def terminate(self):
        self.terminated = True


class _Launcher:
    """Doublure de Popen : chaque lancement rend le code suivant."""
    def __init__(self, codes, on_wait=None):
        self.codes = list(codes)
        self.launches = []
        self.children = []
        self.on_wait = on_wait

    def __call__(self, command, env):
        self.launches.append((command, dict(env)))
        child = _FakeChild(self.codes.pop(0), self.on_wait)
        self.children.append(child)
        return child


def _guard(launcher, clock=None):
    return Supervisor(
        lambda extra: ["borne", *extra],
        restart_codes={EXIT_RESTART: (),
                       EXIT_RESTART_MAINTENANCE: ("--maintenance",)},
        base_env={"PATH": "x", supervisor.CRASH_ENV: "ancien"},
        popen=launcher, clock=clock or (lambda: 0.0))


def test_sortie_normale_arrete_le_gardien():
    launcher = _Launcher([0])
    assert _guard(launcher).run() == 0
    assert len(launcher.launches) == 1
    command, env = launcher.launches[0]
    assert command == ["borne"]
    assert env[supervisor.SUPERVISED_ENV] == "1"
    assert supervisor.CRASH_ENV not in env      # rien d'hérité


def test_plantage_relance_avec_la_cause():
    launcher = _Launcher([-11, 0])
    _guard(launcher).run()
    assert len(launcher.launches) == 2
    _command, env = launcher.launches[1]
    assert "SIGSEGV" in env[supervisor.CRASH_ENV]


def test_relances_demandees_par_la_borne():
    """Configuration puis Alt+F4 : relances immédiates, avec ou sans le
    menu de maintenance, et sans « arrêt anormal » annoncé."""
    launcher = _Launcher([EXIT_RESTART, EXIT_RESTART_MAINTENANCE, 0])
    _guard(launcher).run()
    commands = [c for c, _env in launcher.launches]
    assert commands == [["borne"], ["borne"], ["borne", "--maintenance"]]
    assert all(supervisor.CRASH_ENV not in env for _c, env in launcher.launches)


def test_plantages_en_serie_espaces(monkeypatch):
    delays = []
    monkeypatch.setattr(supervisor, "restart_delay",
                        lambda n: delays.append(n) or 0)
    launcher = _Launcher([1, 1, 1, 0])
    _guard(launcher).run()
    assert delays == [1, 2, 3]
    assert "3 arrêts" in launcher.launches[3][1][supervisor.CRASH_ENV]


def test_lancement_impossible_reessaye(monkeypatch):
    calls = []

    def popen(command, env):
        calls.append(command)
        if len(calls) == 1:
            raise OSError("binaire introuvable")
        return _FakeChild(0)

    _guard(popen).run()
    assert len(calls) == 2


def test_fin_de_session_pas_de_relance():
    """SIGTERM (fin de session, arrêt du PC) : la borne est arrêtée et le
    gardien ne la relance pas, même si elle sort en erreur."""
    holder = {}
    launcher = _Launcher([-15], on_wait=lambda: holder["guard"].stop(15))
    guard = _guard(launcher)
    holder["guard"] = guard
    assert guard.run() == 0
    assert len(launcher.launches) == 1


def test_stop_arrete_la_borne_en_cours():
    started = threading.Event()
    release = threading.Event()
    launcher = _Launcher([1], on_wait=lambda: (started.set(), release.wait(5)))
    guard = _guard(launcher)
    runner = threading.Thread(target=guard.run)
    runner.start()
    assert started.wait(5)
    guard.stop()
    release.set()
    runner.join(5)
    assert launcher.children[0].terminated
    assert len(launcher.launches) == 1


# --- Vrai processus ----------------------------------------------------------------------

_CHILD = r"""
import os, sys
marker = sys.argv[1]
if not os.path.exists(marker):
    open(marker, "w").close()
    os._exit(3)                      # premier lancement : arrêt anormal
with open(marker, "w", encoding="utf-8") as f:
    f.write(os.environ.get("FLOWELIS_BORNE_CRASH", ""))
sys.exit(0)
"""


def test_vrai_processus_plante_puis_relance(tmp_path):
    marker = tmp_path / "lance"
    guard = Supervisor(
        lambda extra: [sys.executable, "-c", _CHILD, str(marker), *extra],
        restart_codes={EXIT_RESTART: ()}, base_env=dict(os.environ))
    assert guard.run() == 0
    assert guard.launches == 2
    assert "code 3" in marker.read_text(encoding="utf-8")


# --- Câblage dans main -----------------------------------------------------------------

def test_borne_supervisee_demande_la_relance_au_gardien(monkeypatch):
    launched = []
    monkeypatch.setenv(supervisor.SUPERVISED_ENV, "1")
    monkeypatch.setattr(main.logging, "shutdown", lambda: None)
    monkeypatch.setattr(main.os, "execve", lambda *a: launched.append(a),
                        raising=False)
    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: launched.append(a))

    with pytest.raises(SystemExit) as plain:
        main._restart_kiosk([])
    with pytest.raises(SystemExit) as with_menu:
        main._restart_kiosk([main.MAINTENANCE_OPTION])

    assert plain.value.code == EXIT_RESTART
    assert with_menu.value.code == EXIT_RESTART_MAINTENANCE
    assert launched == []                # elle ne se relance plus elle-même


def test_option_du_gardien_jamais_transmise_a_la_borne():
    command = main._restart_command(
        argv=["FlowelisBorne", "--supervise", "--foo"],
        executable="FlowelisBorne", frozen=True, extra=["--maintenance"])
    assert command == ["FlowelisBorne", "--foo", "--maintenance"]


def _main_block():
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as handle:
        return handle.read().split("if __name__ == ", 1)[1]


def test_gardien_avant_capture_de_plantage_et_verrou():
    """Le gardien ne prend ni crash.log (celui de la borne) ni le verrou
    d'instance (que la borne lancée doit pouvoir prendre)."""
    block = _main_block()
    supervise_at = block.index("_run_supervisor()")
    assert supervise_at < block.index("crash_guard.install(")
    assert supervise_at < block.index("_acquire_kiosk_lock()")


def test_cause_du_plantage_remontee_au_serveur():
    block = _main_block()
    assert "os.environ.pop(supervisor.CRASH_ENV" in block
    assert "supervisor.SERVER_EVENT_CRASH_RESTART" in block
