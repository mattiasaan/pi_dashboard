"""Docker simulato per i test e per l'anteprima UI."""
from app import docker_api

STATE = {
    "containers": [
        {"id": "a1b2c3d4e5f6", "name": "pihole", "image": "pihole/pihole:latest", "status": "running", "health": "healthy",
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "unless-stopped",
         "ports": ["53→53/tcp", "80→80/tcp"], "cpu": 1.4, "mem_used": 96_000_000, "mem_limit": 8_000_000_000, "is_self": False,
         "project": None, "service": None, "finished_at": None, "oom_killed": False},
        {"id": "b2c3d4e5f6a1", "name": "home-assistant", "image": "ghcr.io/home-assistant/home-assistant:stable", "status": "running", "health": None,
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "always",
         "ports": [], "cpu": 6.2, "mem_used": 412_000_000, "mem_limit": 8_000_000_000, "is_self": False,
         "project": None, "service": None, "finished_at": None, "oom_killed": False},
        {"id": "c3d4e5f6a1b2", "name": "pi-dashboard", "image": "pi-dashboard:latest", "status": "running", "health": "healthy",
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "unless-stopped",
         "ports": [], "cpu": 0.8, "mem_used": 61_000_000, "mem_limit": 8_000_000_000, "is_self": True,
         "project": "pi-dashboard", "service": "pi-dashboard", "finished_at": None, "oom_killed": False},
        {"id": "d4e5f6a1b2c3", "name": "nextcloud", "image": "nextcloud:29", "status": "exited", "health": None,
         "state_text": "exited", "exit_code": 137, "started_at": "x", "restart_policy": "no",
         "ports": [], "cpu": None, "mem_used": None, "mem_limit": None, "is_self": False,
         "project": "immich", "service": "immich-server", "finished_at": "2026-10-26T10:00:00Z", "oom_killed": True},
        {"id": "e5f6a1b2c3d4", "name": "immich_postgres", "image": "tensorchord/pgvecto-rs:pg14", "status": "running", "health": "healthy",
         "state_text": "running", "exit_code": 0, "started_at": "x", "restart_policy": "always",
         "ports": [], "cpu": 3.1, "mem_used": 220_000_000, "mem_limit": 8_000_000_000, "is_self": False,
         "project": "immich", "service": "database", "finished_at": None, "oom_killed": False},
        {"id": "f6a1b2c3d4e5", "name": "immich_redis", "image": "redis:7", "status": "exited", "health": None,
         "state_text": "exited", "exit_code": 0, "started_at": "x", "restart_policy": "always",
         "ports": [], "cpu": None, "mem_used": None, "mem_limit": None, "is_self": False,
         "project": "immich", "service": "redis", "finished_at": "2026-10-26T09:00:00Z", "oom_killed": False},
    ],
    "calls": [],
}


import copy
_INITIAL = copy.deepcopy(STATE["containers"])


def reset():
    STATE["containers"][:] = copy.deepcopy(_INITIAL)
    STATE["calls"].clear()


def install(monkeypatch=None):
    reset()
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

    def details(name):
        c = next((c for c in STATE["containers"] if c["name"] == name), None)
        if not c:
            raise LookupError("Container non trovato")
        return {"name": name, "id": c["id"], "image": c["image"], "created": "2026-09-01T10:00:00Z", "started_at": "2026-10-20T08:00:00Z",
                "finished_at": c["finished_at"], "status": c["status"], "exit_code": c["exit_code"], "oom_killed": c["oom_killed"],
                "error": None, "restart_count": 2, "restart_policy": c["restart_policy"], "command": "/init", "user": "root",
                "workdir": "/app", "network_mode": "bridge", "networks": [{"name": "bridge", "ip": "172.17.0.2"}],
                "mounts": [{"source": "/srv/data", "target": "/data", "rw": True, "type": "bind"}],
                "env_names": ["TZ", "DB_PASSWORD"], "project": c["project"], "memory_limit": None}

    def usage():
        return {"images": {"count": 9, "size": 4_200_000_000, "unused_size": 900_000_000, "unused_count": 3, "dangling_count": 2, "dangling_size": 300_000_000},
                "containers": {"count": 6, "size": 50_000_000}, "volumes": {"count": 4, "size": 1_800_000_000},
                "build_cache": {"count": 7, "size": 600_000_000, "reclaimable": 450_000_000}}

    def prune(what):
        if what not in ("images", "build_cache"):
            raise ValueError("Operazione non valida")
        STATE["calls"].append(("prune", what))
        return {"reclaimed": 300_000_000, "removed": 2}

    patches = dict(list_containers=list_containers, action=action, logs=logs, reboot_host=reboot_host,
                   details=details, usage=usage, prune=prune)
    for k, v in patches.items():
        if monkeypatch:
            monkeypatch.setattr(docker_api, k, v)
        else:
            setattr(docker_api, k, v)
