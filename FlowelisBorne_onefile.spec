# Variante « onefile » de PharmaFileBorne.spec : produit un binaire unique
# autonome (dist/PharmaFileBorne) au lieu du dossier dist/PharmaFileBorne/.
# Utilisée par le workflow de release (.github/workflows/release.yml) pour les
# déploiements qui veulent un seul fichier à copier ; le build « dossier »
# reste recommandé (démarrage plus rapide, pas d'extraction dans /tmp).
#
# Les listes ci-dessous DOIVENT rester identiques à celles de
# PharmaFileBorne.spec : tests/test_build_spec.py compare les deux fichiers
# et échoue en cas de divergence.

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

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

DEV_EXCLUDES = [
    'pytest', 'pyinstaller', 'tkinter',
]

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
    a.binaries,
    a.datas,
    [],
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
