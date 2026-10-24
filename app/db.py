"""SQLite: campioni grezzi (ogni ~10 s, pochi ore), aggregati al minuto (30 giorni), eventi, impostazioni."""
import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from . import config

METRIC_COLS = [
    "cpu", "mem", "swap", "temp", "load1", "disk",
    "net_rx", "net_tx", "disk_r", "disk_w", "freq",
]

_lock = threading.Lock()
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
    """)


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


def events(limit: int = 200, level: str | None = None) -> list[dict]:
    q = "SELECT id, ts, level, kind, message FROM events"
    args: list = []
    if level:
        q += " WHERE level = ?"
        args.append(level)
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
