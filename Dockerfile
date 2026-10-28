# syntax=docker/dockerfile:1

FROM tailscale/tailscale:v1.104.2@sha256:b11583ad2e93e4671128538c88cf11f460444a5d7e449311dd1933a400ab849d AS tailscale

FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58

# tini is PID 1. The rest are for diagnosing connectivity from inside the
# container (curl through the Tailscale proxy, openssl s_client, dig, ldapsearch).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl openssl dnsutils ldap-utils netcat-openbsd procps \
 && rm -rf /var/lib/apt/lists/*

COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/lib/tailscale/tailscale
COPY docker/tailscale /usr/local/bin/tailscale

RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /var/lib/tailscale \
 && chown app:app /data /var/lib/tailscale

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY src ./src
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 \
  CMD ["python", "-m", "ricoh_ldap_sync.health"]

# The entrypoint starts as root only to fix volume ownership, then drops to
# UID 1000 before starting tailscaled and the worker.
ENTRYPOINT ["tini", "-g", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["sh", "-c", "python -m ricoh_ldap_sync.check --wait 120; exec sleep infinity"]
