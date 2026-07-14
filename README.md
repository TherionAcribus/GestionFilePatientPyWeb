# Borne patient — PharmaFile (GestionFilePatientPyWeb)

Application de **borne tactile** en libre-service pour la prise de ticket
patient. Elle affiche la page `/patient` du serveur PharmaFile dans une fenêtre
kiosque (pywebview + Qt) et pilote l'**imprimante ticket ESC/POS** (USB).

- Interface kiosque plein écran, tactile, curseur masqué.
- Impression du ticket via l'imprimante USB (python-escpos).
- Récupération automatique après démarrage hors ligne / imprimante débranchée.
- Envoi périodique de l'état de l'imprimante au serveur.

---

## 1. Prérequis

- **Python 3.11+** (3.12 recommandé).
- Un **serveur PharmaFile** joignable (voir `base_url` dans la configuration).
- Une **imprimante ticket USB** compatible ESC/POS (par défaut Epson TM-T88).
- Un **backend Qt** pour pywebview (PySide6, fourni dans les dépendances).

---

## 2. Installation

Clonez le dépôt, créez un environnement virtuel, installez les dépendances
**épinglées** :

```bash
python -m venv env
# Linux :   source env/bin/activate
# Windows : env\Scripts\activate
pip install -r requirements.txt
```

### 2.1 Linux (Debian/Ubuntu/Raspberry Pi OS)

Paquets système nécessaires (USB + Qt/WebEngine) :

```bash
sudo apt update
sudo apt install -y \
  python3-venv python3-pip \
  libusb-1.0-0 \
  libgl1 libegl1 libxkbcommon0 libdbus-1-3 \
  libnss3 libxcomposite1 libxdamage1 libxrandr2 libasound2
```

> Sur Raspberry Pi, l'accélération WebEngine peut nécessiter une configuration
> GPU/mémoire supplémentaire selon le modèle.

Puis installez les dépendances Python (cf. ci-dessus) et configurez les
**règles udev** (section 3) pour l'accès à l'imprimante sans `root`.

### 2.2 Windows

