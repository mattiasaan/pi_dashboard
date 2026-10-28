"""Motore di allarmi con soglie, durata minima e isteresi (avvisa all'insorgere e al rientro)."""
import threading
import time
from typing import Callable

import httpx

from . import config, db

DEFAULTS = {
    "temp_warn": 75, "temp_crit": 82,
    "cpu_high": 90, "cpu_secs": 120,
    "mem_high": 90, "disk_high": 90,
    "power_alerts": True, "container_alerts": True, "monitor_alerts": True,
    "telegram": True,
}
LIMITS = {
    "temp_warn": (40, 100), "temp_crit": (40, 110), "cpu_high": (10, 100), "cpu_secs": (10, 3600),
    "mem_high": (10, 100), "disk_high": (10, 100),
}


def load_settings() -> dict:
    saved = db.get_setting("alerts", {}) or {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def validate_settings(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if k not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[k], bool):
            out[k] = bool(v)
        else:
            lo, hi = LIMITS[k]
            n = float(v)
            if not lo <= n <= hi:
                raise ValueError(f"{k} deve essere tra {lo} e {hi}")
            out[k] = int(n) if n == int(n) else n
    return out


def telegram_enabled() -> bool:
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT)


def send_telegram(text: str) -> None:
    def _go():
        try:
            httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
                       json={"chat_id": config.TELEGRAM_CHAT, "text": text}, timeout=8)
        except httpx.HTTPError:
            pass
    threading.Thread(target=_go, daemon=True).start()


def notify(level: str, kind: str, message: str, ts: int | None = None) -> None:
    db.add_event(level, kind, message, ts)
    if level in ("warn", "crit") and telegram_enabled() and load_settings().get("telegram"):
        icon = "🔴" if level == "crit" else "🟠"
        send_telegram(f"{icon} Raspberry: {message}")


class AlertEngine:
    """Ogni regola scatta quando la condizione dura da `hold` secondi e rientra quando scompare."""

    def __init__(self, settings_fn: Callable[[], dict] = load_settings,
                 notify_fn: Callable[..., None] = notify):
        self.settings_fn = settings_fn
        self.notify_fn = notify_fn
        self.since: dict[str, float] = {}
        self.active: dict[str, dict] = {}

    def _rules(self, snap: dict, flags: dict, s: dict) -> list[tuple[str, str, str, bool, float]]:
        temp = snap.get("temp")
        rules = [
            ("temp_crit", "crit", f"Temperatura critica: {temp}°C", temp is not None and temp >= s["temp_crit"], 5),
            ("temp_warn", "warn", f"Temperatura alta: {temp}°C",
             temp is not None and temp >= s["temp_warn"] and temp < s["temp_crit"], 20),
            ("cpu", "warn", f"CPU oltre {s['cpu_high']}% da almeno {s['cpu_secs']} s (ora {snap.get('cpu')}%)",
             snap.get("cpu", 0) >= s["cpu_high"], s["cpu_secs"]),
            ("mem", "warn", f"Memoria oltre {s['mem_high']}% (ora {snap.get('mem')}%)",
             snap.get("mem", 0) >= s["mem_high"], 30),
            ("disk", "warn", f"Disco oltre {s['disk_high']}% (ora {snap.get('disk')}%)",
             snap.get("disk", 0) >= s["disk_high"], 60),
        ]
        if s.get("power_alerts"):
            rules.append(("undervolt", "crit", "Sotto-tensione: alimentatore o cavo insufficiente",
                          bool(flags.get("undervoltage")), 0))
            rules.append(("throttle", "warn", "Throttling della CPU attivo",
                          bool(flags.get("throttled")) or bool(flags.get("freq_capped")), 5))
        return rules

    def feed(self, snap: dict, flags: dict | None = None, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        s = self.settings_fn()
        for key, level, msg, cond, hold in self._rules(snap, flags or {}, s):
            if cond:
                first = self.since.setdefault(key, now)
                if now - first >= hold and key not in self.active:
                    self.active[key] = {"level": level, "message": msg, "since": int(now)}
                    self.notify_fn(level, "alert", msg, int(now))
            else:
                self.since.pop(key, None)
                if key in self.active:
                    old = self.active.pop(key)
                    self.notify_fn("info", "alert", f"Rientrato: {old['message'].split(':')[0].split(' oltre')[0]}", int(now))
        # temp_warn e temp_crit sono mutuamente esclusivi: se sale a crit, chiudi warn in silenzio
        if "temp_crit" in self.active:
            self.active.pop("temp_warn", None)

    def active_list(self) -> list[dict]:
        return [{"key": k, **v} for k, v in self.active.items()]
