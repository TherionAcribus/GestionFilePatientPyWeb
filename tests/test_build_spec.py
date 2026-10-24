"""Configuration de build PyInstaller de la borne.

Deux fichiers .spec, et seulement eux, doivent exister :
- ``PharmaFileBorne.spec``         : build « dossier » (dist/PharmaFileBorne/)
- ``PharmaFileBorne_onefile.spec`` : build « binaire unique »
  (dist/PharmaFileBorne), utilisé par le workflow de release

L'application comptoir a déjà subi un spec obsolète (sans les backends
keyring ni les ressources) qui produisait un binaire silencieusement dégradé :
ici, le test compare les listes critiques des deux specs pour interdire toute
divergence.
"""

import ast
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECS = ["PharmaFileBorne.spec", "PharmaFileBorne_onefile.spec"]
SPEC = os.path.join(APP_DIR, "PharmaFileBorne.spec")


def _spec_source(path=SPEC):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _critical_lists(path):
    """Extrait HIDDENIMPORTS / DEV_EXCLUDES (literal_eval) et l'expression
    datas de Analysis (comparaison d'AST : elle contient un appel
    collect_data_files non évaluable)."""
    tree = ast.parse(_spec_source(path), filename=os.path.basename(path))
    constants = {}
    datas = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "HIDDENIMPORTS",
                    "DEV_EXCLUDES",
                ):
                    constants[target.id] = ast.literal_eval(node.value)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Analysis":
            for kw in node.keywords:
                if kw.arg == "datas":
                    datas = ast.dump(kw.value)
    return constants, datas


def test_fichiers_spec():
    specs = sorted(n for n in os.listdir(APP_DIR) if n.endswith(".spec"))
    assert specs == SPECS, f"fichiers .spec inattendus : {specs}"


def test_specs_sont_du_python_valide():
    for name in SPECS:
        ast.parse(
            _spec_source(os.path.join(APP_DIR, name)),
            filename=name,
        )


def test_ressources_assets_embarquees():
    """assets/ est lu via __file__ (ui_assets) : il doit être dans datas."""
    for name in SPECS:
        source = _spec_source(os.path.join(APP_DIR, name))
        assert "('assets', 'assets')" in source, name


def test_donnees_webview_embarquees():
    """Sans webview/lib (js api), pywebview est muet une fois gelé."""
    for name in SPECS:
        source = _spec_source(os.path.join(APP_DIR, name))
        assert "collect_data_files('webview')" in source, name


def test_backends_keyring_forces():
    """Sans Secret Service, la borne ne trouve aucun backend keyring."""
    for name in SPECS:
        source = _spec_source(os.path.join(APP_DIR, name))
        assert "keyring.backends.SecretService" in source, name


def test_listes_critiques_synchronisees():
    reference = _critical_lists(SPEC)
    onefile = _critical_lists(os.path.join(APP_DIR, "PharmaFileBorne_onefile.spec"))
    assert onefile == reference
