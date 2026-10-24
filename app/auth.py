"""Login con password singola, cookie di sessione firmato (HMAC) e limite ai tentativi."""
import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from . import config

COOKIE = "pidash_session"
_secret = b""
_fails: dict[str, list[float]] = {}
MAX_FAILS, WINDOW = 5, 300


def init(secret: str) -> None:
    global _secret
    _secret = secret.encode()


def make_token(now: float | None = None) -> str:
    exp = int((now or time.time()) + config.SESSION_HOURS * 3600)
    sig = hmac.new(_secret, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < (now or time.time()):
        return False
    good = hmac.new(_secret, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def check_password(pw: str) -> bool:
    return hmac.compare_digest(pw.encode(), config.PASSWORD.encode())


def too_many_fails(ip: str, now: float | None = None) -> bool:
    now = now or time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < WINDOW]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)


def require_auth(request: Request) -> None:
    if not valid_token(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Accesso richiesto")
    # protezione CSRF per le operazioni che modificano lo stato (oltre a SameSite=Strict)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "pi-dashboard":
        raise HTTPException(403, "Richiesta non valida")
