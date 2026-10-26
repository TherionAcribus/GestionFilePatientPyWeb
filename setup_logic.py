"""Logique pure de l'assistant de premier lancement (``setup_wizard.py``).

Sans tkinter ni matériel : validation de chaque étape, construction de la
configuration finale, récapitulatif et tri des périphériques USB détectés.
Testable sans afficher de fenêtre.
"""

from dataclasses import fields

import editor_logic
import maintenance_pin
from config import Settings

#: Étapes de l'assistant, dans l'ordre.
STEPS = ("server", "printer", "borne", "summary")

#: Champs de ``Settings`` saisis dans l'assistant.
FORM_FIELDS = ("base_url", "app_secret", "printer_id_vendor",
               "printer_id_product", "printer_model", "check_paper",
               "borne_id", "fullscreen", "hide_cursor", "debug")

#: Options proposées pour une borne au premier lancement : kiosque plein
#: écran, curseur masqué, mode production. Les valeurs par défaut du code
#: restent orientées développement (fenêtre, debug).
RECOMMENDED_KIOSK = {"fullscreen": True, "hide_cursor": True, "debug": False}

#: Fabricants d'imprimantes ticket courants (nom affiché à la détection).
KNOWN_PRINTER_VENDORS = {
    0x04B8: "Epson",
    0x0519: "Star Micronics",
    0x1504: "Bixolon",
    0x1D90: "Citizen",
    0x0DD4: "Custom",
    0x154F: "SNBC",
}

_EPSON = 0x04B8
_USB_HUB_CLASS = 9
_USB_PRINTER_CLASS = 7
_LINUX_FOUNDATION = 0x1D6B   # hubs racine virtuels sous Linux

#: Test serveur/imprimante : pas encore lancé, réussi, échoué.
TEST_NOT_RUN, TEST_OK, TEST_FAILED = "not_run", "ok", "failed"


def initial_values(settings: Settings) -> dict:
    """Valeurs de départ des champs de l'assistant.

    Reprend la configuration existante (identifiants imprimante pré-remplis
    par install.sh, ou assistant relancé avec ``--setup``). Tant que la
    borne n'a jamais été configurée, les options kiosque recommandées
    remplacent les valeurs par défaut du code, orientées développement."""
    values = {name: getattr(settings, name) for name in FORM_FIELDS}
    if not settings.setup_completed:
        values.update(RECOMMENDED_KIOSK)
        # L'URL par défaut du code (serveur de développement local) n'a pas
        # à être proposée : sur une vraie borne, elle serait toujours fausse.
        if values["base_url"] == Settings().base_url:
            values["base_url"] = ""
    values.update(pin_new="", pin_confirm="", pin_remove=False)
    return values


def _clean(values: dict) -> dict:
    """Retire les espaces de bordure des champs texte (copier-coller)."""
    return {k: v.strip() if isinstance(v, str) else v for k, v in values.items()}


def step_errors(step: str, values: dict) -> list:
    """Problèmes bloquant le passage à l'étape suivante (liste vide = OK).

    Chaque étape ne vérifie que ses propres champs. La règle HTTP/HTTPS, qui
    dépend du mode choisi plus loin (étape « borne »), n'est vérifiée qu'à
    la fin (``build_settings``)."""
    values = _clean(values)
    if step == "server":
        # Forme de l'URL seulement : http est toléré ici (debug=True).
        errors = Settings(base_url=values["base_url"], debug=True).base_url_errors()
        if not values["app_secret"]:
            errors.append("Renseignez le secret d'application (fourni par "
                          "l'administrateur Flowelis).")
        return errors
    if step == "printer":
        probe = Settings()
        errors = (probe.usb_id_errors("printer_id_vendor", values["printer_id_vendor"])
                  + probe.usb_id_errors("printer_id_product",
                                        values["printer_id_product"]))
        if not values["printer_model"]:
            errors.append("Le modèle d'imprimante ne peut pas être vide.")
        return errors
    if step == "borne":
        _, pin_error = maintenance_pin.resolve_pin_change(
            values["pin_new"], values["pin_confirm"], values["pin_remove"], "")
        return [pin_error] if pin_error else []
    return []


