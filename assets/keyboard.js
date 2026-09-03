/*
 * Gestionnaire de touches de la borne (ui_assets.keyboard_script) : F11 bascule
 * le plein écran via le pont pywebview au lieu du plein écran natif du moteur.
 */
document.addEventListener('keydown', function(event) {
    if (event.key === 'F11') {
        event.preventDefault();  // Empêche le comportement par défaut du navigateur
        window.pywebview.api.window.toggle_fullscreen();
    }
});
