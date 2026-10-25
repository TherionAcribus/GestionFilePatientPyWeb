# Build « dossier » de la borne : produit dist/FlowelisBorne/ (binaire +
# _internal/). Format recommandé : démarrage plus rapide qu'un exe unique.
# Voir COMPILATION.md ; la variante onefile est FlowelisBorne_onefile.spec.
#
# Les listes critiques ci-dessous DOIVENT rester identiques à celles de
# FlowelisBorne_onefile.spec : tests/test_build_spec.py compare les deux
# fichiers et échoue en cas de divergence.

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

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

# datas :
# - assets/ de la borne (résolus via __file__ -> sys._MEIPASS) ;
# - webview/lib (JS/CSS internes de pywebview) ;
# - capabilities.json de python-escpos (chargé via importlib.resources à
#   l'import de escpos.printer — absent du bundle, l'import plante) ;
# - métadonnées du paquet (lecture de version via importlib.metadata).
DATAS = [
    ('assets', 'assets'),
    *collect_data_files('webview'),
    *collect_data_files('escpos'),
    *copy_metadata('python-escpos'),
]

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
    name='FlowelisBorne',
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
    name='FlowelisBorne',
)
