import time

import pytest

from app import alerts, auth, config, db, docker_api, system
from app.collector import Collector


# ---------------------------------------------------------------- database
def _m(**kw):
    base = {k: 1.0 for k in db.METRIC_COLS}
    base.update(kw)
    return base


def test_history_raw_and_bucketing(fresh_db):
    now = 1_800_000_000
    for i in range(60):
        db.insert_raw(now - 600 + i * 10, _m(cpu=float(i)))
    h = db.history(3600, max_points=360, now=now)
    assert h["source"] == "raw"
    assert len(h["series"]["t"]) == 60
    h = db.history(3600, max_points=10, now=now)       # forza il raggruppamento
    assert len(h["series"]["t"]) <= 12 and h["bucket"] >= 300
    assert all(a <= b for a, b in zip(h["series"]["t"], h["series"]["t"][1:]))


def test_history_uses_minute_table_for_long_ranges(fresh_db):
    now = 1_800_000_000
    for i in range(0, 3 * 86400 // 60, 5):
        db.insert_minute(now - i * 60, _m(cpu=50.0, temp=60.0), 80.0, 65.0)
    h = db.history(7 * 86400, max_points=360, now=now)
    assert h["source"] == "1m"
    assert 0 < len(h["series"]["t"]) <= 400
    assert "cpu_max" in h["series"]


def test_null_values_survive(fresh_db):
    db.insert_raw(1_800_000_000, _m(temp=None))
    h = db.history(3600, now=1_800_000_100)
    assert h["series"]["temp"] == [None]


def test_prune_respects_retention(fresh_db):
    now = int(time.time())
    db.insert_raw(now - 7 * 3600, _m())
    db.insert_raw(now - 60, _m())
    db.insert_minute(now - 40 * 86400, _m(), 1, 1)
    db.insert_minute(now - 10 * 86400, _m(), 1, 1)
    db.add_event("info", "x", "vecchio", now - 40 * 86400)
    db.add_event("info", "x", "nuovo", now)
    db.prune(now)
    s = db.stats()
    assert (s["metrics_raw"], s["metrics_1m"], s["events"]) == (1, 1, 1)


def test_events_and_settings(fresh_db):
    db.add_event("warn", "alert", "a")
    db.add_event("info", "alert", "b")
    assert [e["message"] for e in db.events()] == ["b", "a"]
    assert [e["message"] for e in db.events(level="warn")] == ["a"]
    assert db.get_setting("nope", 5) == 5
    db.set_setting("k", {"x": 1})
    assert db.get_setting("k") == {"x": 1}


# ---------------------------------------------------------------- collector
def test_collector_writes_samples_and_minute_rollup(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "SAMPLE_INTERVAL", 10)
    monkeypatch.setattr(config, "DOCKER_ENABLED", False)
    c = Collector()
    base = 1_800_000_020.0     # secondo 20 di un minuto
    snap = lambda cpu, t: {"ts": 0, "cpu": cpu, "cores": [cpu], "mem": 40, "swap": 0, "temp": t, "load1": 1,
                           "disk": 30, "net_rx": 100, "net_tx": 50, "disk_r": 0, "disk_w": 0, "freq": 1500}
    # 5 campioni live ogni 2 s prima della prima scrittura, poi altri nel minuto successivo
    for i in range(0, 200, 2):
        c.step(snap(10 + (i % 4), 50 + i / 10), base + i)
    s = db.stats()
    assert s["metrics_raw"] >= 15            # ~1 ogni 10 s
    assert s["metrics_1m"] >= 2              # i minuti chiusi sono aggregati
    h = db.history(3600, now=int(base + 200))
    assert max(h["series"]["temp"]) > 50 and h["series"]["cpu"][0] is not None


# ---------------------------------------------------------------- allarmi
class Sink:
    def __init__(self): self.events = []
    def __call__(self, level, kind, msg, ts=None): self.events.append((level, msg))


def _engine(**over):
    sink = Sink()
    s = {**alerts.DEFAULTS, **over}
    return alerts.AlertEngine(lambda: s, sink), sink


def test_alert_requires_duration_and_clears():
    e, sink = _engine()
    hot = {"temp": 80, "cpu": 10, "mem": 10, "disk": 10}
    e.feed(hot, {}, now=0)
    assert not e.active                      # troppo presto
    e.feed(hot, {}, now=25)
    assert "temp_warn" in e.active and sink.events[-1][0] == "warn"
    n = len(sink.events)
    e.feed(hot, {}, now=60)                  # nessun duplicato finché resta attivo
    assert len(sink.events) == n
    e.feed({**hot, "temp": 50}, {}, now=70)
    assert not e.active and sink.events[-1][0] == "info"


def test_temp_crit_replaces_warn_and_cpu_needs_hold():
    e, sink = _engine()
    for t in (0, 25):
        e.feed({"temp": 78, "cpu": 95, "mem": 1, "disk": 1}, {}, now=t)
    assert "temp_warn" in e.active and "cpu" not in e.active   # cpu: serve 120 s
    for t in (50, 56):
        e.feed({"temp": 85, "cpu": 95, "mem": 1, "disk": 1}, {}, now=t)
    assert "temp_crit" in e.active and "temp_warn" not in e.active
    e.feed({"temp": 85, "cpu": 95, "mem": 1, "disk": 1}, {}, now=200)
    assert "cpu" in e.active


def test_undervoltage_flag_fires_immediately():
    e, sink = _engine()
    e.feed({"temp": 40, "cpu": 1, "mem": 1, "disk": 1}, {"undervoltage": True}, now=0)
    assert e.active["undervolt"]["level"] == "crit"
    e2, _ = _engine(power_alerts=False)
    e2.feed({"temp": 40, "cpu": 1, "mem": 1, "disk": 1}, {"undervoltage": True}, now=0)
    assert not e2.active


def test_missing_temperature_does_not_crash():
    e, _ = _engine()
    e.feed({"temp": None, "cpu": 1, "mem": 1, "disk": 1}, {}, now=0)
    assert not e.active


def test_settings_validation():
    assert alerts.validate_settings({"temp_warn": "70", "telegram": 0, "junk": 1}) == {"temp_warn": 70, "telegram": False}
    with pytest.raises(ValueError):
        alerts.validate_settings({"cpu_high": 500})


# ---------------------------------------------------------------- auth
def test_token_roundtrip_expiry_and_tamper():
    auth.init("chiave")
    t = auth.make_token(now=1000)
    assert auth.valid_token(t, now=1001)
    assert not auth.valid_token(t, now=1000 + config.SESSION_HOURS * 3600 + 5)
    exp, sig = t.split(".")
    assert not auth.valid_token(f"{int(exp) + 100}.{sig}", now=1001)
    assert not auth.valid_token("spazzatura") and not auth.valid_token(None)
    auth.init("altra-chiave")
    assert not auth.valid_token(t, now=1001)


def test_login_rate_limit():
    ip = "9.9.9.9"
    auth.clear_fails(ip)
    for _ in range(auth.MAX_FAILS):
        auth.record_fail(ip)
    assert auth.too_many_fails(ip)
    assert not auth.too_many_fails(ip, now=time.time() + auth.WINDOW + 1)


# ---------------------------------------------------------------- sistema / docker
def test_throttle_decoding():
    d = system.decode_throttled(0x50005)
    assert d["flags"]["undervoltage"] and d["flags"]["throttled"] and d["flags"]["undervoltage_past"]
    assert not d["flags"]["freq_capped"]
    assert len(d["messages"]) == 4
    assert system.decode_throttled(0)["messages"] == []
    assert system.decode_throttled(None)["available"] is False


def test_rate_counter():
    r = system.RateCounter()
    assert r.rate((100, 200)) == (0.0, 0.0)
    time.sleep(0.05)
    a, b = r.rate((200, 200))
    assert a > 0 and b == 0
    assert r.rate((0, 0))[0] == 0.0          # contatore che si azzera non dà valori negativi


def test_docker_stats_formulas():
    st = {"cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 2000, "online_cpus": 4},
          "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
          "memory_stats": {"usage": 500, "limit": 1000, "stats": {"inactive_file": 100}}}
    assert docker_api.calc_cpu_percent(st) == 40.0
    assert docker_api.calc_mem(st) == (400, 1000)
    assert docker_api.calc_cpu_percent({}) == 0.0 and docker_api.calc_mem({}) == (0, 0)


def test_signal_guards():
    with pytest.raises(PermissionError):
        system.signal_process(1, "KILL")
    with pytest.raises(ValueError):
        import os
        system.signal_process(os.getppid(), "BOGUS")


def test_process_list_and_sort():
    rows = system.list_processes("mem", 10)
    assert rows and rows == sorted(rows, key=lambda r: r["rss"], reverse=True)
    assert system.list_processes("cpu", 5, "zzz-non-esiste-zzz") == []
