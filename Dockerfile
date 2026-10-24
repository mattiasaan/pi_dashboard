FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data HOST_ROOT=/host PORT=8080
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request as u; h=open('/tmp/bind_host').read().strip(); u.urlopen('http://%s:%s/api/health' % (h, os.environ.get('PORT','8080')), timeout=3)" || exit 1

CMD ["python", "-m", "app"]
