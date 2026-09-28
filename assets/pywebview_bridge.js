/*
 * Pont pywebview compatible avec la CSP du serveur.
 *
 * pywebview 6 génère ses méthodes publiques avec `new Function`, bloqué par
 * `script-src 'self'`. Les méthodes Python restent toutefois accessibles par
 * `_jsApiCallback` ; ce fichier recrée explicitement les seules fonctions
 * attendues par les pages patient, sans `eval` ni `Function`.
 *
 * Idempotent : réinjecté après chaque navigation dans un contexte JS neuf.
 */
(function() {
    if (!window.pywebview || window.__bornePywebviewBridge) {
        return;
    }
    window.__bornePywebviewBridge = true;

    function invoke(funcName, args) {
        var callArgs = Array.prototype.slice.call(args);
        var send;
        if (typeof window.pywebview._jsApiCallback === 'function') {
            send = function(callId) {
                window.pywebview._jsApiCallback(funcName, callArgs, callId);
            };
        } else if (window.pywebview._bridge &&
                   typeof window.pywebview._bridge.call === 'function') {
            // Compatibilité pywebview 5.
            send = function(callId) {
                window.pywebview._bridge.call(funcName, callArgs, callId);
            };
        } else {
            return Promise.reject(new Error('Pont PyWebView indisponible'));
        }

        var callbackStore = window.pywebview._returnValuesCallbacks ||
                            window.pywebview._returnValues;
        if (!callbackStore ||
            typeof window.pywebview._checkValue !== 'function') {
            return Promise.reject(
                new Error('Gestion des réponses PyWebView indisponible'));
        }

        callbackStore[funcName] = callbackStore[funcName] || {};
        var callId = (Math.random() + '').substring(2);
        var promise = new Promise(function(resolve, reject) {
            window.pywebview._checkValue(funcName, resolve, reject, callId);
        });
        send(callId);
        return promise;
    }

    window.pywebview.api = window.pywebview.api || {};
    window.pywebview.api.printer = window.pywebview.api.printer || {};
    window.pywebview.api.printer.print_ticket = function(printData, printJobId) {
        return invoke('printer.print_ticket', arguments);
    };
    // Capacité déclarée explicitement : la page ne doit JAMAIS rejouer un
    // appel après une erreur pour détecter l'arité — une erreur postérieure à
    // l'envoi pourrait déjà avoir imprimé le ticket.
    window.pywebview.api.printer.print_ticket.supportsPrintJobId = true;

    window.pywebview.api.window = window.pywebview.api.window || {};
    window.pywebview.api.window.toggle_fullscreen = function() {
        return invoke('window.toggle_fullscreen', arguments);
    };
})();
