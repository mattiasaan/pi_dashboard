import http.server
import os
import re
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from app import collector as collector_mod
from app import db, monitor as mon
from app.collector import Collector
from tests import fake_docker
from tests.test_api import authed, client  # noqa: F401  (fixture)


# ---------------------------------------------------------------- riavvio automatico
@pytest.fixture()
def ctr(fresh_db, monkeypatch):
    fake_docker.install(monkeypatch)
    events = []
    monkeypatch.setattr(collector_mod.alerts, "notify", lambda lvl, kind, msg, ts=None: events.append((lvl, msg)))
    c = Collector()
    c._watch_containers(now=1000)           # fotografia iniziale
    return c, events


def _crash(name, code=1):
    k = next(x for x in fake_docker.STATE["containers"] if x["name"] == name)
    k["status"], k["exit_code"] = "exited", code


def test_stop_without_autorestart_only_notifies(ctr):
    c, events = ctr
    _crash("pihole", 1)
    c._watch_containers(now=1010)
    assert events and events[-1][0] == "crit" and "pihole" in events[-1][1]
    assert ("pihole", "start") not in fake_docker.STATE["calls"]


def test_autorestart_restarts_then_gives_up_and_recovers(ctr):
    c, events = ctr
    db.set_setting("autorestart", {"pihole": {"enabled": True}})
    t = 1010
    for i in range(3):                       # tre crash entro la finestra: tre riavvii
        _crash("pihole", 137)
        c._watch_containers(now=t)
        t += 20
        assert fake_docker.STATE["calls"].count(("pihole", "start")) == i + 1
        assert "riavviato automaticamente" in events[-1][1]
        c._watch_containers(now=t)           # ora risulta di nuovo in esecuzione
        t += 20
    _crash("pihole", 137)                    # il quarto: si arrende, una sola volta
    c._watch_containers(now=t)
    assert fake_docker.STATE["calls"].count(("pihole", "start")) == 3
    assert events[-1][0] == "crit" and "sospeso" in events[-1][1]
    n = len(events)
    c._watch_containers(now=t + 10)
    assert len(events) == n
    # dopo un periodo stabile si ricomincia
    fake_docker.STATE["containers"][0]["status"] = "running"
    c._watch_containers(now=t + 20)
    c._watch_containers(now=t + 20 + collector_mod.AUTORESTART_WINDOW + 5)
    assert "pihole" not in c._gave_up


def test_dashboard_initiated_stop_is_not_restarted(ctr):
    c, events = ctr
    db.set_setting("autorestart", {"pihole": {"enabled": True}})
    c.expected_stops.add("pihole")
    _crash("pihole", 0)
    c._watch_containers(now=1010)
    assert ("pihole", "start") not in fake_docker.STATE["calls"] and not events


def test_oom_is_reported(ctr):
    c, events = ctr
    k = next(x for x in fake_docker.STATE["containers"] if x["name"] == "pihole")
    k["oom_killed"] = True
    _crash("pihole", 137)
    c._watch_containers(now=1010)
    assert "memoria esaurita" in events[-1][1]


def test_self_is_never_restarted(ctr):
    c, events = ctr
    db.set_setting("autorestart", {"pi-dashboard": {"enabled": True}})
    _crash("pi-dashboard", 1)
    c._watch_containers(now=1010)
    assert not events and ("pi-dashboard", "start") not in fake_docker.STATE["calls"]


# ---------------------------------------------------------------- monitor
@pytest.fixture()
def tcp_server():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(5)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture()
def http_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = {"/ok": 200, "/missing": 404, "/boom": 500}.get(self.path, 200)
            self.send_response(code); self.end_headers(); self.wfile.write(b"x")
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_run_check_tcp_and_http(tcp_server, http_server):
    ok, ms, err = mon.run_check("tcp", f"127.0.0.1:{tcp_server}")[:3]
    assert ok and ms is not None and err is None
    s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
    ok, ms, err = mon.run_check("tcp", f"127.0.0.1:{closed}", timeout=1)[:3]
    assert not ok and ms is None and err
    base = f"http://127.0.0.1:{http_server}"
    assert mon.run_check("http", base + "/ok")[0]
    assert mon.run_check("http", base + "/missing")[0]            # il server risponde: raggiungibile
    ok, _, err = mon.run_check("http", base + "/boom")[:3]
    assert not ok and "500" in err


