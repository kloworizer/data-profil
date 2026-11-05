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
# tolerate CRLF: a SHA256SUMS written/copied from Windows would otherwise be read as
# "./app_image.tar.gz\r" and sha256sum -c fails with "No such file or directory"
if grep -q $'\r' SHA256SUMS 2>/dev/null; then tr -d '\r' < SHA256SUMS > SHA256SUMS.tmp && mv SHA256SUMS.tmp SHA256SUMS; fi
sha256sum -c SHA256SUMS

# only parts listed in SHA256SUMS belong to this delivery (an app-only update leaves
# older Ollama files from the first install in place; they are not re-imported)
sent() { grep -qE "[ *](\./)?$1\$" SHA256SUMS; }   # '*' = binary-mode marker (sha256sum on Windows)
for f in app_image.tar.gz ollama_image.tar.gz; do
  if [ -f "$f" ] && sent "$f"; then echo "==> Loading $f"; $SUDO docker load -i "$f"; fi
done

# bundle.env records the image id that was packaged. If the loaded image differs, this folder is not
# the bundle it claims to be (e.g. an older file kept in place after copying only some files) - the
# usual cause of "the update did nothing".
if sent app_image.tar.gz; then
  LOADED=$($SUDO docker image inspect --format '{{.Id}}' "${TAG:-autoeda:poc}" 2>/dev/null || true)
  if [ -n "${APP_IMAGE_ID:-}" ] && [ -n "$LOADED" ] && [ "$LOADED" != "$APP_IMAGE_ID" ]; then
    echo "WARNING: app image $TAG is $LOADED but bundle.env says $APP_IMAGE_ID (built ${BUILT:-?})." >&2
    echo "         The bundle folder is inconsistent; copy the whole bundle again." >&2
  elif [ -n "$LOADED" ]; then
    echo "    app image $TAG = ${LOADED:7:12} (built ${BUILT:-?})"
  fi
fi

# 'docker load' untags the image it replaces (e.g. "renaming the old one ... to empty string"),
# which leaves a <none>:<none> image on disk. Remove those, but only the ones no container uses
# (a running container keeps its image alive anyway; this just reclaims disk).
echo "==> Removing superseded images"
for id in $($SUDO docker images -q -f "dangling=true" | sort -u); do
  in_use=$($SUDO docker ps -a --filter "ancestor=$id" --format '{{.ID}}' | head -n1)
  if [ -z "$in_use" ]; then
    $SUDO docker image rm "$id" >/dev/null 2>&1 && echo "    removed $id" || \
      echo "    kept $id (in use or shared by another image)"
  fi
done

if [ -f ollama_models.tgz ] && sent ollama_models.tgz; then
  echo "==> Preloading Ollama models into volume autoeda_ollama"
  $SUDO docker volume create autoeda_ollama >/dev/null
  $SUDO docker run --rm --user root --entrypoint sh -v autoeda_ollama:/v -v "$PWD:/b:ro" "$TAG" \
    -c "mkdir -p /v/models && tar xzf /b/ollama_models.tgz -C /v/models"
fi

# An air-gapped server cannot pull. If the compose file references an image that is not loaded,
# compose waits ~16 s and then fails with "Get https://registry-1.docker.io/v2/: ... timeout".
# Check the image names in the compose file up front and stop with a clear hint instead.
echo "==> Checking the images used by $COMPOSE"
missing=""
for img in $($SUDO docker compose -f "$COMPOSE" config --images 2>/dev/null | sort -u); do
  if ! $SUDO docker image inspect "$img" >/dev/null 2>&1; then missing="$missing $img"; fi
done
if [ -n "$missing" ]; then
  cat >&2 <<EOF
ERROR: these images are referenced by $COMPOSE but are not loaded on this server:$missing

This server is air-gapped, so nothing can be pulled. Two ways to fix it:

  1) The bundle was built with APP_ONLY=1 (app image only). Build a full bundle on the
     internet-connected machine and transfer it:
         scripts/offline_bundle.sh          (or .\\scripts\\offline_bundle.ps1 on Windows)
     then run this installer again - it loads ollama_image.tar.gz.

     Already have that file elsewhere? Load it by hand and re-run:
         docker load -i ollama_image.tar.gz

  2) Ollama is already installed on this host: edit $COMPOSE so the 'ollama' service uses
     the host's Ollama (or remove the service and set OLLAMA_HOST=http://<host>:11434).
EOF
  exit 1
fi

mkdir -p data input   # the containers fix ownership of data/ themselves

# `docker compose` in this folder must not pick the wrong file silently (it reads docker-compose.yml,
# or docker-compose.override.yml -> "stat ...: no such file or directory"). The bundle's
# docker-compose wrapper asks compose which file the running project uses; symlink it as the default.
if [ -f ./docker-compose ] && [ ! -L ./docker-compose ] && [ ! -L ./.docker-compose ]; then
  chmod +x ./docker-compose 2>/dev/null || true
  chmod +x ./install_offline.sh 2>/dev/null || true
  ln -s docker-compose ./.docker-compose 2>/dev/null || true
  echo "==> Compose wrapper installed: plain 'docker compose' now follows $COMPOSE"
fi

# The app asks Ollama for OLLAMA_MODEL from the compose file. If the bundle only carries a different
# model (e.g. qwen3.5:2b while the compose file wants qwen3.5:35b-a3b), every AI step fails at runtime
# inside an air-gapped server that cannot pull. Compare bundle.env with the compose file here instead.
COMPOSE_MODEL=$($SUDO docker compose -f "$COMPOSE" config 2>/dev/null | sed -n 's/.*OLLAMA_MODEL: *"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*/\1/p' | head -n1)
if [ -n "$COMPOSE_MODEL" ] && [ -n "${MODELS:-}" ]; then
  case " $MODELS " in
    *" $COMPOSE_MODEL "*) : ;;
    *)
      cat >&2 <<EOF
WARNING: $COMPOSE sets OLLAMA_MODEL=$COMPOSE_MODEL but bundle.env carries: $MODELS
         AI features will fail on this server (it cannot pull $COMPOSE_MODEL).
         Fix one of them:
           * rebuild the bundle with that model: -Models "qwen3.5:35b-a3b qwen3.5:2b"  (or MODELS="...")
           * or set OLLAMA_MODEL in $COMPOSE to a model the bundle contains
EOF
      ;;
  esac
fi
# The Ollama volume is declared `external: true` in the compose files: this script creates it above
# (and unpacks the models into it) before compose starts, so compose never creates it or warns.
echo "==> Starting ($COMPOSE)"
$SUDO docker compose -f "$COMPOSE" up -d --no-build --remove-orphans
$SUDO docker compose -f "$COMPOSE" ps
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
# The UI port comes from AUTOEDA_PORT in the compose file (host network); default 8000.
WEB_PORT=$($SUDO docker compose -f "$COMPOSE" config 2>/dev/null | sed -n 's/.*AUTOEDA_PORT: *"\{0,1\}\([0-9]\{1,5\}\)"\{0,1\}.*/\1/p' | head -n1)
WEB_PORT=${WEB_PORT:-8000}
echo "==> Done. Open http://${IP:-$(hostname)}:$WEB_PORT   (large files go in $PWD/input)"
