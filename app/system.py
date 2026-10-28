"""Lettura delle metriche di sistema (psutil + file di /sys e /proc, specifici del Raspberry Pi)."""
import glob
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import psutil

from . import config

_REAL_FS = {"ext4", "ext3", "ext2", "vfat", "exfat", "ntfs", "ntfs3", "btrfs", "xfs", "f2fs"}

# bit di get_throttled (documentazione Raspberry Pi)
THROTTLE_BITS = {
    0: ("undervoltage", "Sotto-tensione in questo momento"),
    1: ("freq_capped", "Frequenza limitata in questo momento"),
    2: ("throttled", "Throttling attivo in questo momento"),
    3: ("temp_limit", "Limite di temperatura raggiunto"),
    16: ("undervoltage_past", "Sotto-tensione dall'avvio"),
    17: ("freq_capped_past", "Frequenza limitata dall'avvio"),
    18: ("throttled_past", "Throttling dall'avvio"),
    19: ("temp_limit_past", "Limite di temperatura raggiunto dall'avvio"),
}


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip().replace("\x00", "")
    except OSError:
        return None


def host_path(p: str) -> str:
    """Percorso dentro il container che corrisponde a p sull'host."""
    if config.HOST_ROOT and os.path.isdir(config.HOST_ROOT):
        return os.path.join(config.HOST_ROOT, p.lstrip("/"))
    return p


# ---------------------------------------------------------------- temperatura / Pi
def cpu_temp() -> float | None:
    v = _read("/sys/class/thermal/thermal_zone0/temp")
    if v:
        try:
            return round(int(v) / 1000, 1)
        except ValueError:
            pass
    try:
        temps = psutil.sensors_temperatures()
        for entries in temps.values():
            if entries:
                return round(entries[0].current, 1)
    except (AttributeError, OSError):
        pass
    return None


def cpu_freq_mhz() -> float | None:
    v = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if v:
        try:
            return round(int(v) / 1000)
        except ValueError:
            pass
    try:
        f = psutil.cpu_freq()
        return round(f.current) if f else None
    except (OSError, NotImplementedError):
        return None


def _throttled_raw() -> int | None:
    """Legge get_throttled: prima vcgencmd (se presente), poi sysfs."""
    exe = shutil.which("vcgencmd")
    if exe:
        try:
            out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            if "=" in out:
                return int(out.split("=")[1].strip(), 16)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    for pattern in ("/sys/devices/platform/soc/*firmware*/get_throttled",
                    "/sys/devices/platform/*firmware*/get_throttled",
                    "/sys/devices/platform/*/*firmware*/get_throttled"):
        for path in glob.glob(pattern):
            v = _read(path)
            if v:
                try:
                    return int(v, 16) if v.lower().startswith("0x") else int(v)
                except ValueError:
                    continue
    return None


def decode_throttled(raw: int | None) -> dict:
    if raw is None:
        return {"available": False, "raw": None, "flags": {}, "messages": []}
    flags = {name: bool(raw & (1 << bit)) for bit, (name, _) in THROTTLE_BITS.items()}
    messages = [msg for bit, (_, msg) in THROTTLE_BITS.items() if raw & (1 << bit)]
    return {"available": True, "raw": hex(raw), "flags": flags, "messages": messages}


def pi_status() -> dict:
    d = decode_throttled(_throttled_raw())
    d["model"] = (_read("/proc/device-tree/model") or platform.machine())
    d["freq_mhz"] = cpu_freq_mhz()
    fmax = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    d["freq_max_mhz"] = round(int(fmax) / 1000) if fmax and fmax.isdigit() else None
    return d


# ---------------------------------------------------------------- dischi / rete
def disk_partitions() -> list[dict]:
    """Partizioni reali dell'host (via /proc/1/mounts, altrimenti quelle visibili)."""
    mounts: list[tuple[str, str, str]] = []
    src = "/proc/1/mounts" if os.access("/proc/1/mounts", os.R_OK) else "/proc/self/mounts"
    try:
        for line in Path(src).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] in _REAL_FS:
                mounts.append((parts[0], parts[1].replace("\\040", " "), parts[2]))
    except OSError:
        pass
    out, seen = [], set()
    for dev, mnt, fs in mounts:
        if dev in seen or mnt.startswith(("/var/lib/docker", "/etc/", "/host")):
            continue
        seen.add(dev)
        try:
            u = psutil.disk_usage(host_path(mnt))
        except OSError:
            continue
        out.append({"device": dev, "mount": mnt, "fstype": fs, "total": u.total,
                    "used": u.used, "percent": u.percent})
    if not out:
        u = psutil.disk_usage(host_path("/"))
        out.append({"device": "/", "mount": "/", "fstype": "?", "total": u.total,
                    "used": u.used, "percent": u.percent})
    return out


def root_disk_percent() -> float:
    try:
        return psutil.disk_usage(host_path("/")).percent
    except OSError:
        return 0.0


def network_interfaces() -> list[dict]:
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo" or name.startswith(("veth", "br-", "docker")):
            continue
        ipv4 = [a.address for a in addrs if a.family == socket.AF_INET]
        ipv6 = [a.address.split("%")[0] for a in addrs if a.family == socket.AF_INET6]
        mac = next((a.address for a in addrs if a.family == psutil.AF_LINK), None)
        st = stats.get(name)
        out.append({"name": name, "ipv4": ipv4, "ipv6": ipv6[:1], "mac": mac,
                    "up": bool(st and st.isup), "speed": st.speed if st else 0})
    return out


