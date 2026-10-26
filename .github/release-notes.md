## Utilisation rapide

```bash
# Le fichier téléchargé n'est pas exécutable : le rendre exécutable une fois.
chmod +x FlowelisBorne-*-linux-x86_64
./FlowelisBorne-*-linux-x86_64
```

- Premier démarrage : un assistant d'installation en 4 étapes s'ouvre
  (serveur, imprimante avec détection USB, borne, récapitulatif) ;
  `./FlowelisBorne --setup` le relance.
- `./FlowelisBorne --config` : éditeur graphique complet (ouvert aussi
  automatiquement si la configuration est refusée).
- `./FlowelisBorne --selfcheck` : diagnostic du binaire (imports, ressources).
- Le binaire crée lui-même son démarrage automatique et ses raccourcis (menu
  des applications, bureau) ; `--install-shortcuts` / `--remove-shortcuts`
  pour forcer ou retirer.
- Touche **F10** sur la borne en service (clavier branché) : ouvre la
  configuration (code PIN facultatif), puis la borne redémarre d'elle-même.

La variante `.tar.gz` contient le même programme en dossier (démarrage plus
rapide). Reste à installer côté borne : paquets système Qt/WebEngine/USB,
règle udev imprimante — voir `COMPILATION.md` §5 (`install.sh`
couvre tout depuis le dépôt). Ne fonctionne pas sur Raspberry Pi (ARM).
