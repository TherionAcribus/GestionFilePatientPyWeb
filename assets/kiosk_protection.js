/*
 * Protections kiosque des pages SERVIES (clic droit, zoom double-tap, sélection
 * sur appui long) — injecté une seule fois par session
 * (ui_assets.kiosk_protection_script). Idempotent : garde window._kioskProtected.
 *
 * On NE bloque PLUS tous les touchstart : appeler preventDefault() sur chaque
 * touchstart supprime, selon le moteur WebView, le clic synthétique et rend des
 * boutons tactiles inopérants. On privilégie CSS touch-action (supprime le
 * double-tap zoom et le délai de clic tactile SANS empêcher les taps) +
 * user-select (empêche la sélection de texte sur appui long). Le pinch/zoom
 * multitouch est neutralisé par kiosk_input.js (preventDefault UNIQUEMENT si
 * plusieurs points de contact).
 */
if (!window._kioskProtected) {
    // Bloque le menu contextuel (clic droit / appui long)
    document.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        return false;
    }, false);

    // Approche CSS (préférée à un preventDefault global) :
    // - touch-action: manipulation -> désactive le double-tap zoom et le délai
    //   de 300 ms, mais laisse passer les taps -> clics OK.
    // - user-select/touch-callout: none -> pas de sélection ni de menu sur appui
    //   long. Les champs de saisie restent sélectionnables.
    var style = document.createElement('style');
    style.textContent =
        "html { touch-action: manipulation; } " +
        "* { -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; } " +
        "input, textarea { -webkit-user-select: text; user-select: text; }";
    document.head.appendChild(style);

    window._kioskProtected = true;
}
