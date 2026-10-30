"""Monitor di raggiungibilità (stile "uptime robot"): controlli HTTP e TCP con storico, uptime, incidenti e avvisi."""
import json
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, NamedTuple
from urllib.parse import urlparse

import httpx

from . import alerts, db

KINDS = ("http", "tcp")
MAX_CHECKS = 50
MIN_INTERVAL, MAX_INTERVAL = 15, 3600
RETRY_DELAY = 30            # dopo un errore si ricontrolla prima, senza aspettare l'intervallo intero
CERT_RECHECK = 3600         # il certificato si legge al massimo una volta all'ora
MAX_BODY = 1_000_000        # byte di risposta letti per parola chiave e JSON

# Valori che un endpoint di salute usa di solito per dire "tutto bene"
HEALTHY = {"ok", "up", "healthy", "pass", "passing", "true", "alive", "ready", "running", "green", "online"}
LEGACY_EXPECT = "200-499"   # controlli creati prima delle opzioni: giù solo con un errore 5xx
DEFAULTS = {"method": "GET", "expect_status": "200-399", "keyword": "", "json_path": "", "json_equals": "",
            "headers": {}, "timeout": 10, "verify_tls": True, "ssl_warn_days": 14, "retries": 2, "public": False}


class Result(NamedTuple):
    ok: bool
    ms: float | None
    err: str | None
    cert_days: int | None = None


