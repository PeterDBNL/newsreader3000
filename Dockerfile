FROM python:3.12-slim-bookworm

# par2 for repair, unrar (non-free) + 7-Zip for extraction, gosu to drop root, tini as init
RUN sed -i 's/^Components: main$/Components: main contrib non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends par2 unrar p7zip-full gosu tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV NR_CONFIG_DIR=/config \
    NR_DOWNLOAD_DIR=/downloads \
    NR_PORT=8095 \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/downloads"]
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('NR_PORT','8095'))" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${NR_PORT} --no-access-log"]
