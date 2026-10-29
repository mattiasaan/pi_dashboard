"""SQLite: campioni grezzi (ogni ~10 s, pochi ore), aggregati al minuto (30 giorni), eventi, impostazioni."""
import json
import sqlite3
import threading
import time
from typing import Any

from . import config

METRIC_COLS = [
    "cpu", "mem", "swap", "temp", "load1", "disk",
    "net_rx", "net_tx", "disk_r", "disk_w", "freq",
]

CHECK_RETENTION_DAYS = 7
INCIDENT_RETENTION_DAYS = 90

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _create(conn: sqlite3.Connection) -> None:
    cols = ", ".join(f"{c} REAL" for c in METRIC_COLS)
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS metrics_raw (ts INTEGER PRIMARY KEY, {cols});
        CREATE TABLE IF NOT EXISTS metrics_1m  (ts INTEGER PRIMARY KEY, {cols}, cpu_max REAL, temp_max REAL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
            target TEXT NOT NULL, interval INTEGER NOT NULL DEFAULT 60, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS check_results (
            check_id INTEGER NOT NULL, ts INTEGER NOT NULL, ok INTEGER NOT NULL, ms REAL, err TEXT,
            PRIMARY KEY (check_id, ts));
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, check_id INTEGER NOT NULL,
            start INTEGER NOT NULL, end INTEGER, err TEXT);
        CREATE INDEX IF NOT EXISTS idx_incidents_check ON incidents(check_id, start);
    """)
    # migrazione: opzioni dei controlli (metodo, parola chiave, JSON, timeout, ...)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(checks)")}
    if "opts" not in cols:
        conn.execute("ALTER TABLE checks ADD COLUMN opts TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def init(path=None) -> None:
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _create(_conn)
    _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn  # type: ignore[return-value]


def insert_raw(ts: int, m: dict[str, float | None]) -> None:
    with _lock:
        c = _c()
        c.execute(
            f"INSERT OR REPLACE INTO metrics_raw (ts, {','.join(METRIC_COLS)}) VALUES (?{',?' * len(METRIC_COLS)})",
            [ts] + [m.get(k) for k in METRIC_COLS],
        )
        c.commit()


def insert_minute(ts: int, m: dict[str, float | None], cpu_max: float | None, temp_max: float | None) -> None:
    with _lock:
        c = _c()
        c.execute(
            f"INSERT OR REPLACE INTO metrics_1m (ts, {','.join(METRIC_COLS)}, cpu_max, temp_max) "
            f"VALUES (?{',?' * (len(METRIC_COLS) + 2)})",
            [ts] + [m.get(k) for k in METRIC_COLS] + [cpu_max, temp_max],
        )
        c.commit()


def prune(now: int | None = None) -> None:
    now = now or int(time.time())
    with _lock:
        c = _c()
        c.execute("DELETE FROM metrics_raw WHERE ts < ?", (now - config.RAW_RETENTION_HOURS * 3600,))
        c.execute("DELETE FROM metrics_1m WHERE ts < ?", (now - config.RETENTION_DAYS * 86400,))
        c.execute("DELETE FROM events WHERE ts < ?", (now - config.RETENTION_DAYS * 86400,))
        c.execute("DELETE FROM check_results WHERE ts < ?", (now - CHECK_RETENTION_DAYS * 86400,))
        c.execute("DELETE FROM incidents WHERE end IS NOT NULL AND end < ?", (now - INCIDENT_RETENTION_DAYS * 86400,))
        c.commit()


def history(seconds: int, max_points: int = 360, now: int | None = None) -> dict[str, Any]:
    """Serie storica con al massimo ~max_points punti (media per bucket)."""
    now = now or int(time.time())
    since = now - seconds
    # fino a 6 ore: campioni grezzi; oltre: aggregati al minuto
    use_raw = seconds <= config.RAW_RETENTION_HOURS * 3600
    table = "metrics_raw" if use_raw else "metrics_1m"
    base = int(config.SAMPLE_INTERVAL) if use_raw else 60
    bucket = max(base, int(seconds / max_points))
    bucket = max(bucket // base, 1) * base
    avg = ", ".join(f"AVG({c}) AS {c}" for c in METRIC_COLS)
    extra = "" if use_raw else ", MAX(cpu_max) AS cpu_max, MAX(temp_max) AS temp_max"
    sql = (
        f"SELECT (ts / {bucket}) * {bucket} AS t, {avg}{extra} FROM {table} "
        f"WHERE ts >= ? GROUP BY t ORDER BY t"
    )
    with _lock:
        rows = _c().execute(sql, (since,)).fetchall()
    series: dict[str, list] = {"t": []}
    for c in METRIC_COLS + ([] if use_raw else ["cpu_max", "temp_max"]):
        series[c] = []
    for r in rows:
        series["t"].append(r["t"])
        for c in series:
            if c == "t":
                continue
            v = r[c]
            series[c].append(None if v is None else round(v, 2))
    return {"bucket": bucket, "source": "raw" if use_raw else "1m", "series": series}


def add_event(level: str, kind: str, message: str, ts: int | None = None) -> None:
    with _lock:
        c = _c()
        c.execute("INSERT INTO events (ts, level, kind, message) VALUES (?,?,?,?)",
                  (ts or int(time.time()), level, kind, message))
        c.commit()


def events(limit: int = 200, level: str | None = None, kind: str | None = None) -> list[dict]:
    q = "SELECT id, ts, level, kind, message FROM events"
    args: list = []
    where = []
    if level:
        where.append("level = ?")
        args.append(level)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        return [dict(r) for r in _c().execute(q, args).fetchall()]


def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        r = _c().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    with _lock:
        c = _c()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, json.dumps(value)))
        c.commit()


def stats() -> dict[str, int]:
    with _lock:
        c = _c()
        out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("metrics_raw", "metrics_1m", "events")}
    try:
        out["db_bytes"] = config.DB_PATH.stat().st_size
    except OSError:
        out["db_bytes"] = 0
    return out


# ---------------------------------------------------------------- controlli di raggiungibilità
def _check_row(r) -> dict:
    d = dict(r)
    try:
        d["opts"] = json.loads(d.get("opts") or "{}")
    except ValueError:
        d["opts"] = {}
    return d


def add_check(name: str, kind: str, target: str, interval: int, opts: dict | None = None) -> int:
    with _lock:
        c = _c()
        cur = c.execute("INSERT INTO checks (name, kind, target, interval, opts) VALUES (?,?,?,?,?)",
                        (name, kind, target, interval, json.dumps(opts or {})))
        c.commit()
        return int(cur.lastrowid)


def update_check(check_id: int, name: str, kind: str, target: str, interval: int, opts: dict) -> bool:
    with _lock:
        c = _c()
        cur = c.execute("UPDATE checks SET name=?, kind=?, target=?, interval=?, opts=? WHERE id=?",
                        (name, kind, target, interval, json.dumps(opts), check_id))
        c.commit()
        return cur.rowcount > 0


def list_checks() -> list[dict]:
    with _lock:
        return [_check_row(r) for r in _c().execute("SELECT * FROM checks ORDER BY id").fetchall()]


def open_incident(check_id: int, start: int, err: str | None) -> int:
    with _lock:
        c = _c()
        cur = c.execute("INSERT INTO incidents (check_id, start, err) VALUES (?,?,?)", (check_id, start, err))
        c.commit()
        return int(cur.lastrowid)


def open_incident_id(check_id: int) -> int | None:
    with _lock:
        r = _c().execute("SELECT id FROM incidents WHERE check_id=? AND end IS NULL ORDER BY id DESC LIMIT 1",
                         (check_id,)).fetchone()
    return r["id"] if r else None


def close_incident(check_id: int, end: int) -> int | None:
    """Chiude l'incidente aperto; ritorna la durata in secondi."""
    with _lock:
        c = _c()
        r = c.execute("SELECT id, start FROM incidents WHERE check_id=? AND end IS NULL ORDER BY id DESC LIMIT 1",
                      (check_id,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE incidents SET end=? WHERE check_id=? AND end IS NULL", (end, check_id))
        c.commit()
        return max(0, end - r["start"])


def incidents(check_id: int, limit: int = 20) -> list[dict]:
    with _lock:
        rows = _c().execute("SELECT start, end, err FROM incidents WHERE check_id=? ORDER BY start DESC LIMIT ?",
                            (check_id, limit)).fetchall()
    return [dict(r) for r in rows]


def check_latency(check_id: int, since: int) -> list[dict]:
    with _lock:
        rows = _c().execute("SELECT ts, ok, ms FROM check_results WHERE check_id=? AND ts>=? ORDER BY ts",
                            (check_id, since)).fetchall()
    return [dict(r) for r in rows]


def check_stats(check_id: int, since: int) -> dict:
    with _lock:
        r = _c().execute("SELECT AVG(ms) AS avg, MIN(ms) AS mn, MAX(ms) AS mx FROM check_results "
                         "WHERE check_id=? AND ts>=? AND ok=1 AND ms IS NOT NULL", (check_id, since)).fetchone()
    return {"avg": None if r["avg"] is None else round(r["avg"], 1),
            "min": r["mn"], "max": r["mx"]}


def delete_check(check_id: int) -> bool:
    with _lock:
        c = _c()
        cur = c.execute("DELETE FROM checks WHERE id=?", (check_id,))
        c.execute("DELETE FROM check_results WHERE check_id=?", (check_id,))
        c.execute("DELETE FROM incidents WHERE check_id=?", (check_id,))
        c.commit()
        return cur.rowcount > 0


def set_check_enabled(check_id: int, enabled: bool) -> bool:
    with _lock:
        c = _c()
        cur = c.execute("UPDATE checks SET enabled=? WHERE id=?", (1 if enabled else 0, check_id))
        c.commit()
        return cur.rowcount > 0


def insert_check_result(check_id: int, ts: int, ok: bool, ms: float | None, err: str | None) -> None:
    with _lock:
        c = _c()
        c.execute("INSERT OR REPLACE INTO check_results (check_id, ts, ok, ms, err) VALUES (?,?,?,?,?)",
                  (check_id, ts, 1 if ok else 0, ms, err))
        c.commit()


def check_results(check_id: int, limit: int = 60) -> list[dict]:
    with _lock:
        rows = _c().execute("SELECT ts, ok, ms, err FROM check_results WHERE check_id=? ORDER BY ts DESC LIMIT ?",
                            (check_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def check_uptime(check_id: int, since: int) -> float | None:
    """Percentuale di controlli riusciti da `since`; None se non ci sono dati."""
    with _lock:
        r = _c().execute("SELECT COUNT(*) AS n, SUM(ok) AS good FROM check_results WHERE check_id=? AND ts>=?",
                         (check_id, since)).fetchone()
    return None if not r["n"] else round(100.0 * (r["good"] or 0) / r["n"], 2)


# ---------------------------------------------------------------- stime e backup
def disk_forecast(now: int | None = None, days: int = 14) -> dict | None:
    """Regressione lineare sull'uso del disco; None se i dati coprono meno di 2 giorni."""
    now = now or int(time.time())
    with _lock:
        rows = _c().execute("SELECT ts, disk FROM metrics_1m WHERE ts >= ? AND disk IS NOT NULL ORDER BY ts",
                            (now - days * 86400,)).fetchall()
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 2 * 86400:
        return None
    xs = [(r["ts"] - rows[0]["ts"]) / 86400 for r in rows]
    ys = [r["disk"] for r in rows]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    current = sum(ys[-30:]) / len(ys[-30:])
    out = {"current": round(current, 1), "slope_per_day": round(slope, 3), "days_to_full": None}
    if slope > 0.01:
        out["days_to_full"] = max(0, round((100 - current) / slope))
    return out


def backup_to(path) -> None:
    """Copia consistente del database (anche mentre è in uso)."""
    with _lock:
        dest = sqlite3.connect(str(path))
        try:
            _c().backup(dest)
        finally:
            dest.close()
