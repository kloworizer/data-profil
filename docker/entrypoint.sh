#!/bin/sh
# Roles: web (default) | spark-master | spark-worker | any other command
set -e

# Start as root only to make the mounted data folder writable for the app user
# (host folders are often owned by whoever deployed), then drop privileges.
if [ "$(id -u)" = "0" ]; then
  for d in /data /memory; do
    [ -d "$d" ] || continue
    chown -R app:app "$d" 2>/dev/null || \
      echo "WARNING: cannot chown $d (read-only mount or NFS root_squash); make sure it exists and is writable by uid 1000 on the host." >&2
  done
  # a failed privilege switch would otherwise loop forever under restart: unless-stopped
  if ! setpriv --reuid=app --regid=app --init-groups "$0" "$@"; then
    code=$?
    [ "$code" = "127" ] || [ "$code" = "126" ] && {
      echo "ERROR: could not switch to the app user (setpriv failed)" >&2; exit 1; }
    exit $code
  fi
  exit 0
fi

# The app creates /data/{uploads,work,reports} at startup. If it cannot (read-only mount, or a
# folder owned by another uid), uvicorn dies with a traceback and - under
# `restart: unless-stopped` - the container loops forever with nothing listening on :8000.
# Test it here and stop with an actionable message instead.
DATA_DIR="${AUTOEDA_DATA_DIR:-/data}"
probe="$DATA_DIR/.write-test.$$"
if ! ( : > "$probe" ) 2>/dev/null; then
  echo "ERROR: $DATA_DIR is not writable by uid $(id -u), so the container cannot start." >&2
  echo "       Nothing is listening on port ${AUTOEDA_PORT:-8000}; a firewall change will not help." >&2
  echo "       On the host:" >&2
  echo "         sudo mkdir -p /srv/autoeda/data && sudo chown -R 1000:1000 /srv/autoeda" >&2
  echo "       Or run the container as root (not recommended): add 'user: \"0:0\"' to the service." >&2
  exit 1
fi
rm -f "$probe"
mkdir -p "$DATA_DIR/uploads" "$DATA_DIR/work" "$DATA_DIR/reports" 2>/dev/null || {
  echo "ERROR: cannot create the working folders under $DATA_DIR (read-only mount?)" >&2
  echo "       Check the '/data' volume mapping in the compose file." >&2
  exit 1
}
MEMORY_DIR="${AUTOEDA_MEMORY_DIR:-/memory}"
if [ -d "$MEMORY_DIR" ]; then
  if ( : > "$MEMORY_DIR/.write-test.$$" ) 2>/dev/null; then
    rm -f "$MEMORY_DIR/.write-test.$$"
  else
    echo "WARNING: $MEMORY_DIR is not writable; learned skills will not be saved." >&2
  fi
fi

WEB_PORT="${AUTOEDA_PORT:-8000}"

case "$1" in
  web)
    echo "AutoEDA: starting the web UI on http://0.0.0.0:$WEB_PORT"
    exec uvicorn autoeda.web:app --host 0.0.0.0 --port "$WEB_PORT" --timeout-keep-alive 120 ;;
  spark-master)
    exec spark-class org.apache.spark.deploy.master.Master --host 0.0.0.0 --port 7077 --webui-port 8080 ;;
  spark-worker)
    exec spark-class org.apache.spark.deploy.worker.Worker --webui-port 8081 \
      "${SPARK_MASTER_URL:-spark://spark-master:7077}" ;;
  *)
    exec "$@" ;;
esac
