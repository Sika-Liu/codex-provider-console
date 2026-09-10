FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS relay
COPY relay_domain.py relay.py ./
CMD ["uvicorn", "relay:app", "--host", "0.0.0.0", "--port", "57321"]

FROM base AS console
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssh-client libnss-wrapper \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --silent --show-error --location --retry 5 --retry-all-errors --connect-timeout 20 https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh \
    && CODEX_NON_INTERACTIVE=true sh /tmp/install-codex.sh \
    && install -m 755 "$(readlink -f /root/.local/bin/codex)" /usr/local/bin/codex
COPY app.py model_catalog.py provider_domain.py relay_domain.py relay.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint
RUN chmod 755 /usr/local/bin/docker-entrypoint
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