def test_validate_check():
    assert mon.validate_check(" Sito ", "http", "https://example.com/x", 60) == ("Sito", "http", "https://example.com/x", 60)
    assert mon.validate_check("R", "tcp", "192.168.1.1:443", 30)[2] == "192.168.1.1:443"
    for bad in [("", "tcp", "a:1", 60), ("x", "ftp", "a:1", 60), ("x", "tcp", "a:1", 5), ("x", "tcp", "a", 60),
                ("x", "tcp", "a:99999", 60), ("x", "http", "javascript:alert(1)", 60), ("x", "http", "example.com", 60),
                ("y" * 41, "tcp", "a:1", 60)]:
        with pytest.raises(ValueError):
            mon.validate_check(*bad)


def test_monitor_alerts_after_two_failures_and_recovery(fresh_db):
    sink = []
    answers = iter([(True, 5.0, None), (False, None, "timeout"), (False, None, "timeout"), (False, None, "timeout"), (True, 7.0, None)])
    m = mon.Monitor(runner=lambda k, t, **kw: next(answers), notify=lambda lvl, kind, msg, ts=None: sink.append((lvl, msg)))
    cid = db.add_check("Internet", "tcp", "1.1.1.1:443", 30)
    t = 1000.0
    m.tick(t); assert m.state[cid]["ok"] and not sink
    m.tick(t + 10); assert len(db.check_results(cid)) == 1       # intervallo non ancora trascorso
    m.tick(t + 30); assert not sink                              # primo errore: nessun avviso
    m.tick(t + 60); assert sink == [("warn", "«Internet» non è raggiungibile (timeout)")]
    m.tick(t + 90); assert len(sink) == 1                        # niente doppioni
    m.tick(t + 120); assert sink[-1] == ("info", "«Internet» è di nuovo raggiungibile dopo 90 s")
    assert db.check_uptime(cid, 0) == 40.0                       # 2 riusciti su 5
    db.set_check_enabled(cid, False)
    m.tick(t + 500); assert len(db.check_results(cid, 100)) == 5   # disattivato: non gira


def test_monitor_respects_alert_setting(fresh_db):
    db.set_setting("alerts", {"monitor_alerts": False})
    sink = []
    m = mon.Monitor(runner=lambda k, t, **kw: (False, None, "x"), notify=lambda *a, **k: sink.append(a))
    db.add_check("A", "tcp", "a:1", 15)
    for i in range(4):
        m.tick(1000 + i * 15)
    assert not sink


# ---------------------------------------------------------------- database
def test_disk_forecast(fresh_db):
    now = 1_800_000_000
    for i in range(0, 5 * 1440, 10):
        t = now - i * 60
        db.insert_minute(t, {"disk": 50 - i / 1440.0}, None, None)     # +1%/giorno
    f = db.disk_forecast(now)
    assert 0.95 < f["slope_per_day"] < 1.05 and 48 <= f["days_to_full"] <= 52
    # disco stabile: nessuna previsione di riempimento
    for i in range(0, 5 * 1440, 10):
        db.insert_minute(now - i * 60, {"disk": 40.0}, None, None)
    assert db.disk_forecast(now)["days_to_full"] is None


def test_disk_forecast_needs_two_days(fresh_db):
    db.insert_minute(1_800_000_000, {"disk": 10.0}, None, None)
    db.insert_minute(1_800_000_060, {"disk": 11.0}, None, None)
    assert db.disk_forecast(1_800_000_100) is None


def test_backup_is_consistent(fresh_db, tmp_path):
    db.add_event("info", "x", "ciao")
    db.backup_to(tmp_path / "b.db")
    rows = sqlite3.connect(tmp_path / "b.db").execute("SELECT message FROM events").fetchall()
    assert rows == [("ciao",)]


