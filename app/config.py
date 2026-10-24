"""Configurazione letta da variabili d'ambiente (vedi .env.example)."""
import os
import secrets
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "metrics.db"

# Radice del filesystem dell'host montata nel container (sola lettura).
# Se la cartella non esiste si usa il filesystem locale.
HOST_ROOT = os.environ.get("HOST_ROOT", "/host")

PORT = _int("PORT", 8080)
# Dove ascolta: "tailscale" (solo l'IP dell'interfaccia Tailscale), "all", oppure un IP (es. 127.0.0.1).
BIND_ADDRESS = os.environ.get("BIND_ADDRESS", "tailscale")
TAILSCALE_INTERFACE = os.environ.get("TAILSCALE_INTERFACE", "tailscale0")
BIND_WAIT_SECONDS = _int("BIND_WAIT_SECONDS", 120)
# Secondo livello: reti da cui sono accettate le richieste ("any" per disattivare il filtro).
ALLOWED_NETWORKS = os.environ.get("ALLOWED_NETWORKS", "100.64.0.0/10,fd7a:115c:a1e0::/48,127.0.0.0/8,::1/128")
PASSWORD = os.environ.get("DASH_PASSWORD", "")
SESSION_HOURS = _int("SESSION_HOURS", 24 * 7)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

LIVE_INTERVAL = _float("LIVE_INTERVAL", 2.0)      # secondi: aggiornamento dati live
SAMPLE_INTERVAL = _float("SAMPLE_INTERVAL", 10.0)  # secondi: scrittura nel DB
RAW_RETENTION_HOURS = _int("RAW_RETENTION_HOURS", 6)
RETENTION_DAYS = _int("RETENTION_DAYS", 30)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# Nome dell'immagine di questo stesso container: serve per eseguire
# riavvio/spegnimento dell'host tramite un container "helper" temporaneo.
SELF_IMAGE = os.environ.get("SELF_IMAGE", "pi-dashboard:latest")

DOCKER_ENABLED = os.environ.get("DOCKER_ENABLED", "true").lower() == "true"
POWER_ENABLED = os.environ.get("POWER_ENABLED", "true").lower() == "true"


def ensure_runtime() -> str:
    """Crea la cartella dati, risolve password e chiave di sessione."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    global PASSWORD
    if not PASSWORD:
        PASSWORD = secrets.token_urlsafe(9)
        print(f"[pi-dashboard] DASH_PASSWORD non impostata. Password temporanea: {PASSWORD}", flush=True)
    key_file = DATA_DIR / ".secret"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    return key_file.read_text().strip()
