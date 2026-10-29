"""Gestione container Docker tramite /var/run/docker.sock."""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config

try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
except ImportError:  # pragma: no cover
    docker = None  # type: ignore

_client = None
_client_lock = threading.Lock()
_stats_cache: dict[str, tuple[float, dict]] = {}
_pool = ThreadPoolExecutor(max_workers=6)

SELF_ID = os.environ.get("HOSTNAME", "")  # nel container l'hostname è l'id breve (non con network host)
SELF_NAME = os.environ.get("SELF_CONTAINER_NAME", "pi-dashboard")


class DockerUnavailable(Exception):
    pass


def client():
    global _client
    if not config.DOCKER_ENABLED or docker is None:
        raise DockerUnavailable("Docker disabilitato")
    with _client_lock:
        if _client is None:
            try:
                _client = docker.from_env(timeout=8)
                _client.ping()
            except DockerException as e:
                _client = None
                raise DockerUnavailable(f"Docker non raggiungibile: {e}") from e
        return _client


def calc_cpu_percent(st: dict) -> float:
    """Formula ufficiale di docker stats."""
    try:
        cpu = st["cpu_stats"]; pre = st["precpu_stats"]
        dcpu = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        dsys = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        if dsys > 0 and dcpu > 0:
            return round(dcpu / dsys * ncpu * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return 0.0


def calc_mem(st: dict) -> tuple[int, int]:
    """(usata, limite) come docker stats: usage - cache inattiva."""
    try:
        m = st["memory_stats"]
        stats = m.get("stats", {})
        cache = stats.get("inactive_file", stats.get("total_inactive_file", stats.get("cache", 0)))
        return max(m["usage"] - cache, 0), m.get("limit", 0)
    except (KeyError, TypeError):
        return 0, 0


_prev_cpu: dict[str, tuple[float, int]] = {}   # id -> (istante, nanosecondi di CPU usati)
_one_shot = True                                # lo si disattiva da solo con un Docker molto vecchio


def _read_stats(c) -> dict:
    """Una lettura delle statistiche. Con one_shot Docker risponde subito invece di aspettare un secondo
    campione (circa 1-2 s per container): su un Pi 3 con molti container fa la differenza."""
    global _one_shot
    if _one_shot:
        try:
            return c.stats(stream=False, one_shot=True)
        except Exception as e:
            if e.__class__.__name__ != "InvalidVersion":
                raise
            _one_shot = False
    return c.stats(stream=False)


def _stats_for(c) -> dict:
    now = time.monotonic()
    hit = _stats_cache.get(c.id)
    if hit and now - hit[0] < 5:
        return hit[1]
    try:
        st = _read_stats(c)
        used, limit = calc_mem(st)
        cpu = calc_cpu_percent(st)              # daemon che includono già il campione precedente
        total = st["cpu_stats"]["cpu_usage"]["total_usage"]
        prev = _prev_cpu.get(c.id)
        _prev_cpu[c.id] = (now, total)
        if prev and now > prev[0] and total >= prev[1]:
            cpu = round((total - prev[1]) / ((now - prev[0]) * 1e9) * 100, 1)   # % di un core, come docker stats
        elif not cpu:
            cpu = None if not prev else 0.0     # prima lettura: ancora nessun dato
        out = {"cpu": cpu, "mem_used": used, "mem_limit": limit}
    except Exception:
        out = {"cpu": None, "mem_used": None, "mem_limit": None}
    _stats_cache[c.id] = (now, out)
    return out


def _ports(c) -> list[str]:
    out = []
    for cport, binds in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
        for b in binds or []:
            out.append(f"{b['HostPort']}→{cport}")
    return sorted(set(out))


def _is_self(c) -> bool:
    return c.name == SELF_NAME or (bool(SELF_ID) and c.id.startswith(SELF_ID))


def list_containers(with_stats: bool = True) -> list[dict]:
    cl = client()
    try:
        containers = cl.containers.list(all=True)
    except DockerException as e:
        raise DockerUnavailable(str(e)) from e
    running = [c for c in containers if c.status == "running"]
    alive = {c.id for c in containers}          # niente memoria per i container ormai eliminati
    for cache in (_stats_cache, _prev_cpu):
        for gone in [k for k in cache if k not in alive]:
            cache.pop(gone, None)
    stats = {}
    if with_stats and running:
        for c, s in zip(running, _pool.map(_stats_for, running)):
            stats[c.id] = s
    out = []
    for c in containers:
        tags = c.image.tags if c.image else []
        health = (c.attrs.get("State", {}).get("Health") or {}).get("Status")
        s = stats.get(c.id, {})
        out.append({
            "id": c.short_id, "name": c.name, "image": tags[0] if tags else (c.attrs.get("Config", {}).get("Image") or "?"),
            "status": c.status, "health": health,
            "state_text": c.attrs.get("State", {}).get("Status"),
            "exit_code": c.attrs.get("State", {}).get("ExitCode"),
            "started_at": c.attrs.get("State", {}).get("StartedAt"),
            "restart_policy": (c.attrs.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name") or "no",
            "ports": _ports(c), "cpu": s.get("cpu"), "mem_used": s.get("mem_used"),
            "mem_limit": s.get("mem_limit"), "is_self": _is_self(c),
            "project": (c.labels or {}).get("com.docker.compose.project"),
            "service": (c.labels or {}).get("com.docker.compose.service"),
            "finished_at": c.attrs.get("State", {}).get("FinishedAt"),
            "oom_killed": bool(c.attrs.get("State", {}).get("OOMKilled")),
        })
    out.sort(key=lambda x: (x["status"] != "running", x["name"].lower()))
    return out


def _get(name: str):
    try:
        return client().containers.get(name)
    except NotFound as e:
        raise LookupError("Container non trovato") from e
    except DockerException as e:
        raise DockerUnavailable(str(e)) from e


def action(name: str, act: str) -> str:
    c = _get(name)
    if _is_self(c) and act in ("stop", "restart", "kill"):
        raise PermissionError("Non posso fermare la dashboard da sé stessa: usa `docker compose` dall'host")
    try:
        if act == "start":
            c.start()
        elif act == "stop":
            c.stop(timeout=10)
        elif act == "restart":
            c.restart(timeout=10)
        else:
            raise ValueError("Azione non valida")
    except APIError as e:
        raise RuntimeError(e.explanation or str(e)) from e
    _stats_cache.pop(c.id, None)
    return c.name


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def logs(name: str, tail: int = 200) -> str:
    c = _get(name)
    raw = c.logs(tail=max(1, min(tail, 2000)), timestamps=True, stdout=True, stderr=True)
    return _ANSI.sub("", raw.decode("utf-8", errors="replace"))


def reboot_host(action_name: str) -> None:
    """Riavvia/spegne l'host con un container helper che entra nei namespace di PID 1."""
    cmd = {"reboot": "systemctl reboot --force --force || reboot -f",
           "shutdown": "systemctl poweroff --force --force || poweroff -f"}[action_name]
    client().containers.run(
        config.SELF_IMAGE,
        ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c", f"sleep 2; {cmd}"],
        privileged=True, pid_mode="host", remove=True, detach=True, entrypoint="",
    )


# ---------------------------------------------------------------- dettagli, spazio, pulizia
def details(name: str) -> dict:
    """Riepilogo di `docker inspect` senza valori delle variabili d'ambiente (possono contenere segreti)."""
    a = _get(name).attrs
    cfg, st, host = a.get("Config", {}), a.get("State", {}), a.get("HostConfig", {})
    nets = (a.get("NetworkSettings", {}).get("Networks") or {})
    return {
        "name": a.get("Name", "").lstrip("/"), "id": (a.get("Id") or "")[:12], "image": cfg.get("Image"),
        "created": a.get("Created"), "started_at": st.get("StartedAt"), "finished_at": st.get("FinishedAt"),
        "status": st.get("Status"), "exit_code": st.get("ExitCode"), "oom_killed": bool(st.get("OOMKilled")),
        "error": st.get("Error") or None, "restart_count": a.get("RestartCount", 0),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
        "command": " ".join((cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])) or None,
        "user": cfg.get("User") or "root", "workdir": cfg.get("WorkingDir") or None,
        "network_mode": host.get("NetworkMode"),
        "networks": [{"name": n, "ip": v.get("IPAddress") or None} for n, v in nets.items()],
        "mounts": [{"source": m.get("Source") or m.get("Name"), "target": m.get("Destination"),
                    "rw": bool(m.get("RW")), "type": m.get("Type")} for m in a.get("Mounts", [])],
        "env_names": sorted(e.split("=", 1)[0] for e in (cfg.get("Env") or [])),
        "project": (cfg.get("Labels") or {}).get("com.docker.compose.project"),
        "memory_limit": host.get("Memory") or None,
    }


def usage() -> dict:
    """Spazio occupato da Docker (come `docker system df`) e quanto si può recuperare."""
    try:
        df = client().df()
    except DockerException as e:
        raise DockerUnavailable(str(e)) from e
    images = df.get("Images") or []
    unused = [i for i in images if not i.get("Containers")]
    dangling = [i for i in images if not (i.get("RepoTags") or []) or i.get("RepoTags") == ["<none>:<none>"]]
    vols = df.get("Volumes") or []
    cache = df.get("BuildCache") or []
    pos = lambda n: n if isinstance(n, int) and n > 0 else 0
    return {
        "images": {"count": len(images), "size": sum(pos(i.get("Size")) for i in images),
                   "unused_size": sum(pos(i.get("Size")) for i in unused), "unused_count": len(unused),
                   "dangling_count": len(dangling), "dangling_size": sum(pos(i.get("Size")) for i in dangling)},
        "containers": {"count": len(df.get("Containers") or []),
                       "size": sum(pos(c.get("SizeRw")) for c in df.get("Containers") or [])},
        "volumes": {"count": len(vols), "size": sum(pos((v.get("UsageData") or {}).get("Size")) for v in vols)},
        "build_cache": {"count": len(cache), "size": sum(pos(b.get("Size")) for b in cache),
                        "reclaimable": sum(pos(b.get("Size")) for b in cache if not b.get("InUse"))},
    }


def prune(what: str) -> dict:
    """Pulizia sicura: solo immagini senza nome e cache di build. Non tocca container né volumi."""
    cl = client()
    try:
        if what == "images":
            r = cl.images.prune(filters={"dangling": True})
            return {"reclaimed": r.get("SpaceReclaimed", 0), "removed": len(r.get("ImagesDeleted") or [])}
        if what == "build_cache":
            r = cl.api.prune_builds()
            return {"reclaimed": r.get("SpaceReclaimed", 0), "removed": len(r.get("CachesDeleted") or [])}
    except APIError as e:
        raise RuntimeError(e.explanation or str(e)) from e
    except DockerException as e:
        raise DockerUnavailable(str(e)) from e
    raise ValueError("Operazione non valida")
