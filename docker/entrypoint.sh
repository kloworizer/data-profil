#!/bin/sh
# Roles: web (default) | spark-master | spark-worker | any other command
set -e
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
