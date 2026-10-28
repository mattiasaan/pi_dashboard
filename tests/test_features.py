import http.server
import os
import socket
import sqlite3
import threading
import time

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
    ok, ms, err = mon.run_check("tcp", f"127.0.0.1:{tcp_server}")
    assert ok and ms is not None and err is None
    s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
    ok, ms, err = mon.run_check("tcp", f"127.0.0.1:{closed}", timeout=1)
    assert not ok and ms is None and err
    base = f"http://127.0.0.1:{http_server}"
    assert mon.run_check("http", base + "/ok")[0]
    assert mon.run_check("http", base + "/missing")[0]            # il server risponde: raggiungibile
    ok, _, err = mon.run_check("http", base + "/boom")
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
    m = mon.Monitor(runner=lambda k, t: next(answers), notify=lambda lvl, kind, msg, ts=None: sink.append((lvl, msg)))
    cid = db.add_check("Internet", "tcp", "1.1.1.1:443", 30)
    t = 1000.0
    m.tick(t); assert m.state[cid]["ok"] and not sink
    m.tick(t + 10); assert len(db.check_results(cid)) == 1       # intervallo non ancora trascorso
    m.tick(t + 30); assert not sink                              # primo errore: nessun avviso
    m.tick(t + 60); assert sink == [("warn", "«Internet» non è raggiungibile (timeout)")]
    m.tick(t + 90); assert len(sink) == 1                        # niente doppioni
    m.tick(t + 120); assert sink[-1] == ("info", "«Internet» è di nuovo raggiungibile")
    assert db.check_uptime(cid, 0) == 40.0                       # 2 riusciti su 5
    db.set_check_enabled(cid, False)
    m.tick(t + 500); assert len(db.check_results(cid, 100)) == 5   # disattivato: non gira


def test_monitor_respects_alert_setting(fresh_db):
    db.set_setting("alerts", {"monitor_alerts": False})
    sink = []
    m = mon.Monitor(runner=lambda k, t: (False, None, "x"), notify=lambda *a, **k: sink.append(a))
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
