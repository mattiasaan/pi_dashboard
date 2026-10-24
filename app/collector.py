"""Thread di raccolta: dati live ogni pochi secondi, campioni nel DB, aggregazione al minuto, allarmi."""
import logging
import threading
import time

from . import alerts, config, db, docker_api, system

log = logging.getLogger("collector")


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


class Collector:
    def __init__(self):
        self.latest: dict = {}
        self.engine = alerts.AlertEngine()
        self.flags: dict = {}
        self.pi: dict = {}
        self._buf: list[dict] = []        # campioni live in attesa di diventare un campione raw
        self._minute: list[dict] = []     # campioni raw del minuto corrente
        self._minute_id: int | None = None
        self._last_raw = 0.0
        self._last_pi = 0.0
        self._last_prune = 0.0
        self._last_containers = 0.0
        self._ctr_state: dict[str, dict] = {}
        self.expected_stops: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- avvio / arresto -------------------------------------------------
    def start(self) -> None:
        system.snapshot()  # innesca i contatori (cpu_percent, rete)
        self._stop.clear()
        self._last_raw = 0.0
        self._thread = threading.Thread(target=self._run, name="collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._flush_minute()

    def _run(self) -> None:
        while not self._stop.wait(config.LIVE_INTERVAL):
            try:
                self.step()
            except Exception:  # il collector non deve mai morire
                log.exception("errore nel ciclo di raccolta")

    # -- un ciclo ----------------------------------------------------------
    def step(self, snap: dict | None = None, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        snap = snap or system.snapshot()
        self.latest = snap
        self._buf.append(snap)

        if now - self._last_pi >= 30:
            self.pi = system.pi_status()
            self.flags = self.pi.get("flags", {})
            self._last_pi = now
        self.engine.feed(snap, self.flags, now)

        if now - self._last_raw >= config.SAMPLE_INTERVAL:
            self._write_sample(now)
            self._last_raw = now

        if now - self._last_prune >= 3600:
            db.prune(int(now))
            self._last_prune = now

        if now - self._last_containers >= 15:
            self._last_containers = now
            self._watch_containers()

    def _write_sample(self, now: float) -> None:
        buf, self._buf = self._buf, []
        if not buf:
            return
        sample = {k: _avg(buf, k) for k in db.METRIC_COLS}
        sample["_cpu_max"] = max((r["cpu"] for r in buf), default=None)
        sample["_temp_max"] = max((r["temp"] for r in buf if r.get("temp") is not None), default=None)
        ts = int(now)
        db.insert_raw(ts, sample)
        minute_id = ts // 60
        if self._minute_id is not None and minute_id != self._minute_id:
            self._flush_minute()
        self._minute_id = minute_id
        self._minute.append(sample)

    def _flush_minute(self) -> None:
        if not self._minute or self._minute_id is None:
            return
        rows = self._minute
        agg = {k: _avg(rows, k) for k in db.METRIC_COLS}
        db.insert_minute(self._minute_id * 60, agg,
                         max((r["_cpu_max"] for r in rows if r["_cpu_max"] is not None), default=None),
                         max((r["_temp_max"] for r in rows if r["_temp_max"] is not None), default=None))
        self._minute = []

    # -- container -----------------------------------------------------------
    def _watch_containers(self) -> None:
        if not config.DOCKER_ENABLED:
            return
        try:
            current = {c["name"]: c for c in docker_api.list_containers(with_stats=False)}
        except Exception:
            return
        notify_on = alerts.load_settings().get("container_alerts", True)
        if self._ctr_state and notify_on:
            for name, c in current.items():
                old = self._ctr_state.get(name)
                if old and old["status"] == "running" and c["status"] != "running":
                    if name in self.expected_stops:
                        self.expected_stops.discard(name)
                    elif c["is_self"]:
                        pass
                    elif c["exit_code"]:
                        alerts.notify("crit", "container", f"Il container «{name}» si è fermato (codice {c['exit_code']})")
                    else:
                        alerts.notify("warn", "container", f"Il container «{name}» non è più in esecuzione")
                if old and old.get("health") != "unhealthy" and c.get("health") == "unhealthy":
                    alerts.notify("warn", "container", f"Il container «{name}» risulta non sano (unhealthy)")
        self._ctr_state = current


collector = Collector()
