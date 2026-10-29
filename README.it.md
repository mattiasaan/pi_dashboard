# Pi Dashboard

Dashboard web scura e minimale per controllare un Raspberry Pi dal browser, in un solo container Docker. Mostra lo stato in tempo reale, salva lo storico in un database, gestisce processi e container e ha un monitor in stile "uptime robot" per i tuoi servizi. È raggiungibile dalla **rete locale e tramite Tailscale**, mai da Internet, e si può limitare al solo Tailscale.

> English: vedi [README.md](README.md).

## Funzioni

**Panoramica** (aggiornata ogni 2 secondi): CPU totale e per core, frequenza, memoria e swap, temperatura, disco, rete, I/O del disco, carico medio, tempo di accensione, sparkline.

**Storico**: grafici da 1 ora a 30 giorni di CPU, memoria, temperatura, rete, I/O, carico, frequenza e spazio disco, con media, minimo e massimo e download in CSV. I periodi senza dati (Pi spento) appaiono come interruzioni.

**Processi**: ricerca, ordinamento, raggruppamento per nome, dettagli (comando, utente, memoria, file aperti, porte in ascolto), Termina, Forza arresto, Pausa e Riprendi. PID 1 e la dashboard stessa sono protetti.

**Container Docker**
- Tutti i container, anche fermi o terminati, raggruppati per progetto Compose; filtri e ricerca
- **Avvia** un container fermo con un tocco; azioni di gruppo (avvia i fermi, riavvia tutti, ferma tutti)
- **Riavvio automatico** per container: se si ferma senza che l'abbia fermato la dashboard, riparte da solo (fino a 3 volte in 10 minuti, poi smette e ti avvisa)
- Dettagli (immagine, comando, reti, cartelle, nomi delle variabili d'ambiente senza i valori), log anche in diretta
- Spazio usato da Docker e pulizia sicura (immagini senza nome e cache di build; mai container né volumi)
- La dashboard non può fermare o riavviare sé stessa

**Monitor**: vedi [la sezione dedicata](#monitor).

**Modalità schermo**: vista per un tablet appeso al muro (`/#schermo`), con orologio grande, schermo sempre acceso e schermo intero.

**Sistema**: modello, sistema, alimentazione (sotto-tensione e throttling), velocità della ventola (Pi 5), dischi con **previsione di quando si riempiono**, rete, copia di backup del database, riavvio e spegnimento (chiedono di nuovo la password), soglie degli avvisi.

**Avvisi**: temperatura, CPU, memoria, disco, sotto-tensione, throttling, container che si fermano o diventano "unhealthy", controlli del Monitor che non rispondono. In dashboard, su Telegram e/o su un webhook (ntfy, Discord, Slack, Home Assistant). Scattano solo se la condizione dura abbastanza e segnalano anche il rientro.

**Ogni schermo**: telefono (barra in basso), tablet (barra laterale con icone), computer (barra laterale completa). Installabile come app (serve HTTPS).

## Raspberry supportati

| Modello | Sistema | Stato |
|---|---|---|
| Raspberry Pi 5, 500, CM5 | 64 bit | Supportato. Temperatura, frequenza, alimentazione, ventola (Active Cooler). |
| Raspberry Pi 4, 400, CM4 | 64 o 32 bit | Supportato. |
| Raspberry Pi 3 B, 3 B+, 3 A+, CM3, Zero 2 W | 64 o 32 bit | Supportato. Con il sistema a 32 bit la build compila una dipendenza (psutil): la prima volta servono alcuni minuti. |
| Raspberry Pi 2 | 32 bit | Supportato (armv7), stessa nota. |
| Raspberry Pi 1, Zero, Zero W | | **Non supportati**: ARMv6 non ha l'immagine `python:3.12` né i pacchetti per le dipendenze. |
| Altre macchine Linux | | Funziona tutto tranne le letture specifiche del Pi (alimentazione, ventola). |

Come funzionano le letture specifiche del Pi:

- **Temperatura e frequenza**: da `/sys/class/thermal` e `cpufreq`, presenti su tutti i modelli.
- **Alimentazione** (sotto-tensione e throttling, adesso e dall'avvio): da `get_throttled` del firmware (`vcgencmd` se c'è, altrimenti il file in sysfs). Se il kernel non lo espone, ripiega sul driver hwmon `rpi_volt`, che dà solo la **sotto-tensione in questo momento**; la dashboard ti avvisa che il resto non è disponibile.
- **Ventola** (Active Cooler del Pi 5 o altra ventola PWM con driver hwmon): giri al minuto e percentuale, solo se presente.
- **Memoria e CPU dei container** richiedono il cgroup della memoria. In Raspberry Pi OS Bookworm è attivo; nelle versioni più vecchie aggiungi `cgroup_enable=memory cgroup_memory=1` a `/boot/cmdline.txt` e riavvia, altrimenti la memoria dei container risulta "-".
- Le soglie di temperatura dipendono dal modello e dal raffreddamento: i valori predefiniti sono 75 °C e 82 °C, e c'è comunque l'avviso sul throttling del firmware. Adattali in **Sistema**.

Cosa è stato verificato: il codice è coperto da test che girano senza un Pi (sysfs e Docker sono simulati). Per le dipendenze ho controllato che esistano pacchetti pronti per arm64 e, tranne psutil, per armv7 (su armv7 psutil viene compilato dal Dockerfile: quel percorso non è stato eseguito). Il workflow di CI incluso costruisce l'immagine per amd64, arm64 e armv7. **Non è stato provato su un Raspberry reale** da chi l'ha scritto: se qualcosa non torna sul tuo modello, apri una segnalazione con l'output di `cat /proc/device-tree/model; uname -m`.

Consumo misurato su una macchina x86-64 (non su un Pi): circa 90 MB di RAM e lo 0,3% di un core a riposo. Le statistiche dei container si leggono in modalità "one-shot", senza attendere 1-2 secondi per container: con molti container resta leggera anche su un Pi 3.

## Requisiti e installazione

Docker con il plugin Compose (`docker compose version`) e **Tailscale** connesso sul Pi e sui dispositivi da cui apri la dashboard. Se mancano: `curl -fsSL https://get.docker.com | sh` (poi `sudo usermod -aG docker $USER` e riconnettiti) e `curl -fsSL https://tailscale.com/install.sh | sh` (poi `sudo tailscale up`).

```bash
git clone https://github.com/OWNER/pi-dashboard.git && cd pi-dashboard
cp .env.example .env
nano .env                      # imposta DASH_PASSWORD
docker compose up -d --build
```

In alternativa puoi usare l'immagine pubblicata senza costruirla: in `docker-compose.yml` sostituisci `build: .` con `image: ghcr.io/OWNER/pi-dashboard:latest` (arm64, armv7, amd64). Tieni `SELF_IMAGE` uguale al nome dell'immagine.

Con Tailscale attivo apri `http://IP-TAILSCALE-DEL-PI:8080` (`tailscale ip -4` sul Pi) oppure `http://NOME-DEL-PI:8080` con MagicDNS. Dalla rete di casa basta `http://IP-LAN-DEL-PI:8080` (per esempio `http://192.168.1.20:8080`, lo vedi con `hostname -I`) oppure `http://raspberrypi.local:8080`, senza Tailscale. Tailscale resta facoltativo: serve quando sei fuori casa.

Se `DASH_PASSWORD` è vuota o è ancora un valore d'esempio (`cambiami`, `changeme`...), viene generata una password temporanea e scritta nei log (`docker compose logs pi-dashboard`). Meglio impostarne una tua.

Aggiornamento: estrai o scarica la nuova versione sopra la cartella e lancia `docker compose up -d --build`. `.env` e `./data` restano.

### Chi può collegarsi

Lo decidono due livelli indipendenti:

1. **Indirizzo di ascolto** (`BIND_ADDRESS`). `all` (predefinito) ascolta su tutte le interfacce: funzionano sia la LAN sia Tailscale. `tailscale` ascolta solo sull'IP di `tailscale0` (`100.x.y.z`), `127.0.0.1` solo sul Pi stesso.
2. **Filtro sui client** (`ALLOWED_NETWORKS`). Ogni richiesta è rifiutata (403) se non arriva da una rete ammessa. Il predefinito è `tailscale,lan,loopback`: Tailscale, reti private (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`, link-local) e il Pi stesso. Gli indirizzi pubblici sono rifiutati: anche se per errore il router inoltrasse la porta, da Internet non risponde.

| Cosa vuoi | `.env` |
|---|---|
| LAN e Tailscale (predefinito) | niente da impostare |
| **Solo Tailscale** | `BIND_ADDRESS=tailscale` e `ALLOWED_NETWORKS=tailscale,loopback` |
| Solo una certa LAN | `ALLOWED_NETWORKS=192.168.1.0/24,loopback` (nomi e reti CIDR si possono mescolare) |
| Solo Tailscale con HTTPS | `BIND_ADDRESS=127.0.0.1`, `COOKIE_SECURE=true` e `tailscale serve` (sotto) |

Con `BIND_ADDRESS=tailscale`, se all'avvio Tailscale non è ancora attivo la dashboard aspetta fino a 2 minuti l'interfaccia, poi si ferma e Docker la riavvia. Non si apre mai sulla LAN come ripiego.

**L'HTTP semplice sulla LAN non è cifrato.** Dentro Tailscale il traffico è già cifrato da WireGuard, ma sulla LAN password e cookie di sessione viaggiano in chiaro: chi è sulla stessa Wi-Fi potrebbe leggerli. Va bene su una rete di casa di cui ti fidi, non su una rete condivisa o per ospiti: lì usa solo Tailscale oppure l'HTTPS (sotto). Con l'HTTP semplice tieni `COOKIE_SECURE=false`, altrimenti il login non funziona. Se vuoi, limita anche la porta con il firewall, per esempio `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`.

**Variante con HTTPS** (necessaria per installarla come app). Controlla prima cosa pubblichi già con `tailscale serve status`: un secondo `serve` sulla stessa porta e sullo stesso percorso sovrascrive il primo. Usa una porta HTTPS diversa (443, 8443 o 10000):

```bash
# nel .env
BIND_ADDRESS=127.0.0.1
COOKIE_SECURE=true

docker compose up -d
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8080
```

La dashboard sarà su `https://NOME-DEL-PI.NOME-TAILNET.ts.net:8443`. Non pubblicarla sotto un percorso (usa percorsi assoluti) e non usare mai `tailscale funnel`.

**Telefono e tablet.** Per averla come app apri l'indirizzo in HTTPS e usa "Aggiungi alla schermata Home" (iPhone e iPad: Condividi) o "Installa app" (Chrome su Android). Per un tablet fisso apri `https://.../#schermo`, aggiungilo alla Home e attiva lo schermo intero.

## Monitor

Aggiungi controlli per i tuoi altri progetti e ricevi un avviso quando smettono di rispondere e quando tornano.

| Tipo | Cosa controlla |
|---|---|
| **Health endpoint (JSON)** | Un indirizzo http(s) come `https://api.esempio.it/health`. Risulta attivo se il codice HTTP è accettato **e** un campo JSON ha il valore atteso. Campo predefinito `status`, valori accettati `ok`, `up`, `healthy`, `pass`, `true`... oppure un valore tuo. Funzionano i percorsi con il punto (`checks.database.status`). |
| **Pagina web** | Un indirizzo http(s). Attivo se il codice è accettato e, se vuoi, la risposta contiene una parola. |
| **Porta TCP** | `host:porta`. Attivo se la connessione si apre. |

Opzioni (in *Opzioni avanzate*): codici HTTP accettati (predefinito `200-399`, per esempio `200,204` o `200-299`), GET o HEAD, intestazioni personalizzate (una per riga, per i token), timeout (1-30 s), errori di fila prima dell'avviso (1-5), verifica del certificato HTTPS, avviso se il certificato scade entro N giorni, e se mostrarlo nella pagina di stato. Intervalli da 15 secondi a 1 ora, fino a 50 controlli.

- Dopo un errore il controllo si ripete dopo 30 secondi invece di aspettare l'intervallo intero: un guasto vero si conferma presto, un singhiozzo si scarta presto.
- Ogni controllo conserva tempo di risposta, disponibilità a 24 ore e 7 giorni e l'elenco delle interruzioni (inizio, durata, errore) per 90 giorni. Tocca un controllo per vedere il grafico.
- Un avviso quando va giù e uno, con la durata, quando torna.

**Pagina di stato** (facoltativa). Con `STATUS_PAGE=true`, `/status` mostra una pagina di sola lettura con i controlli marcati come pubblici: nome, stato, disponibilità, una piccola cronologia. Senza login e senza indirizzi, ma raggiungibile solo da `ALLOWED_NETWORKS`.

**Webhook.** `WEBHOOK_URL` riceve ogni avviso (warn e critico) più i rientri di Monitor e avvisi. Con `WEBHOOK_FORMAT=json` invia `{"level","kind","message","ts","text","content"}` (`text` per Slack, `content` per Discord); con `text` invia il solo messaggio, adatto a [ntfy](https://ntfy.sh) (`https://ntfy.sh/il-tuo-topic`).

## Uso quotidiano

```bash
docker compose logs -f         # log
docker compose restart         # riavvio
docker compose up -d --build   # aggiornamento
docker compose down            # arresto (i dati restano in ./data)
```

Il container riparte da solo a ogni accensione (`restart: unless-stopped`).

## Configurazione

Tutto nel file `.env`. Dopo ogni modifica: `docker compose up -d`.

| Variabile | Predefinito | Descrizione |
|---|---|---|
| `DASH_PASSWORD` | casuale | Password di accesso. I valori d'esempio sono rifiutati. |
| `PORT` | `8080` | Porta (il container usa la rete dell'host). |
| `BIND_ADDRESS` | `all` | Dove ascolta: `all` (LAN e Tailscale), `tailscale` (solo l'IP Tailscale), `127.0.0.1` (solo il Pi, per `tailscale serve`) o un IP. |
| `TAILSCALE_INTERFACE` | `tailscale0` | Nome dell'interfaccia Tailscale. |
| `BIND_WAIT_SECONDS` | `120` | Attesa di Tailscale all'avvio (solo con `BIND_ADDRESS=tailscale`). |
| `ALLOWED_NETWORKS` | `tailscale,lan,loopback` | Reti ammesse, separate da virgola. Si possono mescolare nomi (`tailscale`, `lan`, `loopback`) e reti CIDR (`192.168.1.0/24`). `any` disattiva il filtro. |
| `RETENTION_DAYS` | `30` | Giorni di storico e di eventi. |
| `COOKIE_SECURE` | `false` | `true` solo dietro HTTPS. |
| `SESSION_HOURS` | `168` | Durata della sessione. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | vuote | Avvisi Telegram. |
| `WEBHOOK_URL`, `WEBHOOK_FORMAT` | vuota, `json` | Webhook per gli avvisi. |
| `STATUS_PAGE`, `STATUS_TITLE` | `false` | Pagina di stato su `/status`. |
| `DOCKER_ENABLED` | `true` | `false` disattiva la gestione dei container. |
| `POWER_ENABLED` | `true` | `false` disattiva riavvio e spegnimento. |
| `LIVE_INTERVAL`, `SAMPLE_INTERVAL` | `2`, `10` | Secondi tra aggiornamenti live e campioni nel database. |
| `RAW_RETENTION_HOURS` | `6` | Ore di campioni a 10 s prima delle sole medie al minuto. |
| `SELF_IMAGE`, `SELF_CONTAINER_NAME` | `pi-dashboard:latest`, `pi-dashboard` | Immagine e container di questa dashboard. |
| `DATA_DIR`, `HOST_ROOT` | `/data`, `/host` | Cartella dati e filesystem dell'host (sola lettura). |

Le soglie degli avvisi si cambiano da **Sistema** e stanno nel database. Predefiniti: temperatura 75 °C (avviso) e 82 °C (critica), CPU oltre 90% per 120 s, memoria oltre 90%, disco oltre 90%.

## Come funziona

```
Browser ── HTTP ──> FastAPI (un processo, un worker)
                      ├─ API REST /api/*   (protette dal login)
                      ├─ file statici      (HTML, CSS, JS, Chart.js, font: inclusi, niente CDN)
                      ├─ thread "collector"  ogni 2 s metriche, 10 s campioni, 1 min aggregati, avvisi, container
                      └─ thread "monitor"    controlli di raggiungibilità
```

Il `docker-compose.yml` dà al container `network_mode: host` e `pid: host` (per vedere interfacce e processi veri), `/:/host:ro` (filesystem dell'host in sola lettura), `/var/run/docker.sock` (gestione container) e `./data:/data` (database e chiave di sessione).

Riavvio e spegnimento: il container non è privilegiato. Chiede a Docker di avviare per pochi secondi un container temporaneo con la stessa immagine e i permessi per entrare nei namespace di PID 1 ed eseguire `systemctl reboot` o `poweroff`.

Dati (`./data/metrics.db`, SQLite in WAL): campioni a 10 s per 6 ore, medie al minuto per 30 giorni, eventi 30 giorni, impostazioni permanenti, risultati del Monitor 7 giorni, interruzioni 90 giorni. Circa 5 MB per 30 giorni e poche scritture sulla SD. Backup: copia il file o scaricalo da **Sistema**.

## API

Le rotte `/api/*` richiedono il login (cookie), tranne `/api/login`, `/api/health` e `/api/status`. Le scritture richiedono `X-Requested-With: pi-dashboard`. L'elenco completo, con un esempio di creazione di un controllo, è in [README.md](README.md#api).

## Sviluppo e test

Python 3.11 o superiore, qualsiasi sistema:

```bash
pip install -r requirements-dev.txt
ruff check . && pytest                       # 73 test, senza Pi né Docker
DASH_PASSWORD=test DATA_DIR=./data PORT=8099 python -m tests.dev_server   # dati di esempio e Docker simulato
```

Il frontend è JavaScript semplice, senza build. Vedi [CONTRIBUTING.md](CONTRIBUTING.md).

## Risoluzione dei problemi

- **La pagina non si apre.** `docker compose ps` e `docker compose logs`. In casa usa l'IP LAN del Pi (`hostname -I`); fuori casa l'IP Tailscale, con Tailscale attivo. Se hai impostato `BIND_ADDRESS=tailscale` o `127.0.0.1`, l'indirizzo LAN non risponde per scelta.
- **"Interfaccia Tailscale non trovata" nei log.** Tailscale non è attivo: `sudo tailscale up`, poi `docker compose restart`. Altro nome di interfaccia: `TAILSCALE_INTERFACE`.
- **403 "Accesso non consentito da questa rete".** L'indirizzo di chi si collega non è in `ALLOWED_NETWORKS`. Dietro a un reverse proxy su un altro host, o con una LAN dagli indirizzi insoliti, aggiungi quell'indirizzo o rete a `ALLOWED_NETWORKS`.
- **Voglio solo Tailscale.** `BIND_ADDRESS=tailscale` e `ALLOWED_NETWORKS=tailscale,loopback`, poi `docker compose up -d`.
- **Il login funziona su Tailscale ma non in LAN (o viceversa).** Con `COOKIE_SECURE=true` il browser tiene il cookie solo su HTTPS, quindi gli indirizzi `http://` non fanno entrare. Usa l'indirizzo HTTPS oppure metti `COOKIE_SECURE=false`.
- **"Docker non disponibile".** Controlla il volume `/var/run/docker.sock` e `DOCKER_ENABLED`.
- **Riavvio o spegnimento non partono.** `SELF_IMAGE` deve coincidere con il nome reale dell'immagine (`docker images`).
- **Un container che ho fermato da terminale riparte.** Ha il riavvio automatico attivo e la dashboard riavvia ciò che non ha fermato lei: fermalo dal pulsante Ferma o spegni l'interruttore.
- **Il riavvio automatico non basta.** Dopo 3 tentativi in 10 minuti si ferma e ti avvisa: guarda i log del container.
- **La memoria dei container è "-".** Cgroup della memoria spento su un Raspberry Pi OS vecchio (vedi [Raspberry supportati](#raspberry-supportati)).
- **Non vedo "Installa app".** Serve HTTPS (variante con `tailscale serve`).
- **Ho dimenticato la password.** Cambia `DASH_PASSWORD` nel `.env` e `docker compose up -d`. Dopo 5 errori il login si blocca 5 minuti (o riavvia il container).
- **Uso troppo la SD?** Le scritture sono poche; aumenta `SAMPLE_INTERVAL` (per esempio `30`) per ridurle.

## Limiti noti

- Una sola password condivisa, niente utenti separati.
- L'interfaccia è solo in italiano e solo a tema scuro.
- Il Monitor controlla porte TCP e pagine web: niente ping ICMP e niente controlli "heartbeat" (push).
- Un solo Docker (quello locale), un solo Raspberry per dashboard.
- I container eliminati (per esempio con `docker compose down`) non esistono più per Docker e non compaiono.
- Non tutti i modelli di Raspberry sono stati provati su hardware reale.
