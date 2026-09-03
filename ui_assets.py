# ui_assets.py
"""Écrans HTML et scripts JS de la borne, chargés depuis ``assets/``.

Pourquoi
--------
``main.py`` embarquait plusieurs centaines de lignes de HTML/CSS/JS dans des
chaînes Python : illisible, sans coloration ni vérification syntaxique, et
mélangé à la logique de cycle de vie. Les ressources vivent désormais dans des
fichiers ``.html`` / ``.js`` autonomes ; ce module se charge de les lire, de
substituer leurs marqueurs et de les mettre en cache.

Substitution
------------
Les marqueurs ont la forme ``__NOM__`` et sont remplacés en **une seule passe**
(:func:`render`) : une valeur substituée ne peut donc pas être réinterprétée
comme un marqueur (un mot de passe contenant littéralement ``__PASSWORD_JSON__``
est inséré tel quel). Un marqueur inconnu laissé dans un fichier est une erreur
(:class:`AssetError`) : mieux vaut un écran d'erreur qu'un ``__X__`` affiché à un
patient.

Robustesse
----------
Une ressource illisible (fichier supprimé, droits) ne doit pas empêcher la borne
d'afficher quelque chose : :func:`offline_html` et :func:`build_config_error_html`
retombent sur un écran minimal intégré au code, et les scripts JS retombent sur
une chaîne vide (la page reste utilisable, seules les protections kiosque
manquent). Tous ces replis sont JOURNALISÉS en erreur.
"""

import html
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("borne.ui")

# Dossier des ressources, résolu par rapport à CE fichier (et non au répertoire
# de travail) : la borne peut être lancée depuis n'importe où.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_PLACEHOLDER_RE = re.compile(r"__([A-Z0-9_]+)__")

# Écran de repli minimal si une ressource HTML est illisible. Volontairement
# réduit : il n'a qu'à informer, il ne doit dépendre de rien.
_FALLBACK_HTML = (
    "<!DOCTYPE html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
    "<style>html,body{margin:0;height:100%;background:#0f172a;color:#e2e8f0;"
    "font-family:sans-serif;display:flex;align-items:center;"
    "justify-content:center;text-align:center;cursor:none;user-select:none}"
    "</style></head><body oncontextmenu=\"return false\"><div>"
    "<h1>Borne indisponible</h1><p>__MESSAGE__</p></div></body></html>"
)

_cache = {}


class AssetError(Exception):
    """Ressource d'interface introuvable, illisible ou mal formée."""


def load(name: str) -> str:
    """Contenu texte de la ressource ``assets/<name>`` (mis en cache).

    Lève :class:`AssetError` si elle est absente ou illisible ; les appelants
    publics de ce module rattrapent cette erreur et affichent un repli."""
    if name in _cache:
        return _cache[name]
    path = ASSETS_DIR / name
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise AssetError(f"ressource d'interface illisible ({path}) : {e}") from e
    _cache[name] = content
    return content


def render(template: str, values=None) -> str:
    """Remplace les marqueurs ``__NOM__`` de ``template`` par ``values[NOM]``.

    Substitution en une seule passe : le texte inséré n'est jamais réexaminé.
    Un marqueur absent de ``values`` lève :class:`AssetError` (ressource et code
    désynchronisés — à corriger, pas à masquer)."""
    values = values or {}

    def _replace(match):
        key = match.group(1)
        if key not in values:
            raise AssetError(f"marqueur inconnu dans la ressource : __{key}__")
        return values[key]

    return _PLACEHOLDER_RE.sub(_replace, template)


def render_asset(name: str, values=None) -> str:
    """``render(load(name), values)``."""
    return render(load(name), values)


def _fallback(message: str) -> str:
    """Écran minimal intégré au code, utilisé si une ressource est illisible."""
    return _FALLBACK_HTML.replace("__MESSAGE__", html.escape(message))


# ---------------------------------------------------------------------------
# Écrans HTML
# ---------------------------------------------------------------------------

def offline_html() -> str:
    """Écran « Borne hors ligne », affiché tant que la borne n'est pas
    opérationnelle (le serveur ne le sert pas : il doit fonctionner hors ligne)."""
    try:
        return load("offline.html")
    except AssetError:
        logger.exception("Écran hors ligne indisponible, repli minimal.")
        return _fallback("Connexion au serveur en cours…")


def build_config_error_html(errors) -> str:
    """Écran de refus de démarrage listant les problèmes de configuration.

    Chaque message est échappé (``html.escape``) avant insertion : une valeur de
    configuration ne peut donc pas injecter de balise dans l'écran."""
    if errors:
        items = "".join(f"<li>{html.escape(str(e))}</li>" for e in errors)
    else:
        items = "<li>Configuration invalide.</li>"
    try:
        return render_asset("config_error.html", {"ERROR_ITEMS": items})
    except AssetError:
        logger.exception("Écran d'erreur de configuration indisponible, "
                         "repli minimal.")
        joined = " ; ".join(str(x) for x in errors) or "Configuration invalide."
        return _fallback(f"Configuration invalide : {joined}")


# ---------------------------------------------------------------------------
# Scripts injectés dans les pages
# ---------------------------------------------------------------------------

def _script(name: str, values=None) -> str:
    """Charge un script injectable ; renvoie une chaîne vide (donc une injection
    sans effet) si la ressource est illisible, plutôt que de faire échouer le
    chargement de la page."""
    try:
        return render_asset(name, values)
    except AssetError:
        logger.exception("Script d'interface indisponible (%s).", name)
        return ""


def kiosk_input_script(hide_cursor: bool) -> str:
    """Menu contextuel, pinch-zoom et gestion du curseur."""
    return _script("kiosk_input.js",
                   {"HIDE_CURSOR": "true" if hide_cursor else "false"})


def kiosk_protection_script() -> str:
    """Protections kiosque des pages servies (clic droit, zoom, sélection)."""
    return _script("kiosk_protection.js")


def keyboard_script() -> str:
    """Gestionnaire de touches (F11 = plein écran via le pont pywebview)."""
    return _script("keyboard.js")


def login_script(username: str, password: str) -> str:
    """Connexion automatique : les identifiants sont sérialisés en littéraux
    JSON (``json.dumps``) AVANT insertion — ils ne peuvent donc pas casser le
    script ni y injecter de code."""
    return _script("login.js", {
        "USERNAME_JSON": json.dumps(username),
        "PASSWORD_JSON": json.dumps(password),
    })
