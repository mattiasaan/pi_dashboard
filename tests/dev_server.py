"""Avvia la dashboard in locale con Docker simulato e uno storico sintetico (anteprima UI e collaudo)."""
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DASH_PASSWORD", "segreta")
os.environ.setdefault("HOST_ROOT", "/nonexistent")

from app import config, db  # noqa: E402
from tests import fake_docker  # noqa: E402

config.ensure_runtime()
db.init()
random.seed(3)
now = int(time.time())


def vals(t):
    day = math.sin((t % 86400) / 86400 * 2 * math.pi - 1.5)
    cpu = max(2, 18 + 14 * day + random.random() * 12)
    return {"cpu": cpu, "mem": 38 + 6 * day + random.random() * 2, "swap": 3 + random.random(),
            "temp": 44 + cpu * 0.35 + random.random() * 2, "load1": cpu / 25, "disk": 41.0,
            "net_rx": max(0, 80_000 + 400_000 * random.random() * (1 + day)), "net_tx": 30_000 + 90_000 * random.random(),
            "disk_r": 20_000 * random.random(), "disk_w": 150_000 * random.random(), "freq": 1500.0}


for t in range(now - 30 * 86400, now, 60):
    if 9 * 86400 < now - t < 9.3 * 86400:       # un buco: Pi spento per un po'
        continue
    v = vals(t)
    db.insert_minute(t, v, v["cpu"] + 10, v["temp"] + 3)
for t in range(now - 6 * 3600, now, 10):
    db.insert_raw(t, vals(t))
db.add_event("warn", "alert", "Temperatura alta: 76.2°C", now - 4000)
db.add_event("info", "alert", "Rientrato: Temperatura alta", now - 3800)
db.add_event("crit", "container", "Il container «nextcloud» si è fermato (codice 137)", now - 900)
db.add_event("info", "container", "Container «pihole» riavviato dalla dashboard", now - 300)

# controlli di esempio con storico
for name, kind, target, ok_rate in (("Internet", "tcp", "1.1.1.1:443", 1.0), ("Router", "tcp", "192.168.1.1:80", 0.97), ("Immich", "http", "https://raspbot.example.ts.net", 1.0)):
    cid = db.add_check(name, kind, target, 60)
    for i in range(60):
        ok = random.random() < ok_rate
        db.insert_check_result(cid, now - (60 - i) * 60, ok, 8 + random.random() * 30 if ok else None, None if ok else "timeout")
bad = db.add_check("NAS", "tcp", "192.168.1.50:445", 60)
for i in range(60):
    db.insert_check_result(bad, now - (60 - i) * 60, i < 40, 12.0 if i < 40 else None, None if i < 40 else "timeout")
db.set_setting("autorestart", {"pihole": {"enabled": True}})

fake_docker.install()
import uvicorn  # noqa: E402
uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8099")), log_level="warning")
