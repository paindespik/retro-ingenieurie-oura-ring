# Oura Ring 5 — setup local, sans cloud ni abonnement

Documentation de l'installation réalisée le **21 septembre 2026**.
Objectif : lire **toutes** les données de l'anneau en Bluetooth, sans l'app officielle,
les stocker en SQLite, les répliquer sur `serv` et les exposer sur un dashboard web privé.

> **État du 22/09** : Phase 2 livrée — première nuit dérivée (score estimé 85/100),
> briefing LLM local quotidien, page Activité (bins MET + segments d'effort), axe
> horaire par epoch de boot. Voir [`docs/etat-des-lieux.md`](docs/etat-des-lieux.md).

> Le plan d'origine est dans `~/travail/oura-plan.md`. Ce dossier documente ce qui
> **tourne réellement**, y compris les cinq pièges qui ne figuraient pas dans le plan.

---

## 1. Identité de l'anneau

| Élément | Valeur |
|---|---|
| Modèle | Oura Ring 5 (caractéristiques GATT `98ed0004/05/06` présentes) |
| Numéro de série | `RING_SERIAL` |
| Firmware | `2.1.25` (API `2.1.0`) |
| Adresse d'identité BLE | `XX:XX:XX:XX:XX:XX` (aléatoire statique) |
| Nom annoncé — non provisionné | `Oura RING_SERIAL` |
| Nom annoncé — provisionné | `Oura Ring 5` |
| Service GATT | `98ed0001-a541-11e4-b6a0-0002a5d5c51b` (write `…0002`, notify `…0003`) |
| Fabricant (manufacturer data) | `0x02B2` |
| Clé app-auth | `~/.oura/ring.key` (16 octets hex) — **sauvegarde-la** |

## 2. Architecture

```
  Oura Ring 5
       │  BLE (protocole reverse-engineered, open_oura)
       ▼
  workstation ── oura-agent.service (agent d'appairage auto-accept)
       │   ── oura-sync.timer  (30 min)  → oura-run.sh sync → ~/.oura/oura.db
       │   ── oura-push.sh (VACUUM INTO + rsync)
       ▼
  serv ── /srv/oura/inbox/inbox-snapshot.db
       ── oura-swap.timer (15 min) → swap atomique → /srv/oura/oura.db
       ── /srv/oura/derived.db  (tables dérivées : scores, hypnogramme, briefings)
       ── oura-web.service (FastAPI, 127.0.0.1:8091)
       ▼
  nginx TLS + auth basique → https://oura.example.com
```

- **Le PC Arch est le seul hôte BLE** (un anneau ne se connecte qu'à un central à la fois).
- **Aucun cloud Oura, aucun LLM externe** : llama-swap local pour les analyses.

## 3. Accès

- Dashboard : **https://oura.example.com** — Basic Auth, utilisateur `oura`.
  Le mot de passe est dans le fichier htpasswd de la machine
  (`/etc/nginx/conf.d/oura.htpasswd` sur `serv`) ; il n'est **pas** dans ce dépôt.
  Rotation : `sudo htpasswd /etc/nginx/conf.d/oura.htpasswd oura && sudo systemctl reload nginx`
  (penser à mettre à jour `phone/scanner/secrets.properties` et à reconstruire l'app).
- API JSON : `/api/overview`, `/api/activity?date=…`, `/api/nights`, `/api/night?date=…`,
  `/api/trends?days=90`, `/api/briefing`, `/api/telemetry?hours=24`, `/api/events`,
  `/api/health`, `POST /api/chat`

## 4. Documentation détaillée

| Fichier | Contenu |
|---|---|
| [`docs/appairage.md`](docs/appairage.md) | **Procédure complète d'appairage** (reset → clé → capteurs → sync), à rejouer en cas de perte |
| [`docs/exploitation.md`](docs/exploitation.md) | Commandes du quotidien, sync manuel, dépannage |
| [`docs/pieges-bluez.md`](docs/pieges-bluez.md) | **Les 5 pièges** qui ont coûté une soirée — à lire avant toute intervention BLE |
| [`docs/serveur.md`](docs/serveur.md) | Installation complète côté `serv` (nginx, TLS, systemd, base, web) |
| [`docs/etat-des-lieux.md`](docs/etat-des-lieux.md) | Ce qui tourne, ce qui reste à faire |
| [`docs/etude-sync-telephone.md`](docs/etude-sync-telephone.md) | **Migration BLE vers le téléphone** : étude, bascule effectuée, pièges Android (Doze, scans BLE, callbacks API 33+) |

## 5. Secrets et construction de l'app téléphone

**Aucun secret n'est versionné.** Trois éléments vivent hors du dépôt :

| Secret | Emplacement | Rôle |
|---|---|---|
| Clé app-auth de l'anneau (16 o) | `~/.oura/ring.key` (PC), `files/ring.key.hex` (téléphone) | authentification auprès de l'anneau |
| Basic Auth du portail | `/etc/nginx/conf.d/oura.htpasswd` (serv) | accès au dashboard et à l'ingest |
| Token d'ingest (`X-Oura-Token`) | `/etc/oura/ingest.env` (serv, `root:oura` 0640) | identifie la source d'un push |

Côté serveur, `oura-web.service` charge le token via `EnvironmentFile=` ; si la
variable manque, l'endpoint d'ingest répond 503 plutôt que d'accepter un token
par défaut. Côté téléphone, Gradle lit `phone/scanner/secrets.properties`
(gitignoré) et l'injecte dans `BuildConfig` ; un build sans secrets refuse de
solliciter l'anneau.

Construction complète depuis un clone frais :

```sh
rustup target add aarch64-linux-android          # cible Android
export ANDROID_NDK=/opt/android-ndk              # NDK r27+
cp phone/scanner/secrets.properties.example phone/scanner/secrets.properties
$EDITOR phone/scanner/secrets.properties         # renseigner les valeurs
./phone/build.sh                                 # core Rust + APK
```

Le `.so` ARM64 n'est pas versionné (artefact) : `phone/build.sh` le reconstruit
et le place dans `jniLibs/`. L'APK sort dans
`phone/scanner/app/build/outputs/apk/debug/`. Pose de la clé sur le téléphone :

```sh
adb shell "run-as io.github.paindespik.ourascan sh -c 'cat > files/ring.key.hex'" < ~/.oura/ring.key
```

## 6. Contenu du dossier

```
phone/
  build.sh                 construit le core Rust (ARM64) puis l'APK
  core/                    core Rust : FFI/JNI + crates open_oura vendorisés
  scanner/                 app Android (GATT, WorkManager, push HTTPS)
scripts/
  oura-agent.py            agent d'appairage D-Bus auto-accept (→ /usr/local/bin/)
  oura-run.sh              lanceur de commandes avec reconnexion fiable (→ ~/.local/bin/)
  oura-push.sh             snapshot + rsync vers serv          (→ ~/.local/bin/)
  bootstrap-appairage.sh   tout-en-un post-reset : clé + capteurs + premier sync
bin/
  nightly.sh               job 05:35 sur serv (Phase 2 : dérive + score + briefing)
serveur/                   miroir des fichiers déployés sur serv (source canonique)
  oura_web.py              dashboard FastAPI + ingest /ingest/events (→ /srv/oura/web/)
  swap.sh                  merge idempotent du snapshot PC (→ /srv/oura/bin/)
  derive_night.py          dérivation des nuits (stdlib Python, → /srv/oura/bin/)
  make_synth.py            générateur de données synthétiques (tests)
  test-run.sh              lance le dashboard en local sur les bases de test/
systemd/
  pc-user/     oura-sync.{service,timer}
  pc-system/   oura-agent.service
  serveur/     oura-web.service, oura-swap.timer, oura-nightly.timer, nginx-oura.conf
```

## 7. Sources amont

- [`Th0rgal/open_oura`](https://github.com/Th0rgal/open_oura) — client Rust (cloné dans `~/src/open_oura`)
- [`LogosIsLife/open_ring`](https://github.com/LogosIsLife/open_ring) — spécification protocole (Ring 4)
- Bug BlueZ rencontré : [bluez/bluez#2282](https://github.com/bluez/bluez/issues/2282)
