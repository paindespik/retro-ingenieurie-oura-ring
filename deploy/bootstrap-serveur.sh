#!/usr/bin/env bash
# À lancer UNE FOIS sur le serveur, en root, pour activer le déploiement continu :
#   - installe /usr/local/sbin/oura-deploy (root:root, jamais modifié par la CI) ;
#   - autorise <utilisateur> à le lancer sans mot de passe, SANS argument ;
#   - ajoute la clé publique de la CI avec une commande forcée (elle ne peut rien
#     faire d'autre que livrer une archive à oura-deploy).
#
#   sudo deploy/bootstrap-serveur.sh <utilisateur_ssh> <clé_ci.pub>
#
# Relancer ce script après toute modification de deploy/oura-deploy.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "à lancer avec sudo" >&2; exit 1; }
USER_SSH=${1:?usage : $0 <utilisateur_ssh> <clé_ci.pub>}
PUB=${2:?usage : $0 <utilisateur_ssh> <clé_ci.pub>}
HERE=$(cd "$(dirname "$0")" && pwd)

install -o root -g root -m 0755 "$HERE/oura-deploy" /usr/local/sbin/oura-deploy
install -d -o root -g root -m 0750 /srv/oura/releases

tmp=$(mktemp)
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/oura-deploy ""\n' "$USER_SSH" > "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/oura-deploy
rm -f "$tmp"

key=$(grep -E '^ssh-(ed25519|rsa) ' "$PUB" | head -n1)
[ -n "$key" ] || { echo "clé publique invalide : $PUB" >&2; exit 1; }
home=$(getent passwd "$USER_SSH" | cut -d: -f6)
ak="$home/.ssh/authorized_keys"
install -d -o "$USER_SSH" -g "$USER_SSH" -m 0700 "$home/.ssh"
touch "$ak"
line="restrict,command=\"sudo -n /usr/local/sbin/oura-deploy\" $key"
if ! grep -qF "$key" "$ak"; then
  printf '%s\n' "$line" >> "$ak"
fi
chown "$USER_SSH:$USER_SSH" "$ak"
chmod 0600 "$ak"
echo "bootstrap OK : oura-deploy installé, règle sudo et clé CI en place"