def test_events_kind_filter(fresh_db):
    db.add_event("info", "monitor", "a"); db.add_event("info", "container", "b")
    assert [e["message"] for e in db.events(kind="monitor")] == ["a"]


# ---------------------------------------------------------------- API
def test_container_details_bulk_autorestart(authed):
    lst = {c["name"]: c for c in authed.get("/api/containers").json()["containers"]}
    assert lst["immich_redis"]["project"] == "immich" and lst["pihole"]["autorestart"] is False
    assert authed.put("/api/containers/pihole/autorestart", json={"enabled": True}).status_code == 200
    assert next(c for c in authed.get("/api/containers").json()["containers"] if c["name"] == "pihole")["autorestart"] is True
    authed.put("/api/containers/pihole/autorestart", json={"enabled": False})
    d = authed.get("/api/containers/pihole/details").json()
    assert d["mounts"][0]["target"] == "/data" and "DB_PASSWORD" in d["env_names"]
    assert "secret" not in str(d).lower()                            # solo i nomi delle variabili
    assert authed.get("/api/containers/ghost/details").status_code == 404
    r = authed.post("/api/containers-bulk", json={"action": "start", "names": ["nextcloud", "immich_redis", "ghost"]}).json()["results"]
    assert [x["ok"] for x in r] == [True, True, False]
    assert ("immich_redis", "start") in fake_docker.STATE["calls"]
    r = authed.post("/api/containers-bulk", json={"action": "stop", "names": ["pi-dashboard"]}).json()["results"]
    assert r[0]["ok"] is False                                       # non ferma se stessa
    assert authed.post("/api/containers-bulk", json={"action": "kill", "names": ["x"]}).status_code == 400
    assert authed.post("/api/containers-bulk", json={"action": "stop", "names": []}).status_code == 400


def test_docker_usage_and_prune(authed):
    u = authed.get("/api/docker/usage").json()
    assert u["images"]["dangling_count"] == 2 and u["build_cache"]["reclaimable"] > 0
    assert authed.post("/api/docker/prune", json={"what": "images"}).json()["reclaimed"] > 0
    assert authed.post("/api/docker/prune", json={"what": "containers"}).status_code == 400   # mai i container
    assert ("prune", "images") in fake_docker.STATE["calls"]


def test_process_detail(authed):
    d = authed.get(f"/api/processes/{os.getpid()}").json()
    assert d["pid"] == os.getpid() and d["name"] and d["protected"] is True and d["started"] > 0
    assert authed.get("/api/processes/99999999").status_code == 404


def test_checks_crud_via_api(authed, tcp_server):
    r = authed.post("/api/checks", json={"name": "Locale", "kind": "tcp", "target": f"127.0.0.1:{tcp_server}", "interval": 30})
    assert r.status_code == 200
    cid = r.json()["id"]
    c = authed.get("/api/checks").json()["checks"][0]
    assert c["name"] == "Locale" and c["ok"] is True and c["ms"] is not None and c["uptime24h"] == 100.0 and len(c["history"]) == 1
    assert authed.post("/api/checks", json={"name": "x", "kind": "tcp", "target": "nope", "interval": 30}).status_code == 400
    assert authed.post(f"/api/checks/{cid}/enabled", json={"enabled": False}).status_code == 200
    assert authed.get("/api/checks").json()["checks"][0]["enabled"] is False
    assert authed.post(f"/api/checks/{cid}/run").status_code == 200
    assert authed.delete(f"/api/checks/{cid}").status_code == 200
    assert authed.get("/api/checks").json()["checks"] == []
    assert authed.delete(f"/api/checks/{cid}").status_code == 404


def test_checks_limit(authed):
    for i in range(mon.MAX_CHECKS):
        db.add_check(f"c{i}", "tcp", "127.0.0.1:1", 3600)
    assert authed.post("/api/checks", json={"name": "extra", "kind": "tcp", "target": "a:1", "interval": 60}).status_code == 400
    for c in db.list_checks():
        db.delete_check(c["id"])


