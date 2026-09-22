# oura-ring — récupération locale des données d'un Oura Ring 5

Lecture des données brutes d'un anneau **Oura Ring 5** sans passer par le cloud
Oura ni l'application officielle : dialogue BLE direct avec l'anneau, décodage
du protocole, stockage SQLite, puis portail web auto-hébergé pour la
consultation et les analyses.

Deux hôtes BLE sont possibles, au choix :

- **un PC Linux** (BlueZ), via le CLI [`open_oura`](https://github.com/Th0rgal/open_oura) ;
- **un téléphone Android**, via l'application incluse dans `phone/` — le cœur
  protocolaire en Rust est partagé, exposé en bibliothèque native.

> ⚠️ **Un anneau ne dialogue qu'avec un seul appareil appairé.** Choisir un hôte
> BLE, c'est en exclure les autres jusqu'à une remise à zéro de l'anneau.

## Fonctionnement

```
       BLE (GATT chiffré, clé applicative)
anneau ──────────────► hôte (PC ou téléphone)
                            │  décodage + SQLite
                            ▼
                       serveur : base + portail FastAPI + dérivations
                       (nginx TLS + authentification basique)
```

- L'anneau publie un service GATT propriétaire. Après appairage BLE classique,
  une **clé applicative de 16 octets** que vous générez est installée sur
  l'anneau ; elle conditionne ensuite chaque session.
- L'hôte vide le tampon d'événements de l'anneau (curseur incrémental,
  reprise après coupure) et stocke les événements bruts **et** décodés.
- Le serveur expose un portail et calcule localement les métriques dérivées ;
  aucun service tiers n'est sollicité.

## Prérequis

| Composant | Détail |
|---|---|
| Anneau | Oura Ring 5 (`COR_*`), **remis à zéro** ou jamais appairé à l'app officielle |
| Hôte PC | Linux + BlueZ ≥ 5.6x, Rust, `open_oura` |
| Hôte téléphone | Android 12+ (API 31), Rust avec la cible `aarch64-linux-android`, NDK r27+ |
| Serveur | Python 3.11+, SQLite, nginx, systemd |

## Mise en route

### 1. Appairer l'anneau

L'anneau doit être **vierge de tout appairage** : sorti d'usine, ou remis à
zéro depuis l'application officielle (le compte associé est alors dissocié).

Depuis le PC, après une remise à zéro, dans une seule fenêtre de connexion :

```sh
scripts/bootstrap-appairage.sh
```

Le script attend l'annonce de l'anneau, établit le lien, installe la clé
applicative (`~/.oura/ring.key`, créée si absente), active les capteurs puis
lance une première synchronisation.

> **Conservez cette clé.** Sans elle, l'anneau devient muet et seule une remise
> à zéro permet d'en installer une nouvelle.

Notez l'adresse d'identité BLE affichée et déclarez-la localement :

```sh
mkdir -p ~/.oura && echo 'OURA_ID=XX:XX:XX:XX:XX:XX' >> ~/.oura/config
```

### 2. Synchroniser depuis le PC

```sh
scripts/oura-run.sh sync     # vide le tampon de l'anneau vers ~/.oura/oura.db
scripts/oura-run.sh info     # firmware, batterie, numéro de série
scripts/oura-push.sh         # envoie un instantané vers le serveur
```

`systemd/pc-user/` contient un timer pour automatiser le cycle.

### 3. Serveur

```sh
# base et code
install -d /srv/oura/{web,bin,inbox}
cp serveur/oura_web.py /srv/oura/web/
cp serveur/{swap.sh,derive_night.py} /srv/oura/bin/

# secret d'ingestion (voir « Secrets »)
install -d /etc/oura
printf 'OURA_PHONE_TOKEN=%s\n' "$(openssl rand -hex 24)" > /etc/oura/ingest.env
chown root:oura /etc/oura/ingest.env && chmod 640 /etc/oura/ingest.env

# services
cp systemd/serveur/*.{service,timer} /etc/systemd/system/
cp systemd/serveur/nginx-oura.conf /etc/nginx/sites-available/oura.conf   # adapter le domaine
htpasswd -c /etc/nginx/conf.d/oura.htpasswd oura                          # accès au portail
systemctl daemon-reload && systemctl enable --now oura-web.service
```

Le portail écoute derrière nginx (TLS + authentification basique) et expose
`/api/overview`, `/api/activity`, `/api/nights`, `/api/night`, `/api/trends`,
`/api/briefing`, `/api/telemetry`, `/api/events`, `/api/health`, ainsi que
`POST /ingest/events` pour les hôtes qui poussent directement.

### 4. Hôte téléphone (optionnel)

L'application Android assure le cycle complet : connexion BLE, décodage via le
cœur Rust, stockage local et envoi HTTPS vers le serveur, en arrière-plan.

```sh
rustup target add aarch64-linux-android
export ANDROID_NDK=/opt/android-ndk
cp phone/scanner/secrets.properties.example phone/scanner/secrets.properties
$EDITOR phone/scanner/secrets.properties      # serveur, identifiants, jeton, série
./phone/build.sh                              # cœur Rust + APK
adb install -r phone/scanner/app/build/outputs/apk/debug/app-debug.apk
```

