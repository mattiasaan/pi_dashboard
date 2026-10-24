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


def _stats_for(c) -> dict:
    now = time.monotonic()
    hit = _stats_cache.get(c.id)
    if hit and now - hit[0] < 5:
        return hit[1]
    try:
        st = c.stats(stream=False)
        used, limit = calc_mem(st)
        out = {"cpu": calc_cpu_percent(st), "mem_used": used, "mem_limit": limit}
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
