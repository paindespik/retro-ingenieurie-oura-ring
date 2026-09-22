#!/usr/bin/env bash
# phone/build.sh — construit le core Rust (ARM64) puis l'APK de l'app.
#
# Depuis un clone frais :
#   1. rustup target add aarch64-linux-android
#   2. NDK r27+ installé (variable ANDROID_NDK, défaut /opt/android-ndk)
#   3. cp phone/scanner/secrets.properties.example phone/scanner/secrets.properties
#      puis renseigner les valeurs
#   4. ./phone/build.sh          (APK dans phone/scanner/app/build/outputs/apk/debug/)
#
# Le .so n'est pas versionné : c'est un artefact, reconstruit ici.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NDK="${ANDROID_NDK:-/opt/android-ndk}"
TOOLCHAIN="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"
API=24
TARGET=aarch64-linux-android

[ -d "$NDK" ] || { echo "NDK introuvable : $NDK (définir ANDROID_NDK)" >&2; exit 1; }

echo "== core Rust ($TARGET) =="
# On passe par clang avec --target explicite : les wrappers
# <target><api>-clang du NDK r27 s'appellent eux-mêmes (boucle infinie).
CC_aarch64_linux_android="$TOOLCHAIN/clang" \
AR_aarch64_linux_android="$TOOLCHAIN/llvm-ar" \
CFLAGS_aarch64_linux_android="--target=$TARGET$API" \
RUSTFLAGS="-C linker=$TOOLCHAIN/clang -C link-args=--target=$TARGET$API" \
  cargo build --release --manifest-path "$ROOT/phone/core/Cargo.toml" \
    --target "$TARGET" -p oura-phone-core

SO="$ROOT/phone/core/target/$TARGET/release/liboura_phone_core.so"
DEST="$ROOT/phone/scanner/app/src/main/jniLibs/arm64-v8a"
mkdir -p "$DEST"
cp -v "$SO" "$DEST/"

echo "== APK =="
if [ ! -f "$ROOT/phone/scanner/secrets.properties" ]; then
  echo "ATTENTION : phone/scanner/secrets.properties absent — l'app sera" >&2
  echo "construite sans identifiants et refusera de pousser vers le serveur." >&2
fi

# Build dans le conteneur Android officiel (aucun SDK requis sur l'hôte).
# Le dossier /root/.android est monté pour garder la MÊME clé de debug entre
# deux builds : sinon la signature change et `pm install -r` échoue avec
# INSTALL_FAILED_UPDATE_INCOMPATIBLE.
if command -v docker >/dev/null 2>&1 && [ -z "${ANDROID_HOME:-}" ]; then
  mkdir -p /tmp/gradle-home /tmp/android-user-home
  docker run --rm \
    -v "$ROOT/phone/scanner":/app -w /app \
    -v /tmp/gradle-home:/root/.gradle \
    -v /tmp/android-user-home:/root/.android \
    -e ANDROID_HOME=/opt/android-sdk-linux \
    ghcr.io/cirruslabs/flutter:stable \
    ./gradlew --no-daemon assembleDebug
else
  (cd "$ROOT/phone/scanner" && ./gradlew --no-daemon assembleDebug)
fi

echo
echo "APK : $ROOT/phone/scanner/app/build/outputs/apk/debug/app-debug.apk"
echo "Installation :  adb install -r <apk>"
echo "Clé de l'anneau (16 o hex, une seule fois) :"
echo "  adb shell \"run-as io.github.paindespik.ourascan sh -c 'cat > files/ring.key.hex'\" < ~/.oura/ring.key"
