"""Raccourcis système créés par le binaire (fichiers .desktop XDG)."""

import os
import shlex
import types

import pytest

import desktop_integration as di
import main

BINARY = ["/opt/flowelis/FlowelisBorne-v1.0.0"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    desktop = home / "Bureau"
    desktop.mkdir(parents=True)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    icon = tmp_path / "flowelis.png"
    icon.write_bytes(b"\x89PNG-icone")
    state = tmp_path / "state" / di.STATE_FILENAME
    return types.SimpleNamespace(home=home, desktop=desktop, icon=icon,
                                 state=state)


def _install(env, command=BINARY, force=False):
    return di.install_shortcuts(env.state, env.icon, force=force,
                                command=command, home=env.home,
                                desktop_dir=env.desktop)


def _autostart(env):
    return env.home / ".config" / "autostart" / "flowelis-borne.desktop"


def _read(path):
    return path.read_text(encoding="utf-8")


# --- Ligne Exec ---------------------------------------------------------------------

def _parse_exec(value):
    """Relecture selon la spécification : déséchappement « string » puis
    découpage façon shell."""
    return shlex.split(value.replace("\\\\", "\\"))


def test_exec_simple_non_quote():
    assert di.exec_line(["/opt/FlowelisBorne", "--config"]) == \
        "/opt/FlowelisBorne --config"


@pytest.mark.parametrize("path", [
    "/home/borne/Mes Programmes/FlowelisBorne",
    '/opt/a"b/FlowelisBorne',
    "/opt/a\\b/FlowelisBorne",
])
def test_exec_quote_et_relu_a_l_identique(path):
    line = di.exec_line([path, "--setup"])
    assert _parse_exec(line) == [path, "--setup"]


def test_exec_double_les_pourcents():
    assert "%%" in di.exec_line(["/opt/100%/FlowelisBorne"])


def test_commande_du_binaire_et_des_sources():
    assert di.app_command(executable="/opt/FB", frozen=True) == ["/opt/FB"]
    command = di.app_command(executable="/venv/bin/python", frozen=False)
    assert command[0] == "/venv/bin/python"
    assert command[1].endswith("main.py")


# --- Création ---------------------------------------------------------------------------

def test_creation_de_tous_les_raccourcis(env):
    written = _install(env)
    apps = env.home / ".local" / "share" / "applications"
    expected = {
        _autostart(env),
        apps / "flowelis-borne.desktop",
        apps / "flowelis-borne-configuration.desktop",
        apps / "flowelis-borne-assistant.desktop",
        env.desktop / "flowelis-configuration.desktop",
    }
    assert set(written) == expected
    assert "Exec=/opt/flowelis/FlowelisBorne-v1.0.0\n" in _read(_autostart(env))
    assert "X-GNOME-Autostart-enabled=true" in _read(_autostart(env))
    assert "--config" in _read(apps / "flowelis-borne-configuration.desktop")
    assert "--setup" in _read(apps / "flowelis-borne-assistant.desktop")
    for path in expected:
        assert di.is_managed(_read(path))
    icon = (env.home / ".local" / "share" / "icons" / "hicolor" / "256x256"
            / "apps" / "flowelis.png")
    assert icon.read_bytes() == env.icon.read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="bit exécutable POSIX")
def test_raccourci_de_bureau_executable(env):
    _install(env)
    mode = (env.desktop / "flowelis-configuration.desktop").stat().st_mode
    assert mode & 0o111


def test_pas_de_bureau_pas_de_raccourci_de_bureau(env):
    written = di.install_shortcuts(env.state, env.icon, command=BINARY,
                                   home=env.home, desktop_dir=None)
    assert all(env.desktop not in p.parents for p in written)


def test_dossiers_xdg_respectes(env, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _install(env)
    assert (tmp_path / "cfg" / "autostart" / "flowelis-borne.desktop").is_file()
    assert (tmp_path / "data" / "applications" / "flowelis-borne.desktop").is_file()


# --- Lancements suivants ------------------------------------------------------------------

def test_relancement_sans_changement_n_ecrit_rien(env):
    _install(env)
    assert _install(env) == []


def test_binaire_deplace_met_a_jour_les_raccourcis(env):
    _install(env)
    new = ["/opt/flowelis/FlowelisBorne-v1.1.0"]
    written = _install(env, command=new)
    assert _autostart(env) in written
    assert "FlowelisBorne-v1.1.0" in _read(_autostart(env))


def test_demarrage_automatique_desactive_reste_desactive(env):
    _install(env)
    path = _autostart(env)
    path.write_text(_read(path).replace("enabled=true", "enabled=false"),
                    encoding="utf-8")
    _install(env, command=["/opt/flowelis/FlowelisBorne-v1.1.0"])
    content = _read(path)
    assert "X-GNOME-Autostart-enabled=false" in content
    assert "FlowelisBorne-v1.1.0" in content


def test_raccourci_supprime_par_l_utilisateur_non_recree(env):
    _install(env)
    _autostart(env).unlink()
    assert _autostart(env) not in _install(env)
    assert not _autostart(env).exists()
    # … sauf demande explicite (--install-shortcuts).
    assert _autostart(env) in _install(env, force=True)


def test_fichier_d_autrui_jamais_ecrase(env):
    path = _autostart(env)
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nExec=python main.py\n", encoding="utf-8")
    _install(env)
    assert _read(path) == "[Desktop Entry]\nExec=python main.py\n"
    _install(env, force=True)
    assert di.is_managed(_read(path))


def test_etat_illisible_traite_comme_vide(env):
    env.state.parent.mkdir(parents=True)
    env.state.write_text("{pas du json", encoding="utf-8")
    assert len(_install(env)) == 5


# --- Suppression ------------------------------------------------------------------------------

def test_suppression_ne_retire_que_nos_fichiers(env):
    _install(env)
    foreign = env.desktop / "flowelis-configuration.desktop"
    foreign.write_text("[Desktop Entry]\nExec=autre\n", encoding="utf-8")
    removed = di.remove_shortcuts(env.state, home=env.home,
                                  desktop_dir=env.desktop)
    assert _autostart(env) in removed
    assert foreign not in removed and foreign.exists()
    assert not _autostart(env).exists()
    # Réinstallation possible ensuite (l'état repart de zéro).
    assert _autostart(env) in _install(env)


# --- Intégration dans main ---------------------------------------------------------------------

def test_plateforme_non_prise_en_charge(monkeypatch):
    monkeypatch.setattr(di, "supported", lambda: False)
    called = []
    monkeypatch.setattr(di, "install_shortcuts",
                        lambda *a, **k: called.append(1))
    assert main._sync_shortcuts() == []
    assert called == []


def test_echec_des_raccourcis_n_empeche_pas_le_demarrage(monkeypatch, tmp_path):
    monkeypatch.setattr(di, "supported", lambda: True)
    monkeypatch.setattr(main, "_shortcuts_state_path", lambda: tmp_path / "s.json")

    def boom(*_a, **_k):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(di, "install_shortcuts", boom)
    assert main._sync_shortcuts() == []


@pytest.mark.parametrize("option", ["--install-shortcuts", "--remove-shortcuts"])
def test_relance_sans_les_options_de_raccourcis(option):
    assert main._restart_command(argv=["FB", option], executable="FB",
                                 frozen=True) == ["FB"]
