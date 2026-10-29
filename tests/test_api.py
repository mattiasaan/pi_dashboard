import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import docker_api
from app.main import app
from tests import fake_docker

H = {"X-Requested-With": "pi-dashboard"}


@pytest.fixture()
def client(monkeypatch):
    fake_docker.install(monkeypatch)
    with TestClient(app, client=("100.101.102.103", 50000)) as c:   # un client Tailscale
        yield c


@pytest.fixture()
def authed(client):
    r = client.post("/api/login", json={"password": "segreta"})
    assert r.status_code == 200
    client.headers.update(H)
    return client


def test_everything_requires_login(client):
    for path in ("/api/live", "/api/history", "/api/processes", "/api/containers", "/api/system", "/api/events", "/api/settings"):
        assert client.get(path).status_code == 401, path
    assert client.get("/", follow_redirects=False).status_code in (302, 307)
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/login").status_code == 200


def test_login_wrong_password_and_cookie_flags(client):
    assert client.post("/api/login", json={"password": "no"}).status_code == 401
    r = client.post("/api/login", json={"password": "segreta"})
    ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck


def test_csrf_header_required_for_writes(client):
    client.post("/api/login", json={"password": "segreta"})
    assert client.post("/api/containers/pihole/stop").status_code == 403
    assert client.put("/api/settings", json={"temp_warn": 70}).status_code == 403
    assert client.post("/api/containers/pihole/stop", headers=H).status_code == 200


def test_live_history_system(authed):
    time.sleep(1.6)  # lascia lavorare il collector
    live = authed.get("/api/live").json()
    for k in ("cpu", "cores", "mem", "disk", "net_rx", "uptime", "alerts"):
        assert k in live
    assert 0 <= live["cpu"] <= 100 and len(live["cores"]) >= 1
    h = authed.get("/api/history?range=15m").json()
    assert h["series"]["t"], "il collector avrebbe dovuto salvare almeno un campione"
    assert authed.get("/api/history?range=1h").status_code == 200
    assert authed.get("/api/history?range=30d").json()["source"] == "1m"
    assert authed.get("/api/history?range=boh").status_code == 400
    s = authed.get("/api/system").json()
    assert s["hostname"] and s["disks"] and "pi" in s and s["db"]["db_bytes"] > 0


def test_processes(authed):
    p = authed.get("/api/processes?sort=cpu&limit=5").json()["processes"]
    assert 0 < len(p) <= 5 and {"pid", "name", "cpu", "rss", "cmd"} <= set(p[0])
    assert authed.post("/api/processes/1/signal", json={"signal": "KILL"}).status_code == 403
    assert authed.post("/api/processes/99999999/signal", json={"signal": "TERM"}).status_code == 404
    assert authed.post("/api/processes/1234/signal", json={"signal": "NOPE"}).status_code in (400, 404)


def test_real_process_terminate(authed):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        r = authed.post(f"/api/processes/{proc.pid}/signal", json={"signal": "TERM"})
        assert r.status_code == 200
        assert proc.wait(timeout=5) != 0
    finally:
        proc.kill()
    evs = authed.get("/api/events").json()["events"]
    assert any(e["kind"] == "process" for e in evs)


def test_containers_actions_logs(authed):
    fake_docker.STATE["calls"].clear()
    lst = authed.get("/api/containers").json()["containers"]
    assert {c["name"] for c in lst} >= {"pihole", "nextcloud"}
    assert authed.post("/api/containers/nextcloud/start").status_code == 200
    assert authed.post("/api/containers/pihole/restart").status_code == 200
    assert fake_docker.STATE["calls"] == [("nextcloud", "start"), ("pihole", "restart")]
    assert authed.post("/api/containers/pi-dashboard/stop").status_code == 403    # non ferma se stessa
    assert authed.post("/api/containers/ghost/stop").status_code == 404
    assert authed.post("/api/containers/pihole/explode").status_code == 400
    logs = authed.get("/api/containers/pihole/logs?tail=5").json()["logs"]
    assert logs.count("\n") == 4
    evs = [e["message"] for e in authed.get("/api/events").json()["events"]]
    assert any("pihole" in m and "riavviato" in m for m in evs)


def test_docker_unavailable_is_503(authed, monkeypatch):
    def boom(*a, **k):
        raise docker_api.DockerUnavailable("Docker non raggiungibile")
    monkeypatch.setattr(docker_api, "list_containers", boom)
    r = authed.get("/api/containers")
    assert r.status_code == 503 and "Docker" in r.json()["detail"]


def test_power_needs_password(authed):
    fake_docker.STATE["calls"].clear()
    assert authed.post("/api/power", json={"action": "reboot", "password": "sbagliata"}).status_code == 403
    assert authed.post("/api/power", json={"action": "format", "password": "segreta"}).status_code == 400
    assert fake_docker.STATE["calls"] == []
    assert authed.post("/api/power", json={"action": "reboot", "password": "segreta"}).status_code == 200
    assert fake_docker.STATE["calls"] == [("host", "reboot")]


def test_settings_roundtrip_and_validation(authed):
    s = authed.get("/api/settings").json()["alerts"]
    assert s["temp_warn"] == 75
    assert authed.put("/api/settings", json={"temp_warn": 70, "mem_high": 85}).json()["alerts"]["mem_high"] == 85
    assert authed.get("/api/settings").json()["alerts"]["temp_warn"] == 70
    assert authed.put("/api/settings", json={"temp_warn": 90}).status_code == 400     # warn >= crit
    assert authed.put("/api/settings", json={"cpu_high": 1000}).status_code == 400
    assert authed.put("/api/settings", json={"temp_warn": 75}).status_code == 200
    assert authed.post("/api/settings/test-telegram").status_code == 400


def test_static_assets_served(client):
    for p in ("style.css", "app.js", "vendor/chart.umd.js", "vendor/fonts/geist-latin-wght-normal.woff2"):
        r = client.get("/static/" + p)
        assert r.status_code == 200 and len(r.content) > 500, p


def test_internet_clients_are_rejected_lan_and_tailscale_are_not(monkeypatch):
    fake_docker.install(monkeypatch)
    for ip in ("8.8.8.8", "203.0.113.9", "2001:db8::1", "testclient"):
        with TestClient(app, client=(ip, 5000)) as c:
            for path in ("/", "/login", "/api/health", "/static/app.js", "/api/live"):
                r = c.get(path, follow_redirects=False)
                assert r.status_code == 403, (ip, path, r.status_code)
            assert c.post("/api/login", json={"password": "segreta"}).status_code == 403
            assert "non consentito" in c.get("/").json()["detail"]
    for ip in ("100.64.0.1", "100.127.255.254", "127.0.0.1", "::1", "fd7a:115c:a1e0::5", "::ffff:100.100.1.1",
               "192.168.1.50", "10.0.0.7", "172.20.0.3", "fe80::1"):
        with TestClient(app, client=(ip, 5000)) as c:
            assert c.get("/api/health").status_code == 200, ip
