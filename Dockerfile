# Funziona su Raspberry Pi a 64 bit (arm64: Pi 3, 4, 5, Zero 2 W, 400, 500, CM4) e a 32 bit (armv7: Pi 2, 3, Zero 2 W con
# Raspberry Pi OS a 32 bit), oltre che su amd64 per provarlo sul computer. Pi 1 e Zero (armv6) non sono supportati.

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt .
# Su arm64 e amd64 i pacchetti sono già compilati. Su armv7 psutil non ha un pacchetto pronto e va compilato:
# il compilatore serve solo in questa fase e non finisce nell'immagine finale.
RUN pip install --only-binary=:all: -r requirements.txt \
 || (apt-get update \
     && apt-get install -y --no-install-recommends gcc libc6-dev \
     && pip install -r requirements.txt)

FROM python:3.12-slim
LABEL org.opencontainers.image.title="Pi Dashboard" \
      org.opencontainers.image.description="Dashboard web per Raspberry Pi: metriche, container Docker, monitor di raggiungibilità" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/venv/bin:$PATH
WORKDIR /srv
COPY --from=build /opt/venv /opt/venv
COPY app ./app

ENV DATA_DIR=/data HOST_ROOT=/host PORT=8080
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request as u; h=open('/tmp/bind_host').read().strip(); u.urlopen('http://%s:%s/api/health' % (h, os.environ.get('PORT','8080')), timeout=3)" || exit 1

CMD ["python", "-m", "app"]