def build_settings(current: Settings, values: dict):
    """Configuration finale à enregistrer : ``(settings, erreurs)``.

    Part de la configuration courante (les champs absents de l'assistant
    sont conservés), applique la saisie, le code PIN, marque la borne comme
    configurée, puis applique les mêmes contrôles que l'éditeur complet. Si
    ``erreurs`` n'est pas vide, ``settings`` vaut None."""
    values = _clean(values)
    data = {f.name: getattr(current, f.name) for f in fields(Settings)}
    data.update({name: values[name] for name in FORM_FIELDS})
    settings = Settings(**data)

    pin_hash, pin_error = maintenance_pin.resolve_pin_change(
        values["pin_new"], values["pin_confirm"], values["pin_remove"],
        current.maintenance_pin_hash)
    if pin_error:
        return None, [pin_error]
    settings.maintenance_pin_hash = pin_hash
    settings.setup_completed = True

    errors = settings.validate()
    if settings.base_url_errors() and settings.base_url.startswith("http://"):
        errors.append("Utilisez une adresse https:// (recommandé) ou, pour un "
                      "serveur du réseau local sans HTTPS, cochez « Mode "
                      "développement » à l'étape « Borne ».")
    cred_error = editor_logic.default_credentials_error(settings)
    if cred_error:
        errors.append(cred_error)
    if errors:
        return None, errors
    return settings, []


def _yes_no(value: bool) -> str:
    return "oui" if value else "non"


def summary_lines(values: dict, server_test: str, printer_test: str,
                  pin_already_set: bool = False) -> list:
    """Lignes du récapitulatif (sans aucun secret)."""
    values = _clean(values)
    test_label = {TEST_OK: "testé ✔", TEST_FAILED: "échec du test ✘",
                  TEST_NOT_RUN: "non testé"}
    if values["pin_new"]:
        pin = "nouveau code défini"
    elif values["pin_remove"]:
        pin = "supprimé"
    elif pin_already_set:
        pin = "inchangé"
    else:
        pin = "aucun"
    return [
        f"Serveur : {values['base_url']} ({test_label[server_test]})",
        "Secret d'application : renseigné",
        (f"Imprimante : {values['printer_model']} — "
         f"{values['printer_id_vendor']} / {values['printer_id_product']} "
         f"({test_label[printer_test]})"),
        f"Vérification du papier : {_yes_no(values['check_paper'])}",
        f"Identifiant de la borne : {values['borne_id'] or 'nom de la machine'}",
        f"Plein écran : {_yes_no(values['fullscreen'])}",
        f"Curseur masqué : {_yes_no(values['hide_cursor'])}",
        ("Mode : DÉVELOPPEMENT (déconseillé en service)" if values["debug"]
         else "Mode : production"),
        f"Code PIN de maintenance : {pin}",
    ]


def usb_candidates(devices) -> list:
    """Périphériques USB susceptibles d'être l'imprimante, du plus probable
    au moins probable : ``[(vendeur "0x…", produit "0x…", libellé)]``.

    ``devices`` : dictionnaires ``vendor``, ``product``, ``device_class`` et
    ``interface_classes`` (lus par l'assistant via pyusb). Les concentrateurs
    sont écartés ; viennent d'abord les interfaces de classe imprimante,
    puis les fabricants connus (Epson en tête), comme install.sh."""
    found = {}
    for dev in devices:
        vendor, product = dev.get("vendor"), dev.get("product")
        if not isinstance(vendor, int) or not isinstance(product, int):
            continue
        if vendor == _LINUX_FOUNDATION or dev.get("device_class") == _USB_HUB_CLASS:
            continue
        found.setdefault((vendor, product), set()).update(
            dev.get("interface_classes") or ())

    def rank(item):
        (vendor, _product), classes = item
        return (_USB_PRINTER_CLASS not in classes,
                vendor not in KNOWN_PRINTER_VENDORS,
                vendor != _EPSON)

    result = []
    for (vendor, product), classes in sorted(found.items(), key=rank):
        name = KNOWN_PRINTER_VENDORS.get(vendor, "fabricant inconnu")
        kind = " — imprimante" if _USB_PRINTER_CLASS in classes else ""
        result.append((f"0x{vendor:04x}", f"0x{product:04x}",
                       f"{name} (0x{vendor:04x} / 0x{product:04x}){kind}"))
    return result
