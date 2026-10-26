## Utilisation rapide

```bash
# Le fichier téléchargé n'est pas exécutable : le rendre exécutable une fois.
chmod +x FlowelisBorne-*-linux-x86_64
./FlowelisBorne-*-linux-x86_64
```

- `./FlowelisBorne --config` : éditeur graphique de configuration (ouvert
  automatiquement au premier démarrage ou si la configuration est refusée).
- `./FlowelisBorne --selfcheck` : diagnostic du binaire (imports, ressources).
- Touche **F10** sur la borne en service (clavier branché) : ouvre la
  configuration (code PIN facultatif), puis la borne redémarre d'elle-même.

La variante `.tar.gz` contient le même programme en dossier (démarrage plus
rapide). Reste à installer côté borne : paquets système Qt/WebEngine/USB,
règle udev imprimante et autostart — voir `COMPILATION.md` §5 (`install.sh`
couvre tout depuis le dépôt). Ne fonctionne pas sur Raspberry Pi (ARM).