Installez ensuite la clé applicative dans l'espace privé de l'application :

```sh
adb shell "run-as <applicationId> sh -c 'cat > files/ring.key.hex'" < ~/.oura/ring.key
```

L'écran principal propose une synchronisation immédiate, l'enregistrement du
cycle périodique, et — pour un anneau vierge — l'appairage complet (lien BLE,
installation de la clé, activation des capteurs, première synchronisation).

Pour que les cycles tiennent en veille, exempter l'application des
optimisations de batterie :

```sh
adb shell dumpsys deviceidle whitelist +<applicationId>
```

## Secrets

**Aucun secret n'est versionné.** Quatre éléments vivent hors du dépôt :

| Secret | Emplacement | Rôle |
|---|---|---|
| Clé applicative (16 o) | `~/.oura/ring.key`, et `files/ring.key.hex` côté téléphone | authentification auprès de l'anneau |
| Adresse BLE de l'anneau | `~/.oura/config` | cible des connexions du PC |
| Authentification du portail | fichier htpasswd de nginx | accès au portail et à l'ingestion |
| Jeton d'ingestion | `/etc/oura/ingest.env` (`root:oura`, 0640) | identifie la source d'un envoi |

Côté serveur, le jeton est chargé par `EnvironmentFile=` ; s'il manque,
`/ingest/events` répond 503 plutôt que d'accepter une valeur par défaut. Côté
téléphone, Gradle lit `phone/scanner/secrets.properties` (ignoré par Git) et
l'injecte dans `BuildConfig` ; un build dépourvu de secrets refuse de
solliciter l'anneau.

## Organisation du dépôt

```
phone/
  build.sh                 construit le cœur Rust (ARM64) puis l'APK
  core/                    cœur Rust : FFI/JNI + crates open_oura vendorisés
  scanner/                 application Android (GATT, cycles, envoi HTTPS)
scripts/
  bootstrap-appairage.sh   appairage complet après remise à zéro
  oura-run.sh              exécution d'une commande avec reconnexion fiable
  oura-push.sh             instantané + envoi vers le serveur
  oura-agent.py            agent D-Bus d'acceptation d'appairage
serveur/
  oura_web.py              portail FastAPI + point d'ingestion
  swap.sh                  fusion idempotente d'un instantané dans la base
  derive_night.py          dérivation des nuits
  make_synth.py            génération de données synthétiques (tests)
  test-run.sh              portail en local sur les bases de test
bin/
  nightly.sh               traitement nocturne (dérivations, résumés)
systemd/                   unités PC et serveur, modèle de configuration nginx
```

Le cœur Rust est compilé en bibliothèque native (`cdylib`) exposant une
interface C/JNI ; l'application Android pilote le transport GATT et alimente la
machine à états protocolaire. L'artefact `.so` n'est pas versionné :
`phone/build.sh` le reconstruit.

## Notes techniques

- **Un seul appareil appairé à la fois** : la reprise de l'anneau par un autre
  hôte exige une remise à zéro préalable.
- **Ne pas réinstaller l'application officielle** sur un téléphone utilisé comme
  hôte : elle ré-enrôlerait l'anneau avec sa propre clé.
- **Déduplication** : la table des événements porte une contrainte d'unicité
  `(série, type, horodatage, contenu)`, ce qui rend les envois répétés
  inoffensifs et la fusion d'instantanés idempotente.
- **Tampon de l'anneau** : plusieurs jours d'événements sont conservés à bord,
  donc une interruption de plusieurs heures n'entraîne aucune perte.

## Sources amont

- [`Th0rgal/open_oura`](https://github.com/Th0rgal/open_oura) — CLI et crates
  `oura-protocol` / `oura-link` / `oura-store` (vendorisés dans `phone/core/`).
- [`LogosIsLife/open_ring`](https://github.com/LogosIsLife/open_ring) —
  documentation du protocole.

## Licence

MIT — voir [`LICENSE`](LICENSE).

Le dossier `phone/core/vendor/` contient une copie figée de crates du projet
[`open_oura`](https://github.com/Th0rgal/open_oura) (Thomas Marchand), sous
licence MIT également : licence et provenance dans
[`phone/core/vendor/LICENSE`](phone/core/vendor/LICENSE) et
[`NOTICE.md`](phone/core/vendor/NOTICE.md).

## Avertissement

Projet personnel publié tel quel, **sans garantie**, à des fins
d'interopérabilité avec un appareil dont on est propriétaire.

Ce dépôt ne contient **aucun code, micrologiciel, ressource ou clé** provenant
d'Ōura Health Oy : il implémente un client indépendant qui dialogue avec
l'anneau au moyen d'une clé applicative que l'utilisateur génère lui-même.

« Oura » est une marque d'Ōura Health Oy ; elle n'est employée ici qu'à titre
descriptif, pour désigner l'appareil avec lequel ce logiciel est compatible. Ce
projet n'est ni affilié à Ōura Health Oy, ni approuvé ou soutenu par elle.

L'utilisation d'un client tiers est susceptible de contrevenir aux conditions
d'utilisation du service officiel et d'affecter la garantie de l'appareil :
chacun reste responsable de son propre usage.
