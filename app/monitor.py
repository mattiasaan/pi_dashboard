"""Controlli di raggiungibilità (HTTP e TCP) con storico, uptime e avvisi."""
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import urlparse

import httpx

from . import alerts, db

KINDS = ("http", "tcp")
MAX_CHECKS = 20
FAILS_BEFORE_ALERT = 2


def validate_check(name: str, kind: str, target: str, interval: int) -> tuple[str, str, str, int]:
    name, target = (name or "").strip(), (target or "").strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("Il nome deve avere da 1 a 40 caratteri")
    if kind not in KINDS:
        raise ValueError("Tipo non valido")
    if not 15 <= int(interval) <= 3600:
        raise ValueError("L'intervallo deve essere tra 15 e 3600 secondi")
    if kind == "http":
        u = urlparse(target)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("Indirizzo non valido: deve iniziare con http:// o https://")
    else:
        m = re.fullmatch(r"\[?([A-Za-z0-9._:-]+?)\]?:(\d{1,5})", target)
        if not m or not 1 <= int(m.group(2)) <= 65535:
            raise ValueError("Indirizzo non valido: usa il formato host:porta")
    return name, kind, target, int(interval)


def run_check(kind: str, target: str, timeout: float = 5.0) -> tuple[bool, float | None, str | None]:
    """Esegue un controllo. Ritorna (riuscito, millisecondi, errore)."""
    t0 = time.perf_counter()
    try:
        if kind == "http":
            r = httpx.get(target, timeout=timeout, follow_redirects=True)
            ms = (time.perf_counter() - t0) * 1000
            # un 404 o 401 significa che il server risponde: è giù solo se risponde con un errore 5xx
            return (r.status_code < 500, round(ms, 1), None if r.status_code < 500 else f"HTTP {r.status_code}")
        host, port = target.rsplit(":", 1)
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            pass
        return True, round((time.perf_counter() - t0) * 1000, 1), None
    except httpx.TimeoutException:
        return False, None, "timeout"
    except (httpx.HTTPError, OSError) as e:
        return False, None, (str(e) or e.__class__.__name__)[:120]


class Monitor:
    def __init__(self, runner: Callable = run_check, notify: Callable = alerts.notify):
        self.runner, self.notify = runner, notify
        self.last_run: dict[int, float] = {}
        self.state: dict[int, dict] = {}       # id -> {ok, ms, err, ts, fails}
        self.pool = ThreadPoolExecutor(max_workers=4)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    def _loop(self) -> None:
        while not self._stop.wait(2):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        due = [c for c in db.list_checks() if c["enabled"] and now - self.last_run.get(c["id"], 0) >= c["interval"]]
        for c in due:
            self.last_run[c["id"]] = now
        if not due:
            return
        results = list(self.pool.map(lambda c: self.runner(c["kind"], c["target"]), due))
        for c, (ok, ms, err) in zip(due, results):
            self._record(c, ok, ms, err, int(now))

    def run_now(self, check_id: int) -> None:
        """Esegue subito un controllo (dopo averlo creato)."""
        for c in db.list_checks():
            if c["id"] == check_id:
                self.last_run[check_id] = time.time()
                ok, ms, err = self.runner(c["kind"], c["target"])
                self._record(c, ok, ms, err, int(time.time()))

    def forget(self, check_id: int) -> None:
        self.state.pop(check_id, None)
        self.last_run.pop(check_id, None)

    def _record(self, c: dict, ok: bool, ms: float | None, err: str | None, ts: int) -> None:
        db.insert_check_result(c["id"], ts, ok, ms, err)
        st = self.state.get(c["id"], {"fails": 0, "down": False})
        st.update(ok=ok, ms=ms, err=err, ts=ts)
        if ok:
            if st["down"] and alerts.load_settings().get("monitor_alerts", True):
                self.notify("info", "monitor", f"«{c['name']}» è di nuovo raggiungibile", ts)
            st["fails"], st["down"] = 0, False
        else:
            st["fails"] += 1
            if st["fails"] >= FAILS_BEFORE_ALERT and not st["down"]:
                st["down"] = True
                if alerts.load_settings().get("monitor_alerts", True):
                    self.notify("warn", "monitor", f"«{c['name']}» non è raggiungibile ({err or 'errore'})", ts)
        self.state[c["id"]] = st


monitor = Monitor()
