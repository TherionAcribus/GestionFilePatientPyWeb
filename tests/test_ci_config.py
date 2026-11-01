"""Garde-fous de la CI : les tests Qt doivent réellement y tourner.

Avant, PySide6 n'était pas installé en CI : les tests du code PIN / menu de
maintenance (``pytest.importorskip``) y étaient ignorés en silence.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pin(path, package):
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(package)}==([\w.]+)\s*$", text, re.MULTILINE)
    assert match, f"{package} non épinglé dans {path}"
    return match.group(1)


def test_qt_des_tests_meme_version_que_la_borne():
    assert (_pin("requirements-dev.txt", "PySide6-Essentials")
            == _pin("requirements.txt", "PySide6"))


def test_la_ci_installe_et_verifie_qt():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements-dev.txt" in ci
    assert "QT_QPA_PLATFORM: offscreen" in ci
    # Qt inutilisable -> échec explicite, pas des tests ignorés en silence.
    assert "QApplication([])" in ci
