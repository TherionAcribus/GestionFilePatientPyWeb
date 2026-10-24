# Build « dossier » de la borne : produit dist/PharmaFileBorne/ (binaire +
# _internal/). Format recommandé : démarrage plus rapide qu'un exe unique.
# Voir COMPILATION.md ; la variante onefile est PharmaFileBorne_onefile.spec.
#
# Les listes critiques ci-dessous DOIVENT rester identiques à celles de
# PharmaFileBorne_onefile.spec : tests/test_build_spec.py compare les deux
# fichiers et échoue en cas de divergence.

from PyInstaller.utils.hooks import collect_data_files

# Imports invisibles pour l'analyse statique :
# - pywebview choisit son backend à l'exécution (webview.start(gui="qt")) ;
# - keyring charge ses backends via des entry points : sans Secret Service /
#   libsecret, la borne ne trouve aucun backend et secret_store refuse
#   d'écrire les secrets en clair.
HIDDENIMPORTS = [
    'webview.platforms.qt',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebChannel',
    'keyring.backends.SecretService',
    'keyring.backends.libsecret',
    'keyring.backends.chainer',
    'keyring.backends.fail',
]

# Modules exclus du build : outils de dev jamais importés par la borne, et
# tkinter qui ne sert qu'à config-editor.py (script hors du binaire).
DEV_EXCLUDES = [
    'pytest', 'pyinstaller', 'tkinter',
]

# datas : assets/ de la borne (résolus via __file__ -> sys._MEIPASS) + les
# fichiers JS/CSS internes de pywebview (webview/lib).
DATAS = [('assets', 'assets'), *collect_data_files('webview')]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=DEV_EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PharmaFileBorne',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PharmaFileBorne',
)
