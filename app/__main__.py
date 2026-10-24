import sys
from pathlib import Path

import uvicorn

from . import config, netguard

try:
    host = netguard.resolve_bind(config.BIND_ADDRESS, config.TAILSCALE_INTERFACE, wait=config.BIND_WAIT_SECONDS)
except (RuntimeError, ValueError) as e:
    # Niente ripiego su 0.0.0.0: se Tailscale manca ci si ferma e Docker riprova (restart: unless-stopped).
    print(f"[pi-dashboard] {e}", flush=True)
    sys.exit(1)

# Indirizzo reale su cui ascolta: lo legge il healthcheck del container.
Path("/tmp/bind_host").write_text("127.0.0.1" if host == "0.0.0.0" else host)
print(f"[pi-dashboard] In ascolto su http://{host}:{config.PORT}", flush=True)

# Un solo worker: il thread di raccolta dati deve esistere in un solo processo.
uvicorn.run("app.main:app", host=host, port=config.PORT, workers=1, log_level="warning")
