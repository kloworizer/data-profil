#!/bin/sh
# Roles: web (default) | spark-master | spark-worker | any other command
set -e

# Start as root only to make the mounted data folder writable for the app user
# (host folders are often owned by whoever deployed), then drop privileges.
if [ "$(id -u)" = "0" ]; then
  for d in /data /data/uploads /data/work /data/reports /data/spark_work; do
    mkdir -p "$d" && chown app:app "$d" 2>/dev/null || true
  done
  exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

case "$1" in
  web)
    exec uvicorn autoeda.web:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 120 ;;
  spark-master)
    exec spark-class org.apache.spark.deploy.master.Master --host 0.0.0.0 --port 7077 --webui-port 8080 ;;
  spark-worker)
    exec spark-class org.apache.spark.deploy.worker.Worker --webui-port 8081 \
      "${SPARK_MASTER_URL:-spark://spark-master:7077}" ;;
  *)
    exec "$@" ;;
esac
