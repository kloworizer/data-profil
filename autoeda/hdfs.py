"""HDFS on the host, through WebHDFS (the NameNode's REST API, default port 9870).

* Browse: the start page lists folders and files so a user can pick an hdfs:// path.
* Stage: files Spark cannot read itself (Excel, PDF, XML...), or any HDFS file analysed with the
  local engine, are streamed into the app's work folder first and read like an upload.
  CSV / Parquet / ORC / JSON with the Spark engine are read by the executors directly; nothing
  is copied.

Simple authentication (user.name) is supported; a Kerberos-secured cluster needs the Spark
engine (the executors authenticate) or a WebHDFS gateway such as Knox / HttpFS.
"""
from __future__ import annotations

import posixpath
from pathlib import Path
from urllib.parse import urlsplit

from . import config

CHUNK = 1 << 20


def enabled() -> bool:
    return bool(config.HDFS_URL)


def path_of(uri: str) -> str:
    """'/data/x.csv' from 'hdfs://nameservice1/data/x.csv' (or a plain path)."""
    return urlsplit(uri).path if "://" in uri else uri


def uri_of(path: str) -> str:
    root = (config.HDFS_ROOT or "hdfs://").rstrip("/")
    return f"{root}{path}" if root != "hdfs:" else f"hdfs://{path}"


def _url(path: str, op: str, **params) -> tuple[str, dict]:
    q = {"op": op, **params}
    if config.HDFS_USER:
        q["user.name"] = config.HDFS_USER
    return f"{config.HDFS_URL.rstrip('/')}/webhdfs/v1{posixpath.normpath('/' + path.lstrip('/'))}", q


def listdir(path: str = "/") -> list[dict]:
    import httpx

    url, q = _url(path, "LISTSTATUS")
    r = httpx.get(url, params=q, timeout=15)
    r.raise_for_status()
    out = []
    for s in r.json()["FileStatuses"]["FileStatus"]:
        p = posixpath.join(path_of(path) or "/", s["pathSuffix"])
        out.append({"name": s["pathSuffix"], "path": p, "uri": uri_of(p), "dir": s["type"] == "DIRECTORY",
                    "size": s.get("length", 0), "modified": s.get("modificationTime", 0) / 1000})
    return sorted(out, key=lambda e: (not e["dir"], e["name"].lower()))


def size(uri: str) -> int | None:
    import httpx

    try:
        url, q = _url(path_of(uri), "GETFILESTATUS")
        return httpx.get(url, params=q, timeout=15).json()["FileStatus"]["length"]
    except Exception:
        return None


def stage(uri: str, dest_dir: Path, progress=lambda p, m: None) -> Path:
    """Stream an HDFS file into dest_dir (the NameNode redirects to a DataNode)."""
    import httpx

    if not enabled():
        raise RuntimeError("This file is on HDFS: set the WebHDFS address (AUTOEDA_HDFS_URL) in Settings, "
                           "or use the Spark engine for CSV / Parquet / ORC / JSON.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (posixpath.basename(path_of(uri).rstrip("/")) or "hdfs_file")
    total = size(uri) or 0
    url, q = _url(path_of(uri), "OPEN")
    done = 0
    with httpx.stream("GET", url, params=q, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(CHUNK):
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(0.08 * done / total, "Copying from HDFS")
    return dest
