# Code tiers vendorisé

Ce dossier contient une copie figée de trois crates du projet
[`open_oura`](https://github.com/Th0rgal/open_oura) de **Thomas Marchand** :

| Crate | Rôle |
|---|---|
| `oura-protocol` | trames, authentification, décodeurs d'événements |
| `oura-link` | client de haut niveau, vidage du tampon, curseur |
| `oura-store` | schéma SQLite et persistance |

- **Version figée** : commit `945470c3f45cc4add1bf5ffe1020a6d0ba56ae21` (2026-09-18).
- **Licence** : MIT — voir `LICENSE` dans ce dossier, conservé tel quel.

Ces crates sont vendorisées plutôt que référencées pour figer la version
compilée dans la bibliothèque native Android et garantir des builds
reproductibles.

## Modifications locales

- `oura-store` : ajout de `recent_events(serial, limit)`, qui renvoie les
  derniers événements (contenu en hexadécimal) afin d'alimenter l'envoi
  différentiel vers le serveur.
- `oura-link` : compilation conditionnelle du transport BLE de bureau
  (`btleplug`), inutile sur Android où le transport est fourni par l'hôte Java.

Toute autre différence avec l'amont n'est pas intentionnelle.
