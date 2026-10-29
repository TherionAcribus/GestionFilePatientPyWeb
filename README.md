# Borne patient — Flowelis (GestionFilePatientPyWeb)

Application de **borne tactile** en libre-service pour la prise de ticket
patient. Elle affiche la page `/patient` du serveur Flowelis dans une fenêtre
kiosque (pywebview + Qt) et pilote l'**imprimante ticket ESC/POS** (USB).

- Interface kiosque plein écran, tactile, curseur masqué.
- Impression du ticket via l'imprimante USB (python-escpos).
- Récupération automatique après démarrage hors ligne / imprimante débranchée.
- Envoi périodique de l'état de l'imprimante au serveur.

---

## 1. Prérequis

- **Python 3.11+** (3.12 recommandé).
- Un **serveur Flowelis** joignable (voir `base_url` dans la configuration).
- Une **imprimante ticket USB** compatible ESC/POS (par défaut Epson TM-T88).
- Un **backend Qt** pour pywebview (PySide6, fourni dans les dépendances).

---

## 2. Installation

### 2.0 Binaire précompilé (Ubuntu x86_64)

Des binaires prêts à l'emploi sont publiés sur la page
[Releases](https://github.com/TherionAcribus/GestionFilePatientPyWeb/releases) :
un dossier complet (`.tar.gz`, recommandé) ou un binaire autonome unique.
Ils dispensent de l'environnement Python mais pas de certains réglages
système (paquets Qt/USB, règle udev) — voir `COMPILATION.md` §5. Le
démarrage automatique et les raccourcis sont créés par le binaire lui-même
(§ 5.1) et la configuration par l'assistant (§ 4.1). Sur Raspberry Pi (ARM),
installer via `install.sh`.

### 2.1 Installation automatique (recommandé, Linux)

```bash
bash install.sh
```

Le script est **idempotent** et couvre toute l'installation : paquets système,
environnement Python, **détection de l'imprimante USB + règle udev**,
démarrage automatique à l'ouverture de session, pré-remplissage de
`settings.json` et raccourci « Configuration » sur le bureau. Il demande le
mot de passe sudo une fois, puis ouvre l'**assistant d'installation** (voir § 4.1) où il
ne reste guère que l'adresse du serveur et le secret d'application à saisir.

Relancez-le simplement pour mettre à jour une borne (`git pull` + dépendances).

Les sections suivantes décrivent l'installation **manuelle** (utile hors
Debian/Ubuntu ou pour comprendre ce que fait le script).

Clonez le dépôt, créez un environnement virtuel, installez les dépendances
**épinglées** :

```bash
python -m venv env
# Linux :   source env/bin/activate
# Windows : env\Scripts\activate
pip install -r requirements.txt
```

### 2.2 Linux (Debian/Ubuntu/Raspberry Pi OS)

Paquets système nécessaires (USB + Qt/WebEngine) :

```bash
sudo apt update
sudo apt install -y \
  python3-venv python3-pip python3-tk \
  libusb-1.0-0 \
  libgl1 libegl1 libxkbcommon0 libdbus-1-3 \
  libnss3 libxcomposite1 libxdamage1 libxrandr2 libasound2t64 \
  libxcb-cursor0
```

> - `python3-tk` : requis par `config_editor.py` **uniquement si vous utilisez
>   le Python système** (un Python géré par `uv` embarque déjà Tcl/Tk).
> - `libasound2t64` est le nom du paquet sur Ubuntu ≥ 24.04 / Mint 22
>   (`libasound2` sur les versions antérieures).
> - `libxcb-cursor0` : **obligatoire depuis Qt 6.5** — sans lui, `main.py`
>   abandonne au démarrage avec « Could not load the Qt platform plugin
>   "xcb" ».

> Sur Raspberry Pi, l'accélération WebEngine peut nécessiter une configuration
> GPU/mémoire supplémentaire selon le modèle.

Puis installez les dépendances Python (cf. ci-dessus) et configurez les
**règles udev** (section 3) pour l'accès à l'imprimante sans `root`.

### 2.3 Windows

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

Exemple pour l'Epson TM-T88 par défaut (`0x04b8` / `0x0202`) — la même règle
que celle posée par `install.sh` :

