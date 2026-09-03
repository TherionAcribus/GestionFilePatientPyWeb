/*
 * Connexion automatique de la borne à la page /login du serveur
 * (ui_assets.login_script).
 *
 * Les marqueurs USERNAME_JSON / PASSWORD_JSON (voir plus bas) sont remplacés
 * par des LITTÉRAUX JSON (json.dumps côté Python, guillemets inclus). Un
 * guillemet, un antislash, un saut de ligne ou tout autre caractère spécial du
 * mot de passe ne peut donc ni casser ce script ni y injecter du code.
 *
 * ATTENTION : ne jamais écrire un marqueur (double blanc soulignés autour du
 * nom) dans ce commentaire — il y serait substitué, et un mot de passe
 * contenant une fin de commentaire pourrait alors s'en échapper.
 */
function performLogin() {
    console.log("Injecting login script");
    var usernameInput = document.querySelector('input[name="username"]');
    var passwordInput = document.querySelector('input[name="password"]');
    var rememberCheckbox = document.querySelector('input[name="remember"]');

    if (usernameInput) {
        console.log("Found username input");
        usernameInput.value = __USERNAME_JSON__;
    } else {
        console.log("Username input not found");
    }

    if (passwordInput) {
        console.log("Found password input");
        passwordInput.value = __PASSWORD_JSON__;
    } else {
        console.log("Password input not found");
    }

    if (rememberCheckbox) {
        console.log("Found remember me checkbox");
        rememberCheckbox.checked = true;
    } else {
        console.log("Remember me checkbox not found");
    }

    var form = usernameInput ? usernameInput.closest('form') : null;
    if (form) {
        console.log("Found form, submitting");
        form.submit();
    } else {
        console.log("Form not found");
    }
}

// Vérifie si le DOM est déjà chargé
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', performLogin);
} else {
    performLogin();
}
