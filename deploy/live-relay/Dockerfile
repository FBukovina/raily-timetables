# Raily Live relay: python:3.12-slim with the importer's scripts, no other dependencies.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --system --home /srv/raily raily && mkdir -p /srv/raily/cache && chown raily /srv/raily/cache
WORKDIR /srv/raily
COPY scripts/ scripts/
USER raily
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/health', timeout=4)"
CMD ["python3", "-u", "scripts/live_relay.py", "--host", "0.0.0.0", "--port", "8080", "--cache", "/srv/raily/cache"]
