import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alerts, auth, config, db, docker_api, netguard, system
from .collector import collector

STATIC = Path(__file__).parent / "static"
RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.init(config.ensure_runtime())
    db.init()
    db.add_event("info", "system", "Dashboard avviata")
    collector.start()
    yield
    collector.stop()


app = FastAPI(title="Pi Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
api_dep = [Depends(auth.require_auth)]
ALLOWED_NETS = netguard.parse_networks(config.ALLOWED_NETWORKS)


@app.middleware("http")
async def only_allowed_networks(request: Request, call_next):
    """Secondo livello oltre al bind: rifiuta chi non arriva da Tailscale (o dal Pi stesso)."""
    ip = request.client.host if request.client else None
    if not netguard.is_allowed(ip, ALLOWED_NETS):
        return JSONResponse({"detail": "Accesso consentito solo tramite Tailscale"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ pagine / auth
@app.get("/", include_in_schema=False)
def index(request: Request):
    if not auth.valid_token(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/login")
    return FileResponse(STATIC / "index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC / "login.html")


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    if auth.too_many_fails(ip):
        raise HTTPException(429, "Troppi tentativi. Riprova tra qualche minuto.")
    if not auth.check_password(body.password):
        auth.record_fail(ip)
        db.add_event("warn", "auth", f"Tentativo di accesso fallito da {ip}")
        raise HTTPException(401, "Password errata")
    auth.clear_fails(ip)
    response.set_cookie(auth.COOKIE, auth.make_token(), max_age=config.SESSION_HOURS * 3600,
                        httponly=True, samesite="strict", secure=config.COOKIE_SECURE)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True}


# ------------------------------------------------------------------ metriche
@app.get("/api/live", dependencies=api_dep)
def live():
    snap = collector.latest or system.snapshot()
    return {**snap, "alerts": collector.engine.active_list(),
            "flags": collector.flags, "docker": config.DOCKER_ENABLED}


@app.get("/api/history", dependencies=api_dep)
def history(range: str = "1h"):
    if range not in RANGES:
        raise HTTPException(400, "Intervallo non valido")
    return db.history(RANGES[range])


@app.get("/api/system", dependencies=api_dep)
def system_info():
    return {**system.system_info(), "disks": system.disk_partitions(), "pi": collector.pi or system.pi_status(),
            "db": db.stats(), "features": {"docker": config.DOCKER_ENABLED, "power": config.POWER_ENABLED,
                                           "telegram": alerts.telegram_enabled()},
            "retention_days": config.RETENTION_DAYS}


# ------------------------------------------------------------------ processi
@app.get("/api/processes", dependencies=api_dep)
def processes(sort: str = "cpu", limit: int = 80, q: str = ""):
    return {"processes": system.list_processes(sort, max(1, min(limit, 500)), q)}


class SignalIn(BaseModel):
    signal: str


@app.post("/api/processes/{pid}/signal", dependencies=api_dep)
def process_signal(pid: int, body: SignalIn):
    import psutil
    try:
        name = system.signal_process(pid, body.signal.upper())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except psutil.NoSuchProcess:
        raise HTTPException(404, "Processo non trovato")
    except psutil.AccessDenied:
        raise HTTPException(403, "Permesso negato")
    db.add_event("info", "process", f"Segnale {body.signal.upper()} inviato a {name} (PID {pid})")
    return {"ok": True, "name": name}


# ------------------------------------------------------------------ container
def _docker_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except docker_api.DockerUnavailable as e:
        raise HTTPException(503, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get("/api/containers", dependencies=api_dep)
def containers(stats: int = 1):
    return {"containers": _docker_guard(docker_api.list_containers, bool(stats))}


@app.post("/api/containers/{name}/{act}", dependencies=api_dep)
def container_action(name: str, act: str):
    if act not in ("start", "stop", "restart"):
        raise HTTPException(400, "Azione non valida")
    if act in ("stop", "restart"):
        collector.expected_stops.add(name)
    real = _docker_guard(docker_api.action, name, act)
    label = {"start": "avviato", "stop": "fermato", "restart": "riavviato"}[act]
    db.add_event("info", "container", f"Container «{real}» {label} dalla dashboard")
    collector._last_containers = 0  # aggiorna subito lo stato osservato
    if act == "start":
        collector.expected_stops.discard(name)
    return {"ok": True}


@app.get("/api/containers/{name}/logs", dependencies=api_dep)
def container_logs(name: str, tail: int = 200):
    return {"logs": _docker_guard(docker_api.logs, name, tail)}


# ------------------------------------------------------------------ alimentazione
class PowerIn(BaseModel):
    action: str
    password: str


@app.post("/api/power", dependencies=api_dep)
def power(body: PowerIn):
    if not config.POWER_ENABLED:
        raise HTTPException(403, "Funzione disabilitata")
    if body.action not in ("reboot", "shutdown"):
        raise HTTPException(400, "Azione non valida")
    if not auth.check_password(body.password):
        # 403 e non 401: 401 significa "sessione scaduta" per il frontend
        raise HTTPException(403, "Password errata")
    label = "Riavvio" if body.action == "reboot" else "Spegnimento"
    db.add_event("warn", "power", f"{label} del Raspberry richiesto dalla dashboard")
    _docker_guard(docker_api.reboot_host, body.action)
    return {"ok": True}


# ------------------------------------------------------------------ impostazioni / eventi
@app.get("/api/settings", dependencies=api_dep)
def get_settings():
    return {"alerts": alerts.load_settings(), "telegram_configured": alerts.telegram_enabled()}


@app.put("/api/settings", dependencies=api_dep)
def put_settings(body: dict):
    try:
        clean = alerts.validate_settings(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    merged = {**alerts.load_settings(), **clean}
    if merged["temp_warn"] >= merged["temp_crit"]:
        raise HTTPException(400, "La soglia di avviso deve essere minore di quella critica")
    db.set_setting("alerts", merged)
    return {"alerts": merged}


@app.post("/api/settings/test-telegram", dependencies=api_dep)
def test_telegram():
    if not alerts.telegram_enabled():
        raise HTTPException(400, "Telegram non configurato (vedi .env)")
    alerts.send_telegram("✅ Messaggio di prova dalla dashboard del Raspberry")
    return {"ok": True}


@app.get("/api/events", dependencies=api_dep)
def events(limit: int = 200, level: str | None = None):
    return {"events": db.events(max(1, min(limit, 1000)), level if level in ("info", "warn", "crit") else None)}
