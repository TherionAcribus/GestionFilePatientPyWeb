"""Extinction du PC depuis le menu de maintenance (system_power).

Aucune commande réelle n'est lancée : ``run`` et ``which`` sont simulés."""

import subprocess
import types

import system_power


def _which(found):
    return lambda name: f"/usr/bin/{name}" if name in found else None


def test_commande_linux_par_systemd():
    command = system_power.poweroff_command("linux", _which({"systemctl"}))
    assert command == ["/usr/bin/systemctl", "poweroff", "--no-wall"]
    assert system_power.supported("linux", _which({"systemctl"}))


def test_non_propose_sans_systemd():
    assert system_power.poweroff_command("linux", _which(set())) is None


def test_non_propose_hors_linux():
    """Windows = poste de développement : pas de bouton qui l'éteindrait."""
    for platform in ("win32", "darwin"):
        assert not system_power.supported(platform, _which({"systemctl"}))


def _run(returncode=0, raises=None, calls=None):
    def run(command, **options):
        if calls is not None:
            calls.append((command, options))
        if raises is not None:
            raise raises
        return types.SimpleNamespace(returncode=returncode,
                                     stderr="Access denied")
    return run


def test_extinction_acceptee():
    calls = []
    assert system_power.power_off(_run(calls=calls), command=["poweroff"])
    [(command, options)] = calls
    assert command == ["poweroff"]
    assert options["timeout"] == system_power.POWEROFF_TIMEOUT_S
    assert options["check"] is False


def test_extinction_refusee(caplog):
    assert not system_power.power_off(_run(returncode=1), command=["poweroff"])
    assert "refusée" in caplog.text and "Access denied" in caplog.text


def test_commande_introuvable_ou_bloquee():
    for exc in (OSError("absent"), subprocess.TimeoutExpired("x", 15)):
        assert not system_power.power_off(_run(raises=exc),
                                          command=["poweroff"])


def test_systeme_non_pris_en_charge(monkeypatch):
    monkeypatch.setattr(system_power, "poweroff_command", lambda: None)
    calls = []
    assert not system_power.power_off(_run(calls=calls))
    assert calls == []
