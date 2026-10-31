#!/usr/bin/env bash
# Install / update AutoEDA on an AIR-GAPPED server from a bundle made by offline_bundle.sh.
# Run inside the bundle folder:   ./install_offline.sh [compose-file]
# Needs only Docker with the compose plugin. Nothing is downloaded.
# Set SUDO=sudo if your user is not in the docker group.
set -euo pipefail
cd "$(dirname "$0")"
COMPOSE=${1:-docker-compose.yml}
SUDO=${SUDO:-}
# shellcheck disable=SC1091
source ./bundle.env

command -v docker >/dev/null || { echo "ERROR: Docker is not installed on this server."; exit 1; }
$SUDO docker compose version >/dev/null 2>&1 || { echo "ERROR: the 'docker compose' plugin is missing."; exit 1; }

echo "==> Verifying checksums"
sha256sum -c SHA256SUMS

# only parts listed in SHA256SUMS belong to this delivery (an app-only update leaves
# older Ollama files from the first install in place; they are not re-imported)
sent() { grep -qE "[ *](\./)?$1\$" SHA256SUMS; }   # '*' = binary-mode marker (sha256sum on Windows)
for f in app_image.tar.gz ollama_image.tar.gz; do
  if [ -f "$f" ] && sent "$f"; then echo "==> Loading $f"; $SUDO docker load -i "$f"; fi
done

if [ -f ollama_models.tgz ] && sent ollama_models.tgz; then
  echo "==> Preloading Ollama models into volume autoeda_ollama"
  $SUDO docker volume create autoeda_ollama >/dev/null
  $SUDO docker run --rm --user root --entrypoint sh -v autoeda_ollama:/v -v "$PWD:/b:ro" "$TAG" \
    -c "mkdir -p /v/models && tar xzf /b/ollama_models.tgz -C /v/models"
fi

mkdir -p data input   # the containers fix ownership of data/ themselves
echo "==> Starting ($COMPOSE)"
$SUDO docker compose -f "$COMPOSE" up -d --no-build --remove-orphans
$SUDO docker compose -f "$COMPOSE" ps
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
echo "==> Done. Open http://${IP:-$(hostname)}:8000   (large files go in $PWD/input)"
