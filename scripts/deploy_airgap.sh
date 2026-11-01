#!/usr/bin/env bash
# One-command deploy to an air-gapped server you can reach with SSH/SCP from this PC.
# Run from the project root (Git Bash on Windows, or Linux/macOS):
#
#   scripts/deploy_airgap.sh user@server                 # first install: app + Ollama + AI models
#   scripts/deploy_airgap.sh user@server --app-only      # update: only the new app image (~10x smaller)
#
# Options:
#   --dir /opt/autoeda       install folder on the server (default: ~/autoeda)
#   --compose FILE           docker-compose.yml (default, single server incl. Spark) or docker-compose.prod.yml
#   --port 22                SSH port
#   --key ~/.ssh/id_ed25519  SSH private key
#   --sudo                   run docker with sudo on the server
#   --skip-build             reuse the existing ./offline_bundle (no rebuild)
#   --bundle DIR             bundle folder (default: offline_bundle)
# Env for the bundle build (see offline_bundle.sh): MODELS (default "qwen3.5:35b-a3b qwen3.5:2b"),
#   OLLAMA_MODELS_DIR, OLLAMA_IMAGE, PYSPARK_VERSION, WITH_DOCLING
#
# Tip: set up SSH key login first (ssh-keygen; then append the .pub to ~/.ssh/authorized_keys on
# the server) so you are not asked for the password several times.
set -euo pipefail

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ $# -ge 1 ] || usage
TARGET=$1; shift
case "$TARGET" in -*) usage ;; esac
REMOTE_DIR="autoeda"; COMPOSE="docker-compose.yml"; PORT=22; KEY=""; SUDO=""; BUILD=1; APP_ONLY=0; BUNDLE="offline_bundle"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) REMOTE_DIR=$2; shift 2 ;;
    --compose) COMPOSE=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --key) KEY=$2; shift 2 ;;
    --sudo) SUDO="sudo"; shift ;;
    --skip-build) BUILD=0; shift ;;
    --app-only) APP_ONLY=1; shift ;;
    --bundle) BUNDLE=$2; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option $1"; usage ;;
  esac
done

# accept-new: trust a server's key on first contact, refuse if a known key ever changes
SSH_OPTS=(-p "$PORT" -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new)
SCP_OPTS=(-P "$PORT" -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new)
if [ -n "$KEY" ]; then SSH_OPTS+=(-i "$KEY"); SCP_OPTS+=(-i "$KEY"); fi
# reuse one SSH connection (one password prompt) where supported; not on Windows SSH builds
CTL="${TMPDIR:-/tmp}/autoeda-ssh-%r@%h-%p"
if ! uname -s | grep -qiE "mingw|msys|cygwin"; then
  SSH_OPTS+=(-o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=15m)
  SCP_OPTS+=(-o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=15m)
fi
rsh() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "1/5 Checking the server ($TARGET)"
rsh "set -e
  command -v docker >/dev/null || { echo 'ERROR: Docker is not installed on the server.'; exit 3; }
  $SUDO docker compose version >/dev/null 2>&1 || { echo 'ERROR: docker compose plugin missing on the server.'; exit 3; }
  mkdir -p $REMOTE_DIR
  echo \"Docker: \$($SUDO docker --version)\"
  echo \"Free disk in $REMOTE_DIR: \$(df -h $REMOTE_DIR | awk 'NR==2{print \$4}')\""

if [ "$BUILD" = "1" ]; then
  step "2/5 Building the offline bundle on this PC (needs internet)"
  APP_ONLY=$APP_ONLY scripts/offline_bundle.sh "$BUNDLE"
else
  step "2/5 Reusing existing bundle in $BUNDLE"
fi
[ -f "$BUNDLE/app_image.tar.gz" ] || { echo "ERROR: $BUNDLE/app_image.tar.gz not found"; exit 1; }
# always ship the current installer and compose files (also with --skip-build)
cp scripts/install_offline.sh docker-compose.yml docker-compose.prod.yml "$BUNDLE/"

FILES=("$BUNDLE/app_image.tar.gz" "$BUNDLE/bundle.env" "$BUNDLE/install_offline.sh" "$BUNDLE/docker-compose.yml" "$BUNDLE/docker-compose.prod.yml")
if [ "$APP_ONLY" != "1" ]; then
  for f in ollama_image.tar.gz ollama_models.tgz; do
    [ -f "$BUNDLE/$f" ] || { echo "ERROR: $BUNDLE/$f missing (build without APP_ONLY first)"; exit 1; }
    FILES+=("$BUNDLE/$f")
  done
fi
# checksum file for exactly what is sent
(cd "$BUNDLE" && sha256sum $(for f in "${FILES[@]}"; do b=$(basename "$f"); case $b in *.gz|*.tgz) echo "./$b";; esac; done) > SHA256SUMS)
FILES+=("$BUNDLE/SHA256SUMS")
du -ch "${FILES[@]}" | tail -1 | awk '{print "Total to transfer: " $1}'

step "3/5 Copying to $TARGET:$REMOTE_DIR"
if command -v rsync >/dev/null && rsh "command -v rsync" >/dev/null 2>&1; then
  rsync -ah --partial --progress -e "ssh ${SSH_OPTS[*]}" "${FILES[@]}" "$TARGET:$REMOTE_DIR/"   # resumable
else
  scp "${SCP_OPTS[@]}" "${FILES[@]}" "$TARGET:$REMOTE_DIR/"
fi

step "4/5 Installing on the server"
rsh "cd $REMOTE_DIR && chmod +x install_offline.sh && sed -i 's/\r\$//' install_offline.sh && SUDO=$SUDO ./install_offline.sh $COMPOSE"

step "5/5 Health check"
rsh "for i in \$(seq 1 30); do curl -fs http://localhost:8000/health >/dev/null 2>&1 && { echo 'AutoEDA is up.'; exit 0; }; sleep 2; done; echo 'WARNING: no answer on port 8000 yet; check: $SUDO docker compose -f $REMOTE_DIR/$COMPOSE logs autoeda'"
HOST=${TARGET#*@}
echo
echo "Open http://$HOST:8000 in a browser on the server's network."
echo "Large data files: copy them to $TARGET:$REMOTE_DIR/input/  (e.g. scp bigfile.csv $TARGET:$REMOTE_DIR/input/)"
