"""Sign-in, per-user reports and files, administrator-only actions, and the app pages."""
from __future__ import annotations

import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from autoeda import auth, config


@pytest.fixture()
def app(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for name, path in (("DATA_DIR", data), ("UPLOAD_DIR", data / "uploads"), ("WORK_DIR", data / "work"),
                       ("REPORT_DIR", data / "reports"), ("MEMORY_DIR", tmp_path / "memory")):
        monkeypatch.setattr(config, name, path)
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_USER", "boss")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "boss-password")
    config.ensure_dirs()
    web = importlib.import_module("autoeda.web")
    monkeypatch.setattr(web, "_jobs", {})
    monkeypatch.setattr(web, "_start", lambda job_id: None)       # no real analysis
    auth._fails.clear()
    assert auth.bootstrap() is None                              # password came from the environment
    auth.create("ann", "ann-password")
    auth.create("bob", "bob-password")
    return web


def client(web, user=None, password=None):
    c = TestClient(web.app)
    if user:
        r = c.post("/login", json={"username": user, "password": password or f"{user}-password"})
        assert r.status_code == 200, r.text
    return c


def fake_report(web, owner: str, name: str = "sales.csv") -> str:
    """An uploaded file with a finished report, as the job runner leaves it."""
    job_id = f"{len(web._jobs):012x}"
    up = config.UPLOAD_DIR / f"u{job_id[1:]}"
    up.mkdir(parents=True)
    (up / name).write_bytes(b"a,b\n1,2\n")
    (config.REPORT_DIR / f"{job_id}.html").write_text("<html>report</html>")
    (config.REPORT_DIR / f"{job_id}.json").write_text(json.dumps({"tables": [], "file": {}}))
    web._jobs[job_id] = {"id": job_id, "file": name, "size": 8, "status": "done", "created": time.time(),
                         "options": {}, "input": str(up / name), "cleanup": True, "run": 1, "owner": owner}
    return job_id


def test_everything_needs_sign_in(app):
    c = client(app)
    assert c.get("/health").status_code == 200
    assert c.get("/static/bulma.min.css").status_code == 200
    assert c.get("/api/jobs").status_code == 401
    r = c.get("/files", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/files"
    assert c.get("/login").status_code == 200
    assert c.post("/login", json={"username": "ann", "password": "wrong-one"}).status_code == 401


def test_users_see_only_their_reports(app):
    ann_job, bob_job = fake_report(app, "ann"), fake_report(app, "bob")
    ann, boss = client(app, "ann"), client(app, "boss")
    assert [j["id"] for j in ann.get("/api/jobs").json()] == [ann_job]
    assert {j["id"] for j in boss.get("/api/jobs").json()} == {ann_job, bob_job}
    assert ann.get(f"/reports/{bob_job}").status_code == 404
    assert ann.get(f"/reports/{ann_job}").status_code == 200
    assert ann.delete(f"/api/jobs/{bob_job}").status_code == 404
    assert ann.get(f"/api/jobs/{ann_job}/file").text == "a,b\n1,2\n"


def test_file_manager(app):
    job = fake_report(app, "ann")
    stray = config.UPLOAD_DIR / "0123456789ab"                  # an upload no job refers to
    stray.mkdir()
    (stray / "lost.csv").write_text("x" * 100)
    ann, boss = client(app, "ann"), client(app, "boss")
    files = ann.get("/api/files").json()
    assert files["usage"]["stored"] == 1 and files["usage"]["uploads"] == 8 and "orphans" not in files
    assert files["files"][0]["stored"] and files["disk"]["total"] > 0
    assert [o["id"] for o in boss.get("/api/files").json()["orphans"]] == ["0123456789ab"]
    assert ann.delete("/api/files/orphans/0123456789ab").status_code == 403
    assert boss.delete("/api/files/orphans/0123456789ab").status_code == 200 and not stray.exists()
    assert ann.post(f"/api/jobs/{job}/drop-file").status_code == 200
    row = ann.get("/api/files").json()["files"][0]
    assert not row["stored"] and row["has_report"] and row["file_deleted"]


def test_admin_only_and_user_management(app):
    ann, boss = client(app, "ann"), client(app, "boss")
    assert ann.put("/api/settings", json={"EDA_DUPLICATES": False}).status_code == 403
    assert ann.get("/api/users").status_code == 403
    assert boss.post("/api/users", json={"name": "cat", "password": "short"}).status_code == 400
    assert boss.post("/api/users", json={"name": "cat", "password": "cat-password"}).status_code == 200
    assert boss.delete("/api/users/boss").status_code == 400                   # not yourself
    assert boss.put("/api/users/boss", json={"role": "user"}).status_code == 400  # the only administrator
    client(app, "cat")
    assert boss.delete("/api/users/cat").status_code == 200
    assert client(app).post("/login", json={"username": "cat", "password": "cat-password"}).status_code == 401


def test_password_change_signs_out_other_sessions(app):
    first, second = client(app, "ann"), client(app, "ann")
    r = first.put("/api/me/password", json={"old": "ann-password", "new": "ann-new-password"})
    assert r.status_code == 200
    assert first.get("/api/me").status_code == 200                # this session got a fresh cookie
    assert second.get("/api/me").status_code == 401
    assert client(app).post("/login", json={"username": "ann", "password": "ann-password"}).status_code == 401
    client(app, "ann", "ann-new-password")


def test_lockout_and_basic_auth(app):
    c = client(app)
    for _ in range(auth.MAX_FAILS):
        c.post("/login", json={"username": "bob", "password": "nope-nope"})
    assert c.post("/login", json={"username": "bob", "password": "bob-password"}).status_code == 429
    assert TestClient(app.app).get("/api/jobs", auth=("ann", "ann-password")).status_code == 200
    assert TestClient(app.app).get("/api/jobs", auth=("ann", "bad")).status_code == 401


def test_cross_site_post_refused(app):
    ann = client(app, "ann")
    assert ann.post("/logout", headers={"Origin": "http://evil.example"}).status_code == 403
    assert ann.post("/logout", headers={"Origin": "http://testserver"}).status_code == 200


def test_pages_render(app):
    ann, boss = client(app, "ann"), client(app, "boss")
    for path in ("/", "/files", "/memory", "/settings", "/users"):
        html = boss.get(path).text
        assert "bulma.min.css" in html and 'data-theme="dark"' in html and "boss" in html
    assert 'href="/settings"' not in ann.get("/").text           # administrators only
    assert ann.get("/login", follow_redirects=False).status_code == 303