```bash
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="04b8", ATTRS{idProduct}=="0202", MODE="0664", GROUP="plugdev", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/99-flowelis-printer.rules

sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
sudo usermod -a -G plugdev "$USER"    # puis déconnexion/reconnexion
```

> Les identifiants USB (vendeur/produit) sont visibles avec `lsusb`.

---

## 4. Configuration

La configuration est stockée dans un fichier `settings.json` de l'espace
utilisateur (jamais committé) :

- Linux : `~/.config/Flowelis/settings.json`
- Windows : `%LOCALAPPDATA%\Flowelis\settings.json`

Un modèle est fourni : [`settings.example.json`](settings.example.json).

### 4.1 Assistant de premier lancement

Tant que la borne n'a jamais été configurée, un **assistant en 4 étapes**
s'ouvre au démarrage, avant l'interface kiosque :

1. **Serveur** : adresse et secret d'application, bouton « Tester la connexion » ;
2. **Imprimante** : détection des imprimantes USB branchées (identifiants
   pré-remplis), modèle, bouton « Tester l'imprimante » ;
3. **Borne** : identifiant, options kiosque (plein écran, curseur masqué,
   mode production proposés par défaut), code PIN de maintenance facultatif ;
4. **Récapitulatif** : réglages, résultats des tests et points bloquants, puis
   « Terminer et démarrer la borne ».

« Configuration avancée… » bascule vers l'éditeur complet. Un assistant
abandonné se rouvre au démarrage suivant ; la borne démarre quand même si la
configuration existante est valide. Pour le relancer à la demande :
`./FlowelisBorne --setup` ou `python setup_wizard.py`.

Une borne déjà en service avant l'arrivée de l'assistant n'est pas concernée :
si sa configuration est valide, elle est considérée comme configurée
(champ `setup_completed`).

### 4.2 Éditeur graphique

```bash
# Depuis le dépôt :
python config_editor.py

# Depuis le binaire compilé :
./FlowelisBorne --config
```

Le binaire ouvre aussi **automatiquement** cet éditeur au premier démarrage
ou quand la configuration est refusée (fichier absent, illisible ou
invalide) — aucune connaissance du chemin n'est nécessaire.

### 4.3 Touche de maintenance (F10)

Sur une borne **en service**, la touche **F10** d'un clavier branché ouvre la
maintenance sans terminal ni redémarrage manuel (F11 reste la bascule
plein écran) :

1. la fenêtre kiosque se ferme ;
2. si un **code PIN de maintenance** est défini, il est demandé (3 essais,
   abandon après 60 s sans saisie) ;
