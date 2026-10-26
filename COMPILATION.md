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

- `FlowelisBorne.spec` : build « dossier », sortie dans
  `dist/FlowelisBorne/` (binaire + `_internal/`). Format recommandé :
  démarrage rapide.
- `FlowelisBorne_onefile.spec` : build « binaire unique », sortie
  `dist/FlowelisBorne`. Utilisé par la release GitHub pour un déploiement
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
pyinstaller --noconfirm --clean FlowelisBorne.spec          # dossier
pyinstaller --noconfirm --clean FlowelisBorne_onefile.spec  # binaire unique
```

Résultat : `dist/FlowelisBorne/` (dossier) ou `dist/FlowelisBorne`
(binaire unique).

## 4. Release GitHub (compilation automatisée)

Le workflow `.github/workflows/release.yml` compile les deux formats sur
`ubuntu-22.04` et publie une release dès qu'un tag `v*` est poussé :

```bash
git tag v1.0.0
git push origin v1.0.0
```

La release contient :

- `FlowelisBorne-<tag>-linux-x86_64.tar.gz` : le dossier complet — format recommandé ;
- `FlowelisBorne-<tag>-linux-x86_64` : le binaire autonome unique.

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

- paquets système Qt/WebEngine/USB : `libgl1 libegl1 libxkbcommon0
  libdbus-1-3 libnss3 libxcomposite1 libxdamage1 libxrandr2 libasound2
  libxcb-cursor0 libusb-1.0-0` (identiques à ceux d'`install.sh`) ;
- la **règle udev** pour l'imprimante ESC/POS (accès USB sans root) ;
- le **démarrage automatique** à l'ouverture de session ;
- la configuration (`~/.config/Flowelis/settings.json` + secrets dans le
  trousseau) — via l'éditeur graphique : `./FlowelisBorne --config` (le
  binaire l'ouvre aussi automatiquement quand la configuration est absente
  ou refusée), `config_editor.py` depuis le dépôt, ou en éditant le fichier.

## 6. Utiliser le binaire téléchargé

Le fichier unique téléchargé depuis la release n'a **pas le bit exécutable**
(téléchargement web) — il faut le rendre exécutable une fois :

```bash
chmod +x FlowelisBorne-*-linux-x86_64
./FlowelisBorne-*-linux-x86_64
# Diagnostic sans interface :  ./FlowelisBorne-*-linux-x86_64 --selfcheck
# Éditeur de configuration :   ./FlowelisBorne-*-linux-x86_64 --config
```

Sans console ni paramètre, le lancement produit l'interface kiosque ; si la
configuration est absente ou refusée, l'éditeur graphique s'ouvre en premier.

`install.sh` reste la voie la plus simple pour ces réglages système ; le
binaire compilé ne dispense que de l'environnement Python/venv.

Un binaire compilé sur x86_64 **ne fonctionne pas sur Raspberry Pi** (ARM) :
sur Pi, installer via `install.sh`.
