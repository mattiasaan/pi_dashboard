"""Docker simulato per i test e per l'anteprima UI."""
from app import docker_api

STATE = {
    "containers": [
        {"id": "a1b2c3d4e5f6", "name": "pihole", "image": "pihole/pihole:latest", "status": "running", "health": "healthy",
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "unless-stopped",
         "ports": ["53→53/tcp", "80→80/tcp"], "cpu": 1.4, "mem_used": 96_000_000, "mem_limit": 8_000_000_000, "is_self": False},
        {"id": "b2c3d4e5f6a1", "name": "home-assistant", "image": "ghcr.io/home-assistant/home-assistant:stable", "status": "running", "health": None,
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "always",
         "ports": [], "cpu": 6.2, "mem_used": 412_000_000, "mem_limit": 8_000_000_000, "is_self": False},
        {"id": "c3d4e5f6a1b2", "name": "pi-dashboard", "image": "pi-dashboard:latest", "status": "running", "health": "healthy",
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "unless-stopped",
         "ports": [], "cpu": 0.8, "mem_used": 61_000_000, "mem_limit": 8_000_000_000, "is_self": True},
        {"id": "d4e5f6a1b2c3", "name": "nextcloud", "image": "nextcloud:29", "status": "exited", "health": None,
         "state_text": "exited", "exit_code": 137, "started_at": "x", "restart_policy": "no",
         "ports": [], "cpu": None, "mem_used": None, "mem_limit": None, "is_self": False},
    ],
    "calls": [],
}


def install(monkeypatch=None):
    def list_containers(with_stats=True):
        return [dict(c) for c in STATE["containers"]]

    def action(name, act):
        c = next((c for c in STATE["containers"] if c["name"] == name), None)
        if not c:
            raise LookupError("Container non trovato")
        if c["is_self"] and act != "start":
            raise PermissionError("Non posso fermare la dashboard da sé stessa")
        STATE["calls"].append((name, act))
        c["status"] = "running" if act in ("start", "restart") else "exited"
        c["exit_code"] = 0
        c["cpu"] = 0.5 if c["status"] == "running" else None
        return name

    def logs(name, tail=200):
        if not any(c["name"] == name for c in STATE["containers"]):
            raise LookupError("Container non trovato")
        return "\n".join(f"2026-10-24T13:00:{i:02d}Z riga di log {i} <b>html</b>" for i in range(min(tail, 30)))

    def reboot_host(a):
        STATE["calls"].append(("host", a))

    patches = dict(list_containers=list_containers, action=action, logs=logs, reboot_host=reboot_host)
    for k, v in patches.items():
        if monkeypatch:
            monkeypatch.setattr(docker_api, k, v)
        else:
            setattr(docker_api, k, v)
