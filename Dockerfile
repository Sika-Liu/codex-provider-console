FROM python:3.12-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssh-client libnss-wrapper \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=true sh \
    && install -m 755 "$(readlink -f /root/.local/bin/codex)" /usr/local/bin/codex
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint
RUN chmod 755 /usr/local/bin/docker-entrypoint
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