def system_info() -> dict:
    os_name = None
    for p in (host_path("/etc/os-release"), "/etc/os-release"):
        txt = _read(p)
        if txt:
            for line in txt.splitlines():
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip('"')
                    break
            if os_name:
                break
    host = _read(host_path("/etc/hostname")) or socket.gethostname()
    vm = psutil.virtual_memory()
    return {
        "hostname": host,
        "os": os_name or platform.platform(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "model": _read("/proc/device-tree/model") or platform.machine(),
        "cores": psutil.cpu_count(logical=True),
        "mem_total": vm.total,
        "boot_time": int(psutil.boot_time()),
        "interfaces": network_interfaces(),
    }


# ---------------------------------------------------------------- snapshot live
class RateCounter:
    """Calcola byte/s a partire da contatori cumulativi."""
    def __init__(self):
        self.prev: tuple[float, tuple[float, ...]] | None = None

    def rate(self, values: tuple[float, ...]) -> tuple[float, ...]:
        now = time.monotonic()
        if self.prev is None:
            self.prev = (now, values)
            return tuple(0.0 for _ in values)
        t0, v0 = self.prev
        dt = max(now - t0, 1e-3)
        self.prev = (now, values)
        return tuple(max(0.0, (a - b) / dt) for a, b in zip(values, v0))


_net, _disk = RateCounter(), RateCounter()


def snapshot() -> dict:
    """Istantanea completa delle metriche principali."""
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    cpu = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    nio = psutil.net_io_counters()
    rx, tx = _net.rate((nio.bytes_recv, nio.bytes_sent)) if nio else (0.0, 0.0)
    try:
        dio = psutil.disk_io_counters()
    except Exception:
        dio = None
    dr, dw = _disk.rate((dio.read_bytes, dio.write_bytes)) if dio else (0.0, 0.0)
    return {
        "ts": int(time.time()),
        "cpu": cpu,
        "cores": [round(x, 1) for x in per_core],
        "mem": vm.percent,
        "mem_used": vm.total - vm.available,
        "mem_total": vm.total,
        "swap": sw.percent,
        "swap_used": sw.used,
        "swap_total": sw.total,
        "temp": cpu_temp(),
        "freq": cpu_freq_mhz(),
        "load1": round(load[0], 2), "load5": round(load[1], 2), "load15": round(load[2], 2),
        "disk": root_disk_percent(),
        "net_rx": round(rx), "net_tx": round(tx),
        "disk_r": round(dr), "disk_w": round(dw),
        "uptime": int(time.time() - psutil.boot_time()),
    }


# ---------------------------------------------------------------- processi
def list_processes(sort: str = "cpu", limit: int = 100, query: str = "") -> list[dict]:
    ncpu = psutil.cpu_count() or 1
    rows = []
    q = query.lower().strip()
    for p in psutil.process_iter(["pid", "name", "username", "memory_percent", "memory_info",
                                  "status", "num_threads", "create_time", "cmdline"]):
        try:
            info = p.info
            cpu = p.cpu_percent(None) / ncpu  # normalizzato 0-100 sul totale dei core
            cmd = " ".join(info["cmdline"] or []) or info["name"] or ""
            if q and q not in (info["name"] or "").lower() and q not in cmd.lower() and q != str(info["pid"]):
                continue
            rows.append({
                "pid": info["pid"], "name": info["name"], "user": info["username"],
                "cpu": round(cpu, 1), "mem": round(info["memory_percent"] or 0, 1),
                "rss": info["memory_info"].rss if info["memory_info"] else 0,
                "status": info["status"], "threads": info["num_threads"],
                "started": int(info["create_time"] or 0), "cmd": cmd[:300],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    key = {"cpu": "cpu", "mem": "rss", "pid": "pid", "name": "name"}.get(sort, "cpu")
    rows.sort(key=lambda r: (r[key] or 0) if key != "name" else (r[key] or "").lower(),
              reverse=key not in ("name", "pid"))
    return rows[:limit]


PROTECTED_PIDS = {0, 1}


def signal_process(pid: int, sig: str) -> str:
    if pid in PROTECTED_PIDS or pid == os.getpid():
        raise PermissionError("Processo protetto")
    s = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "STOP": signal.SIGSTOP, "CONT": signal.SIGCONT}.get(sig)
    if s is None:
        raise ValueError("Segnale non valido")
    p = psutil.Process(pid)
    name = p.name()
    p.send_signal(s)
    return name


def process_detail(pid: int) -> dict:
    """Dettagli di un processo; i campi non leggibili restano None."""
    p = psutil.Process(pid)

    def get(fn, default=None):
        try:
            return fn()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            return default

    mem = get(p.memory_info)
    parent = get(p.parent)
    ports = []
    for c in get(lambda: p.net_connections(kind="inet"), []) or []:
        if c.status == "LISTEN":
            ports.append(f"{c.laddr.ip}:{c.laddr.port}")
    return {
        "pid": pid, "name": get(p.name), "exe": get(p.exe), "cmdline": " ".join(get(p.cmdline, []) or []),
        "user": get(p.username), "status": get(p.status), "started": int(get(p.create_time, 0) or 0),
        "threads": get(p.num_threads), "nice": get(p.nice), "cwd": get(p.cwd),
        "rss": mem.rss if mem else None, "vms": mem.vms if mem else None,
        "fds": get(p.num_fds), "parent": {"pid": parent.pid, "name": get(parent.name)} if parent else None,
        "children": len(get(p.children, []) or []), "listening": sorted(set(ports))[:12],
        "protected": pid in PROTECTED_PIDS or pid == os.getpid(),
    }