1. Installez **Python 3.12** (cochez « Add python.exe to PATH »).
2. Installez le **pilote USB** de l'imprimante. Pour python-escpos/pyusb, il
   faut généralement remplacer le pilote de l'imprimante par **WinUSB** via
   [Zadig](https://zadig.akeo.ie/) (sélectionnez l'imprimante, pilote WinUSB).
3. Créez le venv et installez `requirements.txt` (cf. ci-dessus).

---

## 3. Règles udev (Linux) — accès imprimante sans root

Sans règle, l'ouverture USB échoue (`langid` / permissions). Créez une règle
avec les **identifiants de VOTRE imprimante** (valeurs `printer_id_vendor` /
`printer_id_product` de la configuration, **sans** le préfixe `0x`).

Exemple pour l'Epson TM-T88 par défaut (`0x04b8` / `0x0202`) :

```bash
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="04b8", ATTRS{idProduct}=="0202", MODE="0666", GROUP="dialout"' \
  | sudo tee /etc/udev/rules.d/99-escpos-printer.rules

sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -a -G dialout "$USER"    # puis déconnexion/reconnexion
```

> Les identifiants USB (vendeur/produit) sont visibles avec `lsusb`.

---

## 4. Configuration

La configuration est stockée dans un fichier `settings.json` de l'espace
utilisateur (jamais committé) :

- Linux : `~/.config/FileAttente/settings.json`
- Windows : `%LOCALAPPDATA%\FileAttente\settings.json`

Un modèle est fourni : [`settings.example.json`](settings.example.json).

### 4.1 Éditeur graphique (recommandé)

```bash
python config-editor.py
```

L'éditeur **valide** la configuration avant d'enregistrer et propose deux
diagnostics :

- **Tester le serveur** : joignabilité + validité du secret d'application.
- **Tester l'imprimante** : ouverture du périphérique USB (sans imprimer).

### 4.2 Champs

| Champ | Type | Description |
|-------|------|-------------|
| `base_url` | str | URL racine du serveur. **HTTP autorisé uniquement pour `localhost` ou en mode `debug`** ; un serveur distant doit être en **HTTPS**. |
| `username` | str | Identifiant de session de la borne (non vide). |
| `password` | str | Mot de passe de session. **Secret** : stocké dans le magasin du système, jamais en clair dans `settings.json` (cf. § 4.3). |
| `app_secret` | str | Secret d'application (non vide ; **refusé si valeur par défaut en production**). **Secret** : stocké dans le magasin du système (cf. § 4.3). |
| `printer_id_vendor` | str | ID vendeur USB, hexadécimal (ex. `0x04b8`). |
| `printer_id_product` | str | ID produit USB, hexadécimal (ex. `0x0202`). |
| `printer_model` | str | Profil python-escpos (ex. `TM-T88II`). |
| `check_paper` | bool | Vérifier le papier avant chaque impression. |
| `fullscreen` | bool | Démarrer en plein écran (kiosque). |
| `debug` | bool | Mode développement (autorise HTTP distant, logs DEBUG). `false` = production. |
| `hide_cursor` | bool | Masquer le curseur (borne tactile). `false` pour un poste de maintenance souris. |
| `borne_id` | str | Identifiant de la borne joint aux statuts (vide = nom d'hôte). |

> **Garde-fous** : la borne **refuse de démarrer** si la configuration est
> invalide (URL/secret/identifiants USB/types) ou si des identifiants par
> défaut (`admin/admin`) sont utilisés en production. L'écran d'erreur liste
> les problèmes.

### 4.3 Stockage des secrets (`password`, `app_secret`)

Ces deux valeurs ne sont **jamais** écrites en clair dans `settings.json` : elles
sont conservées dans le **gestionnaire de secrets du système** via `keyring`
(Gestionnaire d'identifiants Windows, Trousseau macOS, Secret Service Linux).
`settings.example.json` ne les contient donc plus ; renseignez-les via
`config-editor.py`.

- **Migration automatique** : si un ancien `settings.json` contient encore ces
  valeurs en clair, elles sont déplacées vers le magasin sécurisé au premier
  chargement, puis effacées du fichier.
- **Pas de repli silencieux** : si aucun magasin sécurisé n'est disponible,
  l'enregistrement est **refusé en production** (mode `debug=false`) avec un
  message explicite ; en mode développement (`debug=true`), le repli en clair
  est toléré mais **journalise un avertissement**.
- **Linux « headless »** : le backend Secret Service nécessite un service de
  trousseau actif (paquets `secretstorage`/`dbus`). Sur une borne sans session
  graphique, installez un backend adapté (ex. `keyrings.alt` /
  `keyrings.cryptfile`) ou effectuez la configuration depuis un poste disposant
  d'un magasin, sans quoi l'enregistrement des secrets sera refusé en production.

---

## 5. Démarrage

```bash
python main.py
```

La borne affiche « Borne hors ligne » tant qu'elle n'a pas obtenu son token,
puis charge `/patient` dès qu'elle est opérationnelle.

### 5.1 Démarrage automatique — Linux

**Option A : session graphique (XDG autostart)** — le plus simple pour un
kiosque. Créez `~/.config/autostart/pharmafile-borne.desktop` :

```ini
[Desktop Entry]
Type=Application
Name=PharmaFile Borne
Exec=/chemin/vers/env/bin/python /chemin/vers/GestionFilePatientPyWeb/main.py
X-GNOME-Autostart-enabled=true
```

**Option B : service systemd utilisateur** (`~/.config/systemd/user/pharmafile-borne.service`) :

```ini
[Unit]
Description=Borne PharmaFile
After=graphical-session.target
PartOf=graphical-session.target

[Service]
WorkingDirectory=/chemin/vers/GestionFilePatientPyWeb
ExecStart=/chemin/vers/env/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now pharmafile-borne.service
loginctl enable-linger "$USER"   # démarrage sans session interactive ouverte
```

> Un service graphique nécessite un affichage (X11/Wayland) et les variables
> d'environnement de session (`DISPLAY`/`WAYLAND_DISPLAY`).

### 5.2 Démarrage automatique — Windows

- **Dossier Démarrage** : créez un raccourci vers
  `env\Scripts\pythonw.exe main.py` dans
  `shell:startup` (`Win+R` → `shell:startup`).
- **ou Planificateur de tâches** : déclencheur « À l'ouverture de session »,
  action = `pythonw.exe` avec l'argument `main.py` et le bon dossier de départ.

---

## 6. Journaux

La borne journalise via `logging` (module [`logging_config.py`](logging_config.py)) :

- Fichier tournant : `<espace utilisateur>/FileAttente/logs/borne.log`
  (rotation locale ~5 Mo : 1 Mo × 5 sauvegardes).
- Format : `horodatage [NIVEAU] composant [job=…] message`.
- Les **secrets/jetons/mots de passe sont masqués** ; le **contenu des tickets
  n'est jamais journalisé**.
- Niveau `DEBUG` si `debug=true`, sinon `INFO`.

---

## 7. Développement, tests et qualité

Installez les outils de développement :

```bash
pip install -r requirements-dev.txt
```

- **Tests** : `pytest -q`
  Les tests tournent **sans matériel** : `conftest.py` fournit des stubs
  `escpos`/`pyusb`, et le **découplage matériel** de `Printer`
  (`device_factory`) permet d'injecter une **fausse imprimante**.
- **Lint** : `ruff check .`
- **Sécurité** : `bandit -r . -ll -x ./tests` et `pip-audit -r requirements.txt`

La CI (GitHub Actions, [`.github/workflows`](.github/workflows)) exécute :

- `ci.yml` : lint (ruff) et tests (pytest) **bloquants**, analyse de sécurité
  (bandit + pip-audit) en **advisory** ;
- `secret-scan.yml` : détection de secrets (gitleaks).

---

## 8. Architecture (survol)

| Fichier | Rôle |
|---------|------|
| `main.py` | Fenêtre kiosque, cycle de vie, token, protections tactiles. |
| `printer.py` | Logique imprimante (impression, papier, statuts, reconnexion) + découplage matériel. |
| `config.py` | Chargement/validation/sauvegarde de la configuration. |
| `config-editor.py` | Éditeur graphique + tests serveur/imprimante. |
| `logging_config.py` | Journalisation (niveaux, rotation, masquage des secrets). |
