#!/usr/bin/env bash
#
# install.sh — Installation complète de la borne PharmaFile (Linux).
#
#   bash install.sh               Installe tout puis ouvre l'éditeur de config.
#   bash install.sh --no-editor   Idem, sans ouvrir l'éditeur.
#
# Automatise : paquets système (apt), environnement Python, détection de
# l'imprimante USB + règle udev, démarrage automatique, pré-remplissage de
# settings.json et raccourci « Configuration » sur le bureau.
#
# Il restera à saisir dans l'éditeur uniquement :
#   - l'URL du serveur (base_url)
#   - le secret d'application (fourni par l'administrateur PharmaFile)
#
# Le script est idempotent : il peut être relancé sans risque.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

OPEN_EDITOR=1
[ "${1:-}" = "--no-editor" ] && OPEN_EDITOR=0

# ---------- helpers ----------

if [ -t 1 ]; then
    C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
    C_ERR=$'\033[1;31m'; C_RST=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_RST=""
fi
info() { echo "${C_INFO}==>${C_RST} $*"; }
ok()   { echo "${C_OK}  ✔${C_RST} $*"; }
warn() { echo "${C_WARN}  !${C_RST} $*"; }
die()  { echo "${C_ERR}  ✘ $*${C_RST}" >&2; exit 1; }

# ---------- préconditions ----------

[ "$(id -u)" -eq 0 ] && die "Ne pas lancer ce script en root. Lancez-le en tant qu'utilisateur de la borne : bash install.sh"
[ "$(uname -s)" = "Linux" ] || die "Ce script ne fonctionne que sous Linux."
command -v sudo >/dev/null 2>&1 || die "sudo est requis (installez-le ou connectez-vous en administrateur)."

info "Installation de la borne PharmaFile"
echo "    Dossier : $ROOT"
sudo -v || die "Mot de passe sudo requis pour installer les paquets système."
# Garde le ticket sudo actif pendant toute l'installation.
( while true; do sudo -n true; sleep 50; done ) 2>/dev/null &
KEEPALIVE=$!
trap 'kill "$KEEPALIVE" 2>/dev/null || true' EXIT

# ---------- 1. mise à jour du code (si dépôt git) ----------

if [ -d .git ] && command -v git >/dev/null 2>&1; then
    info "Mise à jour du code (git pull)"
    if git pull --ff-only >/dev/null 2>&1; then
        ok "Code à jour."
    else
        warn "git pull impossible (pas grave : on continue avec la version actuelle)."
    fi
fi

# ---------- 2. paquets système ----------

if command -v apt-get >/dev/null 2>&1; then
    info "Installation des paquets système"
    # libasound2t64 (Ubuntu ≥ 24.04 / Mint 22) ou libasound2 avant.
    if apt-cache show libasound2t64 >/dev/null 2>&1; then
        ALSA=libasound2t64
    else
        ALSA=libasound2
    fi
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        python3-venv python3-pip python3-tk \
        libusb-1.0-0 \
        libgl1 libegl1 libxkbcommon0 libdbus-1-3 \
        libnss3 libxcomposite1 libxdamage1 libxrandr2 "$ALSA" \
        libxcb-cursor0
    ok "Paquets système installés."
else
    warn "apt-get absent : installez manuellement Python 3.11+, libusb-1.0, Tk et les libs Qt/WebEngine (cf. README §2.1)."
fi

# ---------- 3. environnement Python ----------

if [ -d .venv ]; then
    VENV=".venv"
elif [ -d env ]; then
    VENV="env"
else
    VENV="env"
    info "Création de l'environnement Python ($VENV)"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV" --python 3.12 || uv venv "$VENV"
    else
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
            || die "Python 3.11+ requis (version trouvée : $(python3 --version 2>&1))."
        python3 -m venv "$VENV"
    fi
fi
PYBIN="$ROOT/$VENV/bin/python"
[ -x "$PYBIN" ] || die "Environnement Python introuvable ($VENV)."

info "Installation des dépendances Python"
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYBIN" -r requirements.txt --quiet
else
    "$PYBIN" -m pip install --quiet --upgrade pip
    "$PYBIN" -m pip install --quiet -r requirements.txt
fi
ok "Dépendances Python installées."

# ---------- 4. détection de l'imprimante ----------

info "Détection de l'imprimante USB"
DETECTED="$("$PYBIN" - <<'PY' || true
import usb.core

def usable(dev):
    try:
        if dev.idVendor == 0x1d6b:   # Linux Foundation (hub racine)
            return False
        if dev.bDeviceClass == 9:    # concentrateur USB
            return False
        return True
    except Exception:
        return False

found, seen = [], set()
for dev in usb.core.find(find_all=True) or []:
    pair = (dev.idVendor, dev.idProduct)
    if usable(dev) and pair not in seen:
        seen.add(pair)
        found.append(pair)