def test_db_download_is_valid_sqlite_and_needs_login(client, tmp_path):
    assert client.get("/api/db/download").status_code == 401
    client.post("/api/login", json={"password": "segreta"})
    r = client.get("/api/db/download")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    f = tmp_path / "d.db"; f.write_bytes(r.content)
    tables = {t[0] for t in sqlite3.connect(f).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"metrics_1m", "events", "checks"} <= tables
    leftovers = [p for p in os.listdir(os.environ["DATA_DIR"]) if p.startswith("backup-")]
    assert leftovers == []                                           # il file temporaneo viene rimosso


def test_system_has_forecast_key_and_events_kind(authed):
    assert "disk_forecast" in authed.get("/api/system").json()
    db.add_event("info", "monitor", "m")
    assert all(e["kind"] == "monitor" for e in authed.get("/api/events?kind=monitor").json()["events"])


# ---------------------------------------------------------------- PWA e risorse statiche
def test_manifest_and_icons(client):
    import json
    from io import BytesIO
    from PIL import Image
    r = client.get("/static/manifest.webmanifest")           # niente login: il browser lo scarica senza cookie
    assert r.status_code == 200 and "json" in r.headers["content-type"]
    m = json.loads(r.text)
    assert m["display"] == "standalone" and m["start_url"] == "/" and m["lang"] == "it"
    assert any(i["purpose"] == "maskable" for i in m["icons"])
    for icon in m["icons"]:
        img = Image.open(BytesIO(client.get(icon["src"]).content))
        assert f"{img.width}x{img.height}" == icon["sizes"], icon
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200
    html = client.get("/", follow_redirects=False)
    assert html.status_code in (302, 307)                    # la pagina resta protetta
    assert 'rel="manifest"' in open("app/static/index.html").read() and "viewport-fit=cover" in open("app/static/index.html").read()


# ---------------------------------------------------------------- monitor avanzato (health endpoint)
@pytest.fixture()
def health_server():
    state = {"status": "ok"}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = ('{"status": "%s", "db": {"up": true}}' % state["status"]).encode()
                code = 200 if state["status"] == "ok" else 503
            elif self.path == "/auth":
                ok = self.headers.get("Authorization") == "Bearer s3"
                body, code = b"hello world", 200 if ok else 401
            elif self.path == "/html":
                body, code = b"<h1>Benvenuto</h1>", 200
            elif self.path == "/redir":
                self.send_response(302); self.send_header("Location", "/health"); self.end_headers(); return
            else:
                body, code = b"not json", 200
            self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        do_HEAD = do_GET
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def test_health_json_checks(health_server):
    base, state = health_server
    o = mon.validate_options("http", {"json_path": "status"})
    r = mon.run_check("http", base + "/health", opts=o)
    assert r.ok and r.err is None
    assert mon.run_check("http", base + "/health", opts=mon.validate_options("http", {"json_path": "db.up"})).ok   # true booleano
    r = mon.run_check("http", base + "/health", opts=mon.validate_options("http", {"json_path": "status", "json_equals": "good"}))
    assert not r.ok and "atteso good" in r.err
    state["status"] = "degraded"
    r = mon.run_check("http", base + "/health", opts=o)
    assert not r.ok and "503" in r.err                     # il codice 503 è già un errore
    o2 = mon.validate_options("http", {"json_path": "status", "expect_status": "200-599"})
    r = mon.run_check("http", base + "/health", opts=o2)
    assert not r.ok and "degraded" in r.err                # con codici larghi decide il campo JSON
    r = mon.run_check("http", base + "/other", opts=o)
    assert not r.ok and "non è JSON" in r.err
    state["status"] = "ok"
    r = mon.run_check("http", base + "/health", opts=mon.validate_options("http", {"json_path": "nope"}))
    assert not r.ok and "non trovato" in r.err


