#!/usr/bin/env bash
# Creates the Ollama model that AutoEDA uses from docker/Modelfile: the system prompt
# ("no reasoning steps, final answer only") and num_predict 512, on top of a bundled model.
#
#   ./scripts/create_model.sh                       # docker compose (local dev)
#   ./create_model.sh docker-compose.prod.yml       # inside the offline bundle root (as installer)
#
# It runs against the `ollama` service of the compose file: the model is stored in that
# container's volume (autoeda_ollama), nothing is downloaded. Run it after the service has the
# models (installer does this; for a manual setup: docker compose up -d ollama).
# Re-run it after editing docker/Modelfile: Ollama rebuilds the model from the parents.
#
# Environment:
#   MODEL_NAME  name of the created model (default: autoeda-llm)
#   MODELS      parents, space separated (default: detected among the models in the Ollama service:
#               the bundle's base:qwen35_35b + base:qwen35_2b, else the plain qwen3.5:35b-a3b + 2b)
#
# Afterwards point OLLAMA_MODEL (docker-compose.prod.yml, autoeda service) or Settings -> AI model
# at MODEL_NAME.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# repo:  scripts/ -> ../ ; offline bundle: scripts are copied flat into its root
ROOT=$(cd "$HERE/.." && pwd)
[ -d "$ROOT/docker" ] || ROOT=$HERE
COMPOSE=${1:-docker-compose.yml}
MODEL_NAME=${MODEL_NAME:-autoeda-llm}
MODELFILE=$ROOT/docker/Modelfile
[ -f "$MODELFILE" ] || { echo "ERROR: $MODELFILE not found" >&2; exit 1; }

# docker compose runs from the folder holding the compose file (and, in the bundle, the models)
cd "$ROOT"
SUDO=${SUDO:-}
if ! docker info >/dev/null 2>&1; then SUDO=sudo; fi
run() { $SUDO "$@"; }
in_ollama() { run docker compose -f "$COMPOSE" exec -T ollama "$@"; }

# Parents: the bundle stores the models under registry-prefixed names (base:qwen35_35b:latest);
# an Ollama with plainly pulled models (qwen3.5:35b-a3b) needs those names. Ask the Ollama that
# will host the model.
if [ -z "${MODELS:-}" ]; then
  have=$(in_ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' || true)
  pick() {
    for cand in "$@"; do
      if printf '%s\n' "$have" | grep -qxF "$cand"; then printf '%s ' "$cand"; return 0; fi
    done
    return 1
  }
  MODELS="$(pick base:qwen35_35b:latest qwen3.5:35b-a3b base:qwen35_35b || true)$(pick base:qwen35_2b:latest qwen3.5:2b base:qwen35_2b || true)"
  if [ -z "$(printf '%s' "$MODELS" | tr -d ' ')" ]; then
    echo "ERROR: no bundled model found in the ollama service. Models there:" >&2
    printf '%s\n' "$have" | sed 's/^/    /' >&2
    echo "Hint: the offline bundle stores them as base:qwen35_35b:latest and base:qwen35_2b:latest" >&2
    echo "      (preload, then 'docker compose -f $COMPOSE exec ollama ollama list')." >&2
    exit 1
  fi
fi
MODELS=$(printf '%s' "$MODELS" | sed 's/^ *//; s/ *$//')
echo "==> Creating Ollama model '$MODEL_NAME'"
echo "    FROM $MODELS"
echo "    file: $MODELFILE"

# Substitute the parents where the Modelfile lives and stream it in: `ollama create -f -` reads the
# Modelfile from stdin. One FROM line per model in MODELS, in the order given.
tmp_mf=$(mktemp); trap 'rm -f "$tmp_mf"' EXIT
printf 'FROM %s\n' $MODELS > "$tmp_mf"
sed '/^FROM /d' "$MODELFILE" >> "$tmp_mf"
in_ollama ollama create "$MODEL_NAME" -f - < "$tmp_mf"
in_ollama ollama show "$MODEL_NAME" >/dev/null

echo
echo "Model '$MODEL_NAME' is ready. Tell the app to use it:"
echo "  OLLAMA_MODEL: $MODEL_NAME        # in $COMPOSE, autoeda service (or Settings -> AI model)"
