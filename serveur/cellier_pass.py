#!/usr/bin/env python3
"""Génère le hash scrypt du 2e mot de passe « Cellier ».

Le mot de passe vient de l'environnement CELLIER_PASS (jamais des arguments :
il ne reste ni dans l'historique de commandes ni dans la liste des processus).
Sortie : la ligne à ajouter dans /etc/oura/ingest.env (hors dépôt), puis :

    sudo systemctl restart oura-web

    export CELLIER_PASS='…'   # sur la machine qui génère
    python3 serveur/cellier_pass.py
"""
import hashlib
import os
import sys

def main():
    pw = os.environ.get("CELLIER_PASS")
    if not pw:
        sys.exit("CELLIER_PASS manquant (exportez le mot de passe, ne le tapez pas en argument)")
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    print(f"CELLIER_HASH=scrypt$16384$8$1${salt.hex()}${h.hex()}")

if __name__ == "__main__":
    main()