def test_keyword_status_headers_head_redirect(health_server):
    base, _ = health_server
    kw = lambda **k: mon.validate_options("http", k)
    assert mon.run_check("http", base + "/html", opts=kw(keyword="Benvenuto")).ok
    r = mon.run_check("http", base + "/html", opts=kw(keyword="Addio"))
    assert not r.ok and "Addio" in r.err
    assert not mon.run_check("http", base + "/auth", opts=kw()).ok                       # 401 non è nei codici attesi
    assert mon.run_check("http", base + "/auth", opts=kw(expect_status="401")).ok       # ... ma lo è se lo dico
    assert mon.run_check("http", base + "/auth", opts=kw(headers={"Authorization": "Bearer s3"})).ok
    assert mon.run_check("http", base + "/html", opts=kw(method="HEAD")).ok
    assert mon.run_check("http", base + "/redir", opts=kw()).ok                          # segue il reindirizzamento


def test_validate_options_rejects_bad_input():
    v = mon.validate_options
    assert v("tcp", {"keyword": "ignorata", "retries": 3}) == {"retries": 3, "timeout": 10, "public": False}
    for bad in [{"method": "POST"}, {"expect_status": "abc"}, {"expect_status": "99-200"}, {"timeout": 0},
                {"timeout": 31}, {"retries": 9}, {"json_path": "a b"}, {"keyword": "x" * 101},
                {"headers": {"X": "a\nb"}}, {"headers": {f"H{i}": "v" for i in range(6)}},
                {"method": "HEAD", "keyword": "x"}, {"ssl_warn_days": 400}]:
        with pytest.raises(ValueError):
            v("http", bad)
    assert mon.parse_status_spec("200-299, 404") == [(200, 299), (404, 404)]


def test_retries_incidents_and_fast_recheck(fresh_db):
    sink = []
    answers = iter([(False, None, "timeout"), (False, None, "timeout"), (False, None, "timeout"), (True, 4.0, None)])
    m = mon.Monitor(runner=lambda k, t, **kw: next(answers), notify=lambda lvl, kind, msg, ts=None: sink.append((lvl, msg)))
    cid = db.add_check("API", "http", "http://x", 900, {"retries": 3})
    t = 1000.0
    m.tick(t)
    m.tick(t + 20); assert len(db.check_results(cid)) == 1       # ricontrollo non prima di 30 s
    m.tick(t + 30); m.tick(t + 60)                                # 3 errori di fila con intervallo di 15 minuti
    assert sink == [("warn", "«API» non è raggiungibile (timeout)")]
    assert db.open_incident_id(cid) is not None
    m.tick(t + 60 + 900)
    assert db.open_incident_id(cid) is None
    inc = db.incidents(cid)
    assert len(inc) == 1 and inc[0]["start"] == 1000 and inc[0]["end"] == 1960 and inc[0]["err"] == "timeout"
    assert "dopo 16 min" in sink[-1][1]


def test_incident_survives_restart(fresh_db):
    cid = db.add_check("A", "tcp", "a:1", 15)
    db.open_incident(cid, 500, "timeout")
    m = mon.Monitor(runner=lambda k, t, **kw: (True, 1.0, None), notify=lambda *a, **k: None)
    m.tick(1000)
    assert db.open_incident_id(cid) is None and db.incidents(cid)[0]["end"] == 1000


def test_certificate_warning(fresh_db):
    sink = []
    m = mon.Monitor(runner=lambda k, t, **kw: mon.Result(True, 5.0, None, 5), notify=lambda lvl, kind, msg, ts=None: sink.append(msg))
    db.add_check("Sito", "http", "https://x", 15, {"ssl_warn_days": 14})
    m.tick(1000); m.tick(1020)
    assert sink == ["Il certificato di «Sito» scade tra 5 giorni"]       # una sola volta


def test_legacy_checks_keep_old_behaviour(fresh_db, health_server):
    """Controlli creati prima delle opzioni: giù solo con un 5xx."""
    base, _ = health_server
    db.add_check("Vecchio", "http", base + "/auth", 60)                   # 401
    assert db.list_checks()[0]["opts"] == {}
    assert mon.run_check("http", base + "/auth", opts={}).ok


