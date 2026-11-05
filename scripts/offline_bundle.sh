#!/usr/bin/env bash
# Build an offline bundle for an AIR-GAPPED server. Run on a machine WITH internet
# (Linux, or Git Bash + Docker Desktop on Windows), from the project root:
#
#   scripts/offline_bundle.sh [out_dir]
#
# Env overrides:
#   APP_ONLY=1                       only the AutoEDA image (for updates; Ollama + models already on the server)
#   TAG=autoeda:poc                  image tag to build
#   OLLAMA_IMAGE=ollama/ollama:latest   pin a version for reproducibility
#   MODELS="qwen3.5:35b-a3b qwen3.5:2b"  Ollama models to include. Default: qwen3.5:35b-a3b,
#                                    because docker-compose.prod.yml sets OLLAMA_MODEL=qwen3.5:35b-a3b
#   OLLAMA_MODELS_DIR=~/.ollama/models  take models from this folder (the local Ollama's store); models
#                                    missing there are pulled first with the local `ollama` CLI
#
# Model choice for the production servers (3 x 72 CPU cores / 786 GB RAM, no GPU): on CPU, speed is set by
# the parameters active per token, not by RAM. qwen3.5:35b-a3b is a mixture-of-experts model (35B total,
# ~3B active): about the speed of a 3-4B dense model with far better answers, 22 GB, and it can read images
# (used to lay out borderless PDF tables). qwen3.5:2b stays as the fast fallback selectable in the UI.
# Not chosen: qwen3.5:27b (dense, ~9x slower per token on CPU); qwen3.5:122b-a10b (no vision in Ollama,
# ~3x slower).
#   PYSPARK_VERSION=3.5.3            must match the production Spark cluster
#   WITH_DOCLING=1                   Docling TableFormer PDF table reader + its models
#                                    (about 2 GB larger image; 0 = leave it out)
#
# Produces (in out_dir): app_image.tar.gz, ollama_image.tar.gz, ollama_models.tgz,
# compose files, install_offline.sh, bundle.env, SHA256SUMS
set -euo pipefail
export MSYS_NO_PATHCONV=1

OUT=${1:-offline_bundle}
APP_ONLY=${APP_ONLY:-0}
TAG=${TAG:-autoeda:poc}
OLLAMA_IMAGE=${OLLAMA_IMAGE:-ollama/ollama:latest}
MODELS=${MODELS:-qwen3.5:35b-a3b}
PYSPARK_VERSION=${PYSPARK_VERSION:-3.5.3}
WITH_DOCLING=${WITH_DOCLING:-1}
mkdir -p "$OUT"
OUT_ABS=$(cd "$OUT" && { pwd -W 2>/dev/null || pwd; })   # pwd -W: Windows path for Docker Desktop

BUILD_STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "==> Building $TAG (Spark + OCR, Docling=$WITH_DOCLING)"
docker build --build-arg WITH_SPARK=1 --build-arg WITH_OCR=1 --build-arg WITH_DOCLING="$WITH_DOCLING" \
  --build-arg PYSPARK_VERSION="$PYSPARK_VERSION" --build-arg BUILD="$BUILD_STAMP" -t "$TAG" .

echo "==> Saving app image"
docker save "$TAG" | gzip > "$OUT/app_image.tar.gz"

