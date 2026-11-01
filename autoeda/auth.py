"""Sign-in: users with a password and a role (admin / user), kept in DATA_DIR/users.json.

Passwords are stored as salted PBKDF2-SHA256 hashes. A session is a signed cookie
(HMAC with DATA_DIR/secret.key) naming the user and its expiry; it is tied to the password
hash, so changing a password signs out every other session of that user. Scripts can use
HTTP Basic auth on the REST API instead.

The first start creates the administrator (AUTOEDA_ADMIN_USER / AUTOEDA_ADMIN_PASSWORD; with
no password set, a random one is printed to the log). Manage users on the Users page or with
    python -m autoeda.auth list | add NAME [--admin] | passwd NAME | delete NAME
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import config

COOKIE = "autoeda_session"
ROLES = ("admin", "user")
ITERATIONS = 240_000
MIN_PASSWORD = 8
LOCAL = {"name": "local", "role": "admin"}       # the user when sign-in is switched off
_NAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_lock = threading.Lock()
_fails: dict[str, list[float]] = {}              # login key -> recent failure times
MAX_FAILS, FAIL_WINDOW = 5, 300                  # 5 wrong passwords in 5 minutes: wait
_DUMMY = None


def _users_file():
    return config.DATA_DIR / "users.json"


def _hash(password: str, salt: str | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def _matches(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, _ = stored.split("$")
        return hmac.compare_digest(_hash(password, salt, int(iterations)), stored)
    except ValueError:
        return False


def _secret() -> bytes:
    f = config.DATA_DIR / "secret.key"
    with _lock:
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(secrets.token_hex(32), encoding="utf-8")
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        return f.read_text(encoding="utf-8").strip().encode()


def load() -> dict[str, dict]:
    f = _users_file()
    if not f.exists():
        return {}
    try:
        return {u["name"]: u for u in json.loads(f.read_text(encoding="utf-8"))}
    except (ValueError, KeyError):
        return {}


def _write(users: dict[str, dict]):
    f = _users_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(users.values(), key=lambda u: u["name"]), indent=1), encoding="utf-8")
    os.replace(tmp, f)


def public(u: dict) -> dict:
    return {k: u.get(k) for k in ("name", "role", "created", "last_login")}


def _check_password(password: str):
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"The password needs at least {MIN_PASSWORD} characters")


def create(name: str, password: str, role: str = "user") -> dict:
    if not _NAME.fullmatch(name or ""):
        raise ValueError("User names use letters, digits and . _ @ - (at most 64)")
    if role not in ROLES:
        raise ValueError("Role must be admin or user")
    _check_password(password)
    with _lock:
        users = load()
        if name.lower() in {n.lower() for n in users}:
            raise ValueError("That user name is taken")
        users[name] = {"name": name, "role": role, "hash": _hash(password), "created": time.time()}
        _write(users)
        return public(users[name])


def update(name: str, password: str | None = None, role: str | None = None) -> dict:
    with _lock:
        users = load()
        u = users.get(name)
        if u is None:
            raise KeyError(name)
        if role is not None:
            if role not in ROLES:
                raise ValueError("Role must be admin or user")
            if u["role"] == "admin" and role != "admin" and _admins(users) == 1:
                raise ValueError("This is the only administrator")
            u["role"] = role
        if password is not None:
            _check_password(password)
            u["hash"] = _hash(password)
        _write(users)
        return public(u)


def delete(name: str):
    with _lock:
        users = load()
        u = users.get(name)
        if u is None:
            raise KeyError(name)
        if u["role"] == "admin" and _admins(users) == 1:
            raise ValueError("This is the only administrator")
        users.pop(name)
        _write(users)


def _admins(users) -> int:
    return sum(1 for u in users.values() if u["role"] == "admin")


def authenticate(name: str, password: str, client: str = "") -> dict | None:
    """The user, if name and password match. Raises PermissionError after too many failures."""
    global _DUMMY
    key = f"{client}|{(name or '').lower()}"
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW]
        _fails[key] = recent
    if len(recent) >= MAX_FAILS:
        raise PermissionError("Too many wrong passwords. Wait a few minutes and try again.")
    users = load()
    u = next((x for x in users.values() if x["name"].lower() == (name or "").lower()), None)
    if u is None:
        _DUMMY = _DUMMY or _hash("x")
        _matches(password or "", _DUMMY)       # same time as a real check: names cannot be probed
        ok = False
    else:
        ok = _matches(password or "", u["hash"])
    with _lock:
        if not ok:
            _fails.setdefault(key, []).append(now)
            return None
        _fails.pop(key, None)
    return u


def touch(name: str):
    with _lock:
        users = load()
        if name in users:
            users[name]["last_login"] = time.time()
            _write(users)


def _sign(payload: str, pw_hash: str) -> str:
    return hmac.new(_secret(), f"{payload}|{pw_hash}".encode(), hashlib.sha256).hexdigest()


def issue(u: dict) -> str:
    """A session token for this user: name, expiry and signature."""
    expires = int(time.time() + config.SESSION_HOURS * 3600)
    name = base64.urlsafe_b64encode(u["name"].encode()).decode().rstrip("=")
    payload = f"{name}.{expires}"
    return f"{payload}.{_sign(payload, u['hash'])}"


def from_token(token: str | None) -> dict | None:
    try:
        name, expires, sig = (token or "").split(".")
        if int(expires) < time.time():
            return None
        u = load().get(base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if u is None or not hmac.compare_digest(sig, _sign(f"{name}.{expires}", u["hash"])):
        return None
    return u


_basic_cache: dict[str, tuple[float, str]] = {}  # header digest -> (valid until, user name)


def from_basic(header: str, client: str = "") -> dict | None:
    """HTTP Basic credentials (for scripts); a good pair is remembered for a minute so polling
    does not pay the password hash on every request."""
    digest = hashlib.sha256(header.encode()).hexdigest()
    hit = _basic_cache.get(digest)
    if hit and hit[0] > time.time():
        return load().get(hit[1])
    try:
        name, _, password = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    u = authenticate(name, password, client)
    if u is not None:
        _basic_cache[digest] = (time.time() + 60, u["name"])
    return u


def bootstrap() -> str | None:
    """Create the first administrator when there are no users. Returns the generated password
    (None when it came from the environment or users exist)."""
    if load():
        return None
    password = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
    create(config.ADMIN_USER, password, "admin")
    return None if config.ADMIN_PASSWORD else password


def _cli(argv: list[str]) -> int:
    import getpass

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not argv or argv[0] not in ("list", "add", "passwd", "delete"):
        print(__doc__.split("\n\n")[-1].strip())
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "list":
            for u in load().values():
                print(f"{u['name']:<24} {u['role']}")
            return 0
        if not args:
            print("A user name is needed.")
            return 2
        name = args[0]
        if cmd == "delete":
            delete(name)
        else:
            pw = getpass.getpass(f"Password for {name}: ")
            if pw != getpass.getpass("Again: "):
                print("The passwords differ.")
                return 1
            if cmd == "add":
                create(name, pw, "admin" if "--admin" in args else "user")
            else:
                update(name, password=pw)
        print("Done.")
        return 0
    except KeyError:
        print("No such user.")
    except ValueError as e:
        print(e)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
