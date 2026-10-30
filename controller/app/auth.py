"""Form-login met een ondertekende sessiecookie.

De cookie bevat alleen een vervaltijd plus een HMAC. De sleutel wordt afgeleid
van een geheim in data/ en van het UI-wachtwoord: wijzig je het wachtwoord,
dan zijn alle bestaande sessies direct ongeldig.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .config import DATA

COOKIE = "otlab_session"
USERNAME = "admin"
SESSION_SECONDS = 12 * 3600

_SECRET_FILE = DATA / "session_secret"
_PASSWORD_FILE = DATA / "ui_password"


def _password() -> str:
    return _PASSWORD_FILE.read_text().strip() if _PASSWORD_FILE.exists() else "admin"


def _key() -> bytes:
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_text(secrets.token_hex(32))
        _SECRET_FILE.chmod(0o600)
    return hashlib.sha256((_SECRET_FILE.read_text().strip() + "|" + _password()).encode()).digest()


def _sign(expiry: int) -> str:
    return hmac.new(_key(), str(expiry).encode(), hashlib.sha256).hexdigest()


def check_credentials(username: str, password: str) -> bool:
    ok_user = secrets.compare_digest(username.strip(), USERNAME)
    ok_pw = secrets.compare_digest(password, _password())
    return ok_user and ok_pw


def new_session() -> str:
    expiry = int(time.time()) + SESSION_SECONDS
    return f"{expiry}.{_sign(expiry)}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry, _, sig = token.partition(".")
    if not expiry.isdigit() or int(expiry) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(int(expiry)))
