/*
 * Menu contextuel, pinch-zoom et curseur — injecté à CHAQUE chargement de page
 * (ui_assets.kiosk_input_script). Idempotent : garde window._contextMenuDisabled.
 *
 * Multitouch : preventDefault UNIQUEMENT si plusieurs points de contact
 * (pinch/zoom). Un tap simple doit laisser passer le clic synthétique, sinon des
 * boutons deviennent inopérants selon le moteur WebView.
 *
 * Curseur : masqué par défaut (borne tactile en libre-service) MAIS réapparaît
 * dès qu'une souris est utilisée (maintenance) puis se remasque au toucher
 * suivant. hide_cursor=False (configuration) force l'affichage permanent.
 * Les faux 'mousemove' générés par le tactile sont ignorés.
 *
 * Marqueur HIDE_CURSOR (voir plus bas) -> true / false. Ne jamais écrire un
 * marqueur (double blanc soulignés autour du nom) dans ce commentaire : il y
 * serait substitué comme dans le code.
 */
if (!window._contextMenuDisabled) {
    // Désactive le menu contextuel
    window.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        return false;
    }, true);

    // Ne bloque QUE le multitouch (pinch/zoom) : les taps simples passent
    // normalement (clic synthétique préservé).
    window.addEventListener('touchstart', function(e) {
        if (e.touches.length > 1) {
            e.preventDefault();
        }
    }, {passive: false, capture: true});

    var hideCursor = __HIDE_CURSOR__;
    if (hideCursor) {
        // Curseur masqué tant que la classe 'using-mouse' est absente ;
        // une souris qui bouge la pose, un toucher la retire.
        var style = document.createElement('style');
        style.textContent = "html:not(.using-mouse) * { cursor: none !important; }";
        document.head.appendChild(style);

        var lastTouch = 0;
        window.addEventListener('touchstart', function() {
            lastTouch = Date.now();
            document.documentElement.classList.remove('using-mouse');
        }, true);
        window.addEventListener('mousemove', function() {
            // Ignore les 'mousemove' synthétiques émis juste après un toucher.
            if (Date.now() - lastTouch < 800) { return; }
            document.documentElement.classList.add('using-mouse');
        }, true);
    }

    window._contextMenuDisabled = true;
}