def test_checks_api_options_detail_and_update(authed, health_server):
    base, _ = health_server
    body = {"name": "Health", "kind": "http", "target": base + "/health", "interval": 30,
            "options": {"json_path": "status", "retries": 1}}
    cid = authed.post("/api/checks", json=body).json()["id"]
    c = authed.get("/api/checks").json()["checks"][0]
    assert c["ok"] is True and c["opts"]["json_path"] == "status" and c["opts"]["expect_status"] == "200-399"
    d = authed.get(f"/api/checks/{cid}").json()
    assert d["points"] and d["incidents"] == [] and d["stats"]["avg"] is not None
    body["name"], body["options"] = "Health 2", {"json_path": "status", "json_equals": "good"}
    assert authed.put(f"/api/checks/{cid}", json=body).status_code == 200
    c = authed.get("/api/checks").json()["checks"][0]
    assert c["name"] == "Health 2" and c["ok"] is False and "atteso good" in c["err"]
    assert authed.put("/api/checks/999", json=body).status_code == 404
    bad = {**body, "options": {"timeout": 99}}
    assert authed.put(f"/api/checks/{cid}", json=bad).status_code == 400
    assert authed.post("/api/checks", json=bad).status_code == 400
    assert authed.get("/api/checks/999").status_code == 404


def test_status_page(authed, monkeypatch, health_server):
    from app import config
    base, _ = health_server
    authed.post("/api/checks", json={"name": "Pubblico", "kind": "http", "target": base + "/health", "interval": 30,
                                     "options": {"public": True}})
    authed.post("/api/checks", json={"name": "Privato", "kind": "http", "target": base + "/health", "interval": 30})
    monkeypatch.setattr(config, "STATUS_PAGE", False)
    assert authed.get("/api/status").status_code == 404 and authed.get("/status").status_code == 404
    monkeypatch.setattr(config, "STATUS_PAGE", True)
    authed.cookies.clear()                                       # da qui in poi nessun login
    j = authed.get("/api/status").json()
    assert [c["name"] for c in j["checks"]] == ["Pubblico"] and j["all_ok"] is True
    assert "target" not in j["checks"][0] and "opts" not in j["checks"][0]
    assert authed.get("/status").status_code == 200


def test_webhook(monkeypatch, fresh_db):
    from app import alerts, config
    sent = []
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://hook.test/x")
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **kw: sent.append((url, kw)))
    monkeypatch.setattr(alerts.threading, "Thread", lambda target, daemon=None: type("T", (), {"start": staticmethod(target)})())
    alerts.notify("warn", "monitor", "«API» non è raggiungibile", 5)
    alerts.notify("info", "container", "x", 6)                     # un info qualunque non parte
    alerts.notify("info", "monitor", "«API» è di nuovo raggiungibile", 7)
    assert [k["json"]["message"] for _, k in sent] == ["«API» non è raggiungibile", "«API» è di nuovo raggiungibile"]
    monkeypatch.setattr(config, "WEBHOOK_FORMAT", "text")
    sent.clear(); alerts.notify("crit", "alert", "Temperatura critica", 8)
    assert sent[0][1]["content"] == "Temperatura critica".encode() and sent[0][1]["headers"]["Priority"] == "urgent"


# ---------------------------------------------------------------- compatibilità Raspberry Pi
def _w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)


def test_pi_undervoltage_and_fan_via_hwmon(tmp_path, monkeypatch):
    """Pi 5 e kernel senza get_throttled: sotto-tensione dal driver rpi_volt e ventola dal driver pwm-fan."""
    from app import system
    monkeypatch.setattr(system, "SYS", str(tmp_path))
    monkeypatch.setattr(system.shutil, "which", lambda n: None)          # niente vcgencmd nel container
    _w(tmp_path / "class/hwmon/hwmon2/name", "rpi_volt\n"); _w(tmp_path / "class/hwmon/hwmon2/in0_lcrit_alarm", "1\n")
    _w(tmp_path / "class/hwmon/hwmon3/name", "pwmfan\n")
    _w(tmp_path / "class/hwmon/hwmon3/fan1_input", "3120\n"); _w(tmp_path / "class/hwmon/hwmon3/pwm1", "128\n")
    pi = system.pi_status()
    assert pi["available"] and pi["partial"] and pi["flags"] == {"undervoltage": True}
    assert pi["fan"] == {"rpm": 3120, "percent": 50}
    _w(tmp_path / "class/hwmon/hwmon2/in0_lcrit_alarm", "0\n")
    assert system.pi_status()["flags"] == {"undervoltage": False}