# ------------------------------------------------------------------ validazione
def parse_status_spec(spec: str) -> list[tuple[int, int]]:
    """'200-399,404' -> [(200, 399), (404, 404)]"""
    out = []
    if len(spec or "") > 100:
        raise ValueError("Codici attesi troppo lunghi")
    for part in re.split(r"[,\s]+", (spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{3})(?:-(\d{3}))?", part)
        if not m:
            raise ValueError("Codici attesi non validi: usa per esempio 200-399 oppure 200,204")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (100 <= lo <= hi <= 599):
            raise ValueError("Codici attesi non validi: devono essere tra 100 e 599")
        out.append((lo, hi))
    if not out:
        raise ValueError("Indica almeno un codice atteso (per esempio 200-399)")
    return out


def validate_check(name: str, kind: str, target: str, interval: int) -> tuple[str, str, str, int]:
    name, target = (name or "").strip(), (target or "").strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("Il nome deve avere da 1 a 40 caratteri")
    if kind not in KINDS:
        raise ValueError("Tipo non valido")
    if not MIN_INTERVAL <= int(interval) <= MAX_INTERVAL:
        raise ValueError(f"L'intervallo deve essere tra {MIN_INTERVAL} e {MAX_INTERVAL} secondi")
    if kind == "http":
        u = urlparse(target)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("Indirizzo non valido: deve iniziare con http:// o https://")
    else:
        # Niente espressioni regolari sul testo scritto dall'utente: si taglia con rpartition e si controlla ogni parte.
        host, _, port = target[:300].rpartition(":")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        valid_host = 0 < len(host) <= 253 and all(c.isascii() and (c.isalnum() or c in "._:-") for c in host)
        if len(target) > 300 or not valid_host or not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
            raise ValueError("Indirizzo non valido: usa il formato host:porta")
    return name, kind, target, int(interval)


def _int_in(v, lo: int, hi: int, label: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: numero non valido") from None
    if not lo <= n <= hi:
        raise ValueError(f"{label} deve essere tra {lo} e {hi}")
    return n


def validate_options(kind: str, opts: dict | None) -> dict:
    """Ripulisce e controlla le opzioni. Ritorna solo quelle valide, con i valori predefiniti completati."""
    o = {**DEFAULTS, **{k: v for k, v in (opts or {}).items() if k in DEFAULTS}}
    out: dict = {"retries": _int_in(o["retries"], 1, 5, "Tentativi prima dell'avviso"),
                 "timeout": _int_in(o["timeout"], 1, 30, "Timeout"), "public": bool(o["public"])}
    if kind == "tcp":
        return out
    method = str(o["method"]).upper()
    if method not in ("GET", "HEAD"):
        raise ValueError("Metodo non valido: usa GET o HEAD")
    parse_status_spec(str(o["expect_status"]))
    keyword, jpath, jeq = str(o["keyword"]), str(o["json_path"]).strip(), str(o["json_equals"]).strip()
    if len(keyword) > 100 or len(jpath) > 60 or len(jeq) > 40:
        raise ValueError("Parola chiave, percorso o valore JSON troppo lunghi")
    if jpath and not re.fullmatch(r"[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*", jpath):
        raise ValueError("Percorso JSON non valido: usa per esempio status oppure data.db.status")
    if method == "HEAD" and (keyword or jpath):
        raise ValueError("Con HEAD non c'è un corpo da controllare: usa GET")
    headers = o["headers"] or {}
    if not isinstance(headers, dict) or len(headers) > 5:
        raise ValueError("Al massimo 5 intestazioni")
    clean = {}
    for k, v in headers.items():
        k, v = str(k).strip(), str(v).strip()
        if not k and not v:
            continue
        if not re.fullmatch(r"[A-Za-z0-9\-]{1,40}", k) or len(v) > 300 or re.search(r"[\r\n]", v):
            raise ValueError("Intestazione non valida")
        clean[k] = v
    out.update(method=method, expect_status=str(o["expect_status"]).strip(), keyword=keyword,
               json_path=jpath, json_equals=jeq, headers=clean, verify_tls=bool(o["verify_tls"]),
               ssl_warn_days=_int_in(o["ssl_warn_days"], 0, 90, "Avviso certificato"))
    return out


# ------------------------------------------------------------------ esecuzione
def _json_get(data, path: str):
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            raise KeyError(part)
    return data


def cert_days_left(host: str, port: int, timeout: float = 5.0) -> int | None:
    """Giorni mancanti alla scadenza del certificato (None se non leggibile)."""
    try:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2          # niente TLS 1.0 e 1.1
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
        return int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def _read_body(r: httpx.Response) -> bytes:
    buf = bytearray()
    for chunk in r.iter_bytes():
        buf += chunk
        if len(buf) >= MAX_BODY:
            break
    return bytes(buf)


def _http(target: str, opts: dict, timeout: float, check_cert: bool) -> Result:
    spec = parse_status_spec(opts.get("expect_status") or LEGACY_EXPECT)
    keyword, jpath = opts.get("keyword", ""), opts.get("json_path", "")
    method = "GET" if (keyword or jpath) else opts.get("method", "GET")
    headers = {"User-Agent": "pi-dashboard-monitor/2", **opts.get("headers", {})}
    t0 = time.perf_counter()
    with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=5,
                      verify=opts.get("verify_tls", True)) as cl:
        with cl.stream(method, target, headers=headers) as r:
            body = _read_body(r) if (keyword or jpath) else b""
            ms = round((time.perf_counter() - t0) * 1000, 1)
            code = r.status_code
    err = None
    if not any(lo <= code <= hi for lo, hi in spec):
        err = f"HTTP {code} (atteso {opts.get('expect_status') or LEGACY_EXPECT})"
    elif keyword and keyword not in body.decode("utf-8", "replace"):
        err = f"Parola «{keyword}» non trovata nella risposta"
    elif jpath:
        try:
            val = _json_get(json.loads(body.decode("utf-8", "replace")), jpath)
        except ValueError:
            err = "La risposta non è JSON"
        except KeyError:
            err = f"Campo JSON «{jpath}» non trovato"
        else:
            want = opts.get("json_equals", "")
            shown = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
            good = str(val).strip().lower() == want.lower() if want else str(val).strip().lower() in HEALTHY
            if isinstance(val, bool) and not want:
                good = val
            if not good:
                err = f"{jpath} = {shown[:40]}" + (f" (atteso {want})" if want else "")
    days = None
    u = urlparse(target)
    if check_cert and u.scheme == "https" and opts.get("verify_tls", True) and opts.get("ssl_warn_days", 0):
        days = cert_days_left(u.hostname, u.port or 443, min(timeout, 5))
    return Result(err is None, ms, err, days)


def run_check(kind: str, target: str, timeout: float | None = None, opts: dict | None = None,
              check_cert: bool = False) -> Result:
    """Esegue un controllo. Ritorna (riuscito, millisecondi, errore, giorni al certificato)."""
    opts = opts or {}
    timeout = float(timeout if timeout is not None else opts.get("timeout", 5))
    t0 = time.perf_counter()
    try:
        if kind == "http":
            return _http(target, opts, timeout, check_cert)
        host, port = target.rsplit(":", 1)
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            pass
        return Result(True, round((time.perf_counter() - t0) * 1000, 1), None)
    except httpx.TimeoutException:
        return Result(False, None, "timeout")
    except (httpx.HTTPError, OSError) as e:
        return Result(False, None, (str(e) or e.__class__.__name__)[:120])


def fmt_duration(sec: int) -> str:
    if sec < 120:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    if sec < 172800:
        return f"{sec / 3600:.1f} ore".replace(".0", "")
    return f"{round(sec / 86400)} giorni"


class Monitor:
    def __init__(self, runner: Callable = run_check, notify: Callable = alerts.notify):
        self.runner, self.notify = runner, notify
        self.last_run: dict[int, float] = {}
        self.last_cert: dict[int, float] = {}
        self.state: dict[int, dict] = {}       # id -> {ok, ms, err, ts, fails, down, cert_days}
        self.pool = ThreadPoolExecutor(max_workers=8)
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

    def _execute(self, c: dict, now: float):
        want_cert = now - self.last_cert.get(c["id"], 0) >= CERT_RECHECK
        if want_cert:
            self.last_cert[c["id"]] = now
        return self.runner(c["kind"], c["target"], opts=c.get("opts") or {}, check_cert=want_cert)

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        due = [c for c in db.list_checks() if c["enabled"] and now - self.last_run.get(c["id"], 0) >= c["interval"]]
        for c in due:
            self.last_run[c["id"]] = now
        if not due:
            return
        results = list(self.pool.map(lambda c: self._execute(c, now), due))
        for c, res in zip(due, results):
            self._record(c, res, int(now))

    def run_now(self, check_id: int) -> None:
        """Esegue subito un controllo (dopo averlo creato o modificato)."""
        for c in db.list_checks():
            if c["id"] == check_id:
                now = time.time()
                self.last_run[check_id] = now
                self.last_cert.pop(check_id, None)
                self._record(c, self._execute(c, now), int(now))

    def forget(self, check_id: int) -> None:
        for d in (self.state, self.last_run, self.last_cert):
            d.pop(check_id, None)

    def _alerts_on(self) -> bool:
        return alerts.load_settings().get("monitor_alerts", True)

    def _record(self, c: dict, res, ts: int) -> None:
        ok, ms, err = res[0], res[1], res[2]
        cert = res[3] if len(res) > 3 else None
        cid, opts = c["id"], c.get("opts") or {}
        retries = int(opts.get("retries", 2))
        db.insert_check_result(cid, ts, ok, ms, err)
        st = self.state.get(cid)
        if st is None:
            st = {"fails": 0, "down": db.open_incident_id(cid) is not None, "first_fail": None, "cert_warned": False}
        st.update(ok=ok, ms=ms, err=err, ts=ts)
        if cert is not None:
            st["cert_days"] = cert
        if ok:
            if st["down"]:
                dur = db.close_incident(cid, ts)
                if self._alerts_on():
                    tail = f" dopo {fmt_duration(dur)}" if dur else ""
                    self.notify("info", "monitor", f"«{c['name']}» è di nuovo raggiungibile{tail}", ts)
            st.update(fails=0, down=False, first_fail=None)
        else:
            st["fails"] += 1
            st["first_fail"] = st["first_fail"] or ts
            if st["fails"] >= retries and not st["down"]:
                st["down"] = True
                db.open_incident(cid, st["first_fail"], err)
                if self._alerts_on():
                    self.notify("warn", "monitor", f"«{c['name']}» non è raggiungibile ({err or 'errore'})", ts)
            # dopo un errore si ricontrolla presto, per confermarlo (o smentirlo) senza aspettare l'intervallo intero
            if not st["down"]:
                self.last_run[cid] = ts - c["interval"] + min(c["interval"], RETRY_DELAY)
        warn_days = int(opts.get("ssl_warn_days", 0) or 0)
        days = st.get("cert_days")
        if warn_days and days is not None:
            if days <= warn_days and not st["cert_warned"]:
                st["cert_warned"] = True
                what = "è scaduto" if days < 0 else f"scade tra {days} giorni"
                if self._alerts_on():
                    self.notify("warn", "monitor", f"Il certificato di «{c['name']}» {what}", ts)
            elif days > warn_days:
                st["cert_warned"] = False
        self.state[cid] = st


monitor = Monitor()
