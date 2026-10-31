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
#   MODELS="qwen3.5:2b"              Ollama models to include
#   OLLAMA_MODELS_DIR=~/.ollama/models  copy models from this folder instead of downloading them
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
MODELS=${MODELS:-qwen3.5:2b}
PYSPARK_VERSION=${PYSPARK_VERSION:-3.5.3}
WITH_DOCLING=${WITH_DOCLING:-1}
mkdir -p "$OUT"
OUT_ABS=$(cd "$OUT" && { pwd -W 2>/dev/null || pwd; })   # pwd -W: Windows path for Docker Desktop

echo "==> Building $TAG (Spark + OCR, Docling=$WITH_DOCLING)"
docker build --build-arg WITH_SPARK=1 --build-arg WITH_OCR=1 --build-arg WITH_DOCLING="$WITH_DOCLING" \
  --build-arg PYSPARK_VERSION="$PYSPARK_VERSION" -t "$TAG" .

echo "==> Saving app image"
docker save "$TAG" | gzip > "$OUT/app_image.tar.gz"

if [ "$APP_ONLY" != "1" ]; then
  echo "==> Pulling and saving $OLLAMA_IMAGE"
  docker pull "$OLLAMA_IMAGE"
  docker save "$OLLAMA_IMAGE" | gzip > "$OUT/ollama_image.tar.gz"

  echo "==> Collecting Ollama models: $MODELS"
  if [ -n "${OLLAMA_MODELS_DIR:-}" ]; then
    SRC_ABS=$(cd "$OLLAMA_MODELS_DIR" && { pwd -W 2>/dev/null || pwd; })
    docker run --rm --user root --entrypoint sh -v "$SRC_ABS:/src:ro" -v "$OUT_ABS:/out" "$TAG" \
      -c "tar czf /out/ollama_models.tgz -C /src ."
  else
    docker volume create autoeda_bundle_models >/dev/null
    docker rm -f autoeda_bundle_ollama >/dev/null 2>&1 || true
    docker run -d --name autoeda_bundle_ollama -v autoeda_bundle_models:/root/.ollama "$OLLAMA_IMAGE" >/dev/null
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
cp scripts/install_offline.sh "$OUT/"
printf 'TAG=%s\nOLLAMA_IMAGE=%s\nMODELS="%s"\nPYSPARK_VERSION=%s\nBUILT=%s\n' \
  "$TAG" "$OLLAMA_IMAGE" "$MODELS" "$PYSPARK_VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$OUT/bundle.env"
(cd "$OUT" && sha256sum ./*.tar.gz ./*.tgz 2>/dev/null > SHA256SUMS || true)
echo "==> Bundle ready in $OUT:"
ls -lh "$OUT"