3. une fenêtre propose alors trois suites :
   - **Ouvrir la configuration** (choix par défaut, touche Entrée) : l'éditeur
     s'ouvre et, à sa fermeture — enregistrée ou non — **la borne redémarre
     d'elle-même** avec la configuration en vigueur ;
   - **Quitter l'application** : après confirmation, l'application borne se
     ferme et **ne redémarre pas** (le PC, lui, reste allumé). Pour la
     relancer : « Flowelis Borne » dans le menu des applications, ou
     redémarrage du PC (lancement automatique à l'ouverture de session) ;
   - **Reprendre le service** (Échap, ou 60 s sans réponse) : la borne
     redémarre telle quelle.

La touche fonctionne sur tous les écrans : page patient, « Borne hors ligne »
et « Configuration invalide ». Un éditeur oublié ouvert se ferme sans
enregistrer après **10 minutes** sans clavier ni souris, et la borne reprend
son service.

Le **code PIN est facultatif** (section « Maintenance » de l'éditeur, 4 à 12
chiffres) : le patient n'a pas de clavier, la touche suffit en général. Il
n'est jamais stocké en clair (empreinte PBKDF2 salée dans `settings.json`).
**PIN oublié** : `--config` (ou `python config_editor.py`) lancé depuis le
système ouvre l'éditeur sans le demander, et permet de le supprimer.

L'éditeur **valide** la configuration avant d'enregistrer et propose deux
diagnostics :

- **Tester le serveur** : joignabilité + validité du secret d'application +
  chemin de connexion borne (ticket de session → redirection vers `/patient`),
  en une seule tentative bornée.
- **Tester l'imprimante** : ouverture du périphérique USB (sans imprimer).

### 4.4 Champs

| Champ | Type | Description |
|-------|------|-------------|
| `base_url` | str | URL racine du serveur. **HTTP autorisé uniquement pour `localhost` ou en mode `debug`** ; un serveur distant doit être en **HTTPS**. |
| `app_secret` | str | Secret d'application — **identité machine** de la borne (non vide ; **vide par défaut** et **refusé si valeur d'exemple**). **Secret** : stocké dans le magasin du système (cf. § 4.5). Sert à obtenir le jeton applicatif, lui-même échangé contre un ticket de session signé (`/patient/kiosk_login/…`) — la borne n'a plus de compte utilisateur. |
| `printer_id_vendor` | str | ID vendeur USB, hexadécimal (ex. `0x04b8`). |
| `printer_id_product` | str | ID produit USB, hexadécimal (ex. `0x0202`). |
| `printer_model` | str | Profil python-escpos (ex. `TM-T88II`). |
| `check_paper` | bool | Vérifier le papier avant chaque impression. |
| `fullscreen` | bool | Démarrer en plein écran (kiosque). |
| `debug` | bool | Mode développement (autorise HTTP distant, logs DEBUG). `false` = production. |
| `hide_cursor` | bool | Masquer le curseur (borne tactile). `false` pour un poste de maintenance souris. |
| `borne_id` | str | Identifiant de la borne joint aux statuts (jamais vide : laissé vide, il est généré automatiquement — nom d'hôte — puis persisté dans le fichier). |
| `maintenance_pin_hash` | str | Empreinte du code PIN de la touche de maintenance (vide = pas de PIN). Se règle dans l'éditeur, jamais à la main. |
| `setup_completed` | bool | Borne configurée (assistant terminé ou configuration enregistrée dans l'éditeur). `false` : l'assistant s'ouvre au démarrage. |

> **Garde-fous** : la borne **refuse de démarrer** si la configuration est
> invalide (URL/secret/identifiants USB/types) ou si le secret d'application
> est trivial (vide ou repris de l'exemple) en production. L'écran
> d'erreur liste les problèmes.
>
> **Aucun secret n'est livré dans le code** : `app_secret` naît **vide**, donc
> une borne installée sans passer par l'éditeur refuse de démarrer au lieu de
> tourner avec un accès trivial. Les valeurs usuelles (secret d'exemple…)
> restent refusées en production par une denylist, qui couvre les
> installations existantes.

### 4.5 Stockage du secret (`app_secret`)

Cette valeur n'est **jamais** écrite en clair dans `settings.json` : elle est
conservée dans le **gestionnaire de secrets du système** via `keyring`
(Gestionnaire d'identifiants Windows, Trousseau macOS, Secret Service Linux).
`settings.example.json` ne la contient donc pas ; renseignez-la via
`config_editor.py`.

- **Migration automatique** : si un ancien `settings.json` contient encore
  `app_secret` en clair, il est déplacé vers le magasin sécurisé au premier
  chargement, puis effacé du fichier. Les clés supprimées (`username`,
  `password` des versions avec compte utilisateur) sont ignorées puis
  effacées à la réécriture.
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
puis charge `/patient` dès qu'elle est opérationnelle. Les reconnexions suivent
un délai exponentiel avec un léger décalage aléatoire entre les bornes et un
plafond strict de 60 secondes : le retour du réseau ne nécessite pas de
redémarrage manuel.

Si le démarrage n'aboutit pas, l'écran « Borne hors ligne » ajoute en bas une
ligne **« Diagnostic technicien »** avec la cause probable et le rappel de la
touche F10 (le message destiné aux patients ne change pas) :

| Cause | Affichée |
|-------|----------|
| Secret d'application refusé par le serveur (HTTP 401/403) | immédiatement |
| L'URL répond mais n'est pas un serveur Flowelis (HTTP 404/405) | immédiatement |
| Serveur injoignable (réseau, serveur arrêté, URL erronée) | après 2 min |
| Réponse anormale du serveur, session borne refusée | après 2 min |

Le délai évite d'alarmer quand le réseau arrive quelques secondes après la
borne. La borne continue de réessayer dans tous les cas : corriger le serveur
suffit, sans toucher à la borne.

### 5.1 Démarrage automatique — Linux

**Binaire de release : rien à faire.** À chaque lancement, le binaire crée ou
met à jour lui-même ses raccourcis :

| Raccourci | Fichier |
|-----------|---------|
| Démarrage automatique à l'ouverture de session | `~/.config/autostart/flowelis-borne.desktop` |
| Menu des applications : borne, configuration (`--config`), assistant (`--setup`) | `~/.local/share/applications/flowelis-borne*.desktop` |
| Bureau : « Configuration Flowelis Borne » | `<Bureau>/flowelis-configuration.desktop` |

- Binaire déplacé ou remplacé par une nouvelle version : les raccourcis
  suivent automatiquement.
- Démarrage automatique désactivé dans les réglages de session : il le
  reste. Raccourci supprimé à la main : il n'est pas recréé.
- Un fichier qui n'a pas été créé par la borne (écrit à la main, ou par
  `install.sh`) n'est jamais écrasé.
- `./FlowelisBorne --install-shortcuts` force la (re)création de tous les
  raccourcis ; `./FlowelisBorne --remove-shortcuts` retire ceux de la borne.

Depuis les sources, rien n'est créé automatiquement (un lancement depuis
l'IDE ne doit pas toucher au poste) : `install.sh` s'en charge, ou
`python main.py --install-shortcuts`. Création manuelle, pour mémoire :

**Option A : session graphique (XDG autostart)** — le plus simple pour un
kiosque. Créez `~/.config/autostart/flowelis-borne.desktop` :

```ini
[Desktop Entry]
Type=Application
Name=Flowelis Borne
Exec=/chemin/vers/env/bin/python /chemin/vers/GestionFilePatientPyWeb/main.py
X-GNOME-Autostart-enabled=true
```

**Option B : service systemd utilisateur** (`~/.config/systemd/user/flowelis-borne.service`) :

```ini
[Unit]
Description=Borne Flowelis
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
systemctl --user enable --now flowelis-borne.service
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

- Fichier tournant : `<espace utilisateur>/Flowelis/logs/borne.log`
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
  Le périmètre (voir [`ruff.toml`](ruff.toml)) couvre pycodestyle (`E`/`W`),
  pyflakes (`F`), le tri des imports (`I`), bugbear (`B`), les simplifications
  (`SIM`/`C4`/`RET`/`PIE`), la modernisation (`UP`), les règles ruff (`RUF`),
  les mauvais usages de `logging` (`LOG`/`G`/`TRY400`/`TRY401`) et les dates
  sans fuseau (`DTZ`). Le dépôt est à **zéro violation** : toute nouvelle
  alerte est un vrai défaut à corriger.
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
| `ui_assets.py` | Chargement/substitution des écrans et scripts de `assets/`. |
| `assets/` | Écrans HTML (hors ligne, erreur de configuration) et scripts JS (kiosque, clavier, connexion) — plus aucun bloc HTML/JS dans `main.py`. |
| `errors.py` | Exceptions métier (`BorneError` et ses filles) : panne attendue vs bogue. |
| `printer.py` | Logique imprimante (impression, papier, statuts, reconnexion) + découplage matériel. |
| `config.py` | Chargement/validation/sauvegarde de la configuration. |
| `config_editor.py` | Éditeur graphique + tests serveur/imprimante. |
| `logging_config.py` | Journalisation (niveaux, rotation, masquage des secrets). |
| `editor_logic.py` | Logique pure de l'éditeur (testable sans tkinter). |
| `maintenance_pin.py` | Touche de maintenance et code PIN (empreinte, vérification). |
| `setup_wizard.py` | Assistant de premier lancement (interface). |
| `setup_logic.py` | Logique pure de l'assistant (étapes, configuration finale, détection USB). |
| `desktop_integration.py` | Raccourcis système Linux (démarrage automatique, menu, bureau). |
| `startup_diagnosis.py` | Cause probable d'un démarrage qui échoue, affichée au technicien. |
| `secret_store.py` | Stockage des secrets dans le magasin du système (keyring). |
