"""An existing Spark cluster and HDFS on the host: cluster resources, browsing HDFS through
WebHDFS, and copying HDFS files Spark cannot read into the app."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from autoeda import config, hdfs, settings, spark_engine

FILES = {"/data/sales.xlsx": b"x" * 3000, "/data/big.csv": b"a,b\n1,2\n"}


class WebHDFS(BaseHTTPRequestHandler):
    """Just enough of the NameNode / DataNode REST API."""

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/datanode"):
            data = FILES[u.path[len("/datanode"):]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        path = u.path[len("/webhdfs/v1"):]
        assert q.get("user.name") == "etl"
        if q["op"] == "LISTSTATUS":
            kids = [{"pathSuffix": "archive", "type": "DIRECTORY", "length": 0, "modificationTime": 0}] + [
                {"pathSuffix": p.rsplit("/", 1)[1], "type": "FILE", "length": len(d), "modificationTime": 1.7e12}
                for p, d in FILES.items() if p.rsplit("/", 1)[0] == path.rstrip("/")]
            self._json({"FileStatuses": {"FileStatus": kids}})
        elif q["op"] == "GETFILESTATUS":
            self._json({"FileStatus": {"length": len(FILES[path]), "type": "FILE"}})
        elif q["op"] == "OPEN":                     # the NameNode sends the client to a DataNode
            self.send_response(307)
            self.send_header("Location", f"/datanode{path}")
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture()
def namenode(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), WebHDFS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "HDFS_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(config, "HDFS_USER", "etl")
    monkeypatch.setattr(config, "HDFS_ROOT", "hdfs://nameservice1")
    yield srv
    srv.shutdown()


def test_browse_and_stage(namenode, tmp_path):
    entries = hdfs.listdir("/data")
    assert [e["name"] for e in entries] == ["archive", "big.csv", "sales.xlsx"]       # folders first
    assert entries[2]["uri"] == "hdfs://nameservice1/data/sales.xlsx" and entries[2]["size"] == 3000
    seen = []
    got = hdfs.stage("hdfs://nameservice1/data/sales.xlsx", tmp_path, lambda p, m: seen.append(p))
    assert got.read_bytes() == FILES["/data/sales.xlsx"] and seen and max(seen) <= 0.08


def test_which_hdfs_files_are_copied(monkeypatch):
    from autoeda import web
    assert web._needs_staging("hdfs://ns/data/sales.xlsx", "spark")       # Spark cannot read Excel
    assert web._needs_staging("hdfs://ns/data/big.csv", "local")          # DuckDB cannot read HDFS
    assert not web._needs_staging("hdfs://ns/data/big.csv", "spark")      # executors read it in place
    assert not web._needs_staging("/input/big.csv", "local")


def test_hdfs_path_job(namenode, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from autoeda import web
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    monkeypatch.setattr(web, "_jobs", {})
    monkeypatch.setattr(web, "_start", lambda job_id: None)
    c = TestClient(web.app)
    assert [e["name"] for e in c.get("/api/hdfs", params={"path": "/data"}).json()["entries"]][0] == "archive"
    r = c.post("/api/path", json={"path": "hdfs://nameservice1/data/sales.xlsx", "engine": "local"})
    assert r.status_code == 200 and r.json()["file"] == "sales.xlsx"
    monkeypatch.setattr(config, "HDFS_URL", "")
    r = c.post("/api/path", json={"path": "hdfs://nameservice1/data/sales.xlsx", "engine": "local"})
    assert r.status_code == 400 and "WebHDFS" in r.json()["detail"]
    assert c.post("/api/path", json={"path": "s3a://bucket/x.csv", "engine": "local"}).status_code == 400


def test_cluster_status(monkeypatch):
    monkeypatch.setattr(config, "SPARK_MASTER", "local[*]")
    assert spark_engine.status()["kind"] == "local"
    monkeypatch.setattr(config, "SPARK_MASTER", "spark://spark-master.internal:7077")
    monkeypatch.setattr(config, "SPARK_UI_URL", "")
    assert spark_engine.ui_url() == "http://spark-master.internal:8080"

    class Resp:
        def json(self):
            return {"status": "ALIVE", "cores": 216, "coresused": 70, "memory": 2_100_000, "memoryused": 700_000,
                    "activeapps": [{}], "workers": [
                        {"host": "w1", "state": "ALIVE", "cores": 72, "coresused": 70, "memory": 700_000, "memoryused": 700_000},
                        {"host": "w2", "state": "DEAD", "cores": 72, "coresused": 0, "memory": 700_000, "memoryused": 0}]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, timeout: Resp())
    s = spark_engine.status()
    assert s["reachable"] and s["workers"] == 1 and s["cores"] == 216 and s["apps_running"] == 1
    assert s["worker_list"][0]["host"] == "w1"
    monkeypatch.setattr(httpx, "get", lambda url, timeout: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    assert spark_engine.status()["reachable"] is False


def test_spark_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    try:
        settings.save({"SPARK_MASTER": "spark://10.0.0.5:7077", "SPARK_CONF": "a=1;" * 200, "HDFS_URL": ""})
        assert config.SPARK_MASTER == "spark://10.0.0.5:7077" and len(config.SPARK_CONF) == 800
        with pytest.raises(ValueError):
            settings.save({"SPARK_MASTER": ""})            # the master is required, the rest optional
    finally:
        settings.reset()
