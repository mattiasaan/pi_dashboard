# Pi Dashboard

A dark, minimal web dashboard for your Raspberry Pi, in a single Docker container. Live metrics, history stored in SQLite, Docker container control, process management and an uptime-robot style monitor for your own services. Reachable **only through Tailscale** by default, not from your LAN.

> Italiano: see [README.it.md](README.it.md). The interface itself is currently in Italian only (see [Known limits](#known-limits)).

## Features

- **Overview**: CPU total and per core, memory and swap, temperature, frequency, disk, network, disk I/O, load average. Live every 2 seconds, with sparklines.
- **History**: 1 hour to 30 days of CPU, memory, temperature, network, I/O, load, frequency and disk, with average / min / max and CSV export. Gaps (Pi switched off) show as gaps, not as fake lines.
- **Processes**: search, sort, group by name, details (command, user, memory, open files, listening ports), terminate / kill / pause / resume. PID 1 and the dashboard itself are protected.
- **Docker**: every container including stopped ones, grouped by Compose project; start, stop, restart, bulk actions, details, live logs, per-container **auto-restart**, disk usage and a safe prune (never touches containers or volumes).
- **Monitor** (uptime-robot style): see [below](#monitor).
- **Alerts**: temperature, CPU, memory, disk, under-voltage, throttling, containers that stop or become unhealthy, monitor checks that fail. In the dashboard, by Telegram and/or a generic webhook (ntfy, Discord, Slack, Home Assistant). Each alert fires only after the condition lasts, and reports when it clears.
- **System**: model, OS, power status, fan speed (Pi 5), disks with a "full in N days" forecast, network interfaces, database backup download, reboot and shutdown (password asked again).
- **Any screen**: phone (bottom bar), tablet (icon rail), desktop (sidebar). A kiosk mode (`/#schermo`) for a wall tablet that keeps the screen awake. Installable as an app (needs HTTPS).

## Supported hardware

| Model | OS | Status |
|---|---|---|
| Raspberry Pi 5, 500, CM5 | 64-bit | Supported. Temperature, frequency, power status, fan speed (Active Cooler). |
| Raspberry Pi 4, 400, CM4 | 64-bit or 32-bit | Supported. |
| Raspberry Pi 3 B, 3 B+, 3 A+, CM3, Zero 2 W | 64-bit or 32-bit | Supported. 32-bit builds compile one dependency (psutil), the first build takes a few minutes. |
| Raspberry Pi 2 | 32-bit | Supported (armv7), same note as above. |
| Raspberry Pi 1, Zero, Zero W | | **Not supported**: ARMv6 has no `python:3.12` image and no wheels for the dependencies. |
| Other Linux machines (x86-64, other ARM boards) | | Work for everything except Pi-specific readings (power status, fan). Handy to try it out. |

How the Pi-specific readings work, so you know what to expect on your model:

- **Temperature and frequency** come from `/sys/class/thermal` and `cpufreq`, present on every model.
- **Power status** (under-voltage, throttling, now and since boot) comes from the firmware's `get_throttled`: `vcgencmd` if installed, otherwise the sysfs file. If the kernel does not expose it, the dashboard falls back to the `rpi_volt` hwmon driver, which reports **under-voltage right now** only (you get a note saying the rest is unavailable).
- **Fan** (Pi 5 Active Cooler or any PWM fan with a hwmon driver): RPM and percentage, shown only when present.
- **Container memory and CPU** need the memory cgroup. It is on by default in Raspberry Pi OS Bookworm; on older releases add `cgroup_enable=memory cgroup_memory=1` to `/boot/cmdline.txt` and reboot, otherwise container memory shows as "-".
- Pi 3 B+ and later reduce their clock when they get hot, and the thresholds differ by model. The dashboard warns at 75 °C and 82 °C by default and also alerts on the firmware's throttling flag. Adjust the thresholds in **System** for your board and cooling.

Verification status: the code is covered by tests that run without a Pi (sysfs paths and the Docker client are simulated). The dependencies were checked to have ready-made packages for arm64 and, except psutil, for armv7 (psutil is compiled from source there by the Dockerfile; that path has not been run). The included CI workflow builds the image for amd64, arm64 and armv7. It has **not** been tried on real Raspberry Pi hardware by the author. If something looks wrong on yours, open an issue with the output of `cat /proc/device-tree/model; uname -m`.

Resource use, measured on an x86-64 machine (not on a Pi): about 90 MB of RAM and 0.3 % of one core when idle. Container statistics are read in "one-shot" mode, which avoids a 1 to 2 second wait per container, so many containers stay cheap on slower boards like the Pi 3.

## Install

Requirements: Docker with the Compose plugin (`docker compose version`) and **Tailscale** connected on the Pi and on the devices you will open the dashboard from.

```bash
git clone https://github.com/OWNER/pi-dashboard.git && cd pi-dashboard
cp .env.example .env
nano .env                      # set DASH_PASSWORD
docker compose up -d --build
```

Or use the published image instead of building: in `docker-compose.yml` replace `build: .` with `image: ghcr.io/OWNER/pi-dashboard:latest` (arm64, armv7 and amd64) and run `docker compose up -d`. Keep `SELF_IMAGE` equal to that image name.

With Tailscale on your phone or computer open `http://<tailscale-ip-of-the-pi>:8080` (`tailscale ip -4` on the Pi) or `http://<pi-name>:8080` with MagicDNS. From the LAN **without** Tailscale it does not answer: that is intended.

If `DASH_PASSWORD` is empty or still an example value (`changeme`...), a random temporary password is generated and printed in the log (`docker compose logs pi-dashboard`). Set your own.

Update: `git pull && docker compose up -d --build`. Your data in `./data` is kept.

### Tailscale only

Two independent layers:

1. **Listen address.** With `BIND_ADDRESS=tailscale` (default) the server binds only to the `tailscale0` address (`100.x.y.z`). On the LAN the port does not exist.
2. **Client filter.** Every request is rejected with 403 unless it comes from a Tailscale network or the Pi itself (`ALLOWED_NETWORKS`).

If Tailscale is not up yet at boot, the dashboard waits up to 2 minutes for the interface, then exits (Docker restarts it). It never falls back to the LAN.

**Optional HTTPS** (needed to install it as an app): Tailscale can put a valid certificate in front, still only inside your tailnet. First check what you already publish with `tailscale serve status`, because a second `serve` on the same port and path overwrites the first. Use a different HTTPS port (443, 8443 or 10000):

```bash
# .env
BIND_ADDRESS=127.0.0.1
COOKIE_SECURE=true

docker compose up -d
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8080
```

It is then at `https://<pi-name>.<tailnet>.ts.net:8443`. Do not publish it under a sub-path (it uses absolute paths) and never use `tailscale funnel`.

## Monitor

Add checks for your other projects and get alerted when they stop answering, and when they are back.

| Type | What it checks |
|---|---|
| **Health endpoint (JSON)** | An HTTP(S) URL such as `https://api.example.com/health`. Up when the status code is accepted **and** a JSON field has the expected value. Default field `status`, accepted values `ok`, `up`, `healthy`, `pass`, `true`... or your own value. Dotted paths work (`checks.database.status`). |
| **Web page** | An HTTP(S) URL. Up when the status code is accepted and, optionally, the body contains a keyword. |
| **TCP port** | `host:port`. Up when the connection opens. |

Options (under *Advanced options*): accepted status codes (default `200-399`, for example `200,204` or `200-299`), GET or HEAD, custom headers (one per line, for tokens), timeout (1 to 30 s), number of failures in a row before an alert (1 to 5), TLS certificate verification, warning when the certificate expires within N days, and showing the check on the public status page. Intervals from 15 seconds to 1 hour, up to 50 checks.

- After a failure the check is repeated after 30 seconds instead of waiting the full interval, so a real outage is confirmed quickly and a blip is dismissed quickly.
- Each check keeps response time, uptime over 24 hours and 7 days, and a list of incidents (start, duration, error) for 90 days. Click a check for the chart.
- An alert is sent when it goes down and another with the downtime when it recovers.

**Status page** (optional). With `STATUS_PAGE=true`, `/status` shows a read-only page with the checks marked as public: name, state, uptime, a small history. No login, no addresses. It is still reachable only from `ALLOWED_NETWORKS`.

**Webhook.** `WEBHOOK_URL` receives every warning and critical alert plus Monitor and alert recoveries. `WEBHOOK_FORMAT=json` posts `{"level","kind","message","ts","text","content"}` (`text` for Slack-style hooks, `content` for Discord). `WEBHOOK_FORMAT=text` posts the plain message, ideal for [ntfy](https://ntfy.sh) (`https://ntfy.sh/your-topic`).

## Configuration

Everything is set in `.env`. After a change run `docker compose up -d`.

| Variable | Default | Description |
|---|---|---|
| `DASH_PASSWORD` | random | Login password. Placeholder values are refused. |
| `PORT` | `8080` | Port (the container uses the host network). |
| `BIND_ADDRESS` | `tailscale` | `tailscale`, `127.0.0.1` (for `tailscale serve`), `all` (LAN too, not recommended) or a specific IP. |
| `TAILSCALE_INTERFACE` | `tailscale0` | Name of the Tailscale interface. |
| `BIND_WAIT_SECONDS` | `120` | How long to wait for Tailscale at startup. |
| `ALLOWED_NETWORKS` | Tailscale + loopback | Comma separated networks allowed to connect. `any` disables the filter. |
| `RETENTION_DAYS` | `30` | Days of history and events to keep. |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS. |
| `SESSION_HOURS` | `168` | Login session length. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Telegram alerts. |
| `WEBHOOK_URL`, `WEBHOOK_FORMAT` | empty, `json` | Generic webhook for alerts. |
| `STATUS_PAGE`, `STATUS_TITLE` | `false` | Public (tailnet-only) status page at `/status`. |
| `DOCKER_ENABLED` | `true` | `false` turns off container management. |
| `POWER_ENABLED` | `true` | `false` turns off reboot and shutdown. |
| `LIVE_INTERVAL`, `SAMPLE_INTERVAL` | `2`, `10` | Seconds between live updates and between database samples. |
| `RAW_RETENTION_HOURS` | `6` | Hours of 10-second samples before only 1-minute averages remain. |
| `SELF_IMAGE`, `SELF_CONTAINER_NAME` | `pi-dashboard:latest`, `pi-dashboard` | Image and container name (used by reboot/shutdown and to avoid stopping itself). |
| `DATA_DIR`, `HOST_ROOT` | `/data`, `/host` | Data folder and read-only host mount inside the container. |

Alert thresholds are changed in **System** and stored in the database. Defaults: temperature 75 °C (warning) and 82 °C (critical), CPU above 90 % for 120 s, memory above 90 %, disk above 90 %.

## How it works

```
Browser ── HTTP ──> FastAPI (one process, one worker)
                      ├─ REST API /api/*   (login required)
                      ├─ static files      (HTML, CSS, JS, Chart.js, font: bundled, no CDN)
                      ├─ thread "collector"  every 2 s metrics, 10 s samples, 1 min rollups, alerts, containers
                      └─ thread "monitor"    reachability checks
```

`docker-compose.yml` gives the container: `network_mode: host` and `pid: host` (to see the real interfaces and processes), `/:/host:ro` (read-only host filesystem for disk and system info), `/var/run/docker.sock` (container management), `./data:/data` (database and session key).

Reboot and shutdown: the container is not privileged. It asks Docker to run a short-lived helper container from the same image, with the permissions to enter PID 1's namespaces and run `systemctl reboot` or `poweroff`.

Data (`./data/metrics.db`, SQLite in WAL mode): 10-second samples for 6 hours, 1-minute averages for 30 days, events for 30 days, settings forever, monitor results for 7 days, incidents for 90 days. About 5 MB for 30 days, few writes to the SD card. Back up by copying the file, or download it from **System**.

## Security

Docker socket access is root on the host. Read [SECURITY.md](SECURITY.md) before exposing anything. In short: long password, Tailscale only, never on the Internet, strict Content-Security-Policy, CSRF header on every write, password again for reboot and shutdown, login rate limit. Use `POWER_ENABLED=false` and `DOCKER_ENABLED=false` if you do not need those features.

## API

All `/api/*` routes need the login cookie, except `/api/login`, `/api/health` and `/api/status`. Writes need the header `X-Requested-With: pi-dashboard`.

| Route | Purpose |
|---|---|
| `POST /api/login`, `POST /api/logout`, `GET /api/health` | Session, health check |
| `GET /api/live`, `GET /api/history?range=15m\|1h\|6h\|24h\|7d\|30d`, `GET /api/system` | Metrics, history, device info |
| `GET /api/processes`, `GET /api/processes/{pid}`, `POST /api/processes/{pid}/signal` | Processes |
| `GET /api/containers`, `POST /api/containers/{name}/{start\|stop\|restart}`, `POST /api/containers-bulk` | Containers |
| `PUT /api/containers/{name}/autorestart`, `GET /api/containers/{name}/details`, `GET /api/containers/{name}/logs` | Container extras |
| `GET /api/docker/usage`, `POST /api/docker/prune` | Docker disk usage and prune |
| `GET /api/checks`, `POST /api/checks` | List and create monitor checks |
| `GET /api/checks/{id}`, `PUT /api/checks/{id}`, `DELETE /api/checks/{id}` | Detail (chart, incidents), edit, delete |
| `POST /api/checks/{id}/enabled`, `POST /api/checks/{id}/run` | Pause, run now |
| `GET /api/status`, `GET /status` | Public status page (only with `STATUS_PAGE=true`) |
| `GET /api/events`, `GET/PUT /api/settings`, `POST /api/settings/test-telegram`, `POST /api/settings/test-webhook` | Events, thresholds, notification tests |
| `POST /api/power` | Reboot or shutdown (password in the body) |
| `GET /api/db/download` | Consistent copy of the database |

Creating a check, for example:

```bash
curl -b cookies.txt -H 'X-Requested-With: pi-dashboard' -H 'Content-Type: application/json' \
  -d '{"name":"My API","kind":"http","target":"https://api.example.com/health","interval":60,
       "options":{"json_path":"status","expect_status":"200-299","retries":2,"public":true}}' \
  http://<pi>:8080/api/checks
```

## Development

Python 3.11 or newer, any OS:

```bash
pip install -r requirements-dev.txt
ruff check . && pytest                       # 73 tests, no Pi or Docker needed

DASH_PASSWORD=test DATA_DIR=./data PORT=8099 python -m tests.dev_server   # sample data + fake Docker
```

The frontend is plain JavaScript with no build step. See [CONTRIBUTING.md](CONTRIBUTING.md). Layout:

```
app/            main.py (routes), collector.py, monitor.py, alerts.py, docker_api.py, system.py,
                db.py, auth.py, netguard.py, config.py, static/ (HTML, CSS, JS, vendored Chart.js and font)
tests/          pytest suite, fake Docker, dev server with sample data
.github/        CI (tests, lint, multi-arch image build), release workflow, issue and PR templates
```

## Troubleshooting

- **Page does not open.** `docker compose ps` and `docker compose logs`. Check that the device you use has Tailscale on, and use the Tailscale address of the Pi, not the LAN one.
- **"Tailscale interface not found" in the log.** Tailscale is not up on the Pi: `sudo tailscale up`, then `docker compose restart`. Another interface name goes in `TAILSCALE_INTERFACE`.
- **403 "only through Tailscale".** The request does not come from a Tailscale address. Behind a reverse proxy on another host, add its address to `ALLOWED_NETWORKS`.
- **"Docker not available".** Check the `/var/run/docker.sock` volume and `DOCKER_ENABLED`.
- **Reboot or shutdown do nothing.** `SELF_IMAGE` must match the real image name (`docker images`).
- **A container I stopped from the terminal keeps coming back.** It has auto-restart on, and the dashboard restarts what it did not stop itself. Stop it from the dashboard or turn the switch off.
- **Container memory shows "-".** The memory cgroup is off on older Raspberry Pi OS, see [Supported hardware](#supported-hardware).
- **No "Install app".** It needs HTTPS (the `tailscale serve` variant).
- **Forgot the password.** Change `DASH_PASSWORD` in `.env`, `docker compose up -d`.

## Known limits

- One shared password, no separate users.
- The interface is in Italian only and dark only. Texts are plain strings in `app/static`, a translation layer is a welcome contribution.
- The Monitor checks TCP ports and HTTP(S), no ICMP ping and no push/heartbeat monitors.
- One Docker host, one Raspberry per dashboard.
- Containers removed with `docker compose down` no longer exist for Docker, so they are not listed.
- Not every Raspberry Pi model has been tested on real hardware (see [Supported hardware](#supported-hardware)).

## License

[MIT](LICENSE)
