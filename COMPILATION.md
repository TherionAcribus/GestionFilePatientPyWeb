# Compilation de la borne (binaire Linux)

Ce document explique comment compiler la borne en un binaire Linux autonome
et comment fonctionne la release GitHub automatisée.

## 1. Prérequis

Sur une machine Ubuntu/Debian avec Python 3.12 :

```bash
python -m venv env
source env/bin/activate
pip install -r requirements-build.txt
```

## 2. Les fichiers de configuration (.spec)

Deux fichiers `.spec`, et seulement eux, existent à la racine :

- `PharmaFileBorne.spec` : build « dossier », sortie dans
  `dist/PharmaFileBorne/` (binaire + `_internal/`). Format recommandé :
  démarrage rapide.
- `PharmaFileBorne_onefile.spec` : build « binaire unique », sortie
  `dist/PharmaFileBorne`. Utilisé par la release GitHub pour un déploiement
  en un seul fichier. Démarrage plus lent (extraction dans `/tmp`).

Les deux specs partagent les mêmes listes critiques (`HIDDENIMPORTS`,
`DEV_EXCLUDES`, `datas`) — `tests/test_build_spec.py` les compare et bloque
toute divergence.

Points spécifiques au bundle :

- `webview.platforms.qt` et `PySide6.QtWebEngine*` sont en `hiddenimports` :
  pywebview choisit son backend à l'exécution (`gui="qt"`), l'analyse
  statique ne le voit pas.
- `collect_data_files('webview')` embarque `webview/lib` (JS interne),
  indispensable au fonctionnement de la page une fois gelée.
- Les backends keyring (`SecretService`, `libsecret`…) sont forcés : sans
  eux, `secret_store` ne trouve aucun backend et refuse d'écrire les secrets
  en clair.
- `assets/` est embarqué dans `datas` : `ui_assets` le résout via `__file__`,
  qui pointe vers `sys._MEIPASS` une fois gelé.

## 3. Lancer la compilation

```bash
pyinstaller --noconfirm --clean PharmaFileBorne.spec          # dossier
pyinstaller --noconfirm --clean PharmaFileBorne_onefile.spec  # binaire unique
```

Résultat : `dist/PharmaFileBorne/` (dossier) ou `dist/PharmaFileBorne`
(binaire unique).

## 4. Release GitHub (compilation automatisée)

Le workflow `.github/workflows/release.yml` compile les deux formats sur
`ubuntu-22.04` et publie une release dès qu'un tag `v*` est poussé :

```bash
git tag v1.0.0
git push origin v1.0.0
```

La release contient :

- `PharmaFileBorne-<tag>-linux-x86_64.tar.gz` : le dossier complet — format recommandé ;
- `PharmaFileBorne-<tag>-linux-x86_64` : le binaire autonome unique.

Le workflow exécute aussi un test de fumée avant de publier : le binaire est
lancé avec `--selfcheck`, un mode diagnostic qui importe les modules chargés
dynamiquement (backend Qt de pywebview, backends keyring, escpos/usb) et
vérifie les données embarquées — sans affichage ni matériel. Il est utilisable
aussi sur une borne réelle pour diagnostiquer un déploiement.

Page publique des releases :
<https://github.com/TherionAcribus/GestionFilePatientPyWeb/releases>

## 5. Ce que le binaire ne remplace pas

Le binaire embarque Python, Qt et les dépendances pip, mais pas tout le
système. Sur la borne cible, il reste nécessaire :

- paquets système Qt/audio/USB : `libegl1 libgl1 libxkbcommon0 libdbus-1-3
  libpulse0 libasound2 libusb-1.0-0` ;
- la **règle udev** pour l'imprimante ESC/POS (accès USB sans root) ;
- le **démarrage automatique** à l'ouverture de session ;
- la configuration (`~/.config/FileAttente/settings.json` + secrets dans le
  trousseau) — via `config-editor.py` depuis le dépôt, ou en éditant le
  fichier.

`install.sh` reste la voie la plus simple pour ces réglages système ; le
binaire compilé ne dispense que de l'environnement Python/venv.

Un binaire compilé sur x86_64 **ne fonctionne pas sur Raspberry Pi** (ARM) :
sur Pi, installer via `install.sh`.