# Epson en premier si plusieurs candidats.
found.sort(key=lambda p: p[0] != 0x04b8)
for v, p in found:
    print(f"0x{v:04x}:0x{p:04x}")
PY
)"

SETTINGS_JSON="$HOME/.config/FileAttente/settings.json"
VID=""; PID=""
# Priorité : identifiants déjà configurés dans settings.json.
if [ -f "$SETTINGS_JSON" ]; then
    read -r VID PID < <("$PYBIN" - "$SETTINGS_JSON" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    v, p = d.get("printer_id_vendor", ""), d.get("printer_id_product", "")
    if str(v).startswith("0x") and str(p).startswith("0x"):
        print(v, p)
except Exception:
    pass
PY
    )
fi
# Sinon : imprimante détectée (Epson préférée), sinon valeurs par défaut.
if [ -z "$VID" ] && [ -n "$DETECTED" ]; then
    IFS=: read -r VID PID <<< "$(echo "$DETECTED" | head -1)"
fi
VID="${VID:-0x04b8}"; PID="${PID:-0x0202}"
ok "Imprimante retenue : vendeur $VID / produit $PID"
if [ "$(echo "$DETECTED" | grep -c .)" -gt 1 ]; then
    warn "Plusieurs périphériques USB détectés :$(echo; echo "$DETECTED" | sed 's/^/      /')"
    warn "Vérifiez les identifiants dans l'éditeur de configuration."
fi

# ---------- 5. règle udev ----------

UDEV_RULE="/etc/udev/rules.d/99-pharmafile-printer.rules"
info "Règle udev pour l'imprimante (accès sans root)"
printf 'SUBSYSTEM=="usb", ATTRS{idVendor}=="%s", ATTRS{idProduct}=="%s", MODE="0664", GROUP="plugdev", TAG+="uaccess"\n' \
    "${VID#0x}" "${PID#0x}" | sudo tee "$UDEV_RULE" >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb || true
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
    sudo usermod -aG plugdev "$USER"
    warn "Utilisateur ajouté au groupe plugdev : fermez puis rouvrez la session si l'imprimante reste inaccessible."
fi
ok "Règle udev installée. Si l'imprimante était branchée, débranchez/rebranchez-la."

# ---------- 6. pré-remplissage de settings.json ----------

if [ ! -f "$SETTINGS_JSON" ]; then
    mkdir -p "$(dirname "$SETTINGS_JSON")"
    cat > "$SETTINGS_JSON" <<EOF
{
    "base_url": "",
    "printer_id_vendor": "$VID",
    "printer_id_product": "$PID",
    "printer_model": "TM-T88II",
    "check_paper": true,
    "fullscreen": true,
    "debug": false,
    "hide_cursor": true
}
EOF
    ok "settings.json pré-rempli ($SETTINGS_JSON)."
else
    ok "settings.json existant conservé."
fi

# ---------- 7. démarrage automatique ----------

info "Démarrage automatique de la borne à l'ouverture de session"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/pharmafile-borne.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PharmaFile Borne
Exec=$PYBIN $ROOT/main.py
X-GNOME-Autostart-enabled=true
EOF
ok "Entrée autostart créée (~/.config/autostart/pharmafile-borne.desktop)."

# ---------- 8. raccourci « Configuration » sur le bureau ----------

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
    cat > "$DESKTOP_DIR/pharmafile-configuration.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Configuration borne PharmaFile
Comment=Modifier l'URL du serveur, le secret et l'imprimante
Exec=$PYBIN $ROOT/config-editor.py
Path=$ROOT
Icon=preferences-system
Terminal=false
EOF
    chmod +x "$DESKTOP_DIR/pharmafile-configuration.desktop"
    # Autoriser le lancement sur les bureaux GNOME récents (best effort).
    gio set "$DESKTOP_DIR/pharmafile-configuration.desktop" metadata::trusted true 2>/dev/null || true
    ok "Raccourci « Configuration borne PharmaFile » créé sur le bureau."
fi

# ---------- 9. bilan ----------

echo
ok "Installation terminée."
cat <<EOF

  Il reste 2 champs à renseigner dans l'éditeur de configuration :
    • URL du serveur (base_url)
    • Secret d'application (app_secret) — fourni par l'administrateur PharmaFile

  Puis cliquez « Tester le serveur » et « Tester l'imprimante ».
EOF

if [ "$OPEN_EDITOR" -eq 1 ]; then
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        info "Ouverture de l'éditeur de configuration…"
        "$PYBIN" "$ROOT/config-editor.py" || true
    else
        warn "Pas de session graphique détectée : lancez « $VENV/bin/python config-editor.py » depuis le bureau."
    fi
fi