if [ "$APP_ONLY" != "1" ]; then
  echo "==> Pulling and saving $OLLAMA_IMAGE"
  docker pull "$OLLAMA_IMAGE"
  docker save "$OLLAMA_IMAGE" | gzip > "$OUT/ollama_image.tar.gz"

  echo "==> Collecting Ollama models: $MODELS"
  # docker-compose.prod.yml tells the app which model to call. Bundling only other models produces a
  # bundle that installs fine and then fails every AI step on the air-gapped server.
  COMPOSE_MODEL=$(sed -n 's/^[[:space:]]*OLLAMA_MODEL:[[:space:]]*//p' docker-compose.prod.yml | head -n1)
  if [ -n "$COMPOSE_MODEL" ]; then
    case " $MODELS " in
      *" $COMPOSE_MODEL "*) ;;
      *) echo "WARNING: docker-compose.prod.yml sets OLLAMA_MODEL=$COMPOSE_MODEL but the bundle gets: $MODELS" >&2
         echo "         Add it: MODELS=\"$COMPOSE_MODEL\" scripts/offline_bundle.sh" >&2 ;;
    esac
  fi
  if [ -n "${OLLAMA_MODELS_DIR:-}" ]; then
    # pull what is missing with the local Ollama (it trusts the OS certificate store, so it also works
    # behind HTTPS inspection), then bundle only the selected models, not the whole folder
    if command -v ollama >/dev/null 2>&1; then
      for m in $MODELS; do
        ollama show "$m" >/dev/null 2>&1 || { echo "    pulling $m (first time only)"; ollama pull "$m"; }
      done
    fi
    echo "    source: $OLLAMA_MODELS_DIR"
    : > "$OUT/.model_files"
    for m in $MODELS; do
      name=${m%%:*}; tag=${m#*:}; [ "$tag" = "$m" ] && tag=latest
      case "$name" in */*) ;; *) name="library/$name" ;; esac
      man="manifests/registry.ollama.ai/$name/$tag"
      if [ ! -f "$OLLAMA_MODELS_DIR/$man" ]; then
        echo "ERROR: model $m is not in $OLLAMA_MODELS_DIR (run: ollama pull $m)" >&2; exit 1
      fi
      echo "    $m -> $man"
      echo "$man" >> "$OUT/.model_files"
      grep -o 'sha256:[0-9a-f]\{64\}' "$OLLAMA_MODELS_DIR/$man" | sort -u | sed 's#sha256:#blobs/sha256-#' >> "$OUT/.model_files"
    done
    SRC_ABS=$(cd "$OLLAMA_MODELS_DIR" && { pwd -W 2>/dev/null || pwd; })
    docker run --rm --user root --entrypoint sh -v "$SRC_ABS:/src:ro" -v "$OUT_ABS:/out" "$TAG" \
      -c "cd /src && tar czf /out/ollama_models.tgz -T /out/.model_files"
    rm -f "$OUT/.model_files"
    echo "    ollama_models.tgz: $(du -h "$OUT/ollama_models.tgz" | cut -f1) (selected models only)"
  else
    docker volume create autoeda_bundle_models >/dev/null
    docker rm -f autoeda_bundle_ollama >/dev/null 2>&1 || true
    # extra root CAs from certs/ (HTTPS-inspecting proxy / antivirus) for the pull
    CERTS_ABS=$(cd certs && { pwd -W 2>/dev/null || pwd; })
    docker run -d --name autoeda_bundle_ollama -v autoeda_bundle_models:/root/.ollama \
      -v "$CERTS_ABS:/extra-certs:ro" -e SSL_CERT_DIR=/etc/ssl/certs:/extra-certs "$OLLAMA_IMAGE" >/dev/null
    sleep 3
    for m in $MODELS; do docker exec autoeda_bundle_ollama ollama pull "$m"; done
    docker rm -f autoeda_bundle_ollama >/dev/null
    docker run --rm --user root --entrypoint sh -v autoeda_bundle_models:/v:ro -v "$OUT_ABS:/out" "$TAG" \
      -c "tar czf /out/ollama_models.tgz -C /v/models ."
    docker volume rm autoeda_bundle_models >/dev/null
  fi
else
  rm -f "$OUT/ollama_image.tar.gz" "$OUT/ollama_models.tgz"
fi

cp docker-compose.yml docker-compose.prod.yml "$OUT/"
cp scripts/install_offline.sh scripts/docker-compose "$OUT/"
printf 'TAG=%s\nOLLAMA_IMAGE=%s\nMODELS="%s"\nPYSPARK_VERSION=%s\nBUILT=%s\nAPP_IMAGE_ID=%s\n' \
  "$TAG" "$OLLAMA_IMAGE" "$MODELS" "$PYSPARK_VERSION" "$BUILD_STAMP" \
  "$(docker image inspect --format '{{.Id}}' "$TAG")" > "$OUT/bundle.env"
(cd "$OUT" && sha256sum ./*.tar.gz ./*.tgz 2>/dev/null > SHA256SUMS || true)
echo "==> Bundle ready in $OUT:"
ls -lh "$OUT"