def test_pi_get_throttled_nested_firmware_path(tmp_path, monkeypatch):
    from app import system
    monkeypatch.setattr(system, "SYS", str(tmp_path))
    monkeypatch.setattr(system.shutil, "which", lambda n: None)
    _w(tmp_path / "devices/platform/soc@107c000000/soc@107c000000:firmware/get_throttled", "0x50005\n")
    pi = system.pi_status()
    assert pi["available"] and not pi.get("partial") and pi["flags"]["undervoltage"] and pi["flags"]["undervoltage_past"]
    assert pi["fan"] is None


def test_pi_without_any_sensor(tmp_path, monkeypatch):
    from app import system
    monkeypatch.setattr(system, "SYS", str(tmp_path))
    monkeypatch.setattr(system.shutil, "which", lambda n: None)
    pi = system.pi_status()
    assert pi["available"] is False and pi["fan"] is None


def test_container_stats_one_shot_cpu_from_two_samples(monkeypatch):
    from app import docker_api as d
    samples = iter([1_000_000_000, 1_500_000_000])             # nanosecondi di CPU usati

    class C:
        id = "abc"
        def stats(self, stream=False, one_shot=False):
            assert one_shot is True
            return {"cpu_stats": {"cpu_usage": {"total_usage": next(samples)}}, "precpu_stats": {"cpu_usage": {"total_usage": 0}},
                    "memory_stats": {"usage": 300, "limit": 1000, "stats": {}}}
    t = iter([100.0, 110.0])
    monkeypatch.setattr(d.time, "monotonic", lambda: next(t))
    d._stats_cache.clear(); d._prev_cpu.clear()
    first = d._stats_for(C())
    assert first["cpu"] is None and first["mem_used"] == 300      # al primo giro manca il campione precedente
    d._stats_cache.clear()
    assert d._stats_for(C())["cpu"] == 5.0                        # 0,5 s di CPU in 10 s = 5%


def test_container_stats_old_docker_falls_back(monkeypatch):
    from app import docker_api as d

    class InvalidVersion(Exception): pass

    class C:
        id = "old"
        def stats(self, stream=False, one_shot=False):
            if one_shot:
                raise InvalidVersion("one_shot is not supported")
            return {"cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 2000, "online_cpus": 4},
                    "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
                    "memory_stats": {"usage": 500, "limit": 1000, "stats": {"inactive_file": 100}}}
    monkeypatch.setattr(d, "_one_shot", True)
    d._stats_cache.clear(); d._prev_cpu.clear()
    out = d._stats_for(C())
    assert out == {"cpu": 40.0, "mem_used": 400, "mem_limit": 1000} and d._one_shot is False


# ---------------------------------------------------------------- sicurezza per la release pubblica
def test_placeholder_password_is_rejected(monkeypatch, tmp_path, capsys):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for weak in ("cambiami", "ChangeMe", " password "):
        monkeypatch.setattr(config, "PASSWORD", weak)
        config.ensure_runtime()
        assert config.PASSWORD not in config.PLACEHOLDER_PASSWORDS and len(config.PASSWORD) >= 10
    assert "valore d'esempio" in capsys.readouterr().out
    monkeypatch.setattr(config, "PASSWORD", "una-frase-lunga-mia")
    config.ensure_runtime(); assert config.PASSWORD == "una-frase-lunga-mia"


def test_security_headers_and_no_inline_scripts(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    for page in ("login.html", "index.html", "status.html"):
        html = (Path(__file__).resolve().parent.parent / "app/static" / page).read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), f"{page}: script inline vietato dalla CSP"
        assert not re.search(r"\son(click|load|submit|change)=", html)
